"""scripts/skills.py: the paper skills pipeline next to the live agent — isolation and health checks."""

import json
import os
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from agent.config import LIVE_CONFIRM_PHRASE
from scripts import skills


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"MODE=live\nLIVE_CONFIRM={LIVE_CONFIRM_PHRASE}\nINDODAX_API_KEY=REALKEY-should-not-leak\nINDODAX_API_SECRET=REALSECRET\n"
                        f"TELEGRAM_BOT_TOKEN=111:agent-token\nTELEGRAM_CHAT_ID=42\n")
    for k in ("INDODAX_API_KEY", "INDODAX_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "INDODAX_LIVE",
              "SKILLS_TELEGRAM_BOT_TOKEN", "SKILLS_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INDODAX_LIVE", "true")              # even if the shell is armed ...
    monkeypatch.setattr(skills, "STATE", tmp_path / "skills")
    return env_file


def test_env_carries_telegram_only(agent_env):
    env = skills.skills_env(str(agent_env))
    assert env["TELEGRAM_BOT_TOKEN"] == "111:agent-token" and env["TELEGRAM_CHAT_ID"] == "42"
    assert not {"INDODAX_API_KEY", "INDODAX_API_SECRET", "INDODAX_LIVE"} & set(env)   # ... skills never see it
    assert env["INDODAX_STATE_DIR"].endswith("skills") and env["INDODAX_SKILLS_TG_PREFIX"]
    assert "REALKEY" not in json.dumps(env)


def test_dedicated_skills_bot_overrides(agent_env):
    agent_env.write_text(agent_env.read_text() + "SKILLS_TELEGRAM_BOT_TOKEN=222:skills\nSKILLS_TELEGRAM_CHAT_ID=7\n")
    env = skills.skills_env(str(agent_env))
    assert (env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]) == ("222:skills", "7")


def test_skill_subprocess_cannot_go_live(agent_env, tmp_path):
    env = skills.skills_env(str(agent_env))
    p = skills.skill("broker", "paper-init", "--idr", "300000", env=env)
    assert p.returncode == 0, p.stderr
    assert json.loads((tmp_path / "skills" / "paper_balance.json").read_text())["idr"] == "300000"
    probe = skills.subprocess.run([sys.executable, "-c", "import os,json;print(json.dumps(dict(os.environ)))"],
                                  env=env, capture_output=True, text=True)
    seen = json.loads(probe.stdout)
    assert "INDODAX_API_KEY" not in seen and "INDODAX_LIVE" not in seen
    assert skills.ensure_paper_balance(env, 999) is None             # existing balance is never overwritten


def test_doctor_reports_each_part(agent_env, tmp_path, monkeypatch):
    env = skills.skills_env(str(agent_env))
    monkeypatch.setattr(skills, "skill", lambda *a, **k: skills.subprocess.CompletedProcess(
        a, 0, json.dumps({"age_s": 2}), ""))
    res = {n: (ok, d) for n, ok, d in skills.doctor(None, env, now=1_800_000_000,
                                                    unit_state=lambda u: "active" if u.endswith(".timer") else "inactive")}
    assert res["kredensial exchange TIDAK diteruskan ke skill paper"][0] and res["Telegram tersedia"][0]
    assert not res["saldo paper"][0] and not res["siklus terakhir < 2 jam"][0]
    assert res["timer indodax-skills-cycle"][0] and res["data pasar Indodax"][0]
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "cycle_state.json").write_text(json.dumps({"last_run": {"ran_at": 1_800_000_000 - 600}}))
    res = {n: ok for n, ok, _ in skills.doctor(None, env, now=1_800_000_000, unit_state=lambda u: "failed")}
    assert res["siklus terakhir < 2 jam"] and not res["timer indodax-skills-alerts"]


def test_units_run_as_isolated_agent_user():
    root = Path(skills.__file__).resolve().parent.parent / "deploy"
    for n in ("cycle", "alerts", "report"):
        svc = (root / f"indodax-skills-{n}.service").read_text()
        assert "User=indodax" in svc and "ProtectSystem=strict" in svc and "ReadWritePaths=/opt/indodax-agent/data" in svc
        assert f"-m scripts.skills {n}" in svc and "EnvironmentFile" not in svc
        assert (root / f"indodax-skills-{n}.timer").exists()
    assert "indodax-skills-cycle.timer" in (root / "install.sh").read_text()
    assert os.access(root / "install.sh", os.R_OK)


# ------------------------------------------------------------------ LIVE (owner decision 2026-11-05)

def _arm(env_file, **over):
    vals = {"SKILLS_LIVE": "true", "SKILLS_LIVE_CONFIRM": skills.LIVE_PHRASE, "SKILLS_CAPITAL_IDR": "100000",
            "SKILLS_INDODAX_API_KEY": "SKILLKEY", "SKILLS_INDODAX_API_SECRET": "SKILLSECRET", **over}
    env_file.write_text(env_file.read_text() + "".join(f"{k}={v}\n" for k, v in vals.items() if v is not None))


def test_live_needs_every_condition(agent_env):
    assert skills.live_config(str(agent_env))[0] is None                          # default: paper
    for bad in ({"SKILLS_LIVE_CONFIRM": "yes"}, {"SKILLS_CAPITAL_IDR": "150000"}, {"SKILLS_CAPITAL_IDR": "5000"},
                {"SKILLS_INDODAX_API_KEY": None}, {"SKILLS_INDODAX_API_KEY": "REALKEY-should-not-leak"}):
        base = agent_env.read_text()
        _arm(agent_env, **bad)
        cfg, probs = skills.live_config(str(agent_env))
        assert cfg is None and probs, bad
        agent_env.write_text(base)
    _arm(agent_env)
    cfg, probs = skills.live_config(str(agent_env))
    assert probs == [] and cfg["capital"] == 100000


def test_live_env_uses_only_the_skills_key(agent_env):
    _arm(agent_env)
    env = skills.live_env(str(agent_env), skills.live_config(str(agent_env))[0])
    assert env["INDODAX_API_KEY"] == "SKILLKEY" and env["INDODAX_LIVE_AUTO"] == skills.LIVE_PHRASE
    assert "REALKEY" not in json.dumps(env) and env["INDODAX_STATE_DIR"].endswith("live")
    assert "INDODAX_API_KEY" not in skills.skills_env(str(agent_env))             # paper stays clean


def test_fence_never_touches_the_agents_money():
    book = {"capital_idr": "100000", "idr": "100000", "holdings": {"btc": "0.0001", "eth": "0.01"}}
    account = {"idr": "250000", "idr_on_hold": "20000", "holdings": {"btc": "0.0003", "eth": "0.01"},
               "holdings_on_hold": {"btc": "0.0001"}}
    # agent: cash 200k (20k of it locked in its own buy order), owns 0.0003 BTC (0.0001 locked in its sell)
    f = skills.fence(book, account, D(200000), {"btc": D("0.0003")}, 1.0)
    assert f["idr"] == "65000"                       # 250k free - (200k - 20k) agent - 5k buffer
    # account BTC = 0.0003 free + 0.0001 locked = 0.0004; agent owns 0.0003 -> 0.0001 is the skills'
    assert f["holdings"] == {"btc": "0.0001", "eth": "0.01"}
    all_agent = skills.fence(book, account, D(200000), {"btc": D("0.0004")}, 1.0)
    assert all_agent["holdings"] == {"eth": "0.01"}   # every BTC is the agent's -> skills may sell none
    poor = skills.fence(book, {**account, "idr": "150000"}, D(200000), {}, 1.0)
    assert poor["idr"] == "0"                         # account cannot cover the agent -> skills get nothing


def test_agent_claim_replays_live_fills(tmp_path):
    from agent.storage.db import Database
    db = Database(tmp_path / "agent.db")
    from datetime import datetime, timezone
    db.record_fill(trade_id="t1", client_order_id="ag-1", ts=datetime(2026, 11, 1, tzinfo=timezone.utc), mode="live",
                   pair="btc_idr", side="buy", price=D(1000000000), qty=D("0.0001"), fee_idr=D(300))

    class S:
        storage = type("st", (), {"db_path": str(tmp_path / "agent.db")})
        risk = type("r", (), {"agent_capital_idr": 300000})

    class Sec:
        MODE = "live"
    cash, qty = skills.agent_claim(S, Sec)
    assert cash == D(300000) - D(100000) - D(300) and qty == {"btc": D("0.0001")}
    Sec.MODE = "paper"
    assert skills.agent_claim(S, Sec) == (D(0), {})


def test_run_live_flag_parsing_and_no_manual_live_orders(agent_env, monkeypatch, capsys):
    monkeypatch.chdir(agent_env.parent)                     # main() chdirs to ROOT; restore cwd afterwards
    monkeypatch.setattr(skills, "ROOT", agent_env.parent)
    monkeypatch.setattr(skills, "load_settings", lambda p: None)
    assert skills.main(["run", "--live", "broker", "balance"]) == 1                  # not armed
    _arm(agent_env)
    assert skills.main(["run", "--live", "broker", "order", "--risk", "x"]) == 1      # no manual live orders
    assert "siklus otomatis" in capsys.readouterr().err
