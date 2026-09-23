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
from .regime import Regime, classify_regime


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


def _last(s: pd.Series, back: int = 1) -> float:
    if len(s) < back:
        return math.nan
    v = s.iloc[-back]
    return float(v) if pd.notna(v) else math.nan


def compute_features(df: pd.DataFrame, timeframe: str, p: StrategySettings) -> TimeframeFeatures:
    regime, reason = classify_regime(
        df, ema_fast=p.ema_fast, ema_slow=p.ema_slow, adx_period=p.adx_period,
        atr_period=p.atr_period, adx_trend=p.adx_trend, adx_range=p.adx_range,
        high_vol_atr_ratio=p.high_vol_atr_ratio,
    )
    if len(df) < 2:
        nan = math.nan
        return TimeframeFeatures(timeframe, len(df), nan, nan, nan, nan, nan, nan, nan, nan,
                                 nan, nan, nan, nan, nan, nan, regime, reason)
    close = df["close"]
    m = ind.macd(close)
    bb = ind.bollinger(close)
    atr_s = ind.atr(df, p.atr_period)
    last_close = float(close.iloc[-1])
    atr_v = _last(atr_s)
    return TimeframeFeatures(
        timeframe=timeframe,
        bars=len(df),
        close=last_close,
        ema_fast=_last(ind.ema(close, p.ema_fast)),
        ema_slow=_last(ind.ema(close, p.ema_slow)),
        rsi=_last(ind.rsi(close, p.rsi_period)),
        macd_hist=_last(m["hist"]),
        macd_hist_prev=_last(m["hist"], 2),
        atr=atr_v,
        atr_pct=atr_v / last_close * 100 if last_close else math.nan,
        bb_mid=_last(bb["mid"]),
        bb_upper=_last(bb["upper"]),
        bb_lower=_last(bb["lower"]),
        bb_pctb=_last(bb["pctb"]),
        adx=_last(ind.adx(df, p.adx_period)),
        volume_ratio=_last(ind.volume_ratio(df["volume"])),
        regime=regime,
        regime_reason=reason,
    )


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
