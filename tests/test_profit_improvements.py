"""Regression checks for costs, executable allocations and delayed simulation."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pandas as pd
import pytest

from agent.analysis.costs import CostModel
from agent.backtest.backtester import BacktestConfig, Backtester
from agent.exchange.errors import IndodaxNetworkError
from agent.portfolio.manager import PortfolioManager, target_weights
from agent.portfolio.portfolio import Portfolio
from agent.risk.risk_manager import RiskManager
from agent.storage.db import Database
from tests.helpers import NOW, buy, ctx, market_view, pair_info, sell, settings
from tests.test_live import ex_env, run  # noqa: F401 - shared executor fixture
from tests.test_portfolio_manager import PAIRS, feats, markets_up, pm_settings, series, shifted


def test_cost_filter_prices_marketable_buy_as_taker():
    s = settings()
    rm = RiskManager(s.risk, s.market, s.fees)
    passive = rm.evaluate(buy(price="999000000"), ctx(), market_view())
    crossing = rm.evaluate(buy(price="1000000000"), ctx(), market_view())
    assert passive.approved and crossing.approved
    assert passive.metrics["entry_maker_estimate"] is True
    assert crossing.metrics["entry_maker_estimate"] is False
    assert D(crossing.metrics["round_trip_cost_pct"]) - D(passive.metrics["round_trip_cost_pct"]) == D("0.1")


async def test_unknown_fill_fee_is_conservative_taker(ex_env):  # noqa: F811
    db, pf, fx, ex = ex_env
    fx.timeout_after_accept = True
    _, fills = await run(ex, db, buy(price="1000000000"))
    assert len(fills) == 1
    assert fills[0].fee_idr == CostModel(settings().fees).fee_idr(pair_info(), D(100000), maker=False)


async def test_maker_sell_expires_before_fallback(ex_env):  # noqa: F811
    db, pf, fx, ex = ex_env
    pf.apply_fill("btc_idr", "buy", D("0.0001"), D(1000000000), D(0), NOW, D(900000000))
    fx.fill_ratio = D(0)
    proposal = replace(sell(price="1000000000"), setup="pm_exit_maker")
    did, _ = await run(ex, db, proposal)
    assert db.get_order(ex.coid(did))["status"] == "NEW"
    await ex.check_resting({}, NOW + timedelta(minutes=3))
    assert db.get_order(ex.coid(did))["status"] == "CANCELLED"
    assert len(fx.submits) == 1 and fx.cancels


async def test_failed_cancellation_cannot_create_a_replacement_order(ex_env):  # noqa: F811
    db, pf, fx, ex = ex_env
    fx.fill_ratio = D(0)
    await run(ex, db, buy())
    pf.apply_fill("btc_idr", "buy", D("0.0001"), D(1000000000), D(0), NOW, D(900000000))

    async def fail_cancel(*args):
        raise IndodaxNetworkError("cancel outcome unknown")

    fx.cancel = fail_cancel
    _, events = await run(ex, db, sell(), now=NOW + timedelta(minutes=1))
    assert events == [] and len(fx.submits) == 1


def test_hysteresis_holds_existing_trend_inside_buffer():
    p = pm_settings(trend_buffer_pct=1).portfolio
    c = series(noise=0.001)
    # Place the last close between the +/-1% thresholds.
    c.iloc[-1] = c.ewm(span=p.trend_ema, adjust=False).mean().iloc[-2]
    out, _ = target_weights({"btc_idr": c}, p)
    held, _ = target_weights({"btc_idr": c}, p, {"btc_idr"})
    assert out["btc_idr"] == 0 and held["btc_idr"] > 0
    c.iloc[-1] *= 0.95
    exited, _ = target_weights({"btc_idr": c}, p, {"btc_idr"})
    assert exited["btc_idr"] == 0


def test_position_limit_and_minimum_leave_unexecutable_weight_in_cash():
    closes = {k: series(noise=0.01 + i * 0.002, seed=i) for i, k in enumerate(PAIRS)}
    p = pm_settings(max_positions=2).portfolio
    weights, _ = target_weights(closes, p)
    active = [k for k, v in weights.items() if v > 0]
    assert len(active) == 2 and sum(weights.values()) <= 1
    filtered, diag = target_weights(closes, p, minimum_weights={active[0]: 1.1})
    assert filtered[active[0]] == 0
    assert filtered[active[1]] == weights[active[1]]  # no accidental concentration
    assert "minimum" in diag["excluded"][active[0]]


def test_minimum_aware_handles_optional_exchange_minimums():
    db = Database(":memory:")
    try:
        pm = PortfolioManager(pm_settings(minimum_aware=True).portfolio, db, "paper", D(40000), tuple(PAIRS))
        mk = markets_up()
        mk = {k: replace(m, info=replace(m.info, min_quote=None, min_base=None)) for k, m in mk.items()}
        res = pm.propose(mk, Portfolio(D(40000)), NOW, feats(), "1D", {})
        assert res.plan is not None
    finally:
        db.close()


def test_new_daily_close_revalidates_unfilled_plan_without_weekly_reset():
    db = Database(":memory:")
    try:
        pm = PortfolioManager(pm_settings(react_to_changes=True).portfolio, db, "paper", D(1000000), tuple(PAIRS))
        pf, mk = Portfolio(D(1000000)), markets_up()
        pm.propose(mk, pf, NOW, feats(), "1D", {})
        first = pm.plan()["made_at"]
        stale = pm.propose(mk, pf, NOW + timedelta(days=3), feats(), "1D", {})
        assert not stale.proposals  # same old candles cannot renew the lease
        new = {k: shifted(m, m.candles["1D"]["close"].shift(3, freq="1D")) for k, m in mk.items()}
        renewed = pm.propose(new, pf, NOW + timedelta(days=3), feats(), "1D", {})
        assert renewed.proposals and pm.plan()["made_at"] == first
        assert pm.plan()["validated_at"] == (NOW + timedelta(days=3)).isoformat()
        new["btc_idr"] = mk["btc_idr"]
        assert not pm.propose(new, pf, NOW + timedelta(days=3), feats(), "1D", {}).proposals
    finally:
        db.close()


def test_maker_attempt_survives_restart_then_falls_back_and_pending_buy_blocks_duplicate():
    db = Database(":memory:")
    try:
        p = pm_settings(execution_style="maker_first", maker_wait_minutes=5).portfolio
        pm = PortfolioManager(p, db, "paper", D(1000000), tuple(PAIRS))
        mk, pf = markets_up(), Portfolio(D(1000000))
        first = pm.propose(mk, pf, NOW, feats(), "1D", {})
        assert first.proposals and all(x.price == mk[x.pair].orderbook.best_bid for x in first.proposals)
        assert pm.plan()["execution"]["btc_idr"]["status"] == "proposed"
        pm = PortfolioManager(p, db, "paper", D(1000000), tuple(PAIRS))
        later = pm.propose(mk, pf, NOW + timedelta(minutes=5), feats(), "1D", {})
        assert all(x.price == mk[x.pair].orderbook.best_ask and not x.setup.endswith("_maker") for x in later.proposals)
        blocked = pm.propose(mk, pf, NOW + timedelta(minutes=5), feats(), "1D", {"btc_idr": D(1)})
        assert not any(x.pair == "btc_idr" for x in blocked.proposals)
    finally:
        db.close()


def test_intraday_timeline_never_exposes_unclosed_or_delayed_daily_signal():
    from tests.test_backtester import synthetic_daily
    daily = synthetic_daily(days=160)
    close = pd.Series([1e9 + i * 1e6 for i in range(160)], index=daily.index)
    daily = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * .99,
                          "close": close, "volume": 100})
    idx = pd.date_range(daily.index[-3], periods=48, freq="1h")
    fine = pd.DataFrame({"open": 1e9, "high": 1.01e9, "low": .99e9, "close": 1e9, "volume": 100}, index=idx)
    s = pm_settings()
    s = s.model_copy(update={"market": s.market.model_copy(update={"whitelist": ("btc_idr",)})})
    bt = Backtester(s, {"btc_idr": {"1D": daily, "60": fine}}, {"btc_idr": pair_info()},
                    BacktestConfig(execution_timeframe="60", signal_delay_s=300,
                                   depth_fraction=0.000001, fill_fraction=0.5))
    original = bt.engine.run_cycle
    seen = []

    async def inspect(markets, pf, now, status, **kwargs):
        frame = markets["btc_idr"].candles["1D"]
        assert frame.index[-1] + pd.Timedelta(days=1, seconds=300) <= now
        assert bt.engine.current["btc_idr"]["1D"].close == frame["close"].iloc[-1]
        seen.append(frame.index[-1])
        return await original(markets, pf, now, status, **kwargs)

    bt.engine.run_cycle = inspect
    result = bt.run()
    assert len(seen) == 48 and len(set(seen)) >= 2
    assert result.metrics["partial_fills"] > 0
    assert result.metrics["total_fees_idr"] > 0
    assert result.metrics["turnover_idr"] > 0
    bt.db.close()


@pytest.mark.parametrize("kwargs", [{"signal_delay_s": -1}, {"fill_fraction": 0},
                                    {"fill_fraction": 2}, {"execution_timeframe": "nonsense"}])
def test_invalid_execution_assumptions_are_rejected(kwargs):
    with pytest.raises(ValueError):
        BacktestConfig(**kwargs)
