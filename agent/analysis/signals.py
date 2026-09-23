"""Per-timeframe feature extraction and the structured market summary.

The summary is the only thing the (optional) LLM analyst ever sees:
numbers already computed here, never raw candles or account data.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pandas as pd

from agent.config import StrategySettings

from . import indicators as ind
from .regime import Regime, describe, min_bars, regime_frame


@dataclass(frozen=True)
class TimeframeFeatures:
    timeframe: str
    bars: int
    close: float
    ema_fast: float
    ema_slow: float
    rsi: float
    macd_hist: float
    macd_hist_prev: float
    atr: float
    atr_pct: float
    bb_mid: float
    bb_upper: float
    bb_lower: float
    bb_pctb: float
    adx: float
    volume_ratio: float
    regime: Regime
    regime_reason: str


def feature_frame(df: pd.DataFrame, p: StrategySettings) -> pd.DataFrame:
    """All TimeframeFeatures fields for every bar (vectorised, causal: row i
    only depends on rows <= i). Used by the backtester; the live path takes
    the last row via :func:`compute_features`."""
    close = df["close"]
    reg = regime_frame(
        df, ema_fast=p.ema_fast, ema_slow=p.ema_slow, adx_period=p.adx_period,
        atr_period=p.atr_period, adx_trend=p.adx_trend, adx_range=p.adx_range,
        high_vol_atr_ratio=p.high_vol_atr_ratio,
    )
    m = ind.macd(close)
    bb = ind.bollinger(close)
    atr_s = ind.atr(df, p.atr_period)
    return pd.DataFrame({
        "close": close,
        "ema_fast": reg["ema_fast"],
        "ema_slow": reg["ema_slow"],
        "rsi": ind.rsi(close, p.rsi_period),
        "macd_hist": m["hist"],
        "macd_hist_prev": m["hist"].shift(1),
        "atr": atr_s,
        "atr_pct": atr_s / close * 100,
        "bb_mid": bb["mid"],
        "bb_upper": bb["upper"],
        "bb_lower": bb["lower"],
        "bb_pctb": bb["pctb"],
        "adx": reg["adx"],
        "volume_ratio": ind.volume_ratio(df["volume"]),
        "regime": reg["regime"],
        "regime_code": reg["regime_code"],
        "atr_pct_median": reg["atr_pct_median"],
        "bars": reg["bars"],
    }, index=df.index)


_NUMERIC = ("close", "ema_fast", "ema_slow", "rsi", "macd_hist", "macd_hist_prev", "atr", "atr_pct",
            "bb_mid", "bb_upper", "bb_lower", "bb_pctb", "adx", "volume_ratio")


def row_to_features(row: pd.Series, timeframe: str, p: StrategySettings,
                    with_reason: bool = True) -> TimeframeFeatures:
    vals = {k: (float(row[k]) if pd.notna(row[k]) else math.nan) for k in _NUMERIC}
    reason = ""
    if with_reason:
        reason = describe(row, ema_fast=p.ema_fast, ema_slow=p.ema_slow, adx_range=p.adx_range,
                          high_vol_atr_ratio=p.high_vol_atr_ratio,
                          needed=min_bars(p.ema_slow, p.adx_period, p.atr_period))
    return TimeframeFeatures(timeframe=timeframe, bars=int(row["bars"]), regime=row["regime"],
                             regime_reason=reason, **vals)


def compute_features(df: pd.DataFrame, timeframe: str, p: StrategySettings) -> TimeframeFeatures:
    if len(df) == 0:
        nan = math.nan
        return TimeframeFeatures(timeframe, 0, nan, nan, nan, nan, nan, nan, nan, nan,
                                 nan, nan, nan, nan, nan, nan, Regime.NO_TRADE, "no data")
    return row_to_features(feature_frame(df, p).iloc[-1], timeframe, p)


@dataclass(frozen=True)
class MarketSummary:
    pair: str
    last: float
    bid: float
    ask: float
    spread_pct: float
    change_24h_pct: float | None
    volume_24h_idr: float | None
    features: dict[str, TimeframeFeatures]

    def to_llm_dict(self) -> dict:
        """Compact, rounded, JSON-serialisable view for the LLM analyst."""
        def rnd(v):
            if isinstance(v, float):
                return None if not math.isfinite(v) else round(v, 4)
            return v
        tfs = {}
        for tf, f in self.features.items():
            d = {k: rnd(v) for k, v in asdict(f).items() if k not in ("timeframe",)}
            d["regime"] = f.regime.value
            tfs[{"15": "15m", "60": "1h", "240": "4h"}.get(tf, tf)] = d
        return {
            "pair": self.pair,
            "last_price_idr": rnd(self.last),
            "spread_pct": rnd(self.spread_pct),
            "change_24h_pct": rnd(self.change_24h_pct),
            "volume_24h_idr": rnd(self.volume_24h_idr),
            "timeframes": tfs,
        }
