from dataclasses import replace
from datetime import timezone, datetime
from decimal import Decimal as D

import pytest

from agent.analysis.regime import Regime
from agent.analysis.signals import TimeframeFeatures, compute_features
from agent.portfolio.portfolio import Position
from agent.strategy.strategy import Strategy
from tests.helpers import book, pair_info, settings, uptrend


def feat(tf="15", regime=Regime.TRENDING_UP, **kw) -> TimeframeFeatures:
    base = dict(timeframe=tf, bars=200, close=1e9, ema_fast=0.99e9, ema_slow=0.97e9, rsi=55.0,
                macd_hist=2.0, macd_hist_prev=1.0, atr=5e6, atr_pct=0.5, bb_mid=1.02e9,
                bb_upper=1.04e9, bb_lower=0.98e9, bb_pctb=0.5, adx=30.0, volume_ratio=1.0,
                regime=regime, regime_reason="test")
    base.update(kw)
    return TimeframeFeatures(**base)


@pytest.fixture
def strat():
    s = settings()
    return Strategy(s.strategy, D(1_000_000))


def feats(entry=None, signal=None, trend=None):
    return {"15": entry or feat("15"), "60": signal or feat("60"), "240": trend or feat("240")}


def test_trend_pullback_entry(strat):
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), feats())
    assert p is not None, note
    assert p.side == "buy" and p.order_type == "limit" and p.intent == "entry"
    assert p.price == D("999000000")                     # rests on best bid (maker)
    assert p.stop_loss == D("989000000")                  # entry - 2 * ATR(1h)=5e6
    assert p.take_profit == D("1014000000")               # entry + 3 * ATR
    assert p.setup == "trend_pullback" and p.confidence >= 0.55
    # risk-sized: 1% of capital = Rp 10.000 lost at SL -> qty = 10000 / 10e6
    assert p.qty == D("0.001")


@pytest.mark.parametrize("trend_regime", [Regime.TRENDING_DOWN, Regime.HIGH_VOLATILITY, Regime.NO_TRADE])
def test_no_entry_when_trend_tf_bad(strat, trend_regime):
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), feats(trend=feat("240", trend_regime)))
    assert p is None and "trend TF" in note


def test_no_entry_when_overbought(strat):
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), feats(entry=feat("15", rsi=75)))
    assert p is None and "no pullback" in note


def test_no_entry_when_macd_falling(strat):
    p, _ = strat.propose_entry("btc_idr", pair_info(), book(),
                               feats(entry=feat("15", macd_hist=1.0, macd_hist_prev=2.0)))
    assert p is None


def test_range_reversion_entry(strat):
    e = feat("15", close=0.978e9, bb_lower=0.98e9, rsi=25)
    sig = feat("60", regime=Regime.RANGING, adx=15, bb_mid=1.02e9)
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), feats(entry=e, signal=sig,
                                  trend=feat("240", Regime.RANGING)))
    assert p is not None, note
    assert p.setup == "range_reversion"
    assert p.take_profit == D("1020000000") and p.stop_loss < p.price


def test_range_not_oversold_no_entry(strat):
    sig = feat("60", regime=Regime.RANGING, adx=15)
    p, _ = strat.propose_entry("btc_idr", pair_info(), book(), feats(signal=sig))
    assert p is None


def test_range_tp_below_entry_rejected(strat):
    e = feat("15", close=0.978e9, bb_lower=0.98e9, rsi=25)
    sig = feat("60", regime=Regime.RANGING, adx=15, bb_mid=0.99e9)  # mid below bid
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), feats(entry=e, signal=sig))
    assert p is None and "take-profit" in note


def test_missing_data_and_nan(strat):
    assert strat.propose_entry("btc_idr", pair_info(), book(), {"15": feat()})[0] is None
    p, note = strat.propose_entry("btc_idr", pair_info(), book(), feats(signal=feat("60", atr=float("nan"))))
    assert p is None and "not ready" in note


def test_features_from_real_candles_feed_strategy(strat):
    s = settings()
    f = compute_features(uptrend(), "60", s.strategy)
    assert f.regime == Regime.TRENDING_UP and f.atr > 0 and 0 <= f.rsi <= 100


# ------------------------------------------------------------------ exits

def pos(**kw):
    base = dict(pair="btc_idr", qty=D("0.001"), avg_cost=D("990000000"), stop_loss=D("980000000"),
                take_profit=D("1040000000"), opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    base.update(kw)
    return Position(**base)


def test_stop_loss_exit_is_emergency_market(strat):
    p = strat.propose_exit(pos(), pair_info(), book(bid="979000000", ask="980000000"), feats())
    assert p.order_type == "market" and p.is_emergency_exit and p.side == "sell" and p.qty == D("0.001")


def test_take_profit_exit_is_limit(strat):
    p = strat.propose_exit(pos(), pair_info(), book(bid="1041000000", ask="1042000000"), feats())
    assert p.order_type == "limit" and not p.is_emergency_exit and p.setup == "take_profit"


def test_regime_exit(strat):
    p = strat.propose_exit(pos(), pair_info(), book(), feats(signal=feat("60", Regime.TRENDING_DOWN)))
    assert p is not None and p.setup == "regime_exit"


def test_hold_when_nothing_triggers(strat):
    assert strat.propose_exit(pos(), pair_info(), book(), feats()) is None


def test_no_exit_on_empty_book(strat):
    empty = replace(book(), bids=())
    assert strat.propose_exit(pos(), pair_info(), empty, feats()) is None
