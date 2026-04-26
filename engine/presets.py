# Preset parameter sets — exact replica of Pine Script auto-preset resolver.
# Key format: "{SYMBOL}_{TIMEFRAME}"
# Timeframe 2m and 3m both map to the _3 preset (as per Pine Script logic).

PRESETS = {
    "BANKNIFTY_3": {
        "ema_fast_len": 5, "ema_mid_len": 9, "ema_slow_len": 20,
        "st1_atr": 10, "st1_mult": 3.7,
        "st2_atr": 8,  "st2_mult": 1.4,
        "use_chop_filter": True,
        "adx_len_filter": 12, "adx_min_filter": 16.0,
        "atr_len_filter": 14,
        "ema_spread_atr_min": 0.12,
        "range_lookback": 10, "range_atr_max": 3.0,
        "chop_recent_bars": 4,
        "use_15m_filter": False,
        "htf_tf": "15", "htf_fast_len": 13, "htf_mid_len": 34, "htf_slow_len": 40,
        "use_breakout_filter": True, "breakout_lookback": 3,
        "use_quality_filter": True, "min_quality_score": 6,
        "use_no_trade_zone": False,
        "max_ema_crosses_10": 6, "max_st2_flips_10": 4,
        "min_slow_slope_atr": 0.08,
        "min_body_pct": 0.4,
        "buy_close_pos_min": 0.60, "sell_close_pos_max": 0.40,
        "use_early_exit": False,
        "use_trail_stop": True, "trail_trigger_pct": 0.60, "trail_atr_mult": 0.1,
        "bt_target_pct": 0.45, "bt_wide_candle_pct": 0.10, "bt_wide_entry_fraction": 0.15,
        "use_discount_entry": True,
        "sig_start_candle": 1, "force_exit_hour": 15, "force_exit_minute": 15,
        "avoid_midday": False,
    },
    "BANKNIFTY_4": {
        "ema_fast_len": 5, "ema_mid_len": 20, "ema_slow_len": 28,
        "st1_atr": 14, "st1_mult": 3.7,
        "st2_atr": 7,  "st2_mult": 1.3,
        "use_chop_filter": True,
        "adx_len_filter": 12, "adx_min_filter": 16.0,
        "atr_len_filter": 14,
        "ema_spread_atr_min": 0.12,
        "range_lookback": 8, "range_atr_max": 3.0,
        "chop_recent_bars": 4,
        "use_15m_filter": False,
        "htf_tf": "15", "htf_fast_len": 13, "htf_mid_len": 34, "htf_slow_len": 40,
        "use_breakout_filter": True, "breakout_lookback": 3,
        "use_quality_filter": True, "min_quality_score": 6,
        "use_no_trade_zone": False,
        "max_ema_crosses_10": 6, "max_st2_flips_10": 4,
        "min_slow_slope_atr": 0.08,
        "min_body_pct": 0.4,
        "buy_close_pos_min": 0.60, "sell_close_pos_max": 0.40,
        "use_early_exit": False,
        "use_trail_stop": True, "trail_trigger_pct": 0.55, "trail_atr_mult": 0.1,
        "bt_target_pct": 0.45, "bt_wide_candle_pct": 0.12, "bt_wide_entry_fraction": 0.15,
        "use_discount_entry": True,
        "sig_start_candle": 1, "force_exit_hour": 15, "force_exit_minute": 15,
        "avoid_midday": False,
    },
    "BANKNIFTY_5": {
        "ema_fast_len": 7, "ema_mid_len": 24, "ema_slow_len": 34,
        "st1_atr": 14, "st1_mult": 3.9,
        "st2_atr": 14, "st2_mult": 1.9,
        "use_chop_filter": True,
        "adx_len_filter": 12, "adx_min_filter": 16.0,
        "atr_len_filter": 14,
        "ema_spread_atr_min": 0.12,
        "range_lookback": 8, "range_atr_max": 3.0,
        "chop_recent_bars": 4,
        "use_15m_filter": False,
        "htf_tf": "15", "htf_fast_len": 13, "htf_mid_len": 34, "htf_slow_len": 40,
        "use_breakout_filter": True, "breakout_lookback": 3,
        "use_quality_filter": True, "min_quality_score": 6,
        "use_no_trade_zone": False,
        "max_ema_crosses_10": 6, "max_st2_flips_10": 4,
        "min_slow_slope_atr": 0.08,
        "min_body_pct": 0.4,
        "buy_close_pos_min": 0.60, "sell_close_pos_max": 0.40,
        "use_early_exit": False,
        "use_trail_stop": True, "trail_trigger_pct": 0.60, "trail_atr_mult": 0.1,
        "bt_target_pct": 0.45, "bt_wide_candle_pct": 0.12, "bt_wide_entry_fraction": 0.20,
        "use_discount_entry": True,
        "sig_start_candle": 1, "force_exit_hour": 15, "force_exit_minute": 15,
        "avoid_midday": False,
    },
    "BANKNIFTY_15": {
        "ema_fast_len": 5, "ema_mid_len": 14, "ema_slow_len": 24,
        "st1_atr": 10, "st1_mult": 3.0,
        "st2_atr": 7,  "st2_mult": 1.8,
        "use_chop_filter": True,
        "adx_len_filter": 12, "adx_min_filter": 16.0,
        "atr_len_filter": 14,
        "ema_spread_atr_min": 0.12,
        "range_lookback": 8, "range_atr_max": 3.0,
        "chop_recent_bars": 4,
        "use_15m_filter": False,
        "htf_tf": "15", "htf_fast_len": 13, "htf_mid_len": 34, "htf_slow_len": 40,
        "use_breakout_filter": True, "breakout_lookback": 3,
        "use_quality_filter": True, "min_quality_score": 6,
        "use_no_trade_zone": False,
        "max_ema_crosses_10": 6, "max_st2_flips_10": 4,
        "min_slow_slope_atr": 0.08,
        "min_body_pct": 0.4,
        "buy_close_pos_min": 0.60, "sell_close_pos_max": 0.40,
        "use_early_exit": False,
        "use_trail_stop": True, "trail_trigger_pct": 0.60, "trail_atr_mult": 0.1,
        "bt_target_pct": 0.50, "bt_wide_candle_pct": 0.12, "bt_wide_entry_fraction": 0.20,
        "use_discount_entry": True,
        "sig_start_candle": 1, "force_exit_hour": 15, "force_exit_minute": 15,
        "avoid_midday": False,
    },
    "NIFTY_5": {
        "ema_fast_len": 5, "ema_mid_len": 18, "ema_slow_len": 24,
        "st1_atr": 8,  "st1_mult": 3.3,
        "st2_atr": 8,  "st2_mult": 1.5,
        "use_chop_filter": True,
        "adx_len_filter": 12, "adx_min_filter": 16.0,
        "atr_len_filter": 14,
        "ema_spread_atr_min": 0.12,
        "range_lookback": 8, "range_atr_max": 3.0,
        "chop_recent_bars": 4,
        "use_15m_filter": False,
        "htf_tf": "15", "htf_fast_len": 13, "htf_mid_len": 34, "htf_slow_len": 40,
        "use_breakout_filter": True, "breakout_lookback": 3,
        "use_quality_filter": True, "min_quality_score": 6,
        "use_no_trade_zone": False,
        "max_ema_crosses_10": 6, "max_st2_flips_10": 4,
        "min_slow_slope_atr": 0.08,
        "min_body_pct": 0.4,
        "buy_close_pos_min": 0.60, "sell_close_pos_max": 0.40,
        "use_early_exit": False,
        "use_trail_stop": True, "trail_trigger_pct": 0.60, "trail_atr_mult": 0.1,
        "bt_target_pct": 0.45, "bt_wide_candle_pct": 0.12, "bt_wide_entry_fraction": 0.15,
        "use_discount_entry": True,
        "sig_start_candle": 1, "force_exit_hour": 15, "force_exit_minute": 15,
        "avoid_midday": False,
    },
    "NIFTY_15": {
        "ema_fast_len": 4, "ema_mid_len": 18, "ema_slow_len": 24,
        "st1_atr": 9,  "st1_mult": 3.8,
        "st2_atr": 8,  "st2_mult": 1.0,
        "use_chop_filter": True,
        "adx_len_filter": 12, "adx_min_filter": 16.0,
        "atr_len_filter": 14,
        "ema_spread_atr_min": 0.12,
        "range_lookback": 8, "range_atr_max": 3.0,
        "chop_recent_bars": 4,
        "use_15m_filter": False,
        "htf_tf": "15", "htf_fast_len": 13, "htf_mid_len": 34, "htf_slow_len": 40,
        "use_breakout_filter": True, "breakout_lookback": 3,
        "use_quality_filter": True, "min_quality_score": 6,
        "use_no_trade_zone": False,
        "max_ema_crosses_10": 6, "max_st2_flips_10": 4,
        "min_slow_slope_atr": 0.08,
        "min_body_pct": 0.4,
        "buy_close_pos_min": 0.60, "sell_close_pos_max": 0.40,
        "use_early_exit": False,
        "use_trail_stop": True, "trail_trigger_pct": 0.60, "trail_atr_mult": 0.1,
        "bt_target_pct": 0.50, "bt_wide_candle_pct": 0.12, "bt_wide_entry_fraction": 0.20,
        "use_discount_entry": True,
        "sig_start_candle": 1, "force_exit_hour": 15, "force_exit_minute": 15,
        "avoid_midday": False,
    },
}


def resolve_preset(symbol: str, timeframe: str) -> dict:
    """
    Mirrors Pine Script's preset_key resolution logic exactly.
    symbol: 'BANKNIFTY' or 'NIFTY'
    timeframe: '2', '3', '4', '5', '15'
    """
    tf = timeframe.strip()
    sym = symbol.upper()

    if sym == "BANKNIFTY":
        key = (
            "BANKNIFTY_3"  if tf in ("2", "3") else
            "BANKNIFTY_4"  if tf == "4"         else
            "BANKNIFTY_5"  if tf == "5"         else
            "BANKNIFTY_15" if tf == "15"        else
            None
        )
    elif sym == "NIFTY":
        key = (
            "NIFTY_5"  if tf in ("2", "3", "4", "5") else
            "NIFTY_15" if tf == "15"                  else
            None
        )
    else:
        key = None

    if key is None or key not in PRESETS:
        raise ValueError(f"No preset for symbol={symbol}, timeframe={timeframe}m")

    return dict(PRESETS[key]), key
