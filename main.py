#!/usr/bin/env python3
"""
BrahmAstra — TradingView-independent signal engine.

Commands:
  python main.py backtest --from 2026-04-01 --to 2026-04-25
  python main.py backtest --from 2026-04-21 --to 2026-04-25  (single week)
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(
        description="BrahmAstra Signal Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # ── backtest ──────────────────────────────────────────────────────────────
    bt = sub.add_parser("backtest", help="Run signal engine on historical data")
    bt.add_argument("--from", dest="from_date", required=True,
                    metavar="YYYY-MM-DD", help="Start date (IST)")
    bt.add_argument("--to",   dest="to_date",   required=True,
                    metavar="YYYY-MM-DD", help="End date (IST)")
    bt.add_argument("--config", default="config.yaml", help="Config file path")
    bt.add_argument("--verbose", action="store_true", help="Debug logging")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(args.config)

    if args.command == "backtest":
        from engine.backtester import run_backtest
        run_backtest(config, args.from_date, args.to_date)


if __name__ == "__main__":
    main()
