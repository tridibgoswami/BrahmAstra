"""
OrderManager — wraps AngelOne SmartAPI for all order operations.

Handles:
  - Limit and market order placement
  - Order status polling / fill detection
  - Order cancellation (cancel-first pattern for trail stop vs target race)
  - Net position query
  - LTP fetch (index token, used for trail stop monitoring)
"""

from __future__ import annotations
import time
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

_OPPOSITE = {"BUY": "SELL", "SELL": "BUY"}


class OrderManager:
    def __init__(self, smart_api,
                 futures_cfg: dict,
                 index_cfg: dict,
                 product_type: str = "MIS"):
        """
        futures_cfg : dict with keys symbol, token, exchange, lot_size
        index_cfg   : dict with keys token, exchange  (for LTP monitoring)
        product_type: 'MIS' (intraday) or 'NRML' (carry-forward)
        """
        self._api     = smart_api
        self._sym     = futures_cfg["symbol"]
        self._token   = str(futures_cfg["token"])
        self._exch    = futures_cfg["exchange"]
        self._lot_sz  = int(futures_cfg["lot_size"])
        self._product = product_type
        self._variety = "NORMAL"

        self._idx_token = str(index_cfg["token"])
        self._idx_exch  = index_cfg["exchange"]

    # ─────────────────────────────────────────────────────────────────────────
    # Order placement
    # ─────────────────────────────────────────────────────────────────────────

    def place_limit(self, direction: str, price: float, qty: int) -> Optional[str]:
        """Place a DAY limit order. Returns order_id or None on failure."""
        params = {
            "variety":         self._variety,
            "tradingsymbol":   self._sym,
            "symboltoken":     self._token,
            "transactiontype": direction,
            "exchange":        self._exch,
            "ordertype":       "LIMIT",
            "producttype":     self._product,
            "duration":        "DAY",
            "price":           f"{price:.2f}",
            "squareoff":       "0",
            "stoploss":        "0",
            "quantity":        str(qty),
        }
        return self._place(params, f"LIMIT {direction} @{price:.2f}")

    def place_market(self, direction: str, qty: int) -> Optional[str]:
        """Place a market order. Returns order_id or None on failure."""
        params = {
            "variety":         self._variety,
            "tradingsymbol":   self._sym,
            "symboltoken":     self._token,
            "transactiontype": direction,
            "exchange":        self._exch,
            "ordertype":       "MARKET",
            "producttype":     self._product,
            "duration":        "DAY",
            "price":           "0",
            "squareoff":       "0",
            "stoploss":        "0",
            "quantity":        str(qty),
        }
        return self._place(params, f"MARKET {direction}")

    def _place(self, params: dict, label: str) -> Optional[str]:
        for attempt in range(3):
            try:
                order_id = self._api.placeOrder(params)
                logger.info("Order placed [%s] → id=%s", label, order_id)
                return str(order_id)
            except Exception as exc:
                wait = 2 ** attempt
                logger.warning("placeOrder attempt %d failed: %s — retry in %ds",
                               attempt + 1, exc, wait)
                time.sleep(wait)
        logger.error("placeOrder failed after 3 attempts for %s", label)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Order management
    # ─────────────────────────────────────────────────────────────────────────

    def cancel(self, order_id: str) -> bool:
        """
        Cancel an order. Returns True if cancelled, False if it was already
        terminal (filled/rejected) — callers use this to detect the
        'target already filled' case.
        """
        try:
            self._api.cancelOrder(variety=self._variety, orderid=order_id)
            logger.info("Cancelled order %s", order_id)
            return True
        except Exception as exc:
            status = self.get_status(order_id)
            if status in ("COMPLETE", "FILLED"):
                logger.info("Cancel order %s skipped — already filled", order_id)
                return False
            logger.warning("cancelOrder %s failed: %s (status=%s)", order_id, exc, status)
            return False

    def get_status(self, order_id: str) -> str:
        """Returns order status string, e.g. 'COMPLETE', 'OPEN', 'CANCELLED'."""
        try:
            book = self._api.orderBook()
            if book and book.get("data"):
                for o in book["data"]:
                    if str(o.get("orderid")) == str(order_id):
                        return str(o.get("status", "UNKNOWN")).upper()
        except Exception as exc:
            logger.debug("orderBook failed: %s", exc)
        return "UNKNOWN"

    def wait_for_fill(self, order_id: str,
                      timeout: int = 60,
                      poll_secs: float = 2.0) -> Dict:
        """
        Polls until the order fills or timeout.
        Returns {'filled': bool, 'price': float}.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_status(order_id)
            if status in ("COMPLETE", "FILLED"):
                price = self._fill_price(order_id)
                logger.info("Order %s filled @ %.2f", order_id, price)
                return {"filled": True, "price": price}
            if status in ("CANCELLED", "REJECTED"):
                logger.warning("Order %s ended with status %s", order_id, status)
                return {"filled": False, "price": 0.0}
            time.sleep(poll_secs)

        logger.warning("Order %s timed out after %ds", order_id, timeout)
        return {"filled": False, "price": 0.0}

    def _fill_price(self, order_id: str) -> float:
        try:
            book = self._api.orderBook()
            if book and book.get("data"):
                for o in book["data"]:
                    if str(o.get("orderid")) == str(order_id):
                        return float(o.get("averageprice", 0) or 0)
        except Exception:
            pass
        return 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # Position / LTP
    # ─────────────────────────────────────────────────────────────────────────

    def get_net_qty(self) -> int:
        """
        Returns net position qty for our instrument.
        Positive = long, negative = short, 0 = flat.
        """
        try:
            pos = self._api.position()
            if pos and pos.get("data"):
                for p in pos["data"]:
                    if (p.get("tradingsymbol") == self._sym and
                            p.get("producttype") == self._product):
                        return int(p.get("netqty", 0))
        except Exception as exc:
            logger.warning("position() failed: %s", exc)
        return 0

    def get_ltp(self) -> Optional[float]:
        """Fetch LTP of the INDEX (for trail stop monitoring)."""
        try:
            data = self._api.ltpData(self._idx_exch, "", self._idx_token)
            if data and data.get("data"):
                return float(data["data"].get("ltp", 0) or 0)
        except Exception as exc:
            logger.debug("ltpData failed: %s", exc)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def qty_for_lots(self, lots: int) -> int:
        return lots * self._lot_sz


# ─────────────────────────────────────────────────────────────────────────────
# Paper-mode drop-in replacement — same interface, zero real orders
# ─────────────────────────────────────────────────────────────────────────────

class PaperOrderManager:
    """
    Simulates order execution without touching the broker API.

    - place_limit / place_market: log the simulated order, return a fake ID.
    - wait_for_fill: returns instantly with the price that was passed in.
    - cancel: always returns True (simulated cancel succeeds immediately).
    - get_ltp: fetches real index LTP from AngelOne (needed for trail stop).
    - get_net_qty: always 0 (no real position held).
    """

    def __init__(self, smart_api,
                 futures_cfg: dict,
                 index_cfg: dict,
                 product_type: str = "MIS"):
        self._api      = smart_api
        self._lot_sz   = int(futures_cfg["lot_size"])
        self._idx_token = str(index_cfg["token"])
        self._idx_exch  = index_cfg["exchange"]
        self._counter  = 0
        self._prices: Dict[str, float] = {}

    def _next_id(self) -> str:
        self._counter += 1
        return f"PAPER-{self._counter:04d}"

    def place_limit(self, direction: str, price: float, qty: int) -> Optional[str]:
        oid = self._next_id()
        self._prices[oid] = price
        logger.info("[PAPER] %s LIMIT @%.2f  qty=%d  → %s", direction, price, qty, oid)
        return oid

    def place_market(self, direction: str, qty: int) -> Optional[str]:
        ltp = self.get_ltp() or 0.0
        oid = self._next_id()
        self._prices[oid] = ltp
        logger.info("[PAPER] %s MARKET @~%.2f  qty=%d  → %s", direction, ltp, qty, oid)
        return oid

    def cancel(self, order_id: str) -> bool:
        logger.info("[PAPER] cancel %s → True", order_id)
        return True

    def wait_for_fill(self, order_id: str,
                      timeout: int = 60,
                      poll_secs: float = 2.0) -> Dict:
        price = self._prices.get(order_id, 0.0)
        logger.info("[PAPER] fill %s @ %.2f (instant)", order_id, price)
        return {"filled": True, "price": price}

    def get_net_qty(self) -> int:
        return 0

    def get_ltp(self) -> Optional[float]:
        try:
            data = self._api.ltpData(self._idx_exch, "", self._idx_token)
            if data and data.get("data"):
                return float(data["data"].get("ltp", 0) or 0)
        except Exception as exc:
            logger.debug("ltpData failed: %s", exc)
        return None

    def qty_for_lots(self, lots: int) -> int:
        return lots * self._lot_sz
