"""Deadman Switch heartbeat (POST /tapi/countdownCancelAll).

Every ``heartbeat_s`` the countdown is renewed; if the agent dies or loses
connectivity, Indodax cancels ALL open orders on the listed pairs after
``countdown_ms``. Consecutive failures trip ``healthy = False`` (the runner
then stops new entries) and alert once; recovery is announced.
"""

from __future__ import annotations

import structlog

from agent.exchange.trade_client import LiveTradeClient
from agent.reporting.telegram_bot import Notifier, esc

log = structlog.get_logger(__name__)


class DeadmanSwitch:
    def __init__(self, client: LiveTradeClient, pairs: list[str], countdown_ms: int, notifier: Notifier,
                 fail_threshold: int = 3):
        self.client, self.pairs, self.countdown_ms = client, list(pairs), countdown_ms
        self.notifier, self.fail_threshold = notifier, fail_threshold
        self.failures = 0
        self.last_ok = False

    @property
    def healthy(self) -> bool:
        return self.failures < self.fail_threshold

    async def beat(self) -> bool:
        try:
            await self.client.countdown_cancel_all(self.pairs, self.countdown_ms)
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            log.warning("deadman_heartbeat_failed", failures=self.failures, error=f"{type(e).__name__}: {e}"[:200])
            if self.failures == self.fail_threshold:
                await self.notifier.send(
                    f"🚨 <b>Deadman Switch gagal</b> {self.failures}× berturut-turut "
                    f"({esc(type(e).__name__)}). Entry baru dihentikan sampai pulih.")
            self.last_ok = False
            return False
        if self.failures >= self.fail_threshold:
            await self.notifier.send("✅ Deadman Switch pulih — entry dibuka kembali.")
        self.failures = 0
        self.last_ok = True
        return True
