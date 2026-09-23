"""Entry point: ``python -m agent.main``.

MODE=paper  -> live market data, simulated orders, Telegram, daily report.
MODE=live   -> real orders on Indodax, after every preflight check passes
               (agent/execution/live_gate.py); Deadman Switch heartbeat.
MODE=backtest -> use scripts/run_backtest.py.
Settings file: AGENT_SETTINGS in .env (config/settings.demo.yaml = demo account).
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import structlog

from agent.config import Mode, env_file_permission_problem, load_secrets, load_settings
from agent.data.market_data import MarketDataService
from agent.exchange.public_client import IndodaxPublicClient
from agent.logging_setup import configure_logging
from agent.reporting.telegram_bot import CommandRouter, NullNotifier, TelegramLink, build_application, esc
from agent.runner import AgentRunner
from agent.storage.db import Database

log = structlog.get_logger(__name__)


async def private_api_notes(secrets, settings) -> list[str]:
    """Optional read-only check of the configured API key (never fatal in paper mode)."""
    key, sec = secrets.INDODAX_API_KEY.get_secret_value(), secrets.INDODAX_API_SECRET.get_secret_value()
    if not (key and sec):
        return ["INDODAX_API_KEY/SECRET belum diisi — paper mode tetap jalan tanpa data akun."]
    from agent.exchange.private_client import PrivateReadOnlyClient
    notes = []
    try:
        async with PrivateReadOnlyClient(
            key, sec, tapi_url=settings.exchange.tapi_url, v2_base_url=settings.exchange.tapi_v2_base_url,
            v2_api_key=secrets.INDODAX_V2_API_KEY.get_secret_value() or None,
            v2_secret=secrets.INDODAX_V2_API_SECRET.get_secret_value() or None,
            recv_window_ms=settings.exchange.recv_window_ms,
        ) as pc:
            rep = await pc.permission_report()
        notes += rep.notes
        if rep.withdraw_possible:
            notes.append("‼️ API key punya izin WITHDRAW. Mode live akan DITOLAK. Buat key tanpa izin withdraw.")
    except Exception as e:  # noqa: BLE001
        notes.append(f"Cek API private gagal: {type(e).__name__}")
    return notes


async def run(env_file: str = ".env", settings_file: str | None = None) -> int:
    secrets = load_secrets(env_file)
    settings = load_settings(settings_file or secrets.AGENT_SETTINGS)
    configure_logging(settings.logging.level, settings.logging.dir, secrets.all_secret_values())
    perm = env_file_permission_problem(env_file)
    if perm:
        log.warning("env_permissions", problem=perm)
    live = secrets.MODE == Mode.LIVE
    if live and perm:
        print(f"MODE=live ditolak: {perm}", file=sys.stderr)
        return 3
    if secrets.MODE == Mode.BACKTEST:
        print("MODE=backtest: jalankan `python -m scripts.run_backtest`.", file=sys.stderr)
        return 2

    db = Database(settings.storage.db_path)
    public = IndodaxPublicClient.from_settings(settings.exchange)
    md = MarketDataService(public, settings.strategy.timeframes, settings.strategy.lookback_bars)

    llm = None
    if secrets.LLM_ENABLED:
        from agent.analysis.llm_analyst import LLMAnalyst
        llm = LLMAnalyst(settings.llm, secrets.ANTHROPIC_API_KEY.get_secret_value())

    token, chat_id = secrets.TELEGRAM_BOT_TOKEN.get_secret_value(), secrets.TELEGRAM_CHAT_ID
    link = None
    notifier = NullNotifier()
    runner_holder: dict = {}
    if token and chat_id:
        router = CommandRouter(_Lazy(runner_holder), int(chat_id))
        link = TelegramLink(build_application(token, router), int(chat_id))
        notifier = link.notifier
    else:
        log.warning("telegram_disabled", reason="TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")

    trade_client = deadman = None
    if live:
        from agent.execution.deadman import DeadmanSwitch
        from agent.exchange.trade_client import LiveTradeClient
        trade_client = LiveTradeClient(
            secrets.INDODAX_API_KEY.get_secret_value(), secrets.INDODAX_API_SECRET.get_secret_value(),
            tapi_url=settings.exchange.tapi_url, v2_base_url=settings.exchange.tapi_v2_base_url,
            recv_window_ms=settings.exchange.recv_window_ms)
        deadman = DeadmanSwitch(trade_client, list(settings.market.whitelist), settings.deadman.countdown_ms,
                                notifier)
    runner = AgentRunner(settings, db, md, mode="live" if live else "paper", notifier=notifier, llm=llm,
                         trade_client=trade_client, deadman=deadman)
    runner_holder["runner"] = runner

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        offset = await public.clock_offset_ms()
    except Exception as e:  # noqa: BLE001
        offset = None
        db.record_error("startup", f"clock check failed: {type(e).__name__}")
    if trade_client is not None:
        trade_client.clock.offset_ms = offset or 0
    if link is not None:
        await link.ensure_started()

    if live:
        from agent.execution.live_gate import live_preflight
        pre = await live_preflight(settings, db, trade_client, deadman, runner.pf, runner.broker.is_ours,
                                   offset, link is not None and link.connected)
        if not pre.ok:
            text = "⛔ <b>Mode LIVE ditolak</b>:\n" + "\n".join(f"• {esc(p)}" for p in pre.problems)
            await notifier.send(text)
            log.error("live_preflight_failed", problems=pre.problems)
            print("MODE=live ditolak:\n- " + "\n- ".join(pre.problems), file=sys.stderr)
            await _shutdown(None, link, public, db, trade_client)
            return 3
        notes = pre.info
    else:
        notes = await private_api_notes(secrets, settings)
    await runner.startup(offset, notes)

    sched = build_scheduler(settings, runner, public, link, deadman)
    sched.start()
    await runner.run_cycle()          # first cycle immediately
    log.info("agent_running", mode=runner.mode, environment=settings.exchange.environment)
    await stop.wait()

    log.info("agent_stopping")
    if live:
        # cancel our resting orders now; the deadman timer would do it within countdown_ms anyway
        try:
            n = await runner.broker.cancel_all(runner.now())
            await notifier.send(f"⏹️ Agent berhenti — {n} order agent dibatalkan. Posisi tetap dipegang; "
                                "stop-loss TIDAK dipantau selama agent mati.")
        except Exception as e:  # noqa: BLE001
            log.warning("shutdown_cancel_failed", error=str(e)[:200])
    await _shutdown(sched, link, public, db, trade_client)
    return 0


async def _shutdown(sched, link, public, db, trade_client) -> None:
    if sched is not None:
        sched.shutdown(wait=False)
    if link is not None:
        await link.stop()
    if trade_client is not None:
        await trade_client.aclose()
    await public.aclose()
    db.close()


def build_scheduler(settings, runner, public, link=None, deadman=None):
    """Jobs: trading cycle, clock check, Telegram reconnect, deadman heartbeat, daily report."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    tz = settings.reporting.timezone
    hh, mm = settings.reporting.daily_report_time.split(":")
    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(runner.run_cycle, "interval", minutes=settings.cycle.interval_minutes,
                  id="cycle", max_instances=1, coalesce=True)
    sched.add_job(runner.check_clock_job, "interval", minutes=30, args=[public], id="clock",
                  max_instances=1, coalesce=True)
    if link is not None:
        sched.add_job(link.ensure_started, "interval", minutes=5, id="telegram", max_instances=1, coalesce=True)
    if deadman is not None:
        sched.add_job(deadman.beat, "interval", seconds=settings.deadman.heartbeat_s, id="deadman",
                      max_instances=1, coalesce=True)
    sched.add_job(runner.send_daily_report, CronTrigger(hour=int(hh), minute=int(mm), timezone=tz),
                  id="daily_report")
    return sched


class _Lazy:
    """Forwards attribute access to the runner once it exists (router is built first)."""

    def __init__(self, holder: dict):
        self._h = holder

    def __getattr__(self, name):
        return getattr(self._h["runner"], name)


def main() -> None:
    Path("data").mkdir(exist_ok=True)
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
