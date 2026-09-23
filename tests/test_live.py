"""Phase 5: live executor, reconciliation, deadman switch, preflight — against a fake exchange."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pandas as pd
import pytest

from agent.analysis.costs import CostModel
from agent.data.market_data import PairMarket
from agent.execution.deadman import DeadmanSwitch
from agent.execution.executor import LiveExecutor
from agent.execution.live_gate import live_preflight
from agent.execution.reconcile import reconcile
from agent.exchange.trade_client import OrderState
from agent.portfolio.portfolio import Portfolio
from agent.reporting.telegram_bot import RecordingNotifier
from agent.risk.risk_manager import AgentStatus, RiskDecision, Verdict
from agent.runner import AgentRunner
from agent.storage.db import Database
from tests.fake_exchange import FakeExchange
from tests.helpers import NOW, book, buy, pair_info, sell, settings, ticker
from tests.test_runner import PAIRS, Clock, FakeMD
from tests.test_strategy import feat


def market(pair="btc_idr", b=None):
    return PairMarket(pair, pair_info(pair), ticker(pair), b or book(pair), {"1D": pd.DataFrame()}, NOW.timestamp())


def approve(p):
    return RiskDecision(Verdict.APPROVE, p, p.qty, p.price, ("ok",))


@pytest.fixture
def ex_env(tmp_path):
    db = Database(tmp_path / "l.db")
    pf = Portfolio(D(1_000_000))
    fx = FakeExchange()
    ex = LiveExecutor(db, pf, fx, CostModel(settings().fees), emergency_slippage_pct=1.0)
    yield db, pf, fx, ex
    db.close()


async def run(ex, db, p, m=None, now=NOW):
    did = db.record_decision(approve(p), "live", ts=now)
    return did, await ex.execute(did, approve(p), m or market(p.pair), now)


# ------------------------------------------------------------ executor

async def test_marketable_buy_fills_and_records(ex_env):
    db, pf, fx, ex = ex_env
    did, evs = await run(ex, db, buy(price="1000000000", qty="0.0001", sl="930000000"))
    assert len(evs) == 1 and evs[0].qty == D("0.0001")
    assert pf.positions["btc_idr"].stop_loss == D("930000000")
    o = db.get_order(ex.coid(did))
    assert o["status"] == "FILLED" and o["exchange_order_id"] and D(o["filled_qty"]) == D("0.0001")
    assert fx.submits == [ex.coid(did)]


async def test_client_order_id_format(ex_env):
    db, pf, fx, ex = ex_env
    coid = ex.coid(123456789)
    assert len(coid) <= 36 and all(c.isalnum() or c in "-_" for c in coid) and ex.is_ours(coid)
    assert not ex.is_ours("manual-order-1")


async def test_same_decision_never_submitted_twice(ex_env):
    db, pf, fx, ex = ex_env
    p = buy(price="1000000000", qty="0.0001")
    did = db.record_decision(approve(p), "live", ts=NOW)
    await ex.execute(did, approve(p), market(), NOW)
    await ex.execute(did, approve(p), market(), NOW)
    assert fx.submits == [ex.coid(did)]


async def test_unknown_outcome_resolved_by_lookup_not_resubmit(ex_env):
    db, pf, fx, ex = ex_env
    fx.timeout_after_accept = True          # exchange took the order, response lost
    did, evs = await run(ex, db, buy(price="1000000000", qty="0.0001"))
    assert len(fx.submits) == 1
    assert pf.positions["btc_idr"].qty == D("0.0001")          # adopted from lookup
    assert db.get_order(ex.coid(did))["status"] == "FILLED"


async def test_unknown_outcome_lookup_fails_then_resolves_next_cycle(ex_env):
    db, pf, fx, ex = ex_env
    fx.timeout_after_accept = True
    fx.fail_lookups = 1
    did, evs = await run(ex, db, buy(price="1000000000", qty="0.0001"))
    assert evs == [] and db.get_order(ex.coid(did))["status"] == "PENDING_SUBMIT"
    evs = await ex.check_resting({"btc_idr": market()}, NOW + timedelta(minutes=5))
    assert len(evs) == 1 and len(fx.submits) == 1


async def test_never_reached_exchange_marked_rejected_after_grace(ex_env):
    db, pf, fx, ex = ex_env
    p = buy(price="1000000000", qty="0.0001")
    did = db.record_decision(approve(p), "live", ts=NOW)
    db.record_order(client_order_id=ex.coid(did), decision_id=did, mode="live", pair="btc_idr", side="buy",
                    order_type="limit", price=p.price, qty=p.qty, status="PENDING_SUBMIT", ts=NOW)
    await ex.check_resting({"btc_idr": market()}, NOW + timedelta(minutes=2))
    assert db.get_order(ex.coid(did))["status"] == "PENDING_SUBMIT"      # still within grace
    await ex.check_resting({"btc_idr": market()}, NOW + timedelta(minutes=6))
    assert db.get_order(ex.coid(did))["status"] == "REJECTED"
    await ex.execute(did, approve(p), market(), NOW + timedelta(minutes=7))
    assert fx.submits == []                                               # still never resubmitted


async def test_partial_fill_accumulates_without_double_count_then_ttl_cancel(ex_env):
    db, pf, fx, ex = ex_env
    fx.fill_ratio = D("0.4")
    did, evs = await run(ex, db, buy(price="1000000000", qty="0.0001"))
    coid = ex.coid(did)
    assert pf.positions["btc_idr"].qty == D("0.00004")
    for _ in range(3):   # repeated syncs with no change must not add fills
        await ex.check_resting({"btc_idr": market()}, NOW + timedelta(seconds=30))
    assert pf.positions["btc_idr"].qty == D("0.00004")
    fx.fill_more(coid, D("0.00002"))
    await ex.check_resting({"btc_idr": market()}, NOW + timedelta(seconds=60))
    assert pf.positions["btc_idr"].qty == D("0.00006")
    assert ex.pending_buy_idr()["btc_idr"] == D("1000000000") * D("0.00004")
    await ex.check_resting({"btc_idr": market()}, NOW + timedelta(seconds=130))   # entry TTL 120 s
    assert coid in fx.cancels and db.get_order(coid)["status"] == "CANCELLED"
    assert ex.pending_buy_idr() == {}


async def test_real_indodax_shapes_book_exact_coin_and_reconcile_clean(ex_env):
    # real responses: filled buy has receive_btc=0 and fee-inflated order_rp -> book the submitted qty
    db, pf, fx, ex = ex_env
    fx.indodax_shapes = True
    did, _ = await run(ex, db, buy(price="1506036000", qty="0.00000768", sl="1400000000"))
    assert pf.positions["btc_idr"].qty == D("0.00000768")
    assert db.get_order(ex.coid(did))["status"] == "FILLED"
    assert (await reconcile(fx, db, "live", pf, ["btc_idr"], ex.is_ours)).ok
    did, _ = await run(ex, db, sell(price="1497858000", qty="0.00000768"))
    assert "btc_idr" not in pf.positions and db.get_order(ex.coid(did))["status"] == "FILLED"


async def test_real_indodax_shapes_partial_buy_never_overbooks(ex_env):
    db, pf, fx, ex = ex_env
    fx.indodax_shapes, fx.fill_ratio = True, D("0.4")
    did, _ = await run(ex, db, buy(price="1000000000", qty="0.0001"))
    held = pf.positions["btc_idr"].qty
    assert held <= fx.balances["btc"] and held >= D("0.0000399")
    fx.fill_more(ex.coid(did), D("0.00006"))
    await ex.check_resting({"btc_idr": market()}, NOW + timedelta(seconds=30))
    assert pf.positions["btc_idr"].qty == D("0.0001") == fx.balances["btc"]


async def test_emergency_exit_is_bounded_limit_and_records_stop(ex_env):
    db, pf, fx, ex = ex_env
    await run(ex, db, buy(price="1000000000", qty="0.0001"))
    s = replace(sell(price="999000000", qty="0.0001", emergency=True), setup="stop_loss")
    did, evs = await run(ex, db, s)
    o = fx.orders[ex.coid(did)]
    assert o["price"] == D("989010000")          # best bid 999,000,000 - 1% (rounded down to tick)
    assert evs and evs[0].is_stop_loss and "btc_idr" not in pf.positions
    assert "btc_idr" in db.last_stoploss_times("live")


async def test_sell_cancels_previous_agent_orders_first(ex_env):
    db, pf, fx, ex = ex_env
    await run(ex, db, buy(price="1000000000", qty="0.0001"))
    fx.fill_ratio = D(0)
    did1, _ = await run(ex, db, sell(price="1010000000", qty="0.0001"))   # resting take-profit style sell
    fx.fill_ratio = D(1)
    did2, _ = await run(ex, db, replace(sell(price="999000000", qty="0.0001", emergency=True), setup="stop_loss"))
    assert ex.coid(did1) in fx.cancels and "btc_idr" not in pf.positions


async def test_rejected_order_recorded(ex_env):
    db, pf, fx, ex = ex_env
    from agent.exchange.errors import IndodaxAPIError

    async def reject(*a, **k):
        raise IndodaxAPIError("Insufficient balance")
    fx.place_limit = reject
    did, evs = await run(ex, db, buy(price="1000000000", qty="0.0001"))
    assert evs == [] and db.get_order(ex.coid(did))["status"] == "REJECTED"
    assert "btc_idr" not in pf.positions


async def test_cancel_all_only_touches_agent_orders(ex_env):
    db, pf, fx, ex = ex_env
    fx.fill_ratio = D(0)
    await run(ex, db, buy(price="990000000", qty="0.0001"))
    fx.foreign_orders["btc_idr"] = [OrderState("999", "manual-1", "btc_idr", "buy", D(1), D(1), D(1), "open", {})]
    assert await ex.cancel_all(NOW) == 1
    assert fx.cancels and all(ex.is_ours(c) for c in fx.cancels)


# ------------------------------------------------------------ reconciliation

async def test_reconcile_detects_missing_coins_and_unknown_agent_orders(ex_env):
    db, pf, fx, ex = ex_env
    await run(ex, db, buy(price="1000000000", qty="0.0001"))
    r = await reconcile(fx, db, "live", pf, ["btc_idr"], ex.is_ours)
    assert r.ok and r.exchange_free_idr is not None
    fx.balances["btc"] = D("0.00005")                   # someone sold half outside the agent
    r = await reconcile(fx, db, "live", pf, ["btc_idr"], ex.is_ours)
    assert not r.ok and "BTC" in r.issues[0]
    fx.balances["btc"] = D("0.0001")
    fx.orders["ghost"] = dict(order_id="1", coid=ex.id_prefix + "999", pair="btc_idr", side="buy",
                              price=D(1), qty=D(1), filled=D(0), status="open")
    r = await reconcile(fx, db, "live", pf, ["btc_idr"], ex.is_ours)
    assert not r.ok and "not open in DB" in r.issues[0]


async def test_reconcile_allows_owner_extra_funds_and_warns_on_foreign_orders(ex_env):
    db, pf, fx, ex = ex_env
    fx.balances["btc"] = D("5")            # owner's own coins: fine
    fx.foreign_orders["btc_idr"] = [OrderState("9", "", "btc_idr", "sell", D(1), D(1), D(1), "open", {})]
    r = await reconcile(fx, db, "live", pf, ["btc_idr"], ex.is_ours)
    assert r.ok and r.warnings


# ------------------------------------------------------------ deadman

async def test_deadman_alerts_once_and_recovers():
    fx, notes = FakeExchange(), RecordingNotifier()
    dm = DeadmanSwitch(fx, ["btc_idr", "eth_idr"], 120000, notes)
    assert await dm.beat() and fx.deadman_calls == [(["btc_idr", "eth_idr"], 120000)]
    fx.deadman_fail = True
    for _ in range(5):
        await dm.beat()
    assert not dm.healthy and sum("Deadman Switch gagal" in m for m in notes.messages) == 1
    fx.deadman_fail = False
    assert await dm.beat() and dm.healthy and "pulih" in notes.messages[-1]


# ------------------------------------------------------------ runner in live mode

@pytest.fixture
def live_env(tmp_path):
    s = settings(market={"whitelist": PAIRS})
    db = Database(tmp_path / "lr.db")
    md, clock, notes, fx = FakeMD(), Clock(), RecordingNotifier(), FakeExchange()
    signals = {p: {"1D": feat()} for p in PAIRS}
    dm = DeadmanSwitch(fx, PAIRS, 120000, notes)

    def make():
        r = AgentRunner(s, db, md, "live", notifier=notes, now=clock, trade_client=fx, deadman=dm)
        r.engine.features = lambda m: signals[m.pair]
        return r

    yield make, db, md, clock, notes, fx, dm, s
    db.close()


async def test_live_cycle_trades_with_exchange_balance_cap(live_env):
    make, db, md, clock, notes, fx, dm, s = live_env
    fx.balances["idr"] = D(150_000)          # less IDR on the account than the agent's capital
    r = make()
    fills = await r.run_cycle()
    spent = sum((f.qty * f.price for f in fills), D(0))
    assert fills and spent <= D(150_000)
    assert all(f.qty * f.price <= D(100_000) * D("1.004") for f in fills)


async def test_live_restart_never_double_orders(live_env):
    make, db, md, clock, notes, fx, dm, s = live_env
    r1 = make()
    await r1.run_cycle()
    n = len(fx.submits)
    r2 = make()
    assert set(r2.pf.positions) == set(r1.pf.positions)
    clock.t += timedelta(minutes=5)
    await r2.run_cycle()
    assert len(fx.submits) == n


async def test_live_reconcile_mismatch_pauses_blocks_and_recovers(live_env):
    make, db, md, clock, notes, fx, dm, s = live_env
    r = make()
    await r.run_cycle()
    held = fx.balances["btc"]
    fx.balances["btc"] = D(0)
    md.books["btc_idr"] = book("btc_idr", bid="920000000", ask="921000000")   # stop would trigger
    clock.t += timedelta(minutes=5)
    n = len(fx.submits)
    await r.run_cycle()
    assert r.status == AgentStatus.PAUSED and r.pause_reason == "reconcile"
    assert len(fx.submits) == n                        # nothing sent while inconsistent
    assert any("Rekonsiliasi gagal" in m for m in notes.messages)
    fx.balances["btc"] = held
    md.books["btc_idr"] = book("btc_idr")
    clock.t += timedelta(minutes=5)
    await r.run_cycle()
    assert r.status == AgentStatus.RUNNING and "kembali cocok" in notes.messages[-1]


async def test_live_deadman_unhealthy_pauses_entries(live_env):
    make, db, md, clock, notes, fx, dm, s = live_env
    fx.deadman_fail = True
    for _ in range(3):
        await dm.beat()
    r = make()
    await r.run_cycle()
    assert r.status == AgentStatus.PAUSED and r.pause_reason == "deadman" and not fx.submits
    fx.deadman_fail = False
    await dm.beat()
    await r.run_cycle()
    assert r.status == AgentStatus.RUNNING and fx.submits


async def test_live_kill_cancels_agent_orders(live_env):
    make, db, md, clock, notes, fx, dm, s = live_env
    fx.fill_ratio = D(0)
    r = make()
    await r.run_cycle()
    assert r.broker.resting_orders()
    reply = await r.kill()
    assert "HALTED" in reply and r.status == AgentStatus.HALTED and fx.cancels


async def test_live_startup_banner(live_env):
    make, db, md, clock, notes, fx, dm, s = live_env
    await make().startup(10, ["x"])
    assert "MODE LIVE" in notes.messages[-1]


# ------------------------------------------------------------ preflight

async def _pre(s, db, fx, dm, offset=10, tg=True):
    pf = Portfolio(D(1_000_000))
    return await live_preflight(s, db, fx, dm, pf, lambda c: c.startswith("ag"), offset, tg)


async def test_preflight_blocks_until_all_conditions_hold(tmp_path):
    s = settings(market={"whitelist": PAIRS})
    db = Database(tmp_path / "g.db")
    fx, notes = FakeExchange(), RecordingNotifier()
    dm = DeadmanSwitch(fx, PAIRS, 120000, notes)
    pre = await _pre(s, db, fx, dm)
    assert not pre.ok and any("paper trading" in p for p in pre.problems)
    for i in range(14):
        db.upsert_daily_pnl(f"2026-09-{i + 1:02d}", "paper", start_equity=D(1), end_equity=D(1))
    assert (await _pre(s, db, fx, dm)).ok
    assert not (await _pre(s, db, fx, dm, tg=False)).ok
    assert not (await _pre(s, db, fx, dm, offset=900)).ok
    assert not (await _pre(s, db, fx, dm, offset=None)).ok
    fx.withdraw = True
    assert any("withdraw" in p for p in (await _pre(s, db, fx, dm)).problems)
    fx.withdraw = None
    assert any("dipastikan" in p for p in (await _pre(s, db, fx, dm)).problems)
    fx.withdraw = False
    fx.deadman_fail = True
    assert any("Deadman" in p for p in (await _pre(s, db, fx, dm)).problems)
    db.close()


async def test_preflight_demo_waives_paper_days(tmp_path):
    from agent.config import load_settings
    from tests.helpers import ROOT
    s = load_settings(ROOT / "config/settings.demo.yaml")
    db = Database(tmp_path / "d.db")
    fx, notes = FakeExchange(), RecordingNotifier()
    pre = await _pre(s, db, fx, DeadmanSwitch(fx, list(s.market.whitelist), 120000, notes))
    assert pre.ok and any("DEMO" in i for i in pre.info)
    db.close()


def test_demo_and_production_urls_cannot_be_mixed():
    from pydantic import ValidationError
    from agent.config import ExchangeSettings
    with pytest.raises(ValidationError):
        ExchangeSettings(environment="demo")                                     # production URLs
    with pytest.raises(ValidationError):
        ExchangeSettings(environment="production", tapi_url="https://demo-indodax.com/tapi")


async def test_clock_job_updates_trade_client_offset(live_env):
    make, db, md, clock, notes, fx, dm, s = live_env
    from agent.exchange.signing import Clock as SigClock
    fx.clock = SigClock()
    r = make()

    class Pub:
        async def clock_offset_ms(self):
            return -230

    await r.check_clock_job(Pub())
    assert fx.clock.offset_ms == -230


async def test_preflight_never_raises(tmp_path):
    s = settings(market={"whitelist": PAIRS})
    db = Database(tmp_path / "x.db")
    fx, notes = FakeExchange(), RecordingNotifier()

    async def boom():
        raise RuntimeError("unexpected")
    fx.permission_report = boom
    pre = await _pre(s, db, fx, DeadmanSwitch(fx, PAIRS, 120000, notes))
    assert not pre.ok and "preflight error" in pre.problems[0]
    db.close()
