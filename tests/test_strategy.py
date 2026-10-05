from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from agent.analysis.regime import Regime
from agent.analysis.signals import TimeframeFeatures, compute_features
from agent.portfolio.portfolio import Position
from agent.strategy.strategy import Strategy
from tests.helpers import NOW, book, pair_info, settings


def feat(tf="1D", regime=Regime.TRENDING_UP, **kw) -> TimeframeFeatures:
    """Daily features that trigger a breakout entry unless overridden."""
    base = dict(timeframe=tf, bars=300, close=1e9, ema_fast=0.99e9, ema_slow=0.97e9, rsi=60.0,
                macd_hist=2.0, macd_hist_prev=1.0, atr=3e7, atr_pct=3.0, bb_mid=0.97e9,
                bb_upper=1.02e9, bb_lower=0.92e9, bb_pctb=0.9, adx=30.0, volume_ratio=1.5,
                regime=regime, regime_reason="test",
                donchian_high=0.98e9, ema_trend=0.9e9, chandelier_stop=0.93e9)
    base.update(kw)
    return TimeframeFeatures(**base)


@pytest.fixture
def strat():
    return Strategy(settings().strategy, D(1_000_000))


def test_breakout_entry(strat):
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), {"1D": feat()})
    assert p is not None, note
    assert p.side == "buy" and p.order_type == "limit" and p.intent == "entry"
    assert p.price == D("1000000000")                 # marketable limit at best ask
    assert p.stop_loss == D("930000000")               # chandelier stop
    assert p.take_profit is None                       # no fixed take-profit
    assert p.target == D("1120000000")                 # entry + 4 x ATR (cost check only)
    assert p.setup == "trend_breakout"
    # risk-sized: 1% of capital (Rp 10.000) lost at SL -> qty = 10000 / 70e6, floored to 1e-8
    assert p.qty == D("0.00014285")


def test_no_breakout_no_entry(strat):
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), {"1D": feat(donchian_high=1.0e9)})
    assert p is None and "no breakout" in note


def test_below_trend_ema_no_entry(strat):
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), {"1D": feat(ema_trend=1.01e9)})
    assert p is None and "EMA" in note


def test_chandelier_above_price_falls_back_to_atr_stop(strat):
    p, _ = strat.propose_entry("btc_idr", pair_info(), book(), {"1D": feat(chandelier_stop=1.05e9)})
    assert p.stop_loss == D("910000000")  # entry - 3 x ATR


@pytest.mark.parametrize("kw", [{"atr": float("nan")}, {"donchian_high": float("nan")},
                                {"ema_trend": float("nan")}, {"atr": 0.0}])
def test_indicators_not_ready(strat, kw):
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), {"1D": feat(**kw)})
    assert p is None and "not ready" in note


def test_missing_timeframe_and_empty_book(strat):
    assert strat.propose_entry("btc_idr", pair_info(), book(), {})[0] is None
    empty = replace(book(), asks=())
    assert strat.propose_entry("btc_idr", pair_info(), empty, {"1D": feat()})[0] is None


def test_donchian_excludes_current_bar():
    s = settings().strategy
    n = 150
    close = np.linspace(1e9, 1.2e9, n)
    df = pd.DataFrame({"open": close, "high": close, "low": close * 0.99, "close": close,
                       "volume": 10.0}, index=pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC"))
    f = compute_features(df, "1D", s)
    assert f.donchian_high == pytest.approx(df["high"].iloc[-21:-1].max())
    assert f.close > f.donchian_high   # a new high breaks out of the PRIOR range
    assert f.chandelier_stop == pytest.approx(df["high"].iloc[-22:].max() - 3 * f.atr)


# ------------------------------------------------------------------ exits

def pos(**kw):
    base = dict(pair="btc_idr", qty=D("0.0001"), avg_cost=D("990000000"), stop_loss=D("980000000"),
                take_profit=None, opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    base.update(kw)
    return Position(**base)


def test_stop_hit_is_emergency_market_exit(strat):
    p = strat.propose_exit(pos(), pair_info(), book(bid="979000000", ask="980000000"), {"1D": feat()})
    assert p.order_type == "market" and p.is_emergency_exit and p.side == "sell" and p.qty == D("0.0001")


def test_hold_while_above_stop(strat):
    assert strat.propose_exit(pos(), pair_info(), book(), {"1D": feat()}) is None


def test_take_profit_only_if_position_has_one(strat):
    p = strat.propose_exit(pos(take_profit=D("990000000")), pair_info(), book(), {"1D": feat()})
    assert p is not None and p.setup == "take_profit" and p.order_type == "limit"


def test_no_exit_on_empty_book(strat):
    assert strat.propose_exit(pos(), pair_info(), replace(book(), bids=()), {"1D": feat()}) is None


def test_trailing_stop_only_moves_up(strat):
    up = strat.trail_stop(pos(stop_loss=D("900000000")), {"1D": feat(chandelier_stop=0.95e9)}, pair_info())
    assert up == D("950000000")
    assert strat.trail_stop(pos(stop_loss=D("960000000")), {"1D": feat(chandelier_stop=0.95e9)}, pair_info()) is None
    assert strat.trail_stop(pos(), {"1D": feat(chandelier_stop=float("nan"))}, pair_info()) is None


# ------------------------------------------------------------ short-term styles (2026-10-05)

def st_strat(**kw):
    base = settings().strategy.model_copy(update={"timeframes": ("60",), "trailing": False, **kw})
    return Strategy(base, D(1_000_000))


def test_st_breakout_entry_has_atr_stop_and_fixed_tp():
    s = st_strat(style="st_breakout", stop_atr_mult=1.5, tp_atr_mult=3.0)
    p, note = s.propose_entry("btc_idr", pair_info(), book(), {"60": feat(tf="60", atr=1e7)})
    assert p is not None, note
    assert p.stop_loss == D("1000000000") - D("15000000") and p.take_profit == D("1000000000") + D("30000000")
    assert p.target == p.take_profit and p.setup == "st_breakout"


def test_st_pullback_needs_low_rsi_above_trend():
    s = st_strat(style="st_pullback", rsi_entry=30, tp_atr_mult=2.0)
    assert s.propose_entry("btc_idr", pair_info(), book(), {"60": feat(tf="60", rsi=45)})[0] is None
    p, _ = s.propose_entry("btc_idr", pair_info(), book(), {"60": feat(tf="60", rsi=25)})
    assert p is not None and p.setup == "st_pullback"
    below = feat(tf="60", rsi=20, close=0.85e9)          # under EMA trend: never buy the dip
    assert s.propose_entry("btc_idr", pair_info(), book(), {"60": below})[0] is None


def test_time_stop_sells_after_max_hold_bars():
    from datetime import timedelta
    from agent.portfolio.portfolio import Position
    s = st_strat(style="st_breakout", max_hold_bars=48)
    pos = Position("btc_idr", D("0.0001"), D(1_000_000_000), D(900_000_000), None, NOW)
    assert s.propose_exit(pos, pair_info(), book(), {}, NOW + timedelta(hours=47)) is None
    p = s.propose_exit(pos, pair_info(), book(), {}, NOW + timedelta(hours=48))
    assert p is not None and p.setup == "time_exit" and p.order_type == "limit" and not p.is_emergency_exit


def test_no_trailing_when_disabled():
    s = st_strat(style="st_breakout", trailing=False)
    from agent.portfolio.portfolio import Position
    pos = Position("btc_idr", D("0.0001"), D(1_000_000_000), D(900_000_000), None, NOW)
    assert s.trail_stop(pos, {"60": feat(tf="60", chandelier_stop=0.99e9)}, pair_info()) is None


def test_default_style_is_unchanged_trend_follow():
    assert settings().strategy.style == "trend_follow" and settings().strategy.max_hold_bars is None
