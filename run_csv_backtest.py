"""
Offline backtest using a local CSV file instead of AngelOne API.
Loads all rows as warmup, then prints signals only for a specified date.

Usage:
  python run_csv_backtest.py --csv <path> --date 2026-04-28
"""

import sys
import argparse
import pandas as pd
import pytz

sys.path.insert(0, "/home/user/BrahmAstra")
from engine.presets import resolve_preset
from engine.indicators import compute_indicators
from engine.signal_engine import SignalEngine

IST = pytz.timezone("Asia/Kolkata")


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["time"])
    df = df.rename(columns={"time": "timestamp"})
    # Ensure UTC-aware timestamps
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def run(csv_path: str, target_date: str, symbol: str = "BANKNIFTY",
        timeframe: str = "5", target_pct: float = 0.50,
        trail_trigger: float = 0.60, verbose: bool = False):

    params, preset_key = resolve_preset(symbol, timeframe)
    # Override to user-specified settings
    params["bt_target_pct"]    = target_pct
    params["trail_trigger_pct"] = trail_trigger

    print(f"\n{'='*64}")
    print(f"  BrahmAstra CSV Backtest")
    print(f"  Symbol: {symbol} | TF: {timeframe}m | Preset: {preset_key}")
    print(f"  Target: {target_pct}% | Trail trigger: {trail_trigger*100:.0f}%")
    print(f"  Signals shown for: {target_date}")
    print(f"{'='*64}\n")

    df = load_csv(csv_path)
    print(f"Total rows loaded: {len(df)} (all dates used as warmup)\n")

    # Filter to market hours 9:15–15:30 IST
    ts_ist = df["timestamp"].dt.tz_convert(IST)
    mkt = (
        ((ts_ist.dt.hour > 9) | ((ts_ist.dt.hour == 9) & (ts_ist.dt.minute >= 15))) &
        ((ts_ist.dt.hour < 15) | ((ts_ist.dt.hour == 15) & (ts_ist.dt.minute <= 30)))
    )
    df = df[mkt].reset_index(drop=True)
    print(f"Market-hours rows: {len(df)}\n")

    df = compute_indicators(df, params)

    # Show ATR on target date first bar for reference
    ts_ist2 = df["timestamp"].dt.tz_convert(IST)
    first_target = df[ts_ist2.dt.date.astype(str) == target_date].index
    if len(first_target):
        row0 = df.iloc[first_target[0]]
        print(f"ATR at first bar of {target_date} ({row0['timestamp'].tz_convert(IST).strftime('%H:%M')}): "
              f"{row0['atr_filter']:.2f} pts")
        print(f"  (Trail width = 0.1 × {row0['atr_filter']:.2f} = "
              f"{0.1 * row0['atr_filter']:.2f} pts)\n")

    engine = SignalEngine(params)
    all_events = engine.run(df)

    # Filter events to target date only
    target_events = []
    for ev in all_events:
        ts = ev["timestamp"]
        ts_ist_ev = ts.tz_convert(IST) if ts.tzinfo else ts
        if str(ts_ist_ev.date()) == target_date:
            target_events.append(ev)

    if not target_events:
        print(f"No signals on {target_date}.")
        return

    # Pair entries/exits and print
    print(f"  {'Time':<8} {'Type':<10} {'Entry':>10} {'Target':>10} "
          f"{'Exit':>10} {'Reason':<14} {'P&L':>8} {'Score'}")
    print(f"  {'-'*80}")

    open_trade = None
    total_pnl = 0.0
    wins = losses = targets = trails = 0

    pairs = []
    for ev in target_events:
        if ev["type"] in ("BUY", "SELL"):
            open_trade = ev
        elif ev["type"] in ("EXIT_BUY", "EXIT_SELL") and open_trade:
            pairs.append((open_trade, ev))
            open_trade = None

    if open_trade:
        pairs.append((open_trade, None))

    for entry, exit_ev in pairs:
        ts_e = entry["timestamp"].tz_convert(IST).strftime("%H:%M")
        if exit_ev:
            ts_x = exit_ev["timestamp"].tz_convert(IST).strftime("%H:%M")
            pnl = exit_ev["pnl_pts"]
            pnl_str = f"{pnl:+.2f}"
            if isinstance(pnl, float):
                total_pnl += pnl
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                if exit_ev["exit_type"] == "TARGET":
                    targets += 1
                elif exit_ev["exit_type"] == "TRAIL_STOP":
                    trails += 1
            print(f"  {ts_e:<8} {entry['type']:<10} {entry['entry_price']:>10.2f} "
                  f"{entry['target_price']:>10.2f} "
                  f"{exit_ev['exit_price']:>10.2f} {exit_ev['exit_type']:<14} "
                  f"{pnl_str:>8} {entry.get('quality_score','?')}")
        else:
            print(f"  {ts_e:<8} {entry['type']:<10} {entry['entry_price']:>10.2f} "
                  f"{entry['target_price']:>10.2f} "
                  f"{'OPEN':>10} {'—':<14} {'—':>8} {entry.get('quality_score','?')}")

    print(f"\n{'='*64}")
    total = wins + losses
    wr = wins / total * 100 if total else 0
    sign = "+" if total_pnl >= 0 else ""
    print(f"  Trades: {total} | Wins: {wins} | Losses: {losses} | "
          f"WR: {wr:.1f}%")
    print(f"  Target hits: {targets} | Trail stops: {trails}")
    print(f"  Net P&L: {sign}{total_pnl:.2f} pts")
    print(f"{'='*64}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv",    required=True)
    ap.add_argument("--date",   required=True, help="YYYY-MM-DD")
    ap.add_argument("--symbol", default="BANKNIFTY")
    ap.add_argument("--tf",     default="5")
    ap.add_argument("--target", type=float, default=0.50)
    ap.add_argument("--trail",  type=float, default=0.60)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    run(args.csv, args.date, args.symbol, args.tf, args.target, args.trail, args.verbose)
