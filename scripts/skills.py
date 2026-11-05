"""Run the .claude/skills paper pipeline next to the live agent (systemd timers in deploy/indodax-skills-*).

    python -m scripts.skills cycle            # one PAPER cycle on the agent's whitelist (hourly timer)
    python -m scripts.skills alerts           # alerts check -> Telegram (5-minute timer)
    python -m scripts.skills report           # daily paper summary -> Telegram (21:05 WIB timer)
    python -m scripts.skills doctor           # health check of the whole integration (read-only)
    python -m scripts.skills run <skill> <args...>   # e.g.: run alerts add price btc_idr --above 1500000000

Isolation from the live agent, enforced here:
- the skills get a CLEAN environment: only TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (from the agent's .env, or
  SKILLS_TELEGRAM_BOT_TOKEN/SKILLS_TELEGRAM_CHAT_ID if set there) — never INDODAX_API_KEY/SECRET, never INDODAX_LIVE;
  so the skills physically cannot place a live order;
- their state lives in data/skills/ (never the agent database);
- every Telegram message is prefixed "🧪 PAPER-SKILLS" so it cannot be confused with the live agent.
Pairs = the agent's market.whitelist; paper capital = risk.agent_capital_idr (auto-created on first cycle).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.config import load_secrets, load_settings

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"
STATE = ROOT / "data" / "skills"
PREFIX = "🧪 PAPER-SKILLS"
UNITS = ("indodax-skills-cycle", "indodax-skills-alerts", "indodax-skills-report")
SCRIPTS = {"market": "indodax-market/scripts/market.py", "analyst": "indodax-analyst/scripts/analyst.py",
           "budget": "indodax-budget/scripts/budget.py", "risk": "indodax-risk/scripts/risk.py",
           "broker": "indodax-broker/scripts/broker.py", "alerts": "indodax-alerts/scripts/alerts.py",
           "report": "indodax-report/scripts/report.py", "cycle": "indodax-cycle/scripts/cycle.py"}


def skills_env(env_file: str = ".env") -> dict[str, str]:
    """Minimal environment for skill processes: no exchange credentials, no live switch."""
    sec = load_secrets(env_file)
    raw = _dotenv(env_file)
    token = raw.get("SKILLS_TELEGRAM_BOT_TOKEN") or sec.TELEGRAM_BOT_TOKEN.get_secret_value()
    chat = raw.get("SKILLS_TELEGRAM_CHAT_ID") or sec.TELEGRAM_CHAT_ID
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ", "HTTPS_PROXY", "https_proxy",
                                      "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE") if k in os.environ}
    env.update({"HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "INDODAX_STATE_DIR": str(STATE),
                "INDODAX_SKILLS_TG_PREFIX": PREFIX})
    if token and chat:
        env.update({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat)})
    return env


def _dotenv(path: str) -> dict[str, str]:
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def skill(name: str, *args: str, env: dict, stdin: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SKILLS / SCRIPTS[name]), *args], env=env, input=stdin,
                          capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))


def _echo(p: subprocess.CompletedProcess) -> int:
    if p.stdout:
        print(p.stdout.rstrip())
    if p.stderr:
        print(p.stderr.rstrip(), file=sys.stderr)
    return p.returncode


def ensure_paper_balance(env: dict, capital: int) -> str | None:
    if (STATE / "paper_balance.json").exists():
        return None
    p = skill("broker", "paper-init", "--idr", str(capital), env=env)
    if p.returncode != 0:
        raise SystemExit(f"paper-init gagal: {p.stderr.strip()}")
    return f"saldo paper dibuat: Rp {capital:,}".replace(",", ".")


def cmd_cycle(s, env) -> int:
    note = ensure_paper_balance(env, int(s.risk.agent_capital_idr))
    if note:
        print(note)
        skill("report", "send", "--text", f"Pipeline paper skills aktif. {note} (mengikuti modal agent).", env=env)
    pairs = ",".join(s.market.whitelist)
    return _echo(skill("cycle", "run", "--pairs", pairs, "--tf", "240", "--stop-pct", "5", "--notify",
                       "--pretty", env=env))


def cmd_alerts(s, env) -> int:
    chk = skill("alerts", "check", env=env)
    if chk.returncode != 0:
        return _echo(chk)
    return _echo(skill("report", "send", "--stdin", env=env, stdin=chk.stdout))


def cmd_report(s, env) -> int:
    return _echo(skill("report", "daily", "--source", "paper", "--send", env=env))


def doctor(s, env, now: float | None = None, unit_state=None) -> list[tuple[str, bool, str]]:
    """[(check, ok, detail)] — read-only."""
    now = time.time() if now is None else now
    unit_state = unit_state or _unit_state
    out = []
    out.append(("skills terpasang", (SKILLS / SCRIPTS["cycle"]).exists(), str(SKILLS)))
    out.append(("kredensial exchange TIDAK diteruskan",
                not any(k in env for k in ("INDODAX_API_KEY", "INDODAX_API_SECRET", "INDODAX_LIVE")), "env skills bersih"))
    out.append(("Telegram tersedia", "TELEGRAM_BOT_TOKEN" in env, "dari .env agent" if "TELEGRAM_BOT_TOKEN" in env
                else "isi TELEGRAM_BOT_TOKEN/CHAT_ID di .env agent"))
    bal = STATE / "paper_balance.json"
    out.append(("saldo paper", bal.exists(), json.loads(bal.read_text()).get("idr", "?") if bal.exists()
                else "belum ada — dibuat otomatis pada siklus pertama"))
    for u in UNITS:
        t, svc = unit_state(f"{u}.timer"), unit_state(f"{u}.service")
        out.append((f"timer {u}", t == "active" and svc != "failed", f"timer={t} service={svc}"))
    st = STATE / "cycle_state.json"
    if st.exists():
        last = json.loads(st.read_text()).get("last_run") or {}
        age_h = (now - last.get("ran_at", 0)) / 3600
        out.append(("siklus terakhir < 2 jam", age_h < 2, f"{age_h:.1f} jam lalu · equity Rp {last.get('equity_idr', '?')}"
                    f" · masalah {len(last.get('problems') or [])}"))
    else:
        out.append(("siklus terakhir < 2 jam", False, "belum pernah jalan"))
    p = skill("market", "ticker", "btc_idr", env=env, timeout=60)
    age = json.loads(p.stdout).get("age_s") if p.returncode == 0 else None
    out.append(("data pasar Indodax", p.returncode == 0, f"ticker BTC umur {age} dtk" if age is not None
                else p.stderr.strip()[:120]))
    if (STATE / "HALT").exists():
        out.append(("kill switch skills", False, f"file {STATE / 'HALT'} ada — BUY paper diblokir"))
    return out


def _unit_state(unit: str) -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Paper skills pipeline next to the live agent")
    ap.add_argument("cmd", choices=["cycle", "alerts", "report", "doctor", "run"])
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    os.chdir(ROOT)
    sec = load_secrets(".env")
    s = load_settings(sec.AGENT_SETTINGS)
    STATE.mkdir(parents=True, exist_ok=True)
    env = skills_env(".env")
    if a.cmd == "run":
        if not a.rest or a.rest[0] not in SCRIPTS:
            print(f"pakai: run <{'|'.join(SCRIPTS)}> <argumen...>", file=sys.stderr)
            return 2
        return _echo(skill(a.rest[0], *a.rest[1:], env=env))
    if a.cmd == "doctor":
        res = doctor(s, env)
        print(f"Doctor PAPER-SKILLS {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
        for name, ok, detail in res:
            print(f"  {'✅' if ok else '❌'} {name}: {detail}")
        bad = [n for n, ok, _ in res if not ok]
        print("HASIL:", "SEMUA OK" if not bad else f"{len(bad)} perlu dicek: {', '.join(bad)}")
        return 0 if not bad else 1
    return {"cycle": cmd_cycle, "alerts": cmd_alerts, "report": cmd_report}[a.cmd](s, env)


if __name__ == "__main__":
    sys.exit(main())
