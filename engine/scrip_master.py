"""
ScripMaster — downloads and caches AngelOne's NFO instrument master.
Public endpoint, no auth required. Refreshed once per trading day.

Provides:
  find_option(symbol, expiry, strike, option_type) → instrument row or None
  nearest_expiry(symbol)                           → nearest Thursday date
  strike_interval(symbol)                          → 100 for BankNifty, 50 for Nifty
  lot_size(symbol, expiry)                         → lot size from master
"""

from __future__ import annotations
import json
import logging
import os
import requests
from datetime import date, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MASTER_URL = (
    "https://margincalculator.angelbroking.com"
    "/OpenAPI_File/files/OpenAPIScripMaster.json"
)
_CACHE_FILE = ".scrip_master_cache.json"

_STRIKE_INTERVALS: Dict[str, int] = {
    "BANKNIFTY":   100,
    "NIFTY":        50,
    "FINNIFTY":     50,
    "MIDCPNIFTY":   25,
}
_DEFAULT_LOT_SIZES: Dict[str, int] = {
    "BANKNIFTY": 15,
    "NIFTY":     75,
    "FINNIFTY":  40,
}


def nearest_weekly_expiry(from_date: date | None = None) -> date:
    """
    BankNifty / Nifty / FinNifty weekly options expire every Thursday.
    Returns the nearest upcoming Thursday (including today if it's Thursday).
    """
    d = from_date or date.today()
    days_ahead = (3 - d.weekday()) % 7   # 3 = Thursday
    return d + timedelta(days=days_ahead)


class ScripMaster:
    def __init__(self, cache_path: str = _CACHE_FILE):
        self._cache = cache_path
        self._rows: list = []

    # ─────────────────────────────────────────────────────────────────────────
    # Load
    # ─────────────────────────────────────────────────────────────────────────

    def load(self, force: bool = False):
        """Load from local cache (if today's) or download fresh."""
        today = date.today()

        if not force and os.path.exists(self._cache):
            mdate = date.fromtimestamp(os.path.getmtime(self._cache))
            if mdate == today:
                with open(self._cache) as f:
                    self._rows = json.load(f)
                logger.info("Scrip master loaded from cache (%d instruments)",
                            len(self._rows))
                return

        logger.info("Downloading scrip master from AngelOne (this is slow — ~60 MB)…")
        resp = requests.get(_MASTER_URL, timeout=120)
        resp.raise_for_status()
        self._rows = resp.json()
        with open(self._cache, "w") as f:
            json.dump(self._rows, f)
        logger.info("Scrip master downloaded (%d instruments)", len(self._rows))

    # ─────────────────────────────────────────────────────────────────────────
    # Lookups
    # ─────────────────────────────────────────────────────────────────────────

    def find_option(self, symbol: str, expiry: date,
                    strike: float, option_type: str) -> Optional[Dict[str, Any]]:
        """
        Find an NFO option instrument row.

        symbol      : 'BANKNIFTY', 'NIFTY', etc.
        expiry      : date object (nearest Thursday)
        strike      : float e.g. 53400.0
        option_type : 'CE' or 'PE'

        Returns the scrip-master row dict containing token, symbol, lotsize, etc.
        """
        # AngelOne expiry field format: "05JUN2025"
        exp_key = expiry.strftime("%d%b%Y").upper()
        ot      = option_type.upper()

        for row in self._rows:
            if row.get("exch_seg")      != "NFO":      continue
            if row.get("instrumenttype")!= "OPTIDX":   continue
            if row.get("name", "").upper() != symbol.upper(): continue
            if row.get("expiry", "").upper() != exp_key: continue
            row_strike = float(row.get("strike", "0") or "0")
            if abs(row_strike - strike) > 0.01:         continue
            if ot not in row.get("symbol", "").upper(): continue
            return row

        return None

    def nearest_expiry(self, symbol: str) -> date:
        return nearest_weekly_expiry()

    def strike_interval(self, symbol: str) -> int:
        return _STRIKE_INTERVALS.get(symbol.upper(), 100)

    def lot_size(self, symbol: str, expiry: date) -> int:
        """Look up lot size from master; fall back to known default."""
        exp_key = expiry.strftime("%d%b%Y").upper()
        for row in self._rows:
            if (row.get("exch_seg")       == "NFO" and
                    row.get("instrumenttype") == "OPTIDX" and
                    row.get("name", "").upper() == symbol.upper() and
                    row.get("expiry", "").upper() == exp_key):
                try:
                    return int(row["lotsize"])
                except Exception:
                    pass
        return _DEFAULT_LOT_SIZES.get(symbol.upper(), 15)
