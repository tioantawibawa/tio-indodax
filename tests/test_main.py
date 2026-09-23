

from agent.main import build_scheduler
from agent.reporting.telegram_bot import TelegramLink
from tests.helpers import settings


class Dummy:
    mode = "paper"

    async def run_cycle(self): ...
    async def go_live_review(self): ...
    async def check_clock_job(self, public): ...
    async def send_daily_report(self): ...


async def test_scheduler_jobs_are_active():
    s = settings()
    link = type("L", (), {"ensure_started": lambda self: None})()
    sched = build_scheduler(s, Dummy(), object(), link)
    sched.start()
    try:
        jobs = {j.id: j for j in sched.get_jobs()}
        assert set(jobs) == {"cycle", "clock", "telegram", "daily_report", "go_live_review"}
        assert all(j.next_run_time is not None for j in jobs.values())   # none paused
        rep = jobs["daily_report"].trigger
        assert str(rep.timezone) == "Asia/Jakarta" and "hour='21'" in str(rep)
    finally:
        sched.shutdown(wait=False)


def test_go_live_review_job_only_in_paper_mode():
    live = Dummy()
    live.mode = "live"
    sched = build_scheduler(settings(), live, object())
    assert "go_live_review" not in {j.id for j in sched.get_jobs()}


class FakeApp:
    def __init__(self, exc=None):
        self.exc, self.bot = exc, object()
        self.updater = self
        self.started = 0

    async def initialize(self):
        self.started += 1
        if self.exc:
            raise self.exc

    async def start(self): ...
    async def start_polling(self, **kw): ...
    async def stop(self): ...
    async def shutdown(self): ...


async def test_telegram_outage_is_not_fatal_and_retries():
    app = FakeApp(ConnectionError("no route"))
    link = TelegramLink(app, 1)
    assert await link.ensure_started() is False
    await link.notifier.send("hello")                # logged, not raised
    app.exc = None
    assert await link.ensure_started() is True
    assert app.started == 2


async def test_invalid_token_not_retried():
    from telegram.error import InvalidToken
    app = FakeApp(InvalidToken())
    link = TelegramLink(app, 1)
    assert await link.ensure_started() is False
    assert await link.ensure_started() is False
    assert app.started == 1 and link.invalid_token
