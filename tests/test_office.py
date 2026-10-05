"""Agent Office: snapshot built from a real runner database; loopback-only read-only server."""

import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from agent.office.server import is_loopback, serve
from agent.office.state import build_state
from agent.storage.db import Database
from tests.helpers import D
from tests.test_runner import env  # noqa: F401 - fixture


def desks(st):
    return {d["key"]: d for d in st["desks"]}


async def test_snapshot_after_cycles(env, tmp_path):  # noqa: F811
    make, db, md, clock, notes, signals = env
    r = make()
    await r.run_cycle()                              # buys 3 positions (paper)
    st = build_state(db.path, r.s, clock.t + timedelta(minutes=1))
    assert st["mode"] == "paper" and st["alive"] and st["status"] == "RUNNING"
    assert st["kpi"]["positions"] == 3 and len(st["positions"]) == 3
    assert all(p["stop"] is not None for p in st["positions"])
    assert abs(st["kpi"]["equity"] - float(r.pf.equity(r._marks(r.last_markets)))) < 1
    k = desks(st)
    assert k["market"]["state"] == "working" and k["deadman"]["state"] == "off"
    assert k["analyst"]["state"] in ("working", "idle") and k["risk"]["state"] == "working"
    assert len(st["fills"]) == 3 and len(st["decisions"]) == 3
    assert [x["pair"] for x in st["radar"]] == list(r.s.market.whitelist)
    json.dumps(st, default=str)                      # serialisable


async def test_stale_heartbeat_marks_agent_down(env):  # noqa: F811
    make, db, md, clock, notes, _ = env
    r = make()
    await r.run_cycle()
    st = build_state(db.path, r.s, clock.t + timedelta(minutes=30))
    assert not st["alive"] and desks(st)["market"]["state"] == "down"


async def test_paused_and_halted_shown_on_risk_desk(env):  # noqa: F811
    make, db, md, clock, notes, _ = env
    r = make()
    await r.run_cycle()
    await r.pause()
    assert desks(build_state(db.path, r.s, clock.t))["risk"]["state"] == "warn"
    await r.kill()
    assert desks(build_state(db.path, r.s, clock.t))["risk"]["state"] == "down"


def test_empty_database_does_not_crash(tmp_path):
    from tests.helpers import settings
    db = Database(tmp_path / "e.db")
    db.close()
    st = build_state(str(tmp_path / "e.db"), settings())
    assert not st["alive"] and desks(st)["market"]["state"] == "down" and st["kpi"]["positions"] == 0


def test_office_never_writes(tmp_path):
    from tests.helpers import settings
    db = Database(tmp_path / "ro.db")
    db.close()
    import sqlite3
    conn = sqlite3.connect(f"file:{tmp_path / 'ro.db'}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO state (key, value, ts) VALUES ('x', '1', 'now')")
    build_state(str(tmp_path / "ro.db"), settings())


def test_loopback_only():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("10.0.0.5")
    from tests.helpers import settings
    with pytest.raises(ValueError):
        serve("0.0.0.0", 0, "x.db", settings())


async def test_http_server_serves_page_and_state(env):  # noqa: F811
    make, db, md, clock, notes, _ = env
    r = make()
    await r.run_cycle()
    httpd = serve("127.0.0.1", 0, db.path, r.s)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
        assert page.status == 200 and b"Agent Office" in page.read()
        assert "default-src 'none'" in page.headers["Content-Security-Policy"]
        st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read())
        assert st["kpi"]["positions"] == 3
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", data=b"x", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 405
    finally:
        httpd.shutdown()
        httpd.server_close()


async def test_heartbeat_contains_no_secrets(env):  # noqa: F811
    make, db, md, clock, notes, _ = env
    r = make()
    await r.run_cycle()
    hb = json.dumps(db.get_state("office:heartbeat"))
    for word in ("key", "secret", "token", "Sign"):
        assert word not in hb


def test_port_in_use_exits_3_without_crash(tmp_path):
    import socket

    from agent.office.server import main
    db = Database(tmp_path / "p.db")
    db.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        port = s.getsockname()[1]
        assert main(["--port", str(port), "--db", str(tmp_path / "p.db")]) == 3
    finally:
        s.close()
