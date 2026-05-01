"""
DailyReporter — records trades and generates P&L summaries.
"""

from __future__ import annotations
import pytz
from datetime import datetime
from typing import List, Dict, Optional

IST = pytz.timezone("Asia/Kolkata")


class DailyReporter:
    def __init__(self):
        self._trades: List[Dict] = []
        self._open: Optional[Dict] = None

    # ─────────────────────────────────────────────────────────────────────────

    def on_entry(self, direction: str, entry_price: float, target_price: float,
                 qty: int, timestamp: datetime):
        self._open = {
            "direction":   direction,
            "entry_price": entry_price,
            "target_price": target_price,
            "qty":         qty,
            "entry_time":  timestamp,
        }

    def on_exit(self, exit_price: float, exit_type: str, timestamp: datetime):
        if not self._open:
            return
        t = self._open
        pnl_pts = (exit_price - t["entry_price"] if t["direction"] == "BUY"
                   else t["entry_price"] - exit_price)
        self._trades.append({
            **t,
            "exit_price": exit_price,
            "exit_type":  exit_type,
            "exit_time":  timestamp,
            "pnl_pts":    pnl_pts,
            "pnl_amt":    pnl_pts * t["qty"],
        })
        self._open = None

    # ─────────────────────────────────────────────────────────────────────────

    def unrealized(self, ltp: float) -> str:
        if not self._open:
            return "No open trade."
        t = self._open
        pnl = (ltp - t["entry_price"] if t["direction"] == "BUY"
               else t["entry_price"] - ltp)
        trail_note = ""
        return (
            f"Open {t['direction']} @{t['entry_price']:.0f} | "
            f"Target {t['target_price']:.0f} | LTP {ltp:.0f} | "
            f"P&L {pnl:+.0f} pts{trail_note}"
        )

    def summary(self) -> str:
        if not self._trades:
            return "No completed trades today."

        wins   = sum(1 for t in self._trades if t["pnl_pts"] > 0)
        losses = len(self._trades) - wins
        total_pts = sum(t["pnl_pts"] for t in self._trades)
        total_amt = sum(t["pnl_amt"] for t in self._trades)

        lines = ["📊 *Daily P&L Report*", ""]
        for idx, t in enumerate(self._trades, 1):
            icon = "✅" if t["pnl_pts"] > 0 else "❌"
            ts_e = t["entry_time"].strftime("%H:%M")
            ts_x = t["exit_time"].strftime("%H:%M")
            lines.append(
                f"{icon} #{idx} {t['direction']} {ts_e}→{ts_x}  "
                f"@{t['entry_price']:.0f}→@{t['exit_price']:.0f}  "
                f"({t['pnl_pts']:+.0f} pts)"
            )

        sign = "+" if total_pts >= 0 else ""
        lines += [
            "",
            f"Trades: {len(self._trades)}  W:{wins}  L:{losses}",
            f"Net P&L: {sign}{total_pts:.1f} pts  |  ₹{total_amt:+,.0f}",
        ]
        return "\n".join(lines)

    def has_open_trade(self) -> bool:
        return self._open is not None

    def reset(self):
        self._trades.clear()
        self._open = None
