"""Market regime classification.

Implemented vectorised (:func:`regime_frame`) so the backtester can classify
every bar in one pass; :func:`classify_regime` (used live) takes the last row
of the same frame, so live and backtest share one definition.
"""

from __future__ import annotations

from enum import Enum

import numpy as np
import pandas as pd

from . import indicators as ind


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    NO_TRADE = "no_trade"


_CODES = {
    0: (Regime.NO_TRADE, "insufficient"),
    1: (Regime.NO_TRADE, "illiquid"),
    2: (Regime.HIGH_VOLATILITY, "high_vol"),
    3: (Regime.TRENDING_UP, "trend_up"),
    4: (Regime.TRENDING_DOWN, "trend_down"),
    5: (Regime.NO_TRADE, "misaligned"),
    6: (Regime.RANGING, "ranging"),
    7: (Regime.NO_TRADE, "between"),
}


def min_bars(ema_slow: int, adx_period: int, atr_period: int) -> int:
    return max(ema_slow, adx_period * 2, atr_period) + 10


def regime_frame(
    df: pd.DataFrame,
    *,
    ema_fast: int = 20,
    ema_slow: int = 50,
    adx_period: int = 14,
    atr_period: int = 14,
    adx_trend: float = 25,
    adx_range: float = 20,
    high_vol_atr_ratio: float = 2.0,
) -> pd.DataFrame:
    """Per-bar regime plus the inputs used to decide it.

    Order of checks: not enough data -> NO_TRADE; >=10 of last 20 candles
    with zero volume -> NO_TRADE; ATR% > ratio x its 100-bar median ->
    HIGH_VOLATILITY; ADX >= trend threshold with aligned EMAs and price ->
    TRENDING_UP/DOWN (misaligned -> NO_TRADE); ADX <= range threshold ->
    RANGING; in between -> NO_TRADE.
    """
    close = df["close"]
    atr_pct = ind.atr(df, atr_period) / close * 100 if len(df) else close
    med = atr_pct.rolling(100, min_periods=1).median()
    fast = ind.ema(close, ema_fast)
    slow = ind.ema(close, ema_slow)
    adx_v = ind.adx(df, adx_period) if len(df) else close
    zero_vol = (df["volume"] <= 0).astype(float).rolling(20, min_periods=1).sum()
    bars = np.arange(1, len(df) + 1)
    trend = adx_v >= adx_trend
    conds = [
        bars < min_bars(ema_slow, adx_period, atr_period),
        zero_vol >= 10,
        (med > 0) & (atr_pct > high_vol_atr_ratio * med),
        trend & (fast > slow) & (close > slow),
        trend & (fast < slow) & (close < slow),
        trend,
        adx_v <= adx_range,
    ]
    code = np.select([np.asarray(c, dtype=bool) for c in conds], [0, 1, 2, 3, 4, 5, 6], default=7)
    out = pd.DataFrame(
        {"regime_code": code, "adx": adx_v, "ema_fast": fast, "ema_slow": slow,
         "atr_pct": atr_pct, "atr_pct_median": med, "bars": bars},
        index=df.index,
    )
    out["regime"] = [_CODES[c][0] for c in code]
    return out


def describe(row: pd.Series, *, ema_fast: int = 20, ema_slow: int = 50, adx_range: float = 20,
             high_vol_atr_ratio: float = 2.0, needed: int | None = None) -> str:
    """Human-readable reason for one row of :func:`regime_frame`."""
    kind = _CODES[int(row["regime_code"])][1]
    adx_v = row["adx"]
    if kind == "insufficient":
        return f"insufficient data ({int(row['bars'])} < {needed} bars)"
    if kind == "illiquid":
        return "illiquid: >=10 of last 20 candles have zero volume"
    if kind == "high_vol":
        return (f"ATR {row['atr_pct']:.2f}% > {high_vol_atr_ratio}x median "
                f"{row['atr_pct_median']:.2f}%")
    if kind == "trend_up":
        return f"ADX {adx_v:.1f}, EMA{ema_fast} > EMA{ema_slow}, close above slow EMA"
    if kind == "trend_down":
        return f"ADX {adx_v:.1f}, EMA{ema_fast} < EMA{ema_slow}, close below slow EMA"
    if kind == "misaligned":
        return f"ADX {adx_v:.1f} but EMAs/price not aligned"
    if kind == "ranging":
        return f"ADX {adx_v:.1f} <= {adx_range}"
    return f"ADX {adx_v:.1f} between range and trend thresholds"


def classify_regime(df: pd.DataFrame, **kw) -> tuple[Regime, str]:
    """(regime, reason) for the last closed candle of ``df``."""
    need = min_bars(kw.get("ema_slow", 50), kw.get("adx_period", 14), kw.get("atr_period", 14))
    if len(df) < need:
        return Regime.NO_TRADE, f"insufficient data ({len(df)} < {need} bars)"
    row = regime_frame(df, **kw).iloc[-1]
    desc_kw = {k: kw[k] for k in ("ema_fast", "ema_slow", "adx_range", "high_vol_atr_ratio") if k in kw}
    return row["regime"], describe(row, needed=need, **desc_kw)
