"""Satpam: watchdog that runs OUTSIDE the agent process (systemd timer, every 5 minutes).

Checks: the agent's last trading cycle (heartbeat in the DB, read-only), the systemd service state,
free disk space, the age of the newest DB backup and — when its timers are enabled — the paper skills
pipeline (last hourly cycle, failed indodax-skills-* units). Alerts go straight to Telegram (the agent's own
notifier is useless when the agent is the thing that died). Each problem alerts once, repeats every
``realert_minutes`` while it lasts, and a recovery message follows when it clears.
Satpam never trades, never restarts anything and never writes the agent database.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agent.config import Settings, WatchdogSettings
from agent.portfolio.portfolio import Portfolio


SKILLS_STALE_H = 3                      # hourly paper cycle; alert after 3 missed hours
SKILLS_UNITS = ("indodax-skills-cycle", "indodax-skills-alerts", "indodax-skills-report")


@dataclass(frozen=True)
class Problem:
    key: str
    text: str
    urgent: bool = False


@dataclass
class Snapshot:
    now: datetime
    heartbeat_ts: datetime | None
    mode: str | None
    open_positions: int
    service_state: str | None       # "active", "failed", "inactive", ... or None if unknown
    free_disk_mb: float | None
    newest_backup: datetime | None
    # paper skills pipeline (scripts/skills.py); None when its timers are not enabled
    skills_last_run: datetime | None = None
    skills_enabled: bool = False
    skills_failed_units: tuple[str, ...] = ()
    skills_live_positions: int = 0          # > 0 only in LIVE skills mode: real money without stop checks


def detect(s: Snapshot, w: WatchdogSettings) -> list[Problem]:
    out = []
    pos = (f" {s.open_positions} posisi terbuka TIDAK dilindungi stop-loss selama agent tidak berjalan!"
           if s.open_positions else " Tidak ada posisi terbuka.")
    if s.service_state is not None and s.service_state != "active":
        out.append(Problem("service", f"Service indodax-agent berstatus <b>{s.service_state}</b>.{pos}",
                           urgent=bool(s.open_positions)))
    if s.heartbeat_ts is None:
        out.append(Problem("stale", "Belum ada heartbeat siklus trading di database." + pos, bool(s.open_positions)))
    else:
        age = (s.now - s.heartbeat_ts).total_seconds() / 60
        if age > w.stale_minutes:
            out.append(Problem("stale", f"Tidak ada siklus trading sejak {age:.0f} menit lalu "
                                        f"(batas {w.stale_minutes} menit).{pos}", urgent=bool(s.open_positions)))
    if s.free_disk_mb is not None and s.free_disk_mb < w.min_free_disk_mb:
        out.append(Problem("disk", f"Ruang disk tinggal {s.free_disk_mb:.0f} MB (minimum {w.min_free_disk_mb} MB)."))
    if s.newest_backup is None:
        out.append(Problem("backup", "Belum ada backup database."))
    else:
        hours = (s.now - s.newest_backup).total_seconds() / 3600
        if hours > w.max_backup_age_hours:
            out.append(Problem("backup", f"Backup database terakhir {hours:.0f} jam lalu "
                                         f"(maks {w.max_backup_age_hours} jam)."))
    if s.skills_enabled:
        live = s.skills_live_positions > 0
        tag = "LIVE-SKILLS" if live else "PAPER-SKILLS"
        risk = (f" {s.skills_live_positions} posisi LIVE skills tidak dicek stop-loss-nya!" if live
                else " Agent live tidak terpengaruh.")
        if s.skills_failed_units:
            out.append(Problem("skills", f"Pipeline {tag} gagal: " + ", ".join(s.skills_failed_units) + "." + risk,
                               urgent=live))
        elif s.skills_last_run is None or (s.now - s.skills_last_run).total_seconds() > SKILLS_STALE_H * 3600:
            age = "belum pernah" if s.skills_last_run is None else \
                f"terakhir {(s.now - s.skills_last_run).total_seconds() / 3600:.0f} jam lalu"
            out.append(Problem("skills", f"Siklus {tag} tidak berjalan ({age}, batas {SKILLS_STALE_H} jam)." + risk,
                               urgent=live))
    return out


def decide(problems: list[Problem], state: dict, now: datetime, w: WatchdogSettings) -> tuple[list[str], dict]:
    """Messages to send and the new alert state {key: last_alert_iso}."""
    msgs, new_state = [], {}
    current = {p.key: p for p in problems}
    for key, p in current.items():
        last = state.get(key)
        due = last is None or (now - datetime.fromisoformat(last)).total_seconds() >= w.realert_minutes * 60
        if due:
            head = "🚨 <b>SATPAM — DARURAT</b>" if p.urgent else "⚠️ <b>SATPAM</b>"
            msgs.append(f"{head}\n{p.text}\n{_hint(key)}")
            new_state[key] = now.isoformat()
        else:
            new_state[key] = last
    for key in state:
        if key not in current:
            msgs.append(f"✅ <b>SATPAM</b>: masalah '{_label(key)}' sudah pulih.")
    return msgs, new_state


def _label(key: str) -> str:
    return {"service": "service agent", "stale": "siklus trading", "disk": "ruang disk",
            "backup": "backup database", "skills": "pipeline paper skills"}.get(key, key)


def _hint(key: str) -> str:
    return {
        "service": "Cek: <code>sudo systemctl status indodax-agent</code> lalu "
                   "<code>sudo journalctl -u indodax-agent -n 50 --no-pager</code>",
        "stale": "Cek: <code>sudo journalctl -u indodax-agent --since '30 min ago' --no-pager | tail -30</code>",
        "disk": "Cek: <code>df -h</code>; hapus log/backup lama bila perlu.",
        "backup": "Cek: <code>sudo systemctl status indodax-backup.timer</code>",
        "skills": "Cek: <code>sudo bash deploy/agent-run.sh scripts.skills doctor</code> dan "
                  "<code>sudo journalctl -u 'indodax-skills-*' -n 30 --no-pager</code>",
    }.get(key, "")


# ------------------------------------------------------------------ gathering facts

def snapshot(s: Settings, now: datetime | None = None, service_state: str | None = "auto") -> Snapshot:
    now = now or datetime.now(timezone.utc)
    hb_ts, mode, n_pos = None, None, 0
    db_path = Path(s.storage.db_path)
    if db_path.exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT value FROM state WHERE key = 'office:heartbeat'").fetchone()
            if row:
                hb = json.loads(row["value"])
                hb_ts = datetime.fromisoformat(hb["ts"]) if hb.get("ts") else None
                mode = hb.get("mode")
            if mode:
                pf = Portfolio(Decimal(10**12))
                for f in conn.execute("SELECT * FROM fills WHERE mode = ? ORDER BY ts, id", (mode,)):
                    pf.apply_fill(f["pair"], f["side"], Decimal(f["qty"]), Decimal(f["price"]),
                                  Decimal(f["fee_idr"]), datetime.fromisoformat(f["ts"]), strict=False)
                n_pos = len(pf.positions)
        finally:
            conn.close()
    if service_state == "auto":
        service_state = _service_state("indodax-agent")
    disk = None
    try:
        disk = shutil.disk_usage(db_path.parent if db_path.parent.exists() else ".").free / 2**20
    except OSError:
        pass
    backups = sorted(Path(s.watchdog.backup_dir).glob("agent-*.db.gz"))
    newest = datetime.fromtimestamp(backups[-1].stat().st_mtime, tz=timezone.utc) if backups else None
    sk_enabled, sk_last, sk_failed, sk_live_pos = False, None, (), 0
    if service_state is not None:                     # same switch as the agent-unit lookup (off in tests)
        sk_enabled = _service_state("indodax-skills-cycle.timer") == "active"
        if sk_enabled:
            sk_failed = tuple(u for u in SKILLS_UNITS if _service_state(f"{u}.service") == "failed")
            sk_last, sk_live_pos = _skills_state(db_path.parent / "skills")
    return Snapshot(now, hb_ts, mode, n_pos, service_state, disk, newest, sk_last, sk_enabled, sk_failed, sk_live_pos)


def _skills_state(base: Path) -> tuple[datetime | None, int]:
    """(last hourly cycle, live positions) — live folder wins when the skills trade live."""
    live = base / "live" / "cycle_state.json"
    path = live if live.exists() else base / "cycle_state.json"
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, 0
    ts = (st.get("last_run") or {}).get("ran_at")
    last = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
    return last, len(st.get("positions") or {}) if path == live else 0


def _service_state(unit: str) -> str | None:
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
