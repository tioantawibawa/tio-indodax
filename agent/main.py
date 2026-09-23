"""Entry point: ``python -m agent.main``.

MODE=paper  -> live market data, simulated orders, Telegram, daily report.
MODE=live   -> refused until Phase 5 (live executor + deadman switch).
MODE=backtest -> use scripts/run_backtest.py.
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
from agent.reporting.telegram_bot import CommandRouter, NullNotifier, TelegramNotifier, build_application
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


async def run(env_file: str = ".env", settings_file: str = "config/settings.yaml") -> int:
    settings = load_settings(settings_file)
    secrets = load_secrets(env_file)
    configure_logging(settings.logging.level, settings.logging.dir, secrets.all_secret_values())
    perm = env_file_permission_problem(env_file)
    if perm:
        log.warning("env_permissions", problem=perm)
    if secrets.MODE == Mode.LIVE:
        log.error("live_mode_not_available", reason="Phase 5 (live executor, deadman switch) not implemented")
        print("MODE=live belum tersedia (Fase 5). Gunakan MODE=paper.", file=sys.stderr)
        return 2
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
    app = None
    notifier = NullNotifier()
    if token and chat_id:
        runner_holder: dict = {}
        router = CommandRouter(_Lazy(runner_holder), int(chat_id))
        app = build_application(token, router)
        notifier = TelegramNotifier(app.bot, int(chat_id))
    else:
        log.warning("telegram_disabled", reason="TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")

    runner = AgentRunner(settings, db, md, mode="paper", notifier=notifier, llm=llm)
    if app is not None:
        runner_holder["runner"] = runner

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        offset = await public.clock_offset_ms()
    except Exception as e:  # noqa: BLE001
        offset = None
        db.record_error("startup", f"clock check failed: {type(e).__name__}")
    if app is not None:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
    await runner.startup(offset, await private_api_notes(secrets, settings))

    hh, mm = settings.reporting.daily_report_time.split(":")
    sched = AsyncIOScheduler(timezone=settings.reporting.timezone)
    sched.add_job(runner.run_cycle, "interval", minutes=settings.cycle.interval_minutes,
                  max_instances=1, coalesce=True, next_run_time=None)
    sched.add_job(runner.check_clock_job, "interval", minutes=30, args=[public], max_instances=1, coalesce=True)
    sched.add_job(runner.send_daily_report, CronTrigger(hour=int(hh), minute=int(mm),
                                                        timezone=settings.reporting.timezone))
    sched.start()
    await runner.run_cycle()          # first cycle immediately
    log.info("agent_running", mode="paper")
    await stop.wait()

    log.info("agent_stopping")
    sched.shutdown(wait=False)
    if app is not None:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    await public.aclose()
    db.close()
    return 0


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
