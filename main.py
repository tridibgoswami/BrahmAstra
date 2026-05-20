#!/usr/bin/env python3
"""
BrahmAstra — TradingView-independent signal engine.

Commands:
  python main.py backtest --from 2026-04-01 --to 2026-04-25
  python main.py backtest --from 2026-04-21 --to 2026-04-25  (single week)
  python main.py live
  python main.py live --config config.yaml --verbose
"""

import argparse
import logging
import sys

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
    bt.add_argument("--config",  default="config.yaml", help="Config file path")
    bt.add_argument("--verbose", action="store_true",   help="Debug logging")

    # ── live ──────────────────────────────────────────────────────────────────
    lv = sub.add_parser("live", help="Run live trading engine")
    lv.add_argument("--config",  default="config.yaml", help="Config file path")
    lv.add_argument("--verbose", action="store_true",   help="Debug logging")

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
    # Suppress noisy HTTP-level debug logs from third-party libraries
    for _noisy in ("urllib3", "urllib3.connectionpool", "requests", "smartapi"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    config = load_config(args.config)

    if args.command == "backtest":
        from engine.backtester import run_backtest
        run_backtest(config, args.from_date, args.to_date)

    elif args.command == "live":
        from engine.live_runner import LiveRunner
        runner = LiveRunner(config)
        runner.start()


if __name__ == "__main__":
    main()
