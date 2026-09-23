import numpy as np
import pandas as pd
import pytest

from agent.analysis import indicators as ind
from tests.helpers import candles, ranging, uptrend


def test_sma_ema_basic():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert ind.sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert np.isnan(ind.sma(s, 3).iloc[1])
    e = ind.ema(s, 3)
    assert e.iloc[-1] == pytest.approx(s.ewm(span=3, adjust=False).mean().iloc[-1])


def test_rsi_extremes_and_known_value():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert ind.rsi(up, 14).iloc[-1] == 100
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert ind.rsi(down, 14).iloc[-1] == pytest.approx(0)
    flat = pd.Series(np.full(30, 5.0))
    assert ind.rsi(flat, 14).iloc[-1] == 50
    # alternating +1/-1 -> gains == losses on average -> ~50
    alt = pd.Series(100 + np.tile([0.0, 1.0], 50))
    assert ind.rsi(alt, 14).iloc[-1] == pytest.approx(50, abs=5)
    assert ind.rsi(up, 14).iloc[:14].isna().all()


def test_rsi_bounded():
    r = ind.rsi(uptrend()["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_macd_hist_is_line_minus_signal():
    m = ind.macd(uptrend()["close"]).dropna()
    assert np.allclose(m["hist"], m["macd"] - m["signal"])
    assert m["macd"].iloc[-1] > 0  # uptrend -> fast EMA above slow


def test_atr_constant_range():
    df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=range(50))
    assert ind.atr(df, 14).iloc[-1] == pytest.approx(2.0)


def test_true_range_uses_gaps():
    df = pd.DataFrame({"high": [10.0, 21], "low": [9.0, 20], "close": [10.0, 20.5]})
    assert ind.true_range(df).tolist() == [1.0, 11.0]


def test_bollinger_bands_order_and_pctb():
    bb = ind.bollinger(ranging()["close"]).dropna()
    assert (bb["upper"] >= bb["mid"]).all() and (bb["mid"] >= bb["lower"]).all()
    flat = ind.bollinger(pd.Series(np.full(30, 7.0)))
    assert flat["width"].iloc[-1] == 0


def test_adx_high_in_trend_low_in_range():
    assert ind.adx(uptrend()).iloc[-1] > 25
    assert ind.adx(ranging()).iloc[-1] < ind.adx(uptrend()).iloc[-1]


def test_volume_ratio():
    v = pd.Series([10.0] * 25 + [30.0])
    assert ind.volume_ratio(v, 20).iloc[-1] == pytest.approx(3.0)


def test_candles_helper_valid_ohlc():
    df = candles(np.array([1.0, 2, 1.5]))
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
