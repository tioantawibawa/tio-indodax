import numpy as np

from agent.analysis.regime import Regime, classify_regime
from tests.helpers import candles, downtrend, ranging, uptrend


def test_uptrend():
    r, why = classify_regime(uptrend())
    assert r == Regime.TRENDING_UP, why


def test_downtrend():
    r, why = classify_regime(downtrend())
    assert r == Regime.TRENDING_DOWN, why


def test_ranging():
    r, why = classify_regime(ranging())
    assert r == Regime.RANGING, why


def test_insufficient_data_is_no_trade():
    r, why = classify_regime(uptrend(30))
    assert r == Regime.NO_TRADE and "insufficient" in why


def test_volatility_spike():
    df = ranging(200)
    df.iloc[-1, df.columns.get_loc("high")] *= 1.15
    df.iloc[-1, df.columns.get_loc("low")] *= 0.85
    r, why = classify_regime(df)
    assert r == Regime.HIGH_VOLATILITY, why


def test_zero_volume_is_no_trade():
    df = uptrend()
    df.iloc[-15:, df.columns.get_loc("volume")] = 0
    assert classify_regime(df)[0] == Regime.NO_TRADE


def test_flat_market_ranging_or_no_trade_never_trending():
    df = candles(np.full(200, 1e9) * (1 + np.random.default_rng(3).normal(0, 1e-4, 200)))
    assert classify_regime(df)[0] not in (Regime.TRENDING_UP, Regime.TRENDING_DOWN)
