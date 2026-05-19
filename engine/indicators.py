"""
Technical indicators replicating Pine Script ta.* functions exactly.

Key conventions matching Pine Script:
  - EMA: seeds with SMA, alpha = 2/(length+1)
  - RMA: seeds with SMA, alpha = 1/length  (Wilder smoothing)
  - ATR: RMA of True Range
  - ADX: manual computation using RMA (not built-in) — matches Pine Script code
  - Supertrend: bands adjust one-way, direction flips on close crossing band
    direction = -1 → bullish (supertrend below price)
    direction =  1 → bearish (supertrend above price)
"""

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Core smoothing functions
# ─────────────────────────────────────────────────────────────────────────────

def _rma(values: np.ndarray, length: int) -> np.ndarray:
    """
    Wilder's RMA — ta.rma() in Pine Script.
    Seeds with SMA of first `length` bars, then alpha = 1/length.
    """
    n = len(values)
    out = np.full(n, np.nan)
    if n < length:
        return out

    alpha = 1.0 / length
    # Find first complete window (ignore leading NaNs in input)
    start = 0
    while start < n and np.isnan(values[start]):
        start += 1

    if start + length > n:
        return out

    seed_end = start + length
    seed = float(np.nanmean(values[start:seed_end]))
    out[seed_end - 1] = seed

    for i in range(seed_end, n):
        if np.isnan(values[i]):
            out[i] = out[i - 1]  # carry forward on NaN input
        else:
            out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]

    return out


def _ema(values: np.ndarray, length: int) -> np.ndarray:
    """
    EMA — ta.ema() in Pine Script.
    Seeds with SMA of first `length` bars, then alpha = 2/(length+1).
    """
    n = len(values)
    out = np.full(n, np.nan)
    if n < length:
        return out

    alpha = 2.0 / (length + 1)
    start = 0
    while start < n and np.isnan(values[start]):
        start += 1

    if start + length > n:
        return out

    seed_end = start + length
    seed = float(np.nanmean(values[start:seed_end]))
    out[seed_end - 1] = seed

    for i in range(seed_end, n):
        if np.isnan(values[i]):
            out[i] = out[i - 1]
        else:
            out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]

    return out


# ─────────────────────────────────────────────────────────────────────────────
# ATR — ta.atr() in Pine Script
# ─────────────────────────────────────────────────────────────────────────────

def compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                length: int) -> np.ndarray:
    n = len(close)
    tr = np.full(n, np.nan)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    return _rma(tr, length)


# ─────────────────────────────────────────────────────────────────────────────
# Supertrend — ta.supertrend(factor, atrPeriod) in Pine Script
# ─────────────────────────────────────────────────────────────────────────────

def compute_supertrend(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                       factor: float, atr_period: int):
    """
    Returns (supertrend_line, direction)
      direction = -1 : bullish (line is support, below price)
      direction =  1 : bearish (line is resistance, above price)

    Band adjustment rules (matching Pine Script nz() seeding):
      lower_band only moves UP or resets when price breaks below it
      upper_band only moves DOWN or resets when price breaks above it
    Direction flip:
      bearish→bullish when close > upper_band
      bullish→bearish when close < lower_band
    """
    n = len(close)
    atr = compute_atr(high, low, close, atr_period)
    hl2 = (high + low) / 2.0

    st_line = np.full(n, np.nan)
    direction = np.full(n, np.nan)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)

    prev_upper = 0.0  # nz(upperBand[1]) = 0 for first bar
    prev_lower = 0.0  # nz(lowerBand[1]) = 0 for first bar
    prev_close = close[0]
    prev_dir = None

    for i in range(n):
        if np.isnan(atr[i]):
            prev_close = close[i]
            continue

        basic_upper = hl2[i] + factor * atr[i]
        basic_lower = hl2[i] - factor * atr[i]

        # Lower band: can only move up, resets if prev close broke below it
        if basic_lower > prev_lower or prev_close < prev_lower:
            fu_lower = basic_lower
        else:
            fu_lower = prev_lower

        # Upper band: can only move down, resets if prev close broke above it
        if basic_upper < prev_upper or prev_close > prev_upper:
            fu_upper = basic_upper
        else:
            fu_upper = prev_upper

        final_lower[i] = fu_lower
        final_upper[i] = fu_upper

        # Direction: first valid ATR bar defaults to bearish (1)
        if prev_dir is None or np.isnan(atr[i - 1] if i > 0 else np.nan):
            cur_dir = 1
        elif prev_dir == 1:  # was bearish (supertrend = upper band)
            cur_dir = -1 if close[i] > fu_upper else 1
        else:  # was bullish (supertrend = lower band)
            cur_dir = 1 if close[i] < fu_lower else -1

        direction[i] = cur_dir
        st_line[i] = fu_lower if cur_dir == -1 else fu_upper

        prev_upper = fu_upper
        prev_lower = fu_lower
        prev_close = close[i]
        prev_dir = cur_dir

    return st_line, direction


# ─────────────────────────────────────────────────────────────────────────────
# ADX — manual calculation exactly matching Pine Script BrahmAstra code
# Uses RMA (not EMA) for smoothing, matching the Pine Script implementation.
# ─────────────────────────────────────────────────────────────────────────────

def compute_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                length: int):
    """
    Returns (plus_di, minus_di, adx) — all same length as inputs.
    Matches the manual Pine Script ADX code in BrahmAstra exactly.
    """
    n = len(high)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.full(n, np.nan)

    tr[0] = high[0] - low[0]
    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    trur = _rma(tr, length)
    plus_dm_rma = _rma(plus_dm, length)
    minus_dm_rma = _rma(minus_dm, length)

    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = np.where(trur == 0.0, 0.0, 100.0 * plus_dm_rma / trur)
        minus_di = np.where(trur == 0.0, 0.0, 100.0 * minus_dm_rma / trur)
        di_sum = plus_di + minus_di
        dx = np.where(di_sum == 0.0, 0.0,
                      100.0 * np.abs(plus_di - minus_di) / di_sum)

    adx = _rma(dx, length)
    return plus_di, minus_di, adx


# ─────────────────────────────────────────────────────────────────────────────
# Master: compute all indicators onto a DataFrame
# ─────────────────────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """
    Takes a OHLCV DataFrame (columns: open, high, low, close, volume, timestamp)
    and adds all computed indicator columns needed by the signal engine.

    Also precomputes session columns, chop-derived columns, and
    rolling event counts so the bar-by-bar state machine stays lean.
    """
    df = df.copy()
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values

    # ── EMAs ──────────────────────────────────────────────────────────────────
    df["ema_fast"] = _ema(c, params["ema_fast_len"])
    df["ema_mid"]  = _ema(c, params["ema_mid_len"])
    df["ema_slow"] = _ema(c, params["ema_slow_len"])

    # ── Supertrends ───────────────────────────────────────────────────────────
    st1_line, st1_dir = compute_supertrend(h, l, c, params["st1_mult"], params["st1_atr"])
    st2_line, st2_dir = compute_supertrend(h, l, c, params["st2_mult"], params["st2_atr"])
    df["st1_line"] = st1_line
    df["st1_dir"]  = st1_dir    # -1 bullish, 1 bearish, nan = not yet
    df["st2_line"] = st2_line
    df["st2_dir"]  = st2_dir

    # ── ATR for chop/trail filter ─────────────────────────────────────────────
    df["atr_filter"] = compute_atr(h, l, c, params["atr_len_filter"])

    # ── ADX (manual, matching Pine Script) ────────────────────────────────────
    _, _, adx_vals = compute_adx(h, l, c, params["adx_len_filter"])
    df["adx"] = adx_vals

    # ── Session time fields ───────────────────────────────────────────────────
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    if df["timestamp"].dt.tz is None:
        ts_ist = df["timestamp"].dt.tz_localize("UTC").dt.tz_convert(IST)
    else:
        ts_ist = df["timestamp"].dt.tz_convert(IST)

    df["ist_hour"]   = ts_ist.dt.hour
    df["ist_minute"] = ts_ist.dt.minute
    df["_date"]      = ts_ist.dt.date

    # New-day flag: matches Pine Script logic (day change OR exactly 9:15)
    date_changed = df["_date"] != df["_date"].shift(1)
    at_915 = (df["ist_hour"] == 9) & (df["ist_minute"] == 15)
    df["is_new_day"] = (date_changed | at_915).fillna(True)

    # In-market-hours (9:15 to 15:59)
    df["in_market"] = (
        ((df["ist_hour"] > 9) | ((df["ist_hour"] == 9) & (df["ist_minute"] >= 15))) &
        (df["ist_hour"] < 16)
    )

    # ── EMA spread / ATR ratio ────────────────────────────────────────────────
    df["ema_spread_atr"] = np.where(
        df["atr_filter"] == 0, 0.0,
        (df["ema_fast"] - df["ema_mid"]).abs() / df["atr_filter"]
    )

    # ── EMA-mid cross events (rolling 10-bar count) ───────────────────────────
    close_above_mid = (df["close"] > df["ema_mid"]).astype(float)
    ema_cross_event = (close_above_mid != close_above_mid.shift(1)).astype(float)
    df["ema_mid_crosses_10"] = ema_cross_event.rolling(10, min_periods=1).sum()

    # ── ST2 flip events (rolling 10-bar count) ────────────────────────────────
    st2_bull_flag = (df["st2_dir"] < 0).astype(float)
    st2_flip_event = (st2_bull_flag != st2_bull_flag.shift(1)).astype(float)
    df["st2_flips_10"] = st2_flip_event.rolling(10, min_periods=1).sum()

    # ── EMA slow slope / ATR (over 5 bars) ───────────────────────────────────
    ema_slow_5ago = df["ema_slow"].shift(5)
    df["ema_slow_slope_atr"] = np.where(
        df["atr_filter"] == 0, 0.0,
        (df["ema_slow"] - ema_slow_5ago).abs() / df["atr_filter"]
    )

    # ── Highest/Lowest for breakout check ─────────────────────────────────────
    # ta.highest(high, breakout_lookback)[1] = max of previous N bars (excl. current)
    lb = params["breakout_lookback"]
    df["highest_prev"] = df["high"].rolling(lb, min_periods=1).max().shift(1)
    df["lowest_prev"]  = df["low"].rolling(lb, min_periods=1).min().shift(1)

    # ── Highest/Lowest for chop range check ──────────────────────────────────
    # ta.highest(high, range_lookback) = max of current + previous N-1 bars
    rl = params["range_lookback"]
    df["recent_high"] = df["high"].rolling(rl, min_periods=1).max()
    df["recent_low"]  = df["low"].rolling(rl, min_periods=1).min()
    df["recent_range"] = df["recent_high"] - df["recent_low"]

    df["range_atr"] = np.where(
        df["atr_filter"] == 0, 9999.0,
        df["recent_range"] / df["atr_filter"]
    )

    # ── Chop score (0–3) and is_choppy flag ───────────────────────────────────
    adx_chop   = (df["adx"] < params["adx_min_filter"]).astype(int)
    spread_chop = (df["ema_spread_atr"] < params["ema_spread_atr_min"]).astype(int)
    range_chop  = (df["range_atr"] < params["range_atr_max"]).astype(int)
    df["chop_score"]   = adx_chop + spread_chop + range_chop
    df["is_choppy"]    = df["chop_score"] >= 2

    # ── bars_since_choppy (running counter, not reset on new day) ─────────────
    bars_since = []
    count = 1_000_000
    for choppy in df["is_choppy"]:
        if choppy:
            count = 0
        else:
            count = min(count + 1, 1_000_000)
        bars_since.append(count)
    df["bars_since_choppy"] = bars_since

    # ── HTF (15m) EMA — matches Pine Script request.security(lookahead_off) ──
    # Resample 5m → 15m, compute EMA on the 15m close series, then shift by 1
    # so each 5m bar sees the PREVIOUSLY COMPLETED 15m bar's values.
    htf_tf_mins  = int(params.get("htf_tf", "15"))
    htf_fast_len = params["htf_fast_len"]
    htf_mid_len  = params["htf_mid_len"]
    htf_slow_len = params["htf_slow_len"]

    ist_mins = ts_ist.dt.hour * 60 + ts_ist.dt.minute
    df["_htf_key"] = (
        ts_ist.dt.date.astype(str) + "_" +
        (ist_mins // htf_tf_mins).astype(str)
    )

    htf_agg = (
        df[["_htf_key", "close", "timestamp"]]
        .groupby("_htf_key", sort=False)
        .agg(htf_close=("close", "last"), _slot_ts=("timestamp", "first"))
        .reset_index()
        .sort_values("_slot_ts")
        .reset_index(drop=True)
    )

    htf_c = htf_agg["htf_close"].values
    htf_agg["htf_ema_fast"] = _ema(htf_c, htf_fast_len)
    htf_agg["htf_ema_mid"]  = _ema(htf_c, htf_mid_len)
    htf_agg["htf_ema_slow"] = _ema(htf_c, htf_slow_len)

    # Shift 1 so every 5m bar within a slot sees the *previous* completed slot
    for col in ["htf_close", "htf_ema_fast", "htf_ema_mid", "htf_ema_slow"]:
        htf_agg[col] = htf_agg[col].shift(1)

    df = df.merge(
        htf_agg[["_htf_key", "htf_close", "htf_ema_fast", "htf_ema_mid", "htf_ema_slow"]],
        on="_htf_key", how="left",
    ).drop(columns=["_htf_key"])

    df["htf_buy_ok"] = (
        (df["htf_close"] > df["htf_ema_slow"]) &
        (df["htf_ema_fast"] > df["htf_ema_mid"])
    ).fillna(False)
    df["htf_sell_ok"] = (
        (df["htf_close"] < df["htf_ema_slow"]) &
        (df["htf_ema_fast"] < df["htf_ema_mid"])
    ).fillna(False)

    return df
