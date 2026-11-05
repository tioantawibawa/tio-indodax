"""Run the .claude/skills pipeline next to the live agent (systemd timers in deploy/indodax-skills-*).

    python -m scripts.skills cycle            # one cycle on the agent's whitelist (hourly timer)
    python -m scripts.skills alerts           # alerts -> Telegram, then stop-loss check (5-minute timer)
    python -m scripts.skills report           # daily summary -> Telegram (21:05 WIB timer)
    python -m scripts.skills doctor           # health check of the whole integration (read-only)
    python -m scripts.skills run <skill> <args...>   # e.g.: run alerts add price btc_idr --above 1500000000

PAPER by default. LIVE (owner decision 2026-11-05: automatic, fenced capital) only when the agent's .env has
ALL of:  SKILLS_LIVE=true, SKILLS_LIVE_CONFIRM=I_ACCEPT_AUTOMATIC_LIVE_TRADING, SKILLS_CAPITAL_IDR (10.000 ..
100.000) and a SEPARATE key pair SKILLS_INDODAX_API_KEY / SKILLS_INDODAX_API_SECRET (refused if equal to the
agent's key). Anything missing -> paper, and `doctor` says what is missing.

Isolation from the live agent, enforced here:
- paper skills get a CLEAN environment: Telegram only, never any Indodax key and never INDODAX_LIVE;
- live skills get only the SKILLS_* key (never the agent's), and only the money "fenced" for them:
      IDR   = min(skills book cash, account free IDR − agent's own cash not already locked in its orders − Rp 5.000)
      coin  = min(skills book coins, account free coins − agent's own position not already locked)
  so the skills can never spend the agent's IDR or sell the agent's coins (which would block the agent's
  reconciliation); the broker additionally caps every order at Rp 50.000 and never leaves resting orders;
- state lives in data/skills/ (paper) and data/skills/live/ (live) — never the agent database (read-only use);
- Telegram messages are prefixed "🧪 PAPER-SKILLS" or "💰 LIVE-SKILLS".
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agent.config import load_secrets, load_settings

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"
STATE = ROOT / "data" / "skills"
PREFIX = "🧪 PAPER-SKILLS"
LIVE_PREFIX = "💰 LIVE-SKILLS"
LIVE_PHRASE = "I_ACCEPT_AUTOMATIC_LIVE_TRADING"
LIVE_CAPITAL_MAX = 100_000
IDR_BUFFER = Decimal(5000)
UNITS = ("indodax-skills-cycle", "indodax-skills-alerts", "indodax-skills-report")
SCRIPTS = {"market": "indodax-market/scripts/market.py", "analyst": "indodax-analyst/scripts/analyst.py",
           "budget": "indodax-budget/scripts/budget.py", "risk": "indodax-risk/scripts/risk.py",
           "broker": "indodax-broker/scripts/broker.py", "alerts": "indodax-alerts/scripts/alerts.py",
           "report": "indodax-report/scripts/report.py", "cycle": "indodax-cycle/scripts/cycle.py"}


def live_dir() -> Path:
    return STATE / "live"


# ------------------------------------------------------------------ environments

def skills_env(env_file: str = ".env") -> dict[str, str]:
    """Minimal environment for PAPER skill processes: no exchange credentials, no live switch."""
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


def live_config(env_file: str = ".env") -> tuple[dict | None, list[str]]:
    """(config, problems). config is None unless EVERY live condition holds."""
    raw, sec = _dotenv(env_file), load_secrets(env_file)
    if raw.get("SKILLS_LIVE", "").lower() != "true":
        return None, ["SKILLS_LIVE belum true (mode paper)"]
    probs = []
    if raw.get("SKILLS_LIVE_CONFIRM") != LIVE_PHRASE:
        probs.append(f"SKILLS_LIVE_CONFIRM harus {LIVE_PHRASE}")
    try:
        cap = int(raw.get("SKILLS_CAPITAL_IDR", "0"))
    except ValueError:
        cap = 0
    if not 10_000 <= cap <= LIVE_CAPITAL_MAX:
        probs.append(f"SKILLS_CAPITAL_IDR harus 10000..{LIVE_CAPITAL_MAX}")
    key, secret = raw.get("SKILLS_INDODAX_API_KEY", ""), raw.get("SKILLS_INDODAX_API_SECRET", "")
    if not (key and secret):
        probs.append("SKILLS_INDODAX_API_KEY/SECRET kosong (wajib key terpisah dari agent)")
    elif key == sec.INDODAX_API_KEY.get_secret_value() or secret == sec.INDODAX_API_SECRET.get_secret_value():
        probs.append("SKILLS_INDODAX_API_KEY sama dengan key agent — buat key terpisah")
    if probs:
        return None, probs
    return {"capital": cap, "key": key, "secret": secret}, []


def live_env(env_file: str, cfg: dict) -> dict[str, str]:
    env = skills_env(env_file)
    env.update({"INDODAX_STATE_DIR": str(live_dir()), "INDODAX_SKILLS_TG_PREFIX": LIVE_PREFIX,
                "INDODAX_LIVE": "true", "INDODAX_LIVE_AUTO": LIVE_PHRASE,
                "INDODAX_API_KEY": cfg["key"], "INDODAX_API_SECRET": cfg["secret"]})
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


def _json(p: subprocess.CompletedProcess, what: str) -> dict:
    if p.returncode != 0:
        raise RuntimeError(f"{what}: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout)


# ------------------------------------------------------------------ fence (live)

def agent_claim(s, sec, db_path: str | None = None) -> tuple[Decimal, dict[str, Decimal]]:
    """The live agent's own IDR cash and coin quantities, replayed read-only from its database."""
    if str(sec.MODE.value if hasattr(sec.MODE, "value") else sec.MODE) != "live":
        return Decimal(0), {}
    from agent.portfolio.portfolio import Portfolio
    path = Path(db_path or s.storage.db_path)
    if not path.exists():
        raise RuntimeError(f"database agent {path} tidak ada — tidak bisa memagari modal")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM state WHERE key = 'live:capital'").fetchone()
        cap = Decimal(str(json.loads(row["value"]))) if row else Decimal(str(s.risk.agent_capital_idr))
        pf = Portfolio(cap)
        for f in conn.execute("SELECT * FROM fills WHERE mode = 'live' ORDER BY ts, id"):
            pf.apply_fill(f["pair"], f["side"], Decimal(f["qty"]), Decimal(f["price"]), Decimal(f["fee_idr"]),
                          datetime.fromisoformat(f["ts"]), strict=False)
    finally:
        conn.close()
    return max(pf.cash_idr, Decimal(0)), {p.split("_")[0]: pos.qty for p, pos in pf.positions.items()}


def fence(book: dict, account: dict, agent_cash: Decimal, agent_qty: dict[str, Decimal], now: float) -> dict:
    """Pure: what the skills may use = their own book, capped by what is physically free and not the agent's."""
    free_idr, idr_hold = Decimal(account["idr"]), Decimal(account.get("idr_on_hold") or 0)
    agent_free_claim = max(Decimal(0), agent_cash - idr_hold)          # agent cash not locked in its buy orders
    idr = max(Decimal(0), min(Decimal(book["idr"]), free_idr - agent_free_claim - IDR_BUFFER))
    held = account.get("holdings", {})
    on_hold = account.get("holdings_on_hold", {})
    coins = {}
    for c, q in book.get("holdings", {}).items():
        agent_free = max(Decimal(0), agent_qty.get(c, Decimal(0)) - Decimal(on_hold.get(c, 0)))
        avail = Decimal(held.get(c, 0)) - agent_free
        v = max(Decimal(0), min(Decimal(q), avail))
        if v > 0:
            coins[c] = str(v)
    return {"computed_at": now, "idr": str(idr.quantize(Decimal(1))), "holdings": coins, "book": book,
            "detail": {"account_free_idr": str(free_idr), "agent_cash_claim": str(agent_free_claim),
                       "book_idr": book["idr"]}}


def write_fence(s, sec, env: dict, cfg: dict) -> Path:
    live_dir().mkdir(parents=True, exist_ok=True)
    _json(skill("broker", "live-init", "--capital-idr", str(cfg["capital"]), env=env), "live-init")
    book = _json(skill("broker", "balance", "--source", "book", env=env), "buku skills")
    account = _json(skill("broker", "balance", "--source", "live", env=env, timeout=60), "saldo akun (getInfo)")
    cash, qty = agent_claim(s, sec)
    f = fence(book, account, cash, qty, time.time())
    path = live_dir() / "fenced_balance.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(f))
    tmp.replace(path)
    return path


# ------------------------------------------------------------------ commands

def ensure_paper_balance(env: dict, capital: int) -> str | None:
    if (STATE / "paper_balance.json").exists():
        return None
    p = skill("broker", "paper-init", "--idr", str(capital), env=env)
    if p.returncode != 0:
        raise SystemExit(f"paper-init gagal: {p.stderr.strip()}")
    return f"saldo paper dibuat: Rp {capital:,}".replace(",", ".")


def cmd_cycle(s, sec, env, cfg) -> int:
    pairs = ",".join(s.market.whitelist)
    common = ["--pairs", pairs, "--tf", "240", "--stop-pct", "5", "--notify", "--pretty"]
    if cfg:
        lenv = live_env(".env", cfg)
        first = not (live_dir() / "live_book.json").exists()
        try:
            fb = write_fence(s, sec, lenv, cfg)
        except (RuntimeError, ValueError, OSError) as e:
            skill("report", "send", "--text", f"⚠️ Siklus LIVE dilewati: {e}", env=lenv)
            print(f"siklus live dilewati: {e}", file=sys.stderr)
            return 3
        if first:
            skill("report", "send", "--text", f"Pipeline skills LIVE aktif. Modal dipagari Rp {cfg['capital']:,}."
                  .replace(",", "."), env=lenv)
        return _echo(skill("cycle", "run", *common, "--live", "--balance-file", str(fb), env=lenv))
    note = ensure_paper_balance(env, int(s.risk.agent_capital_idr))
    if note:
        print(note)
        skill("report", "send", "--text", f"Pipeline paper skills aktif. {note} (mengikuti modal agent).", env=env)
    return _echo(skill("cycle", "run", *common, env=env))


def cmd_alerts(s, sec, env, cfg) -> int:
    chk = skill("alerts", "check", env=env)
    rc = _echo(chk) if chk.returncode != 0 else _echo(skill("report", "send", "--stdin", env=env, stdin=chk.stdout))
    # stop-loss check every 5 minutes (the hourly cycle alone could let a crash run for an hour)
    if cfg:
        lenv = live_env(".env", cfg)
        if (live_dir() / "cycle_state.json").exists():
            try:
                fb = write_fence(s, sec, lenv, cfg)
                rc = max(rc, _echo(skill("cycle", "stops", "--notify", "--pretty", "--live", "--balance-file",
                                         str(fb), env=lenv)))
            except (RuntimeError, ValueError, OSError) as e:
                print(f"cek stop live gagal: {e}", file=sys.stderr)
                rc = max(rc, 3)
    elif (STATE / "cycle_state.json").exists():
        rc = max(rc, _echo(skill("cycle", "stops", "--notify", "--pretty", env=env)))
    return rc


def cmd_report(s, sec, env, cfg) -> int:
    if cfg:
        return _echo(skill("report", "daily", "--source", "book", "--send", env=live_env(".env", cfg)))
    return _echo(skill("report", "daily", "--source", "paper", "--send", env=env))


def doctor(s, env, now: float | None = None, unit_state=None, live: tuple[dict | None, list[str]] | None = None,
           sec=None) -> list[tuple[str, bool, str]]:
    """[(check, ok, detail)] — read-only."""
    now = time.time() if now is None else now
    unit_state = unit_state or _unit_state
    cfg, live_probs = live if live is not None else (None, [])
    out = []
    out.append(("skills terpasang", (SKILLS / SCRIPTS["cycle"]).exists(), str(SKILLS)))
    out.append(("kredensial exchange TIDAK diteruskan ke skill paper",
                not any(k in env for k in ("INDODAX_API_KEY", "INDODAX_API_SECRET", "INDODAX_LIVE")), "env skills bersih"))
    out.append(("Telegram tersedia", "TELEGRAM_BOT_TOKEN" in env, "dari .env agent" if "TELEGRAM_BOT_TOKEN" in env
                else "isi TELEGRAM_BOT_TOKEN/CHAT_ID di .env agent"))
    if cfg is None:
        out.append(("mode", True, "PAPER" + (f" ({'; '.join(live_probs)})" if live_probs else "")))
        bal = STATE / "paper_balance.json"
        out.append(("saldo paper", bal.exists(), json.loads(bal.read_text()).get("idr", "?") if bal.exists()
                    else "belum ada — dibuat otomatis pada siklus pertama"))
    else:
        out.append(("mode", True, f"LIVE otomatis · modal dipagari Rp {cfg['capital']:,}".replace(",", ".")))
        out += _live_checks(s, sec, cfg)
    for u in UNITS:
        t, svc = unit_state(f"{u}.timer"), unit_state(f"{u}.service")
        out.append((f"timer {u}", t == "active" and svc != "failed", f"timer={t} service={svc}"))
    st = (live_dir() if cfg else STATE) / "cycle_state.json"
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
    halt = (live_dir() if cfg else STATE) / "HALT"
    if halt.exists():
        out.append(("kill switch skills", False, f"file {halt} ada — BUY diblokir: {halt.read_text().strip()[:100]}"))
    return out


def _live_checks(s, sec, cfg) -> list[tuple[str, bool, str]]:
    out = []
    try:
        rep = _permission_report(s, cfg)
        out.append(("key skills tanpa izin withdraw", rep.withdraw_possible is False,
                    f"withdraw mungkin: {rep.withdraw_possible} · legacy OK: {rep.legacy_ok}"))
    except Exception as e:  # noqa: BLE001 - report any failure of the probe
        out.append(("key skills bisa dipakai", False, f"{type(e).__name__}: {str(e)[:120]}"))
        return out
    try:
        f = json.loads(write_fence(s, sec, live_env(".env", cfg), cfg).read_text())
        d = f["detail"]
        ok = Decimal(f["idr"]) >= 10_000 or bool(f["holdings"])
        out.append(("modal dipagari tersedia", ok,
                    f"IDR untuk skills Rp {f['idr']} (buku Rp {d['book_idr']}, bebas akun Rp {d['account_free_idr']}, "
                    f"jatah agent Rp {Decimal(d['agent_cash_claim']):.0f}) · koin {f['holdings'] or '-'}"
                    + ("" if ok else " — tambah saldo IDR di akun agar jatah agent tidak terpakai")))
    except (RuntimeError, ValueError, OSError) as e:
        out.append(("modal dipagari tersedia", False, str(e)[:200]))
    return out


def _permission_report(s, cfg):
    import asyncio

    from agent.exchange.private_client import PrivateReadOnlyClient
    from agent.exchange.public_client import IndodaxPublicClient
    from agent.exchange.signing import Clock

    async def go():
        async with IndodaxPublicClient.from_settings(s.exchange) as pub:
            offset = await pub.clock_offset_ms()
        async with PrivateReadOnlyClient(cfg["key"], cfg["secret"], tapi_url=s.exchange.tapi_url,
                                         v2_base_url=s.exchange.tapi_v2_base_url,
                                         recv_window_ms=s.exchange.recv_window_ms, clock=Clock(offset_ms=offset)) as pc:
            return await pc.permission_report()
    return asyncio.run(go())


def _unit_state(unit: str) -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Skills pipeline next to the live agent (paper; live when armed)")
    ap.add_argument("cmd", choices=["cycle", "alerts", "report", "doctor", "run"])
    ap.add_argument("rest", nargs=argparse.REMAINDER, help="run: [--live] <skill> <argumen...>")
    a = ap.parse_args(argv)
    a.live = bool(a.rest) and a.rest[0] == "--live"          # run --live <skill> ... (REMAINDER keeps it)
    if a.live:
        a.rest = a.rest[1:]
    os.chdir(ROOT)
    sec = load_secrets(".env")
    s = load_settings(sec.AGENT_SETTINGS)
    STATE.mkdir(parents=True, exist_ok=True)
    env = skills_env(".env")
    cfg, probs = live_config(".env")
    if a.cmd == "run":
        if not a.rest or a.rest[0] not in SCRIPTS:
            print(f"pakai: run [--live] <{'|'.join(SCRIPTS)}> <argumen...>", file=sys.stderr)
            return 2
        if a.live and not cfg:
            print("live belum aktif: " + "; ".join(probs), file=sys.stderr)
            return 1
        if a.live and a.rest[0] == "broker" and a.rest[1:2] == ["order"]:
            print("order live manual tidak lewat sini — siklus otomatis yang mengirim order", file=sys.stderr)
            return 1
        return _echo(skill(a.rest[0], *a.rest[1:], env=live_env(".env", cfg) if a.live else env))
    if a.cmd == "doctor":
        res = doctor(s, env, live=(cfg, probs), sec=sec)
        print(f"Doctor {'LIVE' if cfg else 'PAPER'}-SKILLS {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
        for name, ok, detail in res:
            print(f"  {'✅' if ok else '❌'} {name}: {detail}")
        bad = [n for n, ok, _ in res if not ok]
        print("HASIL:", "SEMUA OK" if not bad else f"{len(bad)} perlu dicek: {', '.join(bad)}")
        return 0 if not bad else 1
    return {"cycle": cmd_cycle, "alerts": cmd_alerts, "report": cmd_report}[a.cmd](s, sec, env, cfg)


if __name__ == "__main__":
    sys.exit(main())
