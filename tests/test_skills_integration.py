"""scripts/skills.py: the paper skills pipeline next to the live agent — isolation and health checks."""

import json
import os
import sys
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
    assert res["kredensial exchange TIDAK diteruskan"][0] and res["Telegram tersedia"][0]
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
