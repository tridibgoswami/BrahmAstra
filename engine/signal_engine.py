"""
BrahmAstra Signal Engine — bar-by-bar state machine.

Replicates Pine Script BrahmAstra_vMaster signal generation exactly:
  - All signals confirmed on bar CLOSE (bar_closed = True for historical bars)
  - Target / trail exits are INTRABAR (check high/low, not just close)
  - Normal / forced / early exits confirmed on bar close only
  - ST2 allow_reentry logic preserved
  - bars_since_choppy NOT reset on new day (var without day-reset in Pine Script)

Event dict keys:
  type            : 'BUY' | 'SELL' | 'EXIT_BUY' | 'EXIT_SELL'
  exit_type       : 'TARGET' | 'TRAIL_STOP' | 'EARLY_EXIT' | 'FORCE_EXIT' | 'NORMAL_EXIT'
  bar_index       : integer index in the DataFrame
  timestamp       : pandas Timestamp
  entry_price     : float
  target_price    : float  (entry events only)
  exit_price      : float  (exit events only)
  pnl_pts         : float  (exit events only, points in price)
  quality_score   : int    (entry events only)
  direction       : 'BUY' | 'SELL'  (exit events — trade direction)
"""

from __future__ import annotations
import math
import numpy as np
import pandas as pd
from typing import List, Dict, Any


class SignalEngine:
    def __init__(self, params: dict):
        self.p = params  # preset parameters dict

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Process entire DataFrame bar by bar.
        df must already have indicator columns from compute_indicators().
        Returns list of event dicts in chronological order.
        """
        state = self._init_state()
        events: List[Dict[str, Any]] = []

        for i in range(len(df)):
            bar_events = self._process_bar(i, df, state)
            events.extend(bar_events)

        return events

    # ─────────────────────────────────────────────────────────────────────────
    # State helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _init_state() -> dict:
        return {
            # Position state
            "in_buy": False,
            "in_sell": False,
            "allow_reentry": False,
            # Entry metadata
            "entry_bar_index": None,
            "signal_candle_high": None,
            "signal_candle_low": None,
            "signal_candle_close": None,
            "early_exit_watch_on": False,
            # Live trade prices
            "live_entry_price": None,
            "live_target_price": None,
            "live_trade_open": False,
            "live_trade_is_buy": False,
            # Trailing stop
            "trail_stop_level": None,
            "trail_active": False,
            "trail_peak_price": None,
            # Day counter (reset on new day)
            "candles_today": 0,
        }

    def _reset_day(self, state: dict):
        state["in_buy"] = False
        state["in_sell"] = False
        state["allow_reentry"] = True
        state["entry_bar_index"] = None
        state["signal_candle_high"] = None
        state["signal_candle_low"] = None
        state["signal_candle_close"] = None
        state["early_exit_watch_on"] = False
        state["live_entry_price"] = None
        state["live_target_price"] = None
        state["live_trade_open"] = False
        state["live_trade_is_buy"] = False
        state["trail_stop_level"] = None
        state["trail_active"] = False
        state["trail_peak_price"] = None
        state["candles_today"] = 0

    def _reset_trade(self, state: dict):
        state["entry_bar_index"] = None
        state["signal_candle_high"] = None
        state["signal_candle_low"] = None
        state["signal_candle_close"] = None
        state["early_exit_watch_on"] = False
        state["live_entry_price"] = None
        state["live_target_price"] = None
        state["live_trade_open"] = False
        state["live_trade_is_buy"] = False
        state["trail_stop_level"] = None
        state["trail_active"] = False
        state["trail_peak_price"] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Per-bar processing
    # ─────────────────────────────────────────────────────────────────────────

    def _process_bar(self, i: int, df: pd.DataFrame,
                     state: dict) -> List[Dict[str, Any]]:
        p = self.p
        events: List[Dict[str, Any]] = []
        row = df.iloc[i]

        # ── 1. New day reset ─────────────────────────────────────────────────
        if row["is_new_day"]:
            self._reset_day(state)

        # ── 2. Candle counter (mirrors Pine Script candles_today) ─────────────
        ist_h = int(row["ist_hour"])
        ist_m = int(row["ist_minute"])
        if not row["is_new_day"] and row["in_market"]:
            state["candles_today"] += 1

        # ── 3. Skip bars where indicators are not yet ready ───────────────────
        ema_fast = row["ema_fast"]
        ema_mid  = row["ema_mid"]
        ema_slow = row["ema_slow"]
        atr_val  = row["atr_filter"]
        adx_val  = row["adx"]

        if any(np.isnan(v) for v in [ema_fast, ema_mid, ema_slow, atr_val, adx_val]):
            return events

        st1_dir_val = row["st1_dir"]
        st2_dir_val = row["st2_dir"]
        if np.isnan(st1_dir_val) or np.isnan(st2_dir_val):
            return events

        # ── 4. Snapshot prev-bar state for exit conditions using in_buy[1] ────
        in_buy_prev  = state["in_buy"]
        in_sell_prev = state["in_sell"]

        # ── 5. Price fields ───────────────────────────────────────────────────
        close = float(row["close"])
        high  = float(row["high"])
        low   = float(row["low"])
        open_ = float(row["open"])
        ts    = row["timestamp"]

        # ── 6. ST direction booleans ──────────────────────────────────────────
        st1_bull = float(st1_dir_val) < 0
        st1_bear = float(st1_dir_val) > 0
        st2_bull = float(st2_dir_val) < 0
        st2_bear = float(st2_dir_val) > 0

        # ST2 flip: compare to previous bar's direction
        if i > 0:
            prev_st2 = df.iloc[i - 1]["st2_dir"]
            st2_bull_prev = (not np.isnan(prev_st2)) and float(prev_st2) < 0
        else:
            st2_bull_prev = st2_bull
        st2_flipped_bull = st2_bull and not st2_bull_prev
        st2_flipped_bear = st2_bear and not (not st2_bull_prev)

        # ST2 trigger respects allow_reentry (matches Pine Script)
        st2_trigger_bull = st2_bull if state["allow_reentry"] else st2_flipped_bull
        st2_trigger_bear = st2_bear if state["allow_reentry"] else st2_flipped_bear

        # ── 7. Session window ─────────────────────────────────────────────────
        in_entry_window = (
            ((ist_h > 9) or (ist_h == 9 and ist_m >= 15)) and
            ist_h < 15 and
            state["candles_today"] >= p["sig_start_candle"]
        )
        is_force_exit_candle = (ist_h == p["force_exit_hour"] and
                                ist_m == p["force_exit_minute"])
        is_midday_chop = (ist_h == 11 and ist_m >= 15) or ist_h == 12

        # ── 8. Chop / no-trade derived values (precomputed columns) ───────────
        ema_spread_atr   = float(row["ema_spread_atr"])
        is_choppy        = bool(row["is_choppy"])
        bars_since_choppy = int(row["bars_since_choppy"])
        was_choppy_recently = bars_since_choppy <= p["chop_recent_bars"]

        # ── 9. Breakout flags ─────────────────────────────────────────────────
        highest_prev = row["highest_prev"]
        lowest_prev  = row["lowest_prev"]
        buy_breakout_ok  = (not np.isnan(highest_prev)) and close > float(highest_prev)
        sell_breakout_ok = (not np.isnan(lowest_prev))  and close < float(lowest_prev)

        # ── 10. No-trade zone (precomputed columns used) ──────────────────────
        ema_mid_crosses_10  = float(row["ema_mid_crosses_10"])
        st2_flips_10        = float(row["st2_flips_10"])
        ema_slow_slope_atr  = float(row["ema_slow_slope_atr"])

        no_trade_zone = p["use_no_trade_zone"] and (
            ema_mid_crosses_10 >= p["max_ema_crosses_10"] or
            st2_flips_10       >= p["max_st2_flips_10"]   or
            ema_slow_slope_atr  < p["min_slow_slope_atr"] or
            (p["avoid_midday"] and is_midday_chop)
        )

        # ── 11. Candle quality ────────────────────────────────────────────────
        candle_range = max(high - low, 1e-8)
        body_pct     = abs(close - open_) / candle_range
        close_pos    = (close - low) / candle_range
        buy_candle_ok  = (close > open_ and
                          body_pct  >= p["min_body_pct"] and
                          close_pos >= p["buy_close_pos_min"])
        sell_candle_ok = (close < open_ and
                          close_pos <= p["sell_close_pos_max"] and
                          body_pct  >= p["min_body_pct"])

        # ── 12. Quality scores (0–10) ─────────────────────────────────────────
        htf_buy_ok  = bool(row["htf_buy_ok"])
        htf_sell_ok = bool(row["htf_sell_ok"])

        buy_quality_score = sum([
            close > ema_slow,
            ema_fast > ema_mid,
            st1_bull,
            st2_trigger_bull,
            adx_val >= p["adx_min_filter"],
            ema_spread_atr >= p["ema_spread_atr_min"],
            buy_breakout_ok,
            buy_candle_ok,
            htf_buy_ok,
            not no_trade_zone,
        ])

        sell_quality_score = sum([
            close < ema_slow,
            ema_fast < ema_mid,
            st1_bear,
            st2_trigger_bear,
            adx_val >= p["adx_min_filter"],
            ema_spread_atr >= p["ema_spread_atr_min"],
            sell_breakout_ok,
            sell_candle_ok,
            htf_sell_ok,
            not no_trade_zone,
        ])

        # ── 13. Base signals ──────────────────────────────────────────────────
        not_in_trade = not state["in_buy"] and not state["in_sell"]

        base_buy = (in_entry_window and not_in_trade and
                    close > ema_slow and ema_fast > ema_mid and
                    st1_bull and st2_trigger_bull)

        base_sell = (in_entry_window and not_in_trade and
                     close < ema_slow and ema_fast < ema_mid and
                     st1_bear and st2_trigger_bear)

        # ── 14. Filters (applied sequentially, same order as Pine Script) ─────
        buy_ok  = True
        sell_ok = True

        if p["use_chop_filter"]:
            buy_ok  = (not is_choppy) and (not was_choppy_recently or buy_breakout_ok)
            sell_ok = (not is_choppy) and (not was_choppy_recently or sell_breakout_ok)

        if p["use_breakout_filter"]:
            buy_ok  = buy_ok  and buy_breakout_ok
            sell_ok = sell_ok and sell_breakout_ok

        if p["use_15m_filter"]:
            buy_ok  = buy_ok  and htf_buy_ok
            sell_ok = sell_ok and htf_sell_ok

        if p["use_quality_filter"]:
            buy_ok  = buy_ok  and buy_quality_score  >= p["min_quality_score"]
            sell_ok = sell_ok and sell_quality_score >= p["min_quality_score"]

        if p["use_no_trade_zone"]:
            buy_ok  = buy_ok  and not no_trade_zone
            sell_ok = sell_ok and not no_trade_zone

        # bar_closed = True always for historical bars
        buy_signal  = base_buy  and buy_ok
        sell_signal = base_sell and sell_ok

        # ── 15. Entry price / target ──────────────────────────────────────────
        range_now = high - low
        is_wide   = range_now > close * (p["bt_wide_candle_pct"] / 100.0)

        if p["use_discount_entry"] and is_wide:
            if buy_signal:
                entry_price = high - range_now * p["bt_wide_entry_fraction"]
            else:
                entry_price = low  + range_now * p["bt_wide_entry_fraction"]
        else:
            entry_price = close

        buy_target  = entry_price * (1.0 + p["bt_target_pct"] / 100.0)
        sell_target = entry_price * (1.0 - p["bt_target_pct"] / 100.0)

        # ── 16. Process entry ─────────────────────────────────────────────────
        if buy_signal:
            state["in_buy"]             = True
            state["in_sell"]            = False
            state["allow_reentry"]      = False
            state["entry_bar_index"]    = i
            state["signal_candle_high"] = high
            state["signal_candle_low"]  = low
            state["signal_candle_close"]= close
            state["early_exit_watch_on"]= p["use_early_exit"]
            state["live_trade_open"]    = True
            state["live_trade_is_buy"]  = True
            state["live_entry_price"]   = entry_price
            state["live_target_price"]  = buy_target
            state["trail_stop_level"]   = None
            state["trail_active"]       = False
            state["trail_peak_price"]   = None
            events.append({
                "type": "BUY",
                "bar_index": i,
                "timestamp": ts,
                "entry_price": round(entry_price, 2),
                "target_price": round(buy_target, 2),
                "quality_score": buy_quality_score,
            })

        elif sell_signal:
            state["in_sell"]            = True
            state["in_buy"]             = False
            state["allow_reentry"]      = False
            state["entry_bar_index"]    = i
            state["signal_candle_high"] = high
            state["signal_candle_low"]  = low
            state["signal_candle_close"]= close
            state["early_exit_watch_on"]= p["use_early_exit"]
            state["live_trade_open"]    = True
            state["live_trade_is_buy"]  = False
            state["live_entry_price"]   = entry_price
            state["live_target_price"]  = sell_target
            state["trail_stop_level"]   = None
            state["trail_active"]       = False
            state["trail_peak_price"]   = None
            events.append({
                "type": "SELL",
                "bar_index": i,
                "timestamp": ts,
                "entry_price": round(entry_price, 2),
                "target_price": round(sell_target, 2),
                "quality_score": sell_quality_score,
            })

        # ── 17. Bars since entry ──────────────────────────────────────────────
        bars_after = (
            (i - state["entry_bar_index"])
            if state["entry_bar_index"] is not None else None
        )

        # ── 18. Trailing stop update (uses PREVIOUS bar's confirmed high/low) ──
        if (p["use_trail_stop"] and
                (state["in_buy"] or state["in_sell"]) and
                state["live_trade_open"] and
                bars_after is not None and bars_after >= 1 and
                state["live_target_price"] is not None and
                state["live_entry_price"]  is not None and
                i > 0):

            prev_high = float(df.iloc[i - 1]["high"])
            prev_low  = float(df.iloc[i - 1]["low"])
            trigger_dist = (abs(state["live_target_price"] - state["live_entry_price"])
                            * p["trail_trigger_pct"])

            if state["in_buy"]:
                new_peak = (prev_high if state["trail_peak_price"] is None
                            else max(state["trail_peak_price"], prev_high))
                state["trail_peak_price"] = new_peak
                if (new_peak - state["live_entry_price"]) >= trigger_dist:
                    state["trail_active"] = True
                if state["trail_active"]:
                    candidate = max(state["live_entry_price"],
                                    new_peak - p["trail_atr_mult"] * atr_val)
                    state["trail_stop_level"] = (
                        candidate if state["trail_stop_level"] is None
                        else max(state["trail_stop_level"], candidate)
                    )

            elif state["in_sell"]:
                new_peak = (prev_low if state["trail_peak_price"] is None
                            else min(state["trail_peak_price"], prev_low))
                state["trail_peak_price"] = new_peak
                if (state["live_entry_price"] - new_peak) >= trigger_dist:
                    state["trail_active"] = True
                if state["trail_active"]:
                    candidate = min(state["live_entry_price"],
                                    new_peak + p["trail_atr_mult"] * atr_val)
                    state["trail_stop_level"] = (
                        candidate if state["trail_stop_level"] is None
                        else min(state["trail_stop_level"], candidate)
                    )

        # ── 19. Exit conditions ───────────────────────────────────────────────
        ep    = state["live_entry_price"]
        tp    = state["live_target_price"]
        trail = state["trail_stop_level"]

        # Target hits (INTRABAR — check high/low)
        exit_buy_target = (
            state["in_buy"] and state["live_trade_open"] and
            state["live_trade_is_buy"] and
            tp is not None and bars_after is not None and bars_after >= 1 and
            high >= tp
        )
        exit_sell_target = (
            state["in_sell"] and state["live_trade_open"] and
            not state["live_trade_is_buy"] and
            tp is not None and bars_after is not None and bars_after >= 1 and
            low <= tp
        )

        # Trail stop hits (INTRABAR, only if target not already hit)
        exit_buy_trail = (
            p["use_trail_stop"] and state["in_buy"] and state["trail_active"] and
            trail is not None and bars_after is not None and bars_after >= 1 and
            low <= trail and not exit_buy_target
        )
        exit_sell_trail = (
            p["use_trail_stop"] and state["in_sell"] and state["trail_active"] and
            trail is not None and bars_after is not None and bars_after >= 1 and
            high >= trail and not exit_sell_target
        )

        # Early exit (bar close) — within first 2 bars after entry
        exit_buy_early = (
            p["use_early_exit"] and state["in_buy"] and state["early_exit_watch_on"] and
            bars_after is not None and bars_after in (1, 2) and
            low  < state["signal_candle_low"] and
            close < state["signal_candle_close"]
        )
        exit_sell_early = (
            p["use_early_exit"] and state["in_sell"] and state["early_exit_watch_on"] and
            bars_after is not None and bars_after in (1, 2) and
            high  > state["signal_candle_high"] and
            close > state["signal_candle_close"]
        )

        # Turn off early-exit watch after 2 bars if not triggered
        if (p["use_early_exit"] and state["early_exit_watch_on"] and
                bars_after is not None and bars_after >= 2 and
                not exit_buy_early and not exit_sell_early):
            state["early_exit_watch_on"] = False

        # Normal exit on bar close — uses in_buy[1] (prev bar state)
        exit_buy_normal  = (in_buy_prev  and close < ema_mid and
                            not exit_buy_target  and not exit_buy_trail)
        exit_sell_normal = (in_sell_prev and close > ema_mid and
                            not exit_sell_target and not exit_sell_trail)

        # Forced EOD exit on bar close — also uses prev bar state
        exit_buy_forced  = (in_buy_prev  and is_force_exit_candle and
                            not exit_buy_target  and not exit_buy_trail)
        exit_sell_forced = (in_sell_prev and is_force_exit_candle and
                            not exit_sell_target and not exit_sell_trail)

        exit_buy  = (exit_buy_target  or exit_buy_trail  or
                     exit_buy_early   or exit_buy_normal  or exit_buy_forced)
        exit_sell = (exit_sell_target or exit_sell_trail or
                     exit_sell_early  or exit_sell_normal or exit_sell_forced)

        # ── 20. Process exits ─────────────────────────────────────────────────
        if exit_buy:
            if exit_buy_target:
                xpx, xtype = float(tp), "TARGET"
            elif exit_buy_trail:
                xpx, xtype = float(trail), "TRAIL_STOP"
            elif exit_buy_early:
                xpx, xtype = close, "EARLY_EXIT"
            elif exit_buy_forced:
                xpx, xtype = close, "FORCE_EXIT"
            else:
                xpx, xtype = close, "NORMAL_EXIT"

            pnl = xpx - float(ep)
            events.append({
                "type": "EXIT_BUY",
                "exit_type": xtype,
                "bar_index": i,
                "timestamp": ts,
                "direction": "BUY",
                "entry_price": round(float(ep), 2),
                "exit_price": round(xpx, 2),
                "pnl_pts": round(pnl, 2),
            })
            state["in_buy"] = False
            state["allow_reentry"] = True
            self._reset_trade(state)

        if exit_sell:
            if exit_sell_target:
                xpx, xtype = float(tp), "TARGET"
            elif exit_sell_trail:
                xpx, xtype = float(trail), "TRAIL_STOP"
            elif exit_sell_early:
                xpx, xtype = close, "EARLY_EXIT"
            elif exit_sell_forced:
                xpx, xtype = close, "FORCE_EXIT"
            else:
                xpx, xtype = close, "NORMAL_EXIT"

            pnl = float(ep) - xpx
            events.append({
                "type": "EXIT_SELL",
                "exit_type": xtype,
                "bar_index": i,
                "timestamp": ts,
                "direction": "SELL",
                "entry_price": round(float(ep), 2),
                "exit_price": round(xpx, 2),
                "pnl_pts": round(pnl, 2),
            })
            state["in_sell"] = False
            state["allow_reentry"] = True
            self._reset_trade(state)

        return events
