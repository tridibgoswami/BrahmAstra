"""
OptionsRunner — independent intraday option-selling engine.

Strategy
--------
  BUY  signal → SELL PE (put) at ATM − (offset × strike_interval)
  SELL signal → SELL CE (call) at ATM + (offset × strike_interval)

Profit comes from theta decay, IV crush, and directional confirmation.
Uses the same BrahmAstra signal engine (own instance — zero shared state).

Entry rules
-----------
  • Signal must fire after entry_after time (default 10:00 IST)
  • Option premium must be ≥ min_premium (skip illiquid / cheap strikes)
  • Only one open position at a time; re-entry allowed after exit

Exit rules (priority order)
---------------------------
  1. STOP LOSS  — premium reaches stop_loss_mult × entry (e.g. 2× = 100% loss on premium)
  2. TARGET     — premium decays to target_pct % of entry (e.g. 30 % remaining = 70 % captured)
                  Set target_pct = 0 to skip and rely only on time exit.
  3. TIME EXIT  — hard square-off at exit_by time (default 15:00)

All three checks run in a background thread polling the option LTP every
ltp_poll_interval seconds.

Usage
-----
  python main.py options [--config config.yaml] [--verbose]

This module is completely independent — it does NOT import or modify
any state from live_runner, order_manager, price_monitor, or reporter.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import pytz

from engine.data_fetcher import AngelOneDataFetcher
from engine.indicators import compute_indicators
from engine.presets import resolve_preset
from engine.scrip_master import ScripMaster
from engine.signal_engine import SignalEngine
from engine.telegram_bot import NullTelegramBot, TelegramBot

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class OptionsRunner:
    """
    Self-contained options selling engine.
    Instantiate, call .start() — it blocks until market close.
    """

    def __init__(self, config: dict):
        eng_cfg    = config["engine"]
        broker_cfg = config["broker"]["angel_one"]
        tg_cfg     = config.get("telegram", {})
        opt_cfg    = config.get("options_selling", {})
        trade_cfg  = config.get("trading", {})

        self._symbol    = eng_cfg["symbol"]
        self._timeframe = str(eng_cfg["timeframe"])
        self._warmup_days = int(trade_cfg.get("warmup_days", 25))
        self._buf_secs    = int(trade_cfg.get("candle_buffer_secs", 5))

        # Options config
        self._paper_mode    = bool(opt_cfg.get("paper_mode", True))
        self._lots          = int(opt_cfg.get("lots", 1))
        self._entry_after   = str(opt_cfg.get("entry_after", "10:00"))
        self._exit_by       = str(opt_cfg.get("exit_by", "15:00"))
        self._strike_offset = int(opt_cfg.get("strike_offset", 1))
        self._min_premium   = float(opt_cfg.get("min_premium", 50))
        self._sl_mult       = float(opt_cfg.get("stop_loss_mult", 2.0))
        self._target_pct    = float(opt_cfg.get("target_pct", 30))
        self._poll_secs     = float(opt_cfg.get("ltp_poll_interval", 5.0))

        eh, em = map(int, self._entry_after.split(":"))
        self._entry_after_mins = eh * 60 + em
        xh, xm = map(int, self._exit_by.split(":"))
        self._exit_by_mins = xh * 60 + xm

        # Broker / data (own connection — independent of futures engine)
        self._fetcher   = AngelOneDataFetcher(broker_cfg)
        self._api       = None      # SmartConnect object; set in _connect()

        # Index config for LTP and candle data
        self._index_cfg = config["symbols"][self._symbol]

        # Signal engine — own instance, zero shared state with live_runner
        self._params, self._preset_key = resolve_preset(self._symbol, self._timeframe)
        self._sig_engine = SignalEngine(self._params)
        self._sig_state  = SignalEngine._init_state()
        self._df         = pd.DataFrame()

        # Scrip master (option token lookup)
        self._scrip = ScripMaster()

        # Telegram
        if tg_cfg.get("bot_token") and tg_cfg.get("chat_id"):
            self._tg = TelegramBot(tg_cfg["bot_token"], str(tg_cfg["chat_id"]))
        else:
            self._tg = NullTelegramBot()

        # ── Open trade state (protected by _lock) ─────────────────────────────
        self._lock        = threading.Lock()
        self._in_trade    = False
        self._opt_type    = ""          # "CE" or "PE"
        self._opt_token   = ""
        self._opt_symbol  = ""
        self._entry_prem  = 0.0        # premium per unit at sell time
        self._trade_qty   = 0          # total units = lots × lot_size
        self._entry_time: Optional[datetime] = None

        # Daily completed trades
        self._trades: List[Dict[str, Any]] = []

        # Control
        self._stop_ev  = threading.Event()
        self._mon_thread: Optional[threading.Thread] = None
        self._paper_ctr = 0

    # ─────────────────────────────────────────────────────────────────────────
    # Entry point
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        self._connect()
        self._tg.start_polling(self._on_command)
        self._warmup()

        now      = datetime.now(IST)
        now_mins = now.hour * 60 + now.minute
        if now_mins >= self._exit_by_mins:
            msg = ("Options engine: market closed — nothing to trade today.\n"
                   "Start again tomorrow before 09:15 IST.")
            logger.info(msg)
            self._tg.send(f"🔴 {msg}")
            self._tg.stop()
            return

        self._market_loop()
        self._eod()

    # ─────────────────────────────────────────────────────────────────────────
    # Connect
    # ─────────────────────────────────────────────────────────────────────────

    def _connect(self):
        logger.info("Options engine: connecting to AngelOne…")
        self._fetcher.connect()
        self._api = self._fetcher._obj

        logger.info("Options engine: loading scrip master…")
        self._scrip.load()

        mode = "PAPER" if self._paper_mode else "LIVE"
        offset_label = "ATM" if self._strike_offset == 0 else f"{self._strike_offset} OTM"
        target_label = (f"{100 - self._target_pct:.0f}% decay"
                        if self._target_pct > 0 else f"hold to {self._exit_by}")
        logger.info("Options engine ready | %s | %s | Lots:%d | Entry>%s | Exit<%s",
                    self._symbol, mode, self._lots, self._entry_after, self._exit_by)
        self._tg.send(
            f"{'📝' if self._paper_mode else '🟢'} *Options Engine* ({mode})\n"
            f"Symbol: {self._symbol} {self._timeframe}m | "
            f"Preset: {self._preset_key}\n"
            f"Strike: {offset_label} | "
            f"Min premium: ₹{self._min_premium:.0f}\n"
            f"SL: {self._sl_mult}× premium | "
            f"Target: {target_label}\n"
            f"Entry after: {self._entry_after} | Exit by: {self._exit_by}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Warmup — seeds signal engine with previous days + today's history
    # ─────────────────────────────────────────────────────────────────────────

    def _warmup(self):
        now_ist    = datetime.now(IST)
        today_date = now_ist.date()
        from_dt    = (now_ist - timedelta(days=self._warmup_days)).replace(
            hour=9, minute=15, second=0, microsecond=0)

        logger.info("Options engine: fetching warmup data %s → now",
                    from_dt.strftime("%Y-%m-%d"))
        df = self._fetcher.get_historical_data(
            self._index_cfg["token"], self._index_cfg["exchange"],
            from_dt, now_ist, self._timeframe,
        )
        if df.empty:
            raise RuntimeError("Options engine: warmup fetch returned no data.")

        ts_raw = df["timestamp"].dt.tz_convert(IST)
        mkt = (
            ((ts_raw.dt.hour > 9) | ((ts_raw.dt.hour == 9) & (ts_raw.dt.minute >= 15))) &
            ((ts_raw.dt.hour < 15) | ((ts_raw.dt.hour == 15) & (ts_raw.dt.minute <= 30)))
        )
        df = df[mkt].reset_index(drop=True)
        df = compute_indicators(df, self._params)
        df = df.reset_index(drop=True)
        self._df = df

        ts_ist   = df["timestamp"].dt.tz_convert(IST)
        is_today = ts_ist.dt.date == today_date
        prev_idx  = df.index[~is_today].tolist()
        today_idx = df.index[is_today].tolist()

        logger.info("Options engine: seeding %d prev-day bars…", len(prev_idx))
        for i in prev_idx:
            self._sig_engine._process_bar(i, df, self._sig_state)

        logger.info("Options engine: replaying %d today bars…", len(today_idx))
        for i in today_idx:
            self._sig_engine._process_bar(i, df, self._sig_state)

        # Reset any open replay trade — no retroactive order was placed
        if self._sig_state["in_buy"] or self._sig_state["in_sell"]:
            self._sig_engine._reset_trade(self._sig_state)
            self._sig_state["in_buy"]        = False
            self._sig_state["in_sell"]       = False
            self._sig_state["allow_reentry"] = True

        last_atr = df["atr_filter"].dropna().iloc[-1] if not df.empty else 0.0
        logger.info("Options engine warmup complete. ATR=%.2f", last_atr)

    # ─────────────────────────────────────────────────────────────────────────
    # Market loop
    # ─────────────────────────────────────────────────────────────────────────

    def _market_loop(self):
        logger.info("Options engine: entering market loop…")
        while not self._stop_ev.is_set():
            now      = datetime.now(IST)
            now_mins = now.hour * 60 + now.minute
            if now_mins >= self._exit_by_mins:
                logger.info("Options engine: reached exit time %s", self._exit_by)
                break

            self._sleep_to_next_candle()

            now      = datetime.now(IST)
            now_mins = now.hour * 60 + now.minute
            if now_mins >= self._exit_by_mins or self._stop_ev.is_set():
                break

            new_row = self._fetch_latest_candle()
            if new_row is None:
                logger.warning("Options engine: candle fetch returned nothing — skipping")
                continue

            self._df = self._append_and_recompute(self._df, new_row)
            i        = len(self._df) - 1
            events   = self._sig_engine._process_bar(i, self._df, self._sig_state)

            for ev in events:
                if ev["type"] in ("BUY", "SELL"):
                    self._on_signal(ev)
                # EXIT events from the signal engine are for futures tracking only;
                # options exits are handled independently by the monitor thread.

    # ─────────────────────────────────────────────────────────────────────────
    # Signal handler — called when signal engine fires a BUY or SELL
    # ─────────────────────────────────────────────────────────────────────────

    def _on_signal(self, ev: dict):
        now      = datetime.now(IST)
        now_mins = now.hour * 60 + now.minute

        # Time gate
        if now_mins < self._entry_after_mins:
            logger.info("Options: %s signal at %s ignored (before entry window %s)",
                        ev["type"], now.strftime("%H:%M"), self._entry_after)
            return

        # Only one position at a time
        with self._lock:
            if self._in_trade:
                logger.info("Options: signal ignored — already in an options trade")
                return

        direction = ev["type"]            # "BUY" or "SELL"
        opt_type  = "PE" if direction == "BUY" else "CE"
        ts_label  = ev["timestamp"].tz_convert(IST).strftime("%H:%M")

        # Real-time spot for strike selection (not bar close — that's 5 min old)
        spot = self._get_index_ltp()
        if spot is None or spot <= 0:
            logger.warning("Options: cannot get index LTP — skipping signal")
            return

        # Strike calculation
        interval = self._scrip.strike_interval(self._symbol)
        atm      = round(spot / interval) * interval
        strike   = (atm - self._strike_offset * interval if opt_type == "PE"
                    else atm + self._strike_offset * interval)

        # Find expiry + instrument
        expiry   = self._scrip.nearest_expiry(self._symbol)
        lot_size = self._scrip.lot_size(self._symbol, expiry)
        instr    = self._scrip.find_option(self._symbol, expiry, strike, opt_type)

        if instr is None:
            msg = (f"⚠️ Options: no instrument found for "
                   f"{self._symbol} {expiry.strftime('%d%b%y')} "
                   f"{strike:.0f} {opt_type}")
            logger.warning(msg.replace("⚠️ Options: ", ""))
            self._tg.send(msg)
            return

        opt_token  = str(instr["token"])
        opt_symbol = str(instr["symbol"])

        # Fetch option LTP for premium check
        opt_ltp = self._get_option_ltp(opt_token)
        if opt_ltp is None or opt_ltp <= 0:
            logger.warning("Options: cannot fetch LTP for %s — skip", opt_symbol)
            return

        if opt_ltp < self._min_premium:
            logger.info("Options: %s premium ₹%.0f < min ₹%.0f — skip",
                        opt_symbol, opt_ltp, self._min_premium)
            self._tg.send(
                f"⚠️ Options: `{opt_symbol}` premium ₹{opt_ltp:.0f} "
                f"< minimum ₹{self._min_premium:.0f} — skipped"
            )
            return

        qty = self._lots * lot_size
        logger.info("Options: SELL %s %s @%.2f  qty=%d  spot=%.2f  expiry=%s",
                    opt_type, opt_symbol, opt_ltp, qty, spot,
                    expiry.strftime("%d%b%y"))

        # Place sell order
        sell_id = self._place_sell_limit(opt_token, opt_symbol, opt_ltp, qty)
        if sell_id is None:
            self._tg.send(f"⚠️ Options: sell order failed for `{opt_symbol}`")
            return

        # Record trade state
        with self._lock:
            self._in_trade   = True
            self._opt_type   = opt_type
            self._opt_token  = opt_token
            self._opt_symbol = opt_symbol
            self._entry_prem = opt_ltp
            self._trade_qty  = qty
            self._entry_time = now

        sl_prem     = opt_ltp * self._sl_mult
        target_prem = opt_ltp * self._target_pct / 100 if self._target_pct > 0 else None

        self._tg.send(
            f"📣 *Options SELL {opt_type}* @{ts_label}\n"
            f"`{opt_symbol}`\n"
            f"Premium sold: `₹{opt_ltp:.2f}` | Qty: {qty} "
            f"({self._lots} lot{'s' if self._lots > 1 else ''})\n"
            f"Spot: {spot:.0f} | Strike: {strike:.0f}\n"
            f"SL: `₹{sl_prem:.2f}` | "
            + (f"Target: `₹{target_prem:.2f}`"
               if target_prem else f"Time exit: {self._exit_by}")
        )

        self._start_monitor()

    # ─────────────────────────────────────────────────────────────────────────
    # Monitor thread — polls option LTP for SL / target / time exit
    # ─────────────────────────────────────────────────────────────────────────

    def _start_monitor(self):
        self._stop_ev.clear()
        self._mon_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="opt-monitor"
        )
        self._mon_thread.start()
        logger.info("Options monitor started")

    def _monitor_loop(self):
        while not self._stop_ev.is_set():
            with self._lock:
                if not self._in_trade:
                    return
                token      = self._opt_token
                entry_prem = self._entry_prem

            ltp = self._get_option_ltp(token)
            if ltp is None or ltp <= 0:
                self._stop_ev.wait(self._poll_secs)
                continue

            # 1. Stop loss
            if ltp >= entry_prem * self._sl_mult:
                logger.warning(
                    "Options SL hit: %s  premium=%.2f  entry=%.2f  SL_level=%.2f",
                    self._opt_symbol, ltp, entry_prem, entry_prem * self._sl_mult)
                self._exit_trade("STOP_LOSS", ltp)
                return

            # 2. Target
            if self._target_pct > 0:
                target = entry_prem * self._target_pct / 100
                if ltp <= target:
                    logger.info(
                        "Options target hit: %s  premium=%.2f  target=%.2f",
                        self._opt_symbol, ltp, target)
                    self._exit_trade("TARGET", ltp)
                    return

            # 3. Time exit
            now_mins = datetime.now(IST).hour * 60 + datetime.now(IST).minute
            if now_mins >= self._exit_by_mins:
                logger.info("Options time exit triggered: %s", self._opt_symbol)
                self._exit_trade("TIME_EXIT", ltp)
                return

            self._stop_ev.wait(self._poll_secs)

    # ─────────────────────────────────────────────────────────────────────────
    # Exit trade — called from monitor thread or _eod()
    # ─────────────────────────────────────────────────────────────────────────

    def _exit_trade(self, reason: str, exit_prem: float):
        with self._lock:
            if not self._in_trade:
                return                # already exited (double-fire guard)
            symbol     = self._opt_symbol
            token      = self._opt_token
            qty        = self._trade_qty
            entry_prem = self._entry_prem
            opt_type   = self._opt_type
            entry_time = self._entry_time
            self._in_trade = False   # mark closed immediately (prevents double-exit)

        self._stop_ev.set()         # stop the monitor loop if running

        # Buy back the option
        self._place_buy_market(token, symbol, qty)

        # P&L (selling means we want premium to fall)
        pnl_per_unit = entry_prem - exit_prem
        pnl_inr      = pnl_per_unit * qty

        now_ist  = datetime.now(IST)
        duration = ""
        if entry_time:
            mins     = int((now_ist - entry_time).total_seconds() / 60)
            duration = f"  |  {mins}m"

        self._trades.append({
            "symbol":      symbol,
            "opt_type":    opt_type,
            "entry_prem":  entry_prem,
            "exit_prem":   exit_prem,
            "qty":         qty,
            "pnl_per_unit": pnl_per_unit,
            "pnl_inr":     pnl_inr,
            "reason":      reason,
            "entry_time":  entry_time,
            "exit_time":   now_ist,
        })

        icon = "✅" if pnl_inr >= 0 else "❌"
        self._tg.send(
            f"{icon} *Options {reason}* — {opt_type} closed\n"
            f"`{symbol}`\n"
            f"Sold `₹{entry_prem:.2f}` → Bought `₹{exit_prem:.2f}`\n"
            f"P&L: `{pnl_per_unit:+.2f} pts × {qty} = ₹{pnl_inr:+.0f}`"
            f"{duration}"
        )
        logger.info("Options %s: %s entry=%.2f exit=%.2f pnl=₹%.0f",
                    reason, symbol, entry_prem, exit_prem, pnl_inr)

        # Clear stop so the market loop can take the next signal
        self._stop_ev.clear()

    # ─────────────────────────────────────────────────────────────────────────
    # End of day
    # ─────────────────────────────────────────────────────────────────────────

    def _eod(self):
        logger.info("Options engine: EOD — force-closing any open position")

        with self._lock:
            still_open = self._in_trade

        if still_open:
            ltp = self._get_option_ltp(self._opt_token)
            self._exit_trade("TIME_EXIT", ltp or self._entry_prem)

        self._stop_ev.set()
        if self._mon_thread and self._mon_thread.is_alive():
            self._mon_thread.join(timeout=5)

        self._tg.stop()

        # Daily P&L summary
        if not self._trades:
            logger.info("Options engine: no trades today.")
            self._tg.send("📊 *Options P&L*: No trades today.")
            return

        total_pnl = sum(t["pnl_inr"] for t in self._trades)
        wins      = sum(1 for t in self._trades if t["pnl_inr"] >= 0)
        losses    = len(self._trades) - wins

        lines = ["📊 *Options Daily P&L*\n"]
        for idx, t in enumerate(self._trades, 1):
            icon = "✅" if t["pnl_inr"] >= 0 else "❌"
            ei   = t["entry_time"].strftime("%H:%M") if t["entry_time"] else "?"
            xo   = t["exit_time"].strftime("%H:%M")  if t["exit_time"]  else "?"
            lines.append(
                f"{icon} #{idx} {t['opt_type']} {ei}→{xo}  "
                f"@{t['entry_prem']:.0f}→@{t['exit_prem']:.0f}  "
                f"({t['pnl_per_unit']:+.0f} pts)  "
                f"₹{t['pnl_inr']:+.0f}  [{t['reason']}]"
            )

        lines.append(f"\nTrades: {len(self._trades)}  W:{wins}  L:{losses}")
        lines.append(f"Net P&L: ₹{total_pnl:+.0f}")
        if self._paper_mode:
            lines.append("_(Paper mode — no real orders)_")

        report = "\n".join(lines)
        logger.info("Options EOD Report:\n%s", report)
        self._tg.send(report)

    # ─────────────────────────────────────────────────────────────────────────
    # Order placement
    # ─────────────────────────────────────────────────────────────────────────

    def _place_sell_limit(self, token: str, symbol: str,
                          price: float, qty: int) -> Optional[str]:
        """Sell option at limit price (entry — collect premium)."""
        if self._paper_mode:
            self._paper_ctr += 1
            oid = f"OPT-PAPER-{self._paper_ctr:04d}"
            logger.info("[PAPER-OPT] SELL LIMIT %s @%.2f qty=%d → %s",
                        symbol, price, qty, oid)
            return oid

        params = {
            "variety":         "NORMAL",
            "tradingsymbol":   symbol,
            "symboltoken":     token,
            "transactiontype": "SELL",
            "exchange":        "NFO",
            "ordertype":       "LIMIT",
            "producttype":     "MIS",
            "duration":        "DAY",
            "price":           f"{price:.2f}",
            "squareoff":       "0",
            "stoploss":        "0",
            "quantity":        str(qty),
        }
        for attempt in range(3):
            try:
                oid = self._api.placeOrder(params)
                logger.info("Options SELL LIMIT placed: %s @%.2f → %s",
                            symbol, price, oid)
                return str(oid)
            except Exception as exc:
                wait = 2 ** attempt
                logger.warning("Options placeOrder SELL attempt %d failed: %s — retry in %ds",
                               attempt + 1, exc, wait)
                time.sleep(wait)

        logger.error("Options SELL order failed after 3 attempts: %s", symbol)
        return None

    def _place_buy_market(self, token: str, symbol: str,
                          qty: int) -> Optional[str]:
        """Buy back option at market (exit — close the short)."""
        if self._paper_mode:
            self._paper_ctr += 1
            ltp = self._get_option_ltp(token) or 0.0
            oid = f"OPT-PAPER-{self._paper_ctr:04d}"
            logger.info("[PAPER-OPT] BUY MARKET %s @~%.2f qty=%d → %s",
                        symbol, ltp, qty, oid)
            return oid

        params = {
            "variety":         "NORMAL",
            "tradingsymbol":   symbol,
            "symboltoken":     token,
            "transactiontype": "BUY",
            "exchange":        "NFO",
            "ordertype":       "MARKET",
            "producttype":     "MIS",
            "duration":        "DAY",
            "price":           "0",
            "squareoff":       "0",
            "stoploss":        "0",
            "quantity":        str(qty),
        }
        for attempt in range(3):
            try:
                oid = self._api.placeOrder(params)
                logger.info("Options BUY MARKET placed: %s → %s", symbol, oid)
                return str(oid)
            except Exception as exc:
                wait = 2 ** attempt
                logger.warning("Options placeOrder BUY attempt %d: %s — retry in %ds",
                               attempt + 1, exc, wait)
                time.sleep(wait)

        logger.error("Options BUY MARKET failed after 3 attempts: %s", symbol)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # LTP helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _get_index_ltp(self) -> Optional[float]:
        """Fetch real-time index LTP for strike selection."""
        try:
            data = self._api.ltpData(
                self._index_cfg["exchange"], "", self._index_cfg["token"]
            )
            if data and data.get("data"):
                return float(data["data"].get("ltp", 0) or 0)
        except Exception as exc:
            logger.debug("Options index ltpData failed: %s", exc)
        return None

    def _get_option_ltp(self, token: str) -> Optional[float]:
        """Fetch real-time option premium LTP."""
        try:
            data = self._api.ltpData("NFO", "", token)
            if data and data.get("data"):
                return float(data["data"].get("ltp", 0) or 0)
        except Exception as exc:
            logger.debug("Options ltpData failed: %s", exc)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Candle helpers (independent implementation — mirrors live_runner)
    # ─────────────────────────────────────────────────────────────────────────

    def _sleep_to_next_candle(self):
        now = datetime.now(IST)
        rem = now.minute % 5
        wait_min = (5 - rem) if rem != 0 else 5
        target = (now.replace(second=0, microsecond=0)
                  + timedelta(minutes=wait_min)
                  + timedelta(seconds=self._buf_secs))
        secs = (target - datetime.now(IST)).total_seconds()
        if secs > 0:
            deadline = time.time() + secs
            while time.time() < deadline and not self._stop_ev.is_set():
                time.sleep(min(1.0, deadline - time.time()))

    def _fetch_latest_candle(self) -> Optional[pd.Series]:
        now_ist = datetime.now(IST)
        from_dt = now_ist - timedelta(minutes=20)
        try:
            df = self._fetcher.get_historical_data(
                self._index_cfg["token"], self._index_cfg["exchange"],
                from_dt, now_ist, self._timeframe,
            )
            if df.empty:
                return None
            ts_ist = df["timestamp"].dt.tz_convert(IST)
            mkt = (
                ((ts_ist.dt.hour > 9) | ((ts_ist.dt.hour == 9) & (ts_ist.dt.minute >= 15))) &
                (ts_ist.dt.hour < 16)
            )
            df = df[mkt]
            return df.iloc[-1] if not df.empty else None
        except Exception as exc:
            logger.error("Options candle fetch failed: %s", exc)
            return None

    def _append_and_recompute(self, df: pd.DataFrame,
                               new_row: pd.Series) -> pd.DataFrame:
        new_df   = pd.DataFrame([new_row])
        combined = pd.concat([df, new_df], ignore_index=True)
        combined = (combined.drop_duplicates(subset="timestamp")
                   .sort_values("timestamp")
                   .reset_index(drop=True))
        return compute_indicators(combined, self._params)

    # ─────────────────────────────────────────────────────────────────────────
    # Telegram commands
    # ─────────────────────────────────────────────────────────────────────────

    def _on_command(self, text: str) -> Optional[str]:
        cmd = text.strip().lower().split()[0]

        if cmd == "/optstatus":
            with self._lock:
                if not self._in_trade:
                    return "Options: no open trade."
                ltp  = self._get_option_ltp(self._opt_token) or 0.0
                pnl  = (self._entry_prem - ltp) * self._trade_qty
                sl   = self._entry_prem * self._sl_mult
            return (
                f"Options open: `{self._opt_symbol}`\n"
                f"Sold @ ₹{self._entry_prem:.2f} | Now: ₹{ltp:.2f}\n"
                f"SL level: ₹{sl:.2f}\n"
                f"Unrealised P&L: ₹{pnl:+.0f}"
            )

        if cmd == "/optpnl":
            if not self._trades:
                return "Options: no completed trades today."
            total = sum(t["pnl_inr"] for t in self._trades)
            lines = [f"Options P&L today: ₹{total:+.0f}"]
            for t in self._trades:
                icon = "✅" if t["pnl_inr"] >= 0 else "❌"
                lines.append(
                    f"{icon} {t['opt_type']} "
                    f"@{t['entry_prem']:.0f}→{t['exit_prem']:.0f}  "
                    f"₹{t['pnl_inr']:+.0f}"
                )
            return "\n".join(lines)

        if cmd == "/optstop":
            self._stop_ev.set()
            with self._lock:
                still_open = self._in_trade
            if still_open:
                threading.Thread(
                    target=lambda: self._exit_trade(
                        "TIME_EXIT",
                        self._get_option_ltp(self._opt_token) or self._entry_prem
                    ),
                    daemon=True,
                ).start()
            return "🛑 Options engine stopping — closing position if open."

        return None   # not an options command — don't reply
