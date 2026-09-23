from datetime import timedelta
from decimal import Decimal as D

import pandas as pd
import pytest

from agent.analysis.regime import Regime
from agent.data.market_data import PairMarket
from agent.engine import DecisionEngine
from agent.portfolio.portfolio import Portfolio
from agent.risk.risk_manager import AgentStatus, Verdict
from agent.storage.db import Database
from tests.helpers import NOW, book, pair_info, settings, ticker
from tests.test_strategy import feat

PAIRS = ["btc_idr", "eth_idr", "sol_idr", "xrp_idr", "btc_usdt"]


def market(pair):
    return PairMarket(pair=pair, info=pair_info(pair), ticker=ticker(pair), orderbook=book(pair),
                      candles={"15": pd.DataFrame(), "60": pd.DataFrame(), "240": pd.DataFrame()},
                      fetched_at=NOW.timestamp())


@pytest.fixture
def env(tmp_path):
    s = settings()
    db = Database(tmp_path / "e.db")
    eng = DecisionEngine(s, db, "paper")
    signals = {p: {"15": feat("15"), "60": feat("60"), "240": feat("240")} for p in PAIRS}
    eng.features = lambda m: signals[m.pair]  # controlled indicator output
    yield eng, db, signals
    db.close()


async def test_cycle_enforces_max_positions_within_one_cycle(env):
    eng, db, _ = env
    pf = Portfolio(D(1_000_000))
    res = await eng.run_cycle({p: market(p) for p in PAIRS}, pf, NOW, AgentStatus.RUNNING)
    verdicts = [d.verdict for _, d in res.decisions]
    assert verdicts.count(Verdict.RESIZE) == 3          # 3 entries, each capped to 10%
    assert verdicts.count(Verdict.VETO) == 2            # 4th and 5th: max open positions
    for _, d in res.approved:
        assert d.qty * d.price <= D(100_000)
    assert len(db.decisions_between(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))) == 5


async def test_stop_loss_exit_runs_before_entries_and_blocks_reentry(env):
    eng, _, _ = env
    pf = Portfolio(D(1_000_000))
    pf.apply_fill("btc_idr", "buy", D("0.0001"), D("1010000000"), D(0), NOW, D("1000000000"), D("1100000000"))
    res = await eng.run_cycle({p: market(p) for p in PAIRS}, pf, NOW, AgentStatus.RUNNING)
    first = res.decisions[0][1]
    assert first.proposal.pair == "btc_idr" and first.proposal.is_emergency_exit and first.approved
    assert "btc_idr" in res.notes  # no re-entry in the same cycle


async def test_paused_only_journals_vetoes(env):
    eng, _, _ = env
    res = await eng.run_cycle({p: market(p) for p in PAIRS}, Portfolio(D(1_000_000)), NOW, AgentStatus.PAUSED)
    assert res.decisions and all(d.verdict == Verdict.VETO for _, d in res.decisions)


async def test_drawdown_triggers_halt_flag(env):
    eng, db, _ = env
    db.set_state("paper:peak_equity", "1300000")
    res = await eng.run_cycle({p: market(p) for p in PAIRS}, Portfolio(D(1_000_000)), NOW, AgentStatus.RUNNING)
    assert res.trigger_halt and not res.approved


async def test_no_signal_notes(env):
    eng, _, signals = env
    for p in PAIRS:
        signals[p]["240"] = feat("240", Regime.TRENDING_DOWN)
    res = await eng.run_cycle({p: market(p) for p in PAIRS}, Portfolio(D(1_000_000)), NOW, AgentStatus.RUNNING)
    assert res.decisions == [] and all("trend TF" in n for n in res.notes.values())


async def test_llm_veto_is_journaled_and_llm_failure_is_harmless(env):
    from agent.analysis.llm_analyst import LLMOpinion
    eng, db, _ = env

    class Bear:
        async def analyze(self, summary, proposal):
            assert summary["pair"] == proposal.pair and "timeframes" in summary
            return LLMOpinion(bias="bearish", confidence=0.9, reasoning="r", risks=[])

    class Broken:
        async def analyze(self, summary, proposal):
            return None  # what LLMAnalyst returns on timeout/error

    eng.llm = Bear()
    res = await eng.run_cycle({"btc_idr": market("btc_idr")}, Portfolio(D(1_000_000)), NOW, AgentStatus.RUNNING)
    assert [d.verdict for _, d in res.decisions] == [Verdict.VETO]
    assert "LLM veto" in res.decisions[0][1].reasons[0]
    eng.llm = Broken()
    res = await eng.run_cycle({"btc_idr": market("btc_idr")}, Portfolio(D(1_000_000)), NOW, AgentStatus.RUNNING)
    assert res.approved


async def test_missing_market_for_open_position_raises(env):
    eng, _, _ = env
    pf = Portfolio(D(1_000_000))
    pf.apply_fill("eth_idr", "buy", D("1"), D("1000"), D(0), NOW)
    with pytest.raises(ValueError):
        await eng.run_cycle({"btc_idr": market("btc_idr")}, pf, NOW, AgentStatus.RUNNING)


async def test_equity_tracker_day_start_uses_jakarta_day(env):
    eng, db, _ = env
    pf = Portfolio(D(1_000_000))
    await eng.run_cycle({}, pf, NOW, AgentStatus.RUNNING)       # 12:00 WIB 23 Sep
    assert db.get_daily_pnl("2026-09-23", "paper")["start_equity"] == "1000000"
    later = NOW + timedelta(hours=13)                            # 01:00 WIB 24 Sep
    await eng.run_cycle({}, pf, later, AgentStatus.RUNNING)
    assert db.get_daily_pnl("2026-09-24", "paper") is not None
