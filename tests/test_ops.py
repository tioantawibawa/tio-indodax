"""Satpam (watchdog) and database backup."""

import gzip
import os
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

import httpx
import respx

from agent.ops.backup import backup
from agent.ops.watchdog import Snapshot, decide, detect, snapshot
from agent.storage.db import Database
from tests.helpers import NOW, settings
from tests.test_runner import env  # noqa: F401 - fixture

W = settings().watchdog


def snap(**kw):
    base = dict(now=NOW, heartbeat_ts=NOW - timedelta(minutes=3), mode="live", open_positions=0,
                service_state="active", free_disk_mb=5000, newest_backup=NOW - timedelta(hours=5))
    base.update(kw)
    return Snapshot(**base)


def test_all_good_means_no_problem():
    assert detect(snap(), W) == []


def test_stale_agent_with_positions_is_urgent():
    ps = detect(snap(heartbeat_ts=NOW - timedelta(minutes=40), open_positions=3), W)
    assert [p.key for p in ps] == ["stale"] and ps[0].urgent and "3 posisi terbuka" in ps[0].text


def test_service_down_disk_and_backup_problems():
    keys = {p.key for p in detect(snap(service_state="failed", free_disk_mb=100,
                                       newest_backup=NOW - timedelta(hours=50)), W)}
    assert keys == {"service", "disk", "backup"}
    assert {p.key for p in detect(snap(newest_backup=None, heartbeat_ts=None), W)} == {"backup", "stale"}


def test_alert_once_repeat_hourly_then_recover():
    ps = detect(snap(heartbeat_ts=NOW - timedelta(minutes=40), open_positions=1), W)
    msgs, st = decide(ps, {}, NOW, W)
    assert len(msgs) == 1 and "DARURAT" in msgs[0]
    msgs, st = decide(ps, st, NOW + timedelta(minutes=10), W)
    assert msgs == []                                             # no spam
    msgs, st = decide(ps, st, NOW + timedelta(minutes=61), W)
    assert len(msgs) == 1                                         # reminder while it lasts
    msgs, st = decide([], st, NOW + timedelta(minutes=70), W)
    assert len(msgs) == 1 and "pulih" in msgs[0] and st == {}


async def test_snapshot_reads_heartbeat_and_positions_from_agent_db(env, tmp_path):  # noqa: F811
    make, db, md, clock, notes, _ = env
    r = make()
    await r.run_cycle()                                            # paper: opens 3 positions
    s = r.s.model_copy(update={"storage": r.s.storage.model_copy(update={"db_path": db.path}),
                               "watchdog": r.s.watchdog.model_copy(update={"backup_dir": str(tmp_path / "b")})})
    sn = snapshot(s, clock.t + timedelta(minutes=30), service_state=None)
    assert sn.mode == "paper" and sn.open_positions == 3 and sn.newest_backup is None
    assert {p.key for p in detect(sn, s.watchdog)} == {"stale", "backup"}


def test_backup_is_consistent_compressed_and_rotated(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.set_state("x", {"a": 1})
    old = tmp_path / "b" / "agent-20200101-0000.db.gz"
    old.parent.mkdir()
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 20 * 86400, time.time() - 20 * 86400))
    gz, removed = backup(tmp_path / "agent.db", tmp_path / "b", keep_days=14)
    db.close()
    assert gz.exists() and removed == 1 and not old.exists()
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(gz.read_bytes()))
    conn = sqlite3.connect(restored)
    assert conn.execute("SELECT value FROM state WHERE key='x'").fetchone()[0] == '{"a": 1}'
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@respx.mock
async def test_agent_pings_external_healthcheck_and_reports_failures(env):  # noqa: F811
    make, db, md, clock, notes, _ = env
    ok = respx.get("https://hc.example/abc").mock(return_value=httpx.Response(200))
    fail = respx.get("https://hc.example/abc/fail").mock(return_value=httpx.Response(200))
    r = make()
    r.healthcheck_url = "https://hc.example/abc"
    await r.run_cycle()
    assert ok.call_count == 1 and fail.call_count == 0
    md.fail.update(["btc_idr", "eth_idr", "sol_idr"])
    for _ in range(3):
        clock.t += timedelta(minutes=5)
        await r.run_cycle()
    assert fail.call_count == 1


def test_healthcheck_url_is_redacted_from_logs():
    from agent.config import Secrets
    s = Secrets(HEALTHCHECK_URL="https://hc-ping.com/secret-uuid")
    assert "https://hc-ping.com/secret-uuid" in s.all_secret_values()
