"""SQLite storage: audit journal and durable agent state.

Every risk decision (including VETOs) is written to ``decisions`` with its
reasons. Decimals are stored as TEXT to avoid float rounding. Timestamps are
ISO-8601 UTC strings.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    intent TEXT NOT NULL,
    order_type TEXT NOT NULL,
    setup TEXT,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    stop_loss TEXT,
    take_profit TEXT,
    confidence REAL,
    proposal_reason TEXT,
    llm_json TEXT,
    verdict TEXT NOT NULL,
    approved_qty TEXT,
    approved_price TEXT,
    reasons_json TEXT NOT NULL,
    metrics_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT,
    decision_id INTEGER REFERENCES decisions(id),
    ts_created TEXT NOT NULL,
    ts_updated TEXT NOT NULL,
    mode TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_qty TEXT NOT NULL DEFAULT '0',
    is_emergency INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_orders_ts ON orders(ts_created);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL UNIQUE,
    client_order_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    fee_idr TEXT NOT NULL,
    realized_pnl TEXT
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT NOT NULL,
    mode TEXT NOT NULL,
    start_equity TEXT NOT NULL,
    end_equity TEXT,
    realized TEXT,
    unrealized TEXT,
    fees TEXT,
    peak_equity TEXT,
    PRIMARY KEY (date, mode)
);

CREATE TABLE IF NOT EXISTS stoploss_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    pair TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts TEXT NOT NULL
);
"""

# Orders that count toward the hourly/daily order limits: anything actually
# submitted (or about to be). Rejected-before-submit rows do not count.
COUNTED_STATUSES = ("PENDING_SUBMIT", "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELLED")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; use timezone-aware UTC")
    return dt.astimezone(timezone.utc).isoformat()


def _s(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def _json(v: Any) -> str:
    return json.dumps(v, default=str, sort_keys=True)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------ decisions

    def record_decision(self, decision, mode: str, llm: dict | None = None,
                        ts: datetime | None = None) -> int:
        """Persist a RiskDecision (any verdict). Returns the row id."""
        p = decision.proposal
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO decisions (ts, mode, proposal_id, pair, side, intent, order_type, setup,
                   price, qty, stop_loss, take_profit, confidence, proposal_reason, llm_json, verdict,
                   approved_qty, approved_price, reasons_json, metrics_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_iso(ts or datetime.now(timezone.utc)), mode, p.proposal_id, p.pair, p.side, p.intent,
                 p.order_type, p.setup, str(p.price), str(p.qty), _s(p.stop_loss), _s(p.take_profit),
                 p.confidence, p.reason, _json(llm) if llm else None, decision.verdict.value,
                 str(decision.qty), str(decision.price), _json(list(decision.reasons)),
                 _json(decision.metrics)),
            )
            return int(cur.lastrowid)

    def decisions_between(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM decisions WHERE ts >= ? AND ts < ? ORDER BY id", (_iso(start), _iso(end))))

    # --------------------------------------------------------------- orders

    def record_order(self, *, client_order_id: str, decision_id: int | None, mode: str, pair: str,
                     side: str, order_type: str, price: Decimal, qty: Decimal,
                     status: str = "PENDING_SUBMIT", is_emergency: bool = False,
                     ts: datetime | None = None) -> int:
        now = _iso(ts or datetime.now(timezone.utc))
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO orders (client_order_id, decision_id, ts_created, ts_updated, mode, pair,
                   side, order_type, price, qty, status, is_emergency) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (client_order_id, decision_id, now, now, mode, pair, side, order_type, str(price),
                 str(qty), status, int(is_emergency)),
            )
            return int(cur.lastrowid)

    def update_order(self, client_order_id: str, *, status: str | None = None,
                     exchange_order_id: str | None = None, filled_qty: Decimal | None = None,
                     ts: datetime | None = None) -> None:
        sets, vals = ["ts_updated = ?"], [_iso(ts or datetime.now(timezone.utc))]
        if status is not None:
            sets.append("status = ?"); vals.append(status)
        if exchange_order_id is not None:
            sets.append("exchange_order_id = ?"); vals.append(exchange_order_id)
        if filled_qty is not None:
            sets.append("filled_qty = ?"); vals.append(str(filled_qty))
        with self.tx() as c:
            c.execute(f"UPDATE orders SET {', '.join(sets)} WHERE client_order_id = ?",
                      (*vals, client_order_id))

    def get_order(self, client_order_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM orders WHERE client_order_id = ?",
                                  (client_order_id,)).fetchone()

    def open_orders(self, mode: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM orders WHERE mode = ? AND status IN ('PENDING_SUBMIT','NEW','PARTIALLY_FILLED')",
            (mode,)))

    def count_orders_since(self, since: datetime, mode: str, include_emergency: bool = False) -> int:
        q = (f"SELECT COUNT(*) FROM orders WHERE mode = ? AND ts_created >= ? "
             f"AND status IN ({','.join('?' * len(COUNTED_STATUSES))})")
        args: list[Any] = [mode, _iso(since), *COUNTED_STATUSES]
        if not include_emergency:
            q += " AND is_emergency = 0"
        return int(self._conn.execute(q, args).fetchone()[0])

    # ---------------------------------------------------------------- fills

    def record_fill(self, *, trade_id: str, client_order_id: str, ts: datetime, mode: str, pair: str,
                    side: str, price: Decimal, qty: Decimal, fee_idr: Decimal,
                    realized_pnl: Decimal | None = None) -> bool:
        """Idempotent on trade_id. Returns False if the fill was already recorded."""
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO fills (trade_id, client_order_id, ts, mode, pair, side, price, qty,
                   fee_idr, realized_pnl) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (trade_id, client_order_id, _iso(ts), mode, pair, side, str(price), str(qty),
                 str(fee_idr), _s(realized_pnl)),
            )
            return cur.rowcount == 1

    def fills_between(self, start: datetime, end: datetime, mode: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM fills WHERE mode = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (mode, _iso(start), _iso(end))))

    # ------------------------------------------------------------ stop-loss

    def record_stoploss(self, pair: str, mode: str, ts: datetime) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO stoploss_events (ts, mode, pair) VALUES (?,?,?)", (_iso(ts), mode, pair))

    def last_stoploss_times(self, mode: str) -> dict[str, datetime]:
        rows = self._conn.execute(
            "SELECT pair, MAX(ts) AS ts FROM stoploss_events WHERE mode = ? GROUP BY pair", (mode,))
        return {r["pair"]: datetime.fromisoformat(r["ts"]) for r in rows}

    # ------------------------------------------------------------ daily pnl

    def upsert_daily_pnl(self, date: str, mode: str, *, start_equity: Decimal,
                         end_equity: Decimal | None = None, realized: Decimal | None = None,
                         unrealized: Decimal | None = None, fees: Decimal | None = None,
                         peak_equity: Decimal | None = None) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO daily_pnl (date, mode, start_equity, end_equity, realized, unrealized, fees,
                   peak_equity) VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(date, mode) DO UPDATE SET
                     end_equity = COALESCE(excluded.end_equity, end_equity),
                     realized = COALESCE(excluded.realized, realized),
                     unrealized = COALESCE(excluded.unrealized, unrealized),
                     fees = COALESCE(excluded.fees, fees),
                     peak_equity = COALESCE(excluded.peak_equity, peak_equity)""",
                (date, mode, str(start_equity), _s(end_equity), _s(realized), _s(unrealized), _s(fees),
                 _s(peak_equity)),
            )

    def get_daily_pnl(self, date: str, mode: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM daily_pnl WHERE date = ? AND mode = ?",
                                  (date, mode)).fetchone()

    def paper_days_recorded(self) -> int:
        """Completed paper-trading days (with an end equity) — used by the live gate."""
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM daily_pnl WHERE mode = 'paper' AND end_equity IS NOT NULL").fetchone()[0])

    # --------------------------------------------------------------- errors

    def record_error(self, component: str, message: str, details: dict | None = None,
                     ts: datetime | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO errors (ts, component, message, details_json) VALUES (?,?,?,?)",
                      (_iso(ts or datetime.now(timezone.utc)), component, message,
                       _json(details) if details else None))

    def errors_between(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM errors WHERE ts >= ? AND ts < ? ORDER BY id", (_iso(start), _iso(end))))

    # ---------------------------------------------------------------- state

    def set_state(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO state (key, value, ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, ts = excluded.ts",
                (key, _json(value), _iso(datetime.now(timezone.utc))))

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
