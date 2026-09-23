"""Reconciliation: exchange state vs the local ledger/DB (live mode, every cycle).

Mismatches that block trading (reconciliation_ok = False):
- the exchange holds LESS of an asset than the agent's ledger says it owns
  (coins sold/withdrawn outside the agent, or a fill recorded twice);
- an order with the agent's client_order_id prefix is open on the exchange
  but unknown or closed in the DB (possible double order).

The account may hold MORE coins/IDR than the agent (the owner's own funds):
that is expected and ignored. Non-agent open orders on whitelisted pairs are
reported as warnings because the Deadman Switch would cancel them too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from agent.exchange.errors import IndodaxError
from agent.exchange.pairs import base_asset
from agent.exchange.trade_client import LiveTradeClient
from agent.portfolio.portfolio import Portfolio
from agent.storage.db import Database

DUST = Decimal("0.00000010")


@dataclass
class ReconResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    exchange_free_idr: Decimal | None = None


async def reconcile(client: LiveTradeClient, db: Database, mode: str, pf: Portfolio, pairs: list[str],
                    is_ours) -> ReconResult:
    res = ReconResult(ok=True)
    try:
        bal, _ = await client.balances_legacy()
    except IndodaxError as e:
        return ReconResult(ok=False, issues=[f"balances unavailable: {type(e).__name__}: {e}"[:200]])
    res.exchange_free_idr = bal.free_of("idr")
    for pair, pos in pf.positions.items():
        asset = base_asset(pair)
        total = bal.free_of(asset) + bal.locked.get(asset, Decimal(0))
        if total + DUST < pos.qty:
            res.issues.append(f"{asset.upper()}: exchange {total} < ledger {pos.qty}")
    db_open = {o["client_order_id"] for o in db.open_orders(mode)}
    for pair in pairs:
        try:
            orders = await client.open_orders(pair)
        except IndodaxError as e:
            res.issues.append(f"open orders {pair} unavailable: {type(e).__name__}")
            continue
        for o in orders:
            if is_ours(o.client_order_id):
                if o.client_order_id not in db_open:
                    res.issues.append(f"agent order {o.client_order_id} open on exchange but not open in DB")
            else:
                res.warnings.append(f"non-agent order {o.order_id} on {pair} (deadman switch would cancel it)")
    res.ok = not res.issues
    return res
