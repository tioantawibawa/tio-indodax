from datetime import timedelta
from decimal import Decimal as D

import pandas as pd
import pytest

from agent.data.market_data import PairMarket
from agent.reporting.daily_report import build_daily_report
from agent.reporting.telegram_bot import CommandRouter, RecordingNotifier, split_message
from agent.risk.risk_manager import AgentStatus
from agent.runner import AgentRunner
from agent.storage.db import Database
from tests.helpers import NOW, book, pair_info, settings, ticker
from tests.test_strategy import feat

PAIRS = ["btc_idr", "eth_idr", "sol_idr"]
CHAT = 12345


class FakeMD:
    def __init__(self):
        self.books = {p: book(p) for p in PAIRS}
        self.fail: set[str] = set()

    async def fetch(self, pair):
        if pair in self.fail:
            raise ConnectionError("boom")
        return PairMarket(pair, pair_info(pair), ticker(pair), self.books[pair], {"1D": pd.DataFrame()},
                          NOW.timestamp())


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    s = settings(market={"whitelist": PAIRS})
    db = Database(tmp_path / "r.db")
    md, clock, notes = FakeMD(), Clock(), RecordingNotifier()
    signals = {p: {"1D": feat()} for p in PAIRS}

    def make():
        r = AgentRunner(s, db, md, "paper", notifier=notes, now=clock)
        r.engine.features = lambda m: signals[m.pair]
        return r

    yield make, db, md, clock, notes, signals
    db.close()


def n_orders(db):
    return db._conn.execute("SELECT COUNT(*) FROM orders WHERE mode='paper'").fetchone()[0]


async def test_cycle_buys_journals_and_alerts(env):
    make, db, md, clock, notes, _ = env
    r = make()
    fills = await r.run_cycle()
    assert len(fills) == 3 and all(f.side == "buy" for f in fills)
    assert all(p.stop_loss == D("930000000") for p in r.pf.positions.values())
    assert sum("Order terisi" in m for m in notes.messages) == 3
    row = db.get_daily_pnl("2026-09-23", "paper")
    assert row["end_equity"] is not None and D(row["fees"]) > 0
    for p in r.pf.positions.values():   # every position within the 10% hard limit
        assert p.qty * p.avg_cost <= D(100_000) * D("1.004")


async def test_restart_rebuilds_state_and_never_double_orders(env):
    make, db, md, clock, notes, _ = env
    r1 = make()
    await r1.run_cycle()
    before = n_orders(db)
    positions = {k: (p.qty, p.avg_cost, p.stop_loss) for k, p in r1.pf.positions.items()}
    r2 = make()                       # "crash" + restart
    assert {k: (p.qty, p.avg_cost, p.stop_loss) for k, p in r2.pf.positions.items()} == positions
    assert r2.pf.cash_idr == r1.pf.cash_idr
    clock.t += timedelta(minutes=5)
    await r2.run_cycle()
    assert n_orders(db) == before     # already positioned -> no new orders


async def test_stop_loss_exit_alert_and_cooldown(env):
    make, db, md, clock, notes, signals = env
    r = make()
    await r.run_cycle()
    md.books["btc_idr"] = book("btc_idr", bid="920000000", ask="921000000")
    clock.t += timedelta(minutes=5)
    fills = await r.run_cycle()
    assert any(f.is_stop_loss and f.pair == "btc_idr" for f in fills)
    assert "btc_idr" not in r.pf.positions
    assert any("Stop-loss" in m for m in notes.messages)
    assert "btc_idr" in db.last_stoploss_times("paper")


async def test_trailing_stop_persists_across_restart(env):
    make, db, md, clock, notes, signals = env
    r = make()
    await r.run_cycle()
    signals["btc_idr"]["1D"] = feat(chandelier_stop=0.97e9, donchian_high=1.1e9)
    clock.t += timedelta(minutes=5)
    await r.run_cycle()
    assert r.pf.positions["btc_idr"].stop_loss == D("970000000")
    assert make().pf.positions["btc_idr"].stop_loss == D("970000000")


async def test_kill_switch_on_drawdown_halts_and_persists(env):
    make, db, md, clock, notes, _ = env
    db.set_state("paper:peak_equity", "1300000")
    r = make()
    await r.run_cycle()
    assert r.status == AgentStatus.HALTED and not r.pf.positions
    assert any("KILL SWITCH" in m for m in notes.messages)
    assert make().status == AgentStatus.HALTED        # survives restart
    reply = await r.resume()
    assert r.status == AgentStatus.RUNNING and "di-reset" in reply


async def test_daily_loss_pauses_until_next_day(env):
    make, db, md, clock, notes, _ = env
    db.upsert_daily_pnl("2026-09-23", "paper", start_equity=D(1_040_000))
    r = make()
    await r.run_cycle()
    assert r.status == AgentStatus.PAUSED and r.pause_reason == "daily_loss:2026-09-23"
    assert not r.pf.positions
    clock.t = NOW + timedelta(hours=20)                    # 01:00 WIB next day
    await r.run_cycle()
    assert r.status == AgentStatus.RUNNING and r.pf.positions


async def test_repeated_errors_alert_once(env):
    make, db, md, clock, notes, _ = env
    r = make()
    await r.run_cycle()
    md.fail.add("btc_idr")               # held pair without data -> cycle fails
    for _ in range(4):
        await r.run_cycle()
    assert sum("Error berulang" in m for m in notes.messages) == 1
    assert r.consecutive_errors == 4
    md.fail.clear()
    await r.run_cycle()
    assert r.consecutive_errors == 0


async def test_clock_offset_pauses_on_startup_and_auto_resumes(env):
    make, db, md, clock, notes, _ = env
    r = make()
    await r.startup(clock_offset_ms=1500)
    assert r.status == AgentStatus.PAUSED and r.pause_reason == "clock"
    assert "Agent start" in notes.messages[-1] and "chrony" in notes.messages[-1]

    class Pub:
        def __init__(self, v):
            self.v = v

        async def clock_offset_ms(self):
            return self.v

    await r.check_clock_job(Pub(120))
    assert r.status == AgentStatus.RUNNING and "normal" in notes.messages[-1]
    await r.check_clock_job(Pub(-900))
    assert r.status == AgentStatus.PAUSED
    await r.pause()                               # manual pause is never auto-resumed by the clock
    await r.check_clock_job(Pub(10))
    assert r.status == AgentStatus.PAUSED and r.pause_reason == "manual"


async def test_router_commands_and_chat_filter(env):
    make, db, md, clock, notes, _ = env
    r = make()
    await r.run_cycle()
    router = CommandRouter(r, CHAT)
    assert await router.handle(999, "/status") is None                  # other chats ignored
    assert await router.handle(999, "/kill CONFIRM") is None
    assert r.status == AgentStatus.RUNNING
    assert "Status" in await router.handle(CHAT, "/status")
    assert "btc_idr" in await router.handle(CHAT, "/positions")
    assert "Pandangan pasar" in await router.handle(CHAT, "/report")
    warn = await router.handle(CHAT, "/kill")
    assert "CONFIRM" in warn and r.status == AgentStatus.RUNNING       # needs confirmation
    assert "PAUSED" in await router.handle(CHAT, "/pause")
    assert r.status == AgentStatus.PAUSED
    assert "HALTED" in await router.handle(CHAT, "/kill CONFIRM")
    assert r.status == AgentStatus.HALTED and r.pf.positions             # positions kept with stops
    assert "RUNNING" in await router.handle(CHAT, "/resume@mybot")
    assert "/status" in await router.handle(CHAT, "/help")
    assert "tidak dikenal" in await router.handle(CHAT, "/foo")


async def test_daily_report_has_every_section(env):
    make, db, md, clock, notes, _ = env
    r = make()
    await r.run_cycle()
    md.books["btc_idr"] = book("btc_idr", bid="920000000", ask="921000000")
    clock.t += timedelta(minutes=5)
    await r.run_cycle()
    db.record_error("exchange", "timeout <b>x</b>")
    await r.send_daily_report()
    text = notes.messages[-1]
    for section in ("Laporan harian", "Mode <b>PAPER</b>", "Uptime", "Awal hari", "PnL realized",
                    "Drawdown dari puncak", "Transaksi hari ini (4)", "Posisi terbuka (2)", "SL ",
                    "Proposal", "di-veto", "Total fee", "Error/anomali (1)", "Pandangan pasar"):
        assert section in text, section
    assert "&lt;b&gt;x&lt;/b&gt;" in text        # dynamic text is HTML-escaped


def test_split_message_respects_limit():
    text = "\n".join(f"baris {i} " + "x" * 50 for i in range(300)) + "\n" + "y" * 9000
    chunks = split_message(text)
    assert all(len(c) <= 3900 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_report_builder_empty_day():
    from agent.reporting.daily_report import ReportData
    rd = ReportData(mode="paper", status="RUNNING", pause_reason=None, uptime=timedelta(hours=3),
                    now_local=NOW, day_start_equity=D(500000), equity=D(500000), peak_equity=D(500000),
                    realized_today=D(0), fees_today=D(0), fills=[], positions=[], proposals=0, approved=0,
                    vetoed=0, top_veto_reasons=[], errors=[], outlook="BTC: sideways.")
    t = build_daily_report(rd)
    assert "Rp 500.000" in t and "— tidak ada" in t


async def test_main_refuses_live_with_world_readable_env(tmp_path, monkeypatch):
    from agent import main as m
    for k in ("MODE", "LIVE_CONFIRM", "INDODAX_API_KEY", "INDODAX_API_SECRET", "AGENT_SETTINGS"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("MODE=live\nLIVE_CONFIRM=I_UNDERSTAND_THE_RISK\nINDODAX_API_KEY=k1234567\nINDODAX_API_SECRET=s1234567\n")
    env.chmod(0o644)
    monkeypatch.chdir(tmp_path)
    import shutil
    from tests.helpers import ROOT
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config/settings.yaml", tmp_path / "config/settings.yaml")
    assert await m.run(str(env)) == 3


async def test_go_live_review_sends_status_on_date_and_repeats_until_ready(env):
    from datetime import date, datetime, timezone
    make, db, md, clock, notes, _ = env
    r = make()
    r.s = r.s.model_copy(update={"reporting": r.s.reporting.model_copy(
        update={"go_live_review_date": date(2026, 10, 7), "go_live_review_time": "09:00"})})
    await r.update_clock(30)
    clock.t = datetime(2026, 10, 7, 1, 59, tzinfo=timezone.utc)          # 08:59 WIB
    assert not await r.go_live_review()
    clock.t = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)           # 09:00 WIB
    assert await r.go_live_review()                                      # not ready: 0 paper days
    msg = "\n".join(notes.messages[-2:])
    assert "Review go-live" in msg and "belum" in msg and "<b>Status</b>" in msg and "❌ hari paper" in msg
    clock.t = datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc)
    assert not await r.go_live_review()                                  # once per day
    for i in range(14):
        db.upsert_daily_pnl(f"2026-09-{23 + i:02d}" if 23 + i <= 30 else f"2026-10-{i - 7:02d}", "paper",
                            start_equity=D(500000), end_equity=D(500000))
    clock.t = datetime(2026, 10, 8, 2, 5, tzinfo=timezone.utc)
    n = len(notes.messages)
    assert await r.go_live_review()
    msg = "\n".join(notes.messages[n:])
    assert "semua syarat otomatis terpenuhi" in msg and "❌" not in msg and "Langkah berikut" in msg
    clock.t = datetime(2026, 10, 9, 2, 5, tzinfo=timezone.utc)
    assert not await r.go_live_review()                                  # done: never again


async def test_go_live_review_waits_for_telegram(env):
    from datetime import date, datetime, timezone
    make, db, md, clock, notes, _ = env
    r = make()
    r.s = r.s.model_copy(update={"reporting": r.s.reporting.model_copy(
        update={"go_live_review_date": date(2026, 10, 7)})})
    notes.available = False
    clock.t = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
    assert not await r.go_live_review()
    notes.available = True
    assert await r.go_live_review()
