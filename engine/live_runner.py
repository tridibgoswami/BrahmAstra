"""
LiveRunner — Phase 2 live trading orchestrator.

Flow:
  1. Connect to AngelOne, fetch warmup data (N days + today so far)
  2. Replay all bars through the signal engine to initialise indicator state
  3. Wait for each new 5-minute candle close (+buffer)
  4. Process the closed bar → act on entry / exit signals
  5. PriceMonitor thread handles intrabar trail stop hits
  6. Force-exit all positions at 3:15 PM IST, send EOD report

Start:
  python main.py live --config config.yaml [--verbose]

Notes:
  - Signals are generated from INDEX price data (same as backtester).
  - Orders are executed on the FUTURES instrument (NFO).
  - Trail stop level is updated at each bar close; PriceMonitor checks
    LTP against it every poll_interval seconds.
  - Cancel-first pattern: on trail hit → cancel target limit order;
    if cancel fails the target was already filled → do nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import pytz

from engine.data_fetcher import AngelOneDataFetcher
from engine.indicators import compute_indicators
from engine.order_manager import OrderManager
from engine.presets import resolve_preset
from engine.price_monitor import PriceMonitor
from engine.reporter import DailyReporter
from engine.signal_engine import SignalEngine
from engine.telegram_bot import NullTelegramBot, TelegramBot

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class LiveRunner:
    def __init__(self, config: dict):
        eng_cfg     = config["engine"]
        broker_cfg  = config["broker"]["angel_one"]
        trading_cfg = config.get("trading", {})
        tg_cfg      = config.get("telegram", {})

        self._symbol    = eng_cfg["symbol"]
        self._timeframe = str(eng_cfg["timeframe"])
        self._warmup_days = int(trading_cfg.get("warmup_days", 25))
        self._lots        = int(trading_cfg.get("lots", 1))
        self._product     = trading_cfg.get("product_type", "MIS")
        self._buf_secs    = int(trading_cfg.get("candle_buffer_secs", 5))
        self._ltp_poll    = float(trading_cfg.get("ltp_poll_interval", 2.0))

        # Preset + optional overrides from config
        self._params, self._preset_key = resolve_preset(self._symbol, self._timeframe)
        if "target_pct" in trading_cfg:
            self._params["bt_target_pct"] = float(trading_cfg["target_pct"])
        if "trail_trigger_pct" in trading_cfg:
            self._params["trail_trigger_pct"] = float(trading_cfg["trail_trigger_pct"])

        # Instrument configs
        sym_key         = self._symbol.lower()
        self._instr_cfg = trading_cfg[sym_key]          # futures: symbol/token/exchange/lot_size
        self._index_cfg = config["symbols"][self._symbol]  # index:   token/exchange

        # Data fetcher
        self._fetcher = AngelOneDataFetcher(broker_cfg)

        # Signal engine + state (shared across all bars, including warmup)
        self._engine = SignalEngine(self._params)
        self._state  = SignalEngine._init_state()
        self._df     = pd.DataFrame()

        # Order manager (set after connect)
        self._order_mgr: Optional[OrderManager] = None

        # Price monitor
        self._monitor = PriceMonitor(
            ltp_getter=self._get_ltp,
            poll_interval=self._ltp_poll,
        )

        # Telegram
        if tg_cfg.get("bot_token") and tg_cfg.get("chat_id"):
            self._tg = TelegramBot(tg_cfg["bot_token"], str(tg_cfg["chat_id"]))
        else:
            logger.info("Telegram not configured — using console logger")
            self._tg = NullTelegramBot()

        # Daily reporter
        self._reporter = DailyReporter()

        # ── Live trade state (protected by _trade_lock) ───────────────────────
        self._trade_lock        = threading.Lock()
        self._in_trade          = False
        self._trade_dir: Optional[str] = None   # 'BUY' or 'SELL'
        self._entry_order_id: Optional[str] = None
        self._target_order_id: Optional[str] = None
        self._trade_qty         = 0
        self._fill_price        = 0.0
        self._target_price      = 0.0

        # Engine stop flag (set by /stop command)
        self._stop_ev = threading.Event()

    # ─────────────────────────────────────────────────────────────────────────
    # Entry point
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        self._connect()
        self._tg.start_polling(self._on_command)
        self._warmup()
        self._market_loop()
        self._end_of_day()

    # ─────────────────────────────────────────────────────────────────────────
    # Startup helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _connect(self):
        logger.info("Connecting to AngelOne…")
        self._fetcher.connect()
        # Reuse the same SmartConnect object for orders
        self._order_mgr = OrderManager(
            smart_api   = self._fetcher._obj,
            futures_cfg = self._instr_cfg,
            index_cfg   = self._index_cfg,
            product_type= self._product,
        )
        logger.info("Connected. Futures: %s | Lots: %d",
                    self._instr_cfg["symbol"], self._lots)
        self._tg.send(
            f"🟢 *BrahmAstra Live*\n"
            f"Symbol: {self._symbol} {self._timeframe}m | Preset: {self._preset_key}\n"
            f"Instrument: {self._instr_cfg['symbol']} | Lots: {self._lots}"
        )

    def _warmup(self):
        """Fetch historical data and replay bars to seed indicator state."""
        now_ist = datetime.now(IST)
        from_dt = (now_ist - timedelta(days=self._warmup_days)).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
        to_dt = now_ist

        logger.info("Fetching warmup data: %s → now", from_dt.strftime("%Y-%m-%d"))
        df = self._fetcher.get_historical_data(
            self._index_cfg["token"], self._index_cfg["exchange"],
            from_dt, to_dt, self._timeframe,
        )

        if df.empty:
            raise RuntimeError("Warmup fetch returned no data. Check credentials/symbol.")

        # Market hours only
        ts_ist = df["timestamp"].dt.tz_convert(IST)
        mkt = (
            ((ts_ist.dt.hour > 9) | ((ts_ist.dt.hour == 9) & (ts_ist.dt.minute >= 15))) &
            ((ts_ist.dt.hour < 15) | ((ts_ist.dt.hour == 15) & (ts_ist.dt.minute <= 30)))
        )
        df = df[mkt].reset_index(drop=True)

        df = compute_indicators(df, self._params)
        self._df = df

        # Replay all bars — builds state without placing any orders
        logger.info("Replaying %d warmup bars…", len(df))
        for i in range(len(df)):
            self._engine._process_bar(i, df, self._state)

        first_atr = df["atr_filter"].dropna().iloc[-1] if not df.empty else 0
        logger.info(
            "Warmup complete. Bars: %d | Latest ATR: %.2f | "
            "State: in_buy=%s in_sell=%s",
            len(df), first_atr,
            self._state["in_buy"], self._state["in_sell"],
        )

        if self._state["in_buy"] or self._state["in_sell"]:
            logger.warning(
                "Warmup replay shows an open trade — engine started mid-session. "
                "Signals will resume from next candle but no order was placed for "
                "the existing warmup trade."
            )
            # Reset so we don't try to 'exit' a trade we never entered live
            self._engine._reset_day(self._state)

    # ─────────────────────────────────────────────────────────────────────────
    # Main market loop
    # ─────────────────────────────────────────────────────────────────────────

    def _market_loop(self):
        logger.info("Entering market loop…")
        while not self._stop_ev.is_set():
            now = datetime.now(IST)

            # EOD check
            if now.hour > 15 or (now.hour == 15 and now.minute >= 15):
                logger.info("Reached force-exit time. Exiting market loop.")
                break

            # Sleep until the next 5-minute candle close + buffer
            self._sleep_to_next_candle()

            # Re-check after sleep
            now = datetime.now(IST)
            if now.hour > 15 or (now.hour == 15 and now.minute >= 15):
                break
            if self._stop_ev.is_set():
                break

            # Fetch the most recently closed candle
            new_row = self._fetch_latest_candle()
            if new_row is None:
                logger.warning("Could not fetch latest candle — skipping bar")
                continue

            # Append + recompute all indicators
            self._df = self._append_and_recompute(self._df, new_row)
            i = len(self._df) - 1

            # Run signal engine on the new bar
            events = self._engine._process_bar(i, self._df, self._state)

            # Sync trail level to price monitor (always, even if no events)
            self._sync_trail_monitor()

            # Act on any events
            for ev in events:
                self._dispatch(ev)

    # ─────────────────────────────────────────────────────────────────────────
    # Candle timing helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _sleep_to_next_candle(self):
        """Sleep until the next 5-minute candle close + self._buf_secs."""
        now = datetime.now(IST)
        rem = now.minute % 5
        wait_mins = (5 - rem) if rem != 0 else 5
        target = now.replace(second=0, microsecond=0) + timedelta(minutes=wait_mins)
        target += timedelta(seconds=self._buf_secs)
        sleep_secs = (target - datetime.now(IST)).total_seconds()
        if sleep_secs > 0:
            logger.debug("Sleeping %.1fs until %s", sleep_secs, target.strftime("%H:%M:%S"))
            # Sleep in 1-second chunks so stop_ev is responsive
            deadline = time.time() + sleep_secs
            while time.time() < deadline and not self._stop_ev.is_set():
                time.sleep(min(1.0, deadline - time.time()))

    def _fetch_latest_candle(self) -> Optional[pd.Series]:
        """Fetch the last 3 completed candles from AngelOne; return the newest."""
        now_ist = datetime.now(IST)
        from_dt = now_ist - timedelta(minutes=20)
        try:
            df = self._fetcher.get_historical_data(
                self._index_cfg["token"], self._index_cfg["exchange"],
                from_dt, now_ist, self._timeframe,
            )
            if df.empty:
                return None
            # Filter to market hours and take most recent bar
            ts_ist = df["timestamp"].dt.tz_convert(IST)
            mkt = (
                ((ts_ist.dt.hour > 9) | ((ts_ist.dt.hour == 9) & (ts_ist.dt.minute >= 15))) &
                ((ts_ist.dt.hour < 15) | ((ts_ist.dt.hour == 15) & (ts_ist.dt.minute <= 30)))
            )
            df = df[mkt]
            if df.empty:
                return None
            return df.iloc[-1]
        except Exception as exc:
            logger.error("fetch_latest_candle failed: %s", exc)
            return None

    def _append_and_recompute(self, df: pd.DataFrame,
                              new_row: pd.Series) -> pd.DataFrame:
        """Append new_row to df, deduplicate, recompute all indicators."""
        new_df = pd.DataFrame([new_row])
        combined = pd.concat([df, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset="timestamp") \
                           .sort_values("timestamp") \
                           .reset_index(drop=True)
        return compute_indicators(combined, self._params)

    # ─────────────────────────────────────────────────────────────────────────
    # Signal dispatch
    # ─────────────────────────────────────────────────────────────────────────

    def _dispatch(self, ev: dict):
        etype = ev["type"]
        if etype in ("BUY", "SELL"):
            self._enter_trade(ev)
        elif etype in ("EXIT_BUY", "EXIT_SELL"):
            self._handle_bar_close_exit(ev)

    # ─────────────────────────────────────────────────────────────────────────
    # Entry
    # ─────────────────────────────────────────────────────────────────────────

    def _enter_trade(self, ev: dict):
        direction   = ev["type"]          # 'BUY' or 'SELL'
        entry_px    = ev["entry_price"]
        target_px   = ev["target_price"]
        quality     = ev.get("quality_score", "?")
        qty         = self._order_mgr.qty_for_lots(self._lots)
        ts          = ev["timestamp"].tz_convert(IST).strftime("%H:%M")

        logger.info("Entry signal: %s @%.2f  target=%.2f  score=%s",
                    direction, entry_px, target_px, quality)
        self._tg.send(
            f"📣 *{direction} Signal* @{ts}\n"
            f"Entry: `{entry_px:.2f}` | Target: `{target_px:.2f}`\n"
            f"Score: {quality}/10"
        )

        # Place limit entry order
        order_id = self._order_mgr.place_limit(direction, entry_px, qty)
        if order_id is None:
            self._tg.send(f"⚠️ Entry order placement failed for {direction} @{entry_px:.2f}")
            return

        with self._trade_lock:
            self._entry_order_id = order_id

        # Wait for fill (one candle = 300 s; we give 60 s)
        result = self._order_mgr.wait_for_fill(order_id, timeout=60)
        if not result["filled"]:
            self._order_mgr.cancel(order_id)
            with self._trade_lock:
                self._entry_order_id = None
            self._tg.send(f"⚠️ Entry not filled in 60s — order cancelled")
            # Signal engine expects a trade; reset so it can re-signal next bar
            self._engine._reset_trade(self._state)
            self._state["in_buy"]  = False
            self._state["in_sell"] = False
            self._state["allow_reentry"] = True
            return

        actual_fill = result["price"] or entry_px

        # Place limit target order (opposite direction)
        exit_dir = "SELL" if direction == "BUY" else "BUY"
        tgt_id   = self._order_mgr.place_limit(exit_dir, target_px, qty)

        with self._trade_lock:
            self._in_trade         = True
            self._trade_dir        = direction
            self._fill_price       = actual_fill
            self._target_price     = target_px
            self._trade_qty        = qty
            self._target_order_id  = tgt_id

        now_ist = datetime.now(IST)
        self._reporter.on_entry(direction, actual_fill, target_px, qty, now_ist)

        # Start intrabar price monitor
        self._monitor.start(direction, trail_callback=self._on_trail_hit)

        self._tg.send(
            f"✅ *{direction} Filled* @`{actual_fill:.2f}`\n"
            f"Target order: `{target_px:.2f}` | Qty: {qty}"
        )
        logger.info("Trade open: %s fill=%.2f target=%.2f qty=%d",
                    direction, actual_fill, target_px, qty)

    # ─────────────────────────────────────────────────────────────────────────
    # Bar-close exits (NORMAL, FORCE, EARLY)
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_bar_close_exit(self, ev: dict):
        exit_type = ev.get("exit_type", "")

        # TARGET and TRAIL_STOP exits are handled intrabar.
        # If the signal engine also fires them at bar-close (safety net),
        # check whether we're still actually in a trade before acting.
        if exit_type in ("TARGET", "TRAIL_STOP"):
            with self._trade_lock:
                still_open = self._in_trade
            if not still_open:
                return  # already handled intrabar — ignore
            # If still open (monitor missed it), fall through to exit
            logger.warning("Bar-close safety-net exit: %s", exit_type)

        self._do_exit(exit_type, ev.get("exit_price") or None)

    # ─────────────────────────────────────────────────────────────────────────
    # Trail stop hit callback (from PriceMonitor thread)
    # ─────────────────────────────────────────────────────────────────────────

    def _on_trail_hit(self, ltp: float):
        """Cancel-first pattern: cancel target limit, then market-exit."""
        with self._trade_lock:
            if not self._in_trade:
                return
            tgt_id  = self._target_order_id
            tgt_px  = self._target_price
            qty     = self._trade_qty
            fillpx  = self._fill_price
            direction = self._trade_dir

        logger.info("Trail hit callback: LTP=%.2f", ltp)

        # Try to cancel the target limit order
        cancelled = False
        if tgt_id:
            cancelled = self._order_mgr.cancel(tgt_id)

        if not cancelled:
            # cancel() returns False when order was already FILLED
            # → target was hit before trail; we are already flat
            logger.info("Target already filled — position flat, no action needed")
            self._close_trade_state("TARGET", tgt_px)
            return

        # Target cancelled successfully → place market exit
        exit_dir = "SELL" if direction == "BUY" else "BUY"
        self._order_mgr.place_market(exit_dir, qty)
        self._close_trade_state("TRAIL_STOP", ltp)

    # ─────────────────────────────────────────────────────────────────────────
    # Generic exit executor
    # ─────────────────────────────────────────────────────────────────────────

    def _do_exit(self, exit_type: str, hint_price: Optional[float] = None):
        with self._trade_lock:
            if not self._in_trade:
                return
            tgt_id    = self._target_order_id
            qty       = self._trade_qty
            direction = self._trade_dir

        # Stop the price monitor
        self._monitor.stop()

        # Cancel target limit if still open
        if tgt_id:
            self._order_mgr.cancel(tgt_id)

        # Place market exit
        exit_dir = "SELL" if direction == "BUY" else "BUY"
        self._order_mgr.place_market(exit_dir, qty)

        exit_px = hint_price or self._order_mgr.get_ltp() or 0.0
        self._close_trade_state(exit_type, exit_px)

    # ─────────────────────────────────────────────────────────────────────────
    # Trade state cleanup (always called under lock already acquired or new)
    # ─────────────────────────────────────────────────────────────────────────

    def _close_trade_state(self, exit_type: str, exit_px: float):
        with self._trade_lock:
            if not self._in_trade:
                return
            direction  = self._trade_dir
            fill_px    = self._fill_price
            self._in_trade        = False
            self._trade_dir       = None
            self._entry_order_id  = None
            self._target_order_id = None
            self._trade_qty       = 0

        pnl = (exit_px - fill_px if direction == "BUY" else fill_px - exit_px)
        icon = "✅" if pnl >= 0 else "❌"
        now_ist = datetime.now(IST)

        self._reporter.on_exit(exit_px, exit_type, now_ist)
        self._tg.send(
            f"{icon} *{exit_type}* — {direction} closed\n"
            f"Entry `{fill_px:.2f}` → Exit `{exit_px:.2f}` | "
            f"P&L: `{pnl:+.2f} pts`"
        )
        logger.info("Trade closed: %s %s fill=%.2f exit=%.2f pnl=%.2f",
                    direction, exit_type, fill_px, exit_px, pnl)

    # ─────────────────────────────────────────────────────────────────────────
    # Trail stop sync
    # ─────────────────────────────────────────────────────────────────────────

    def _sync_trail_monitor(self):
        """Push latest trail level from signal engine state to PriceMonitor."""
        with self._trade_lock:
            if not self._in_trade:
                return
        self._monitor.update_trail(
            trail_level  = self._state.get("trail_stop_level"),
            trail_active = bool(self._state.get("trail_active", False)),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # LTP helper (passed to PriceMonitor)
    # ─────────────────────────────────────────────────────────────────────────

    def _get_ltp(self) -> Optional[float]:
        if self._order_mgr is None:
            return None
        return self._order_mgr.get_ltp()

    # ─────────────────────────────────────────────────────────────────────────
    # End of day
    # ─────────────────────────────────────────────────────────────────────────

    def _end_of_day(self):
        logger.info("EOD reached — force-exiting any open position")

        with self._trade_lock:
            still_open = self._in_trade

        if still_open:
            self._do_exit("FORCE_EXIT")

        self._monitor.stop()
        self._tg.stop()

        report = self._reporter.summary()
        logger.info("EOD Report:\n%s", report)
        self._tg.send(report)

    # ─────────────────────────────────────────────────────────────────────────
    # Telegram command handler
    # ─────────────────────────────────────────────────────────────────────────

    def _on_command(self, text: str) -> Optional[str]:
        cmd = text.lower().split()[0]

        if cmd == "/status":
            with self._trade_lock:
                open_trade = self._in_trade
            if open_trade:
                ltp = self._get_ltp() or 0
                return self._reporter.unrealized(ltp)
            return "No open trade."

        if cmd == "/pnl":
            return self._reporter.summary()

        if cmd == "/stop":
            self._stop_ev.set()
            with self._trade_lock:
                still_open = self._in_trade
            if still_open:
                threading.Thread(
                    target=self._do_exit, args=("FORCE_EXIT",), daemon=True
                ).start()
            return "🛑 Engine stopping — force-exiting if in trade."

        return f"Unknown command: {text}\nAvailable: /status /pnl /stop"
