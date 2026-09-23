"""Crash-safe portfolio state.

The fills table is the source of truth: on restart the ledger is rebuilt by
replaying every fill of the mode in order. Stop levels (which trail over
time) are stored in the ``state`` table and re-applied.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from agent.storage.db import Database

from .portfolio import Portfolio


def _stops_key(mode: str) -> str:
    return f"{mode}:stops"


def save_stops(db: Database, mode: str, pf: Portfolio) -> None:
    db.set_state(_stops_key(mode), {k: str(p.stop_loss) for k, p in pf.positions.items()
                                    if p.stop_loss is not None})


def rebuild_portfolio(db: Database, mode: str, capital_idr: Decimal) -> Portfolio:
    pf = Portfolio(capital_idr)
    rows = db._conn.execute("SELECT * FROM fills WHERE mode = ? ORDER BY ts, id", (mode,)).fetchall()
    for r in rows:
        pf.apply_fill(r["pair"], r["side"], Decimal(r["qty"]), Decimal(r["price"]), Decimal(r["fee_idr"]),
                      datetime.fromisoformat(r["ts"]), strict=False)
    for pair, stop in (db.get_state(_stops_key(mode), {}) or {}).items():
        if pair in pf.positions:
            pf.raise_stop(pair, Decimal(stop))
    return pf
