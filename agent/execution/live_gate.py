"""Live-mode preflight. Every check must pass or the agent refuses to start live.

1. LIVE_CONFIRM phrase (enforced when secrets are loaded)
2. Telegram configured and connected (live start/alerts must reach the owner)
3. >= live_gate.min_paper_days completed paper days (waived on the demo account)
4. VPS clock within max_clock_offset_ms of the Indodax server
5. API key usable on legacy /tapi and proven WITHOUT withdraw permission
6. Deadman Switch heartbeat accepted
7. Initial reconciliation clean (no unknown agent orders, ledger <= exchange)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from agent.config import Settings
from agent.execution.deadman import DeadmanSwitch
from agent.execution.reconcile import reconcile
from agent.exchange.trade_client import LiveTradeClient
from agent.portfolio.portfolio import Portfolio
from agent.storage.db import Database


@dataclass
class Preflight:
    problems: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


async def live_preflight(settings: Settings, db: Database, client: LiveTradeClient, deadman: DeadmanSwitch,
                         portfolio: Portfolio, is_ours, clock_offset_ms: int | None,
                         telegram_connected: bool) -> Preflight:
    """Never raises: any unexpected error becomes a refusal reason."""
    try:
        return await _preflight(settings, db, client, deadman, portfolio, is_ours, clock_offset_ms,
                                telegram_connected)
    except Exception as e:  # noqa: BLE001
        return Preflight(problems=[f"preflight error: {type(e).__name__}: {e}"[:200]])


async def _preflight(settings, db, client, deadman, portfolio, is_ours, clock_offset_ms,
                     telegram_connected) -> Preflight:
    pf = Preflight()
    if not telegram_connected:
        pf.problems.append("Telegram belum tersambung (wajib untuk mode live)")

    need = settings.live_gate.min_paper_days
    if settings.exchange.environment == "demo":
        pf.info.append("akun DEMO: syarat hari paper trading tidak berlaku")
    else:
        days = db.paper_days_recorded()   # paper and live share the production journal
        if days < need:
            pf.problems.append(f"baru {days} hari paper trading tercatat (minimal {need})")
        else:
            pf.info.append(f"{days} hari paper trading tercatat")

    if clock_offset_ms is None:
        pf.problems.append("selisih jam dengan server Indodax tidak bisa diukur")
    elif abs(clock_offset_ms) > settings.exchange.max_clock_offset_ms:
        pf.problems.append(f"jam VPS selisih {clock_offset_ms} ms (maks {settings.exchange.max_clock_offset_ms})")

    rep = await client.permission_report()
    if not rep.legacy_ok:
        pf.problems.append("API key tidak bisa dipakai di /tapi (legacy)")
    if rep.withdraw_possible is True:
        pf.problems.append("API key PUNYA izin withdraw — buat key tanpa izin withdraw")
    elif rep.withdraw_possible is None:
        pf.problems.append("izin withdraw API key tidak bisa dipastikan")
    else:
        pf.info.append("API key tanpa izin withdraw ✓")

    if not await deadman.beat():
        pf.problems.append("Deadman Switch (countdownCancelAll) tidak merespons")
    else:
        pf.info.append(f"Deadman Switch aktif ({settings.deadman.countdown_ms // 1000} dtk)")

    pairs = list(dict.fromkeys([*settings.market.whitelist, *portfolio.positions]))
    rec = await reconcile(client, db, "live", portfolio, pairs, is_ours)
    pf.problems += [f"rekonsiliasi: {i}" for i in rec.issues]
    pf.info += [f"peringatan: {w}" for w in rec.warnings]
    if rec.exchange_free_idr is not None:
        cap = Decimal(str(settings.risk.agent_capital_idr))
        pf.info.append(f"IDR bebas di akun: {rec.exchange_free_idr:,.0f} (modal agent {cap:,.0f})")
        if rec.exchange_free_idr < cap:
            pf.info.append("IDR bebas < modal agent: ukuran order dibatasi saldo yang ada")
    return pf
