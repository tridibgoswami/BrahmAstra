"""
PriceMonitor — background thread that polls index LTP every N seconds
while in a live trade and fires a callback when the trail stop is hit.

Trail stop level and active flag are updated from the main thread via
update_trail() after each candle close.
"""

from __future__ import annotations
import threading
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class PriceMonitor:
    def __init__(self, ltp_getter: Callable[[], Optional[float]],
                 poll_interval: float = 2.0):
        """
        ltp_getter     : zero-arg callable that returns current index LTP
        poll_interval  : seconds between LTP polls
        """
        self._get_ltp      = ltp_getter
        self._poll_interval = poll_interval

        self._thread: Optional[threading.Thread] = None
        self._stop_ev  = threading.Event()
        self._lock     = threading.Lock()

        # Trade parameters — written by main thread, read by monitor thread
        self._direction:     Optional[str]   = None
        self._trail_level:   Optional[float] = None
        self._trail_active:  bool            = False
        self._trail_cb:      Optional[Callable[[float], None]] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Control
    # ─────────────────────────────────────────────────────────────────────────

    def start(self, direction: str,
              trail_callback: Callable[[float], None]):
        """
        Start monitoring. direction = 'BUY' or 'SELL'.
        trail_callback(ltp) is called (once) when the trail stop is hit.
        """
        with self._lock:
            self._direction   = direction
            self._trail_cb    = trail_callback
            self._trail_level = None
            self._trail_active = False

        self._stop_ev.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="price-monitor"
        )
        self._thread.start()
        logger.info("PriceMonitor started (%s)", direction)

    def stop(self):
        """Signal the monitor thread to exit and wait briefly for it."""
        self._stop_ev.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("PriceMonitor stopped")

    def update_trail(self, trail_level: Optional[float], trail_active: bool):
        """
        Called by the main thread after each candle close to push the
        latest trail stop level computed by the signal engine.
        """
        with self._lock:
            self._trail_level  = trail_level
            self._trail_active = trail_active

    # ─────────────────────────────────────────────────────────────────────────
    # Monitor loop (runs in daemon thread)
    # ─────────────────────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop_ev.is_set():
            ltp = self._get_ltp()

            if ltp and ltp > 0:
                with self._lock:
                    active    = self._trail_active
                    level     = self._trail_level
                    direction = self._direction
                    cb        = self._trail_cb

                if active and level is not None and cb is not None:
                    hit = (direction == "BUY"  and ltp <= level) or \
                          (direction == "SELL" and ltp >= level)
                    if hit:
                        logger.info(
                            "Trail stop hit intrabar: LTP=%.2f level=%.2f dir=%s",
                            ltp, level, direction,
                        )
                        # Fire callback exactly once then exit
                        self._stop_ev.set()
                        cb(ltp)
                        return

            self._stop_ev.wait(self._poll_interval)
