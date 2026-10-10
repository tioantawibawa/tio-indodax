"""Daily backup of the agent database: consistent online copy (SQLite backup API, safe while the agent
writes), integrity check, gzip, retention, optional off-VPS copy to the owner's Telegram.
The database holds no API keys or tokens (those live only in .env)."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def backup(db_path: str | Path, backup_dir: str | Path, keep_days: int,
           now: datetime | None = None) -> tuple[Path, int]:
    """Returns (gzip path, number of old backups removed). Raises if the copy is not intact."""
    now = now or datetime.now(timezone.utc)
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M")
    raw = out_dir / f"agent-{stamp}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if check != "ok":
        raw.unlink(missing_ok=True)
        raise RuntimeError(f"backup integrity check failed: {check}")
    gz = raw.with_suffix(".db.gz")
    with raw.open("rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    raw.unlink()
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for old in out_dir.glob("agent-*.db.gz"):
        if old != gz and old.stat().st_mtime < cutoff:
            old.unlink()
            removed += 1
    return gz, removed


def restore_hint(gz: Path) -> str:
    return (f"Pulihkan: stop agent, lalu <code>gunzip -c {gz.name} &gt; data/agent.db</code> "
            "di /opt/indodax-agent (sebagai user indodax), lalu start agent.")
