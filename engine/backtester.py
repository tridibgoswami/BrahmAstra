"""
Backtester — Phase 1 validation tool.

Fetches historical data, computes indicators, runs the signal engine,
and prints a timestamped trade log for comparison against TradingView.

Usage:
  python main.py backtest --from 2026-04-01 --to 2026-04-25
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import List, Dict, Any

import pytz
import pandas as pd

from engine.presets import resolve_preset
from engine.indicators import compute_indicators
from engine.signal_engine import SignalEngine
from engine.data_fetcher import AngelOneDataFetcher

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def run_backtest(config: dict, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    """
    Main backtest runner.

    config  : parsed config.yaml dict
    from_date: 'YYYY-MM-DD'
    to_date  : 'YYYY-MM-DD'

    Returns list of all signal events.
    """
    eng_cfg    = config["engine"]
    broker_cfg = config["broker"]["angel_one"]
    sym_cfg    = config["symbols"][eng_cfg["symbol"]]

    symbol_token = sym_cfg["token"]
    exchange     = sym_cfg["exchange"]
    timeframe    = eng_cfg["timeframe"]
    symbol       = eng_cfg["symbol"]

    # ── Resolve preset ────────────────────────────────────────────────────────
    params, preset_key = resolve_preset(symbol, timeframe)
    print(f"\n{'='*60}")
    print(f"  BrahmAstra Backtester")
    print(f"  Symbol: {symbol} | Timeframe: {timeframe}m | Preset: {preset_key}")
    print(f"  Period: {from_date} → {to_date}")
    print(f"{'='*60}\n")

    # ── Fetch data ────────────────────────────────────────────────────────────
    fetcher = AngelOneDataFetcher(broker_cfg)
    fetcher.connect()

    from_dt = IST.localize(datetime.strptime(from_date, "%Y-%m-%d").replace(hour=9, minute=15))
    to_dt   = IST.localize(datetime.strptime(to_date,   "%Y-%m-%d").replace(hour=15, minute=30))

    print(f"Fetching candles...")
    df = fetcher.get_historical_data(symbol_token, exchange, from_dt, to_dt, timeframe)

    if df.empty:
        print("No data returned. Check credentials and symbol token.")
        return []

    # Filter to market hours 9:15–15:30 IST only (remove pre/post-market noise)
    ts_ist = df["timestamp"].dt.tz_convert(IST)
    market_mask = (
        ((ts_ist.dt.hour > 9) | ((ts_ist.dt.hour == 9) & (ts_ist.dt.minute >= 15))) &
        ((ts_ist.dt.hour < 15) | ((ts_ist.dt.hour == 15) & (ts_ist.dt.minute <= 30)))
    )
    df = df[market_mask].reset_index(drop=True)

    print(f"Loaded {len(df)} candles ({from_date} → {to_date})\n")

    # ── Compute indicators ────────────────────────────────────────────────────
    df = compute_indicators(df, params)

    # ── Run signal engine ─────────────────────────────────────────────────────
    engine = SignalEngine(params)
    events = engine.run(df)

    # ── Print trade log ───────────────────────────────────────────────────────
    _print_trade_log(events, symbol, timeframe, preset_key)

    return events


def _print_trade_log(events: List[Dict], symbol: str, tf: str, preset_key: str):
    if not events:
        print("No signals generated in this period.")
        return

    # Pair entries with exits
    trades = []
    open_trade = None

    for ev in events:
        if ev["type"] in ("BUY", "SELL"):
            open_trade = ev
        elif ev["type"] in ("EXIT_BUY", "EXIT_SELL") and open_trade:
            trades.append({
                "Date": _fmt_ts(open_trade["timestamp"]),
                "Dir":  open_trade["type"],
                "Entry @": open_trade["entry_price"],
                "Target": open_trade["target_price"],
                "Exit @": ev["exit_price"],
                "Exit Reason": ev["exit_type"],
                "P&L pts": ev["pnl_pts"],
                "Score": open_trade.get("quality_score", "?"),
                "Exit Time": _fmt_ts(ev["timestamp"]),
            })
            open_trade = None

    if open_trade:
        trades.append({
            "Date": _fmt_ts(open_trade["timestamp"]),
            "Dir":  open_trade["type"],
            "Entry @": open_trade["entry_price"],
            "Target": open_trade["target_price"],
            "Exit @": "OPEN",
            "Exit Reason": "—",
            "P&L pts": "—",
            "Score": open_trade.get("quality_score", "?"),
            "Exit Time": "—",
        })

    # Group by date
    from collections import defaultdict
    by_day: dict = defaultdict(list)
    for t in trades:
        by_day[t["Date"].split()[0]].append(t)

    total_pnl = 0.0
    wins = losses = targets = trails = 0

    for day, day_trades in sorted(by_day.items()):
        print(f"  📅  {day}")
        print(f"  {'#':<4} {'Dir':<5} {'Entry':>10} {'Target':>10} "
              f"{'Exit':>10} {'Reason':<12} {'P&L':>8} {'Score':<6}")
        print(f"  {'-'*72}")
        day_pnl = 0.0
        for idx, t in enumerate(day_trades, 1):
            pnl_str = f"{t['P&L pts']:+.2f}" if isinstance(t["P&L pts"], float) else str(t["P&L pts"])
            print(f"  {idx:<4} {t['Dir']:<5} {t['Entry @']:>10.2f} "
                  f"{t['Target']:>10.2f} "
                  f"{str(t['Exit @']):>10} {t['Exit Reason']:<12} "
                  f"{pnl_str:>8} {t['Score']:<6}")
            if isinstance(t["P&L pts"], float):
                day_pnl   += t["P&L pts"]
                total_pnl += t["P&L pts"]
                if t["P&L pts"] > 0:
                    wins += 1
                else:
                    losses += 1
                if t["Exit Reason"] == "TARGET":
                    targets += 1
                elif t["Exit Reason"] == "TRAIL_STOP":
                    trails += 1
        sign = "+" if day_pnl >= 0 else ""
        print(f"  {'Day P&L':>62} {sign}{day_pnl:.2f}")
        print()

    total_trades = wins + losses
    win_rate = wins / total_trades * 100 if total_trades else 0

    print(f"{'='*60}")
    print(f"  SUMMARY — {symbol} {tf}m ({preset_key})")
    print(f"{'='*60}")
    print(f"  Total trades  : {total_trades}")
    print(f"  Target hits   : {targets}")
    print(f"  Trail stops   : {trails}")
    print(f"  Wins          : {wins}")
    print(f"  Losses        : {losses}")
    print(f"  Win rate      : {win_rate:.1f}%")
    sign = "+" if total_pnl >= 0 else ""
    print(f"  Net P&L (pts) : {sign}{total_pnl:.2f}")
    print(f"{'='*60}\n")


def _fmt_ts(ts) -> str:
    if isinstance(ts, pd.Timestamp):
        ist_ts = ts.tz_convert(IST) if ts.tzinfo else ts
        return ist_ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)
