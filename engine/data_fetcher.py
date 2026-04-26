"""
AngelOne SmartAPI data fetcher.
Fetches historical OHLCV candles and returns a clean pandas DataFrame.
"""

from __future__ import annotations
import time
import logging
import pyotp
import pandas as pd
import pytz
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

INTERVAL_MAP = {
    "1":  "ONE_MINUTE",
    "3":  "THREE_MINUTE",
    "5":  "FIVE_MINUTE",
    "10": "TEN_MINUTE",
    "15": "FIFTEEN_MINUTE",
    "30": "THIRTY_MINUTE",
    "60": "ONE_HOUR",
    "D":  "ONE_DAY",
}

IST = pytz.timezone("Asia/Kolkata")


class AngelOneDataFetcher:
    def __init__(self, broker_cfg: dict):
        self.api_key     = broker_cfg["api_key"]
        self.client_id   = broker_cfg["client_id"]
        self.mpin        = broker_cfg["mpin"]
        self.totp_secret = broker_cfg["totp_secret"]
        self._obj        = None
        self._auth_token = None

    # ─────────────────────────────────────────────────────────────────────────
    # Authentication
    # ─────────────────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            from SmartApi import SmartConnect
        except ImportError:
            raise ImportError(
                "smartapi-python not installed. Run: pip install smartapi-python"
            )

        self._obj = SmartConnect(api_key=self.api_key)
        totp_val  = pyotp.TOTP(self.totp_secret).now()

        try:
            data = self._obj.generateSession(self.client_id, self.mpin, totp_val)
        except Exception as exc:
            raise ConnectionError(f"AngelOne session failed: {exc}") from exc

        if not data or not data.get("status"):
            raise ConnectionError(
                f"AngelOne login failed: {data.get('message', 'unknown error')}"
            )

        self._auth_token = data["data"]["jwtToken"]
        logger.info("AngelOne connected — client %s", self.client_id)
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Historical candle data
    # ─────────────────────────────────────────────────────────────────────────

    def get_historical_data(
        self,
        symbol_token: str,
        exchange: str,
        from_dt: datetime,
        to_dt: datetime,
        interval_min: str = "5",
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candles between from_dt and to_dt (both IST-aware or naive IST).
        Returns DataFrame with columns: timestamp, open, high, low, close, volume
        Timestamps are UTC-aware pandas Timestamps.

        AngelOne API limit: max 60 days per request for 5m data.
        Chunks automatically if range exceeds limit.
        """
        if self._obj is None:
            raise RuntimeError("Call connect() first.")

        interval = INTERVAL_MAP.get(str(interval_min))
        if interval is None:
            raise ValueError(f"Unknown interval: {interval_min}")

        # Ensure IST-aware datetimes
        if from_dt.tzinfo is None:
            from_dt = IST.localize(from_dt)
        if to_dt.tzinfo is None:
            to_dt = IST.localize(to_dt)

        # AngelOne accepts max 60-day windows; chunk if larger
        max_days = 59
        all_frames = []
        chunk_start = from_dt
        while chunk_start < to_dt:
            chunk_end = min(chunk_start + timedelta(days=max_days), to_dt)
            frame = self._fetch_chunk(symbol_token, exchange, chunk_start,
                                      chunk_end, interval)
            if frame is not None and not frame.empty:
                all_frames.append(frame)
            chunk_start = chunk_end + timedelta(minutes=1)
            if chunk_start < to_dt:
                time.sleep(0.3)  # be polite to the API

        if not all_frames:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low",
                                         "close", "volume"])

        df = pd.concat(all_frames, ignore_index=True)
        df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        return df

    def _fetch_chunk(self, token: str, exchange: str,
                     from_dt: datetime, to_dt: datetime,
                     interval: str) -> pd.DataFrame | None:
        fmt = "%Y-%m-%d %H:%M"
        params = {
            "exchange":    exchange,
            "symboltoken": token,
            "interval":    interval,
            "fromdate":    from_dt.strftime(fmt),
            "todate":      to_dt.strftime(fmt),
        }

        for attempt in range(4):
            try:
                resp = self._obj.getCandleData(params)
                break
            except Exception as exc:
                wait = 2 ** attempt
                logger.warning("Candle fetch error (attempt %d): %s — retry in %ds",
                               attempt + 1, exc, wait)
                time.sleep(wait)
        else:
            logger.error("Candle fetch failed after 4 attempts for %s", params)
            return None

        if not resp or not resp.get("status") or not resp.get("data"):
            logger.warning("No data returned: %s", resp.get("message", ""))
            return None

        rows = resp["data"]
        # AngelOne format: [timestamp_str, open, high, low, close, volume]
        records = []
        for r in rows:
            try:
                ts = pd.Timestamp(r[0]).tz_convert("UTC")
                records.append({
                    "timestamp": ts,
                    "open":   float(r[1]),
                    "high":   float(r[2]),
                    "low":    float(r[3]),
                    "close":  float(r[4]),
                    "volume": int(r[5]) if len(r) > 5 else 0,
                })
            except Exception:
                continue

        return pd.DataFrame(records)

    # ─────────────────────────────────────────────────────────────────────────
    # Warmup helper: fetch N calendar days + today up to now
    # ─────────────────────────────────────────────────────────────────────────

    def get_warmup_data(
        self,
        symbol_token: str,
        exchange: str,
        warmup_days: int = 5,
        interval_min: str = "5",
    ) -> pd.DataFrame:
        """
        Fetches warmup_days of history + today's candles up to current moment.
        Used on engine startup to replay past signals and determine current state.
        """
        now_ist = datetime.now(IST)
        # Start from market open of (warmup_days) ago
        from_dt = (now_ist - timedelta(days=warmup_days)).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
        to_dt = now_ist
        logger.info("Fetching warmup data: %s → %s", from_dt, to_dt)
        return self.get_historical_data(symbol_token, exchange, from_dt, to_dt,
                                        interval_min)
