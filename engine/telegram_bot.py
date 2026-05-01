"""
TelegramBot — sends trade alerts and handles phone commands.

Commands supported:
  /status  — current open trade + unrealized P&L
  /pnl     — today's realized P&L summary
  /stop    — gracefully stop the engine (force-exits if in trade)
"""

from __future__ import annotations
import threading
import logging
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, bot_token: str, chat_id: str):
        self._token   = bot_token
        self._chat_id = str(chat_id)
        self._base    = f"https://api.telegram.org/bot{bot_token}"
        self._last_id = 0
        self._stop_ev = threading.Event()
        self._handler: Optional[Callable[[str], Optional[str]]] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def send(self, text: str):
        """Send a Markdown message to the configured chat."""
        try:
            requests.post(
                f"{self._base}/sendMessage",
                json={"chat_id": self._chat_id, "text": text,
                      "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    def start_polling(self, command_handler: Callable[[str], Optional[str]]):
        """Start background thread that receives /commands and replies."""
        self._handler = command_handler
        t = threading.Thread(target=self._poll_loop, daemon=True, name="tg-poll")
        t.start()
        logger.info("Telegram command polling started")

    def stop(self):
        self._stop_ev.set()

    # ─────────────────────────────────────────────────────────────────────────
    # Internal long-polling loop
    # ─────────────────────────────────────────────────────────────────────────

    def _poll_loop(self):
        while not self._stop_ev.is_set():
            try:
                resp = requests.get(
                    f"{self._base}/getUpdates",
                    params={"offset": self._last_id + 1, "timeout": 15},
                    timeout=20,
                )
                data = resp.json()
                if data.get("ok"):
                    for upd in data.get("result", []):
                        self._last_id = upd["update_id"]
                        msg  = upd.get("message", {})
                        text = msg.get("text", "").strip()
                        if text.startswith("/") and self._handler:
                            reply = self._handler(text)
                            if reply:
                                self.send(reply)
            except Exception as exc:
                logger.debug("Telegram poll error: %s", exc)


class NullTelegramBot:
    """Drop-in replacement when Telegram is not configured."""
    def send(self, text: str):
        logger.info("[TG] %s", text)

    def start_polling(self, *_):
        pass

    def stop(self):
        pass
