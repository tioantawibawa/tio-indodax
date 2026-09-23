"""Market regime classification."""

from __future__ import annotations

from enum import Enum

import pandas as pd

from . import indicators as ind


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    NO_TRADE = "no_trade"


def classify_regime(
    df: pd.DataFrame,
    *,
    ema_fast: int = 20,
    ema_slow: int = 50,
    adx_period: int = 14,
    atr_period: int = 14,
    adx_trend: float = 25,
    adx_range: float = 20,
    high_vol_atr_ratio: float = 2.0,
) -> tuple[Regime, str]:
    """Return (regime, human-readable reason) for the last closed candle.

    Order of checks: not enough data -> NO_TRADE; abnormal volatility
    (ATR% > ratio x its 100-bar median) -> HIGH_VOLATILITY; ADX >= trend
    threshold with aligned EMAs -> TRENDING_UP/DOWN; ADX <= range threshold
    -> RANGING; anything in between -> NO_TRADE (ambiguous).
    """
    min_bars = max(ema_slow, adx_period * 2, atr_period) + 10
    if len(df) < min_bars:
        return Regime.NO_TRADE, f"insufficient data ({len(df)} < {min_bars} bars)"
    if (df["volume"].tail(20) <= 0).sum() >= 10:
        return Regime.NO_TRADE, "illiquid: >=10 of last 20 candles have zero volume"

    close = df["close"]
    atr_pct = ind.atr(df, atr_period) / close * 100
    median_atr_pct = atr_pct.tail(100).median()
    last_atr_pct = atr_pct.iloc[-1]
    if median_atr_pct > 0 and last_atr_pct > high_vol_atr_ratio * median_atr_pct:
        return Regime.HIGH_VOLATILITY, (
            f"ATR {last_atr_pct:.2f}% > {high_vol_atr_ratio}x median {median_atr_pct:.2f}%")

    fast = ind.ema(close, ema_fast).iloc[-1]
    slow = ind.ema(close, ema_slow).iloc[-1]
    adx_v = ind.adx(df, adx_period).iloc[-1]
    last = close.iloc[-1]
    if adx_v >= adx_trend:
        if fast > slow and last > slow:
            return Regime.TRENDING_UP, f"ADX {adx_v:.1f}, EMA{ema_fast} > EMA{ema_slow}, close above slow EMA"
        if fast < slow and last < slow:
            return Regime.TRENDING_DOWN, f"ADX {adx_v:.1f}, EMA{ema_fast} < EMA{ema_slow}, close below slow EMA"
        return Regime.NO_TRADE, f"ADX {adx_v:.1f} but EMAs/price not aligned"
    if adx_v <= adx_range:
        return Regime.RANGING, f"ADX {adx_v:.1f} <= {adx_range}"
    return Regime.NO_TRADE, f"ADX {adx_v:.1f} between range and trend thresholds"
