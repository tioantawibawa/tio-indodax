#!/usr/bin/env python3
"""indodax-alerts: price / move / RSI / EMA-cross alerts for Indodax pairs (standard library only).

    alerts.py add price btc_idr --above 1500000000        # or --below
    alerts.py add move  btc_idr --pct 5 --window 60       # |change| >= 5 % within 60 minutes
    alerts.py add rsi   eth_idr --tf 60 --above 70        # RSI(14) enters >= 70 (or --below 30)
    alerts.py add cross sol_idr --tf 1D --fast 12 --slow 26   # fresh EMA cross on the last closed candle
    alerts.py list | remove a3 | check [--pretty] [--dry-run]

`check` evaluates every rule once (run it from cron/systemd every few minutes). Alerts are edge-triggered:
a price/RSI rule fires when its condition becomes true and re-arms once it is false again; a move rule
fires at most once per --cooldown minutes; a cross fires once per crossing candle. Output JSON has
`triggered` and a ready-to-send `text` (empty when nothing fired) — pipe it to indodax-report `send`.

Rules: $INDODAX_STATE_DIR/alerts.json, state: alerts_state.json (default dir ~/.indodax-skills).
Informational only — never places orders. Exit codes: 0 ok, 2 bad input, 3 market data unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "indodax-market" / "scripts"))
sys.path.insert(0, str(SKILLS / "indodax-analyst" / "scripts"))
import analyst  # noqa: E402
import market  # noqa: E402

KINDS = ("price", "move", "rsi", "cross")
MAX_RULES = 100


class InputError(Exception):
    pass


class MarketDown(Exception):
    pass


# ------------------------------------------------------------------ files

def state_dir() -> Path:
    return Path(os.environ.get("INDODAX_STATE_DIR") or Path.home() / ".indodax-skills")


def rules_path() -> Path:
    return state_dir() / "alerts.json"


def status_path() -> Path:
    return state_dir() / "alerts_state.json"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except ValueError as e:
        raise InputError(f"{path} rusak: {e}") from e


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


# ------------------------------------------------------------------ formatting

def rp(x: float) -> str:
    if abs(x) >= 100:
        return "Rp " + f"{x:,.0f}".replace(",", ".")
    return "Rp " + f"{x:.6g}".replace(".", ",")


def label(pair: str) -> str:
    return pair.upper().replace("_", "/")


def describe(r: dict) -> str:
    p = label(r["pair"])
    if r["kind"] == "price":
        return f"{p} {'≥' if r['op'] == 'above' else '≤'} {rp(r['level'])}"
    if r["kind"] == "move":
        return f"{p} bergerak ≥ {r['pct']}% dalam {r['window']} menit (jeda {r['cooldown']} mnt)"
    if r["kind"] == "rsi":
        return f"{p} RSI{r['period']} ({r['tf']}) {'≥' if r['op'] == 'above' else '≤'} {r['level']}"
    return f"{p} EMA{r['fast']}/EMA{r['slow']} cross ({r['tf']})"


# ------------------------------------------------------------------ rule management

def make_rule(kind: str, a) -> dict:
    pair = market.ticker_name(market.pair_id(a.pair))
    if not pair.endswith("_idr"):
        raise InputError("hanya pair IDR")
    r = {"kind": kind, "pair": pair}
    if kind in ("price", "rsi"):
        if (a.above is None) == (a.below is None):
            raise InputError("pilih tepat satu: --above atau --below")
        r["op"], r["level"] = ("above", a.above) if a.above is not None else ("below", a.below)
        if r["level"] <= 0 or (kind == "rsi" and r["level"] >= 100):
            raise InputError("level di luar rentang")
    if kind == "rsi":
        r.update(tf=a.tf, period=a.period)
        if not 2 <= a.period <= 100:
            raise InputError("--period 2..100")
    if kind == "move":
        if not (0 < a.pct <= 100 and 1 <= a.window <= 7 * 1440 and 1 <= a.cooldown <= 7 * 1440):
            raise InputError("--pct 0..100, --window/--cooldown 1..10080 menit")
        r.update(pct=a.pct, window=a.window, cooldown=a.cooldown)
    if kind == "cross":
        if not 1 <= a.fast < a.slow <= 400:
            raise InputError("butuh 1 <= --fast < --slow <= 400")
        r.update(tf=a.tf, fast=a.fast, slow=a.slow)
    if a.note:
        r["note"] = a.note[:200]
    return r


def add_rule(rule: dict) -> dict:
    rules = _read(rules_path(), [])
    if len(rules) >= MAX_RULES:
        raise InputError(f"maksimal {MAX_RULES} alert")
    core = {k: v for k, v in rule.items() if k != "note"}
    for r in rules:
        if {k: v for k, v in r.items() if k not in ("id", "note", "created_at")} == core:
            return {"added": False, "existing": r["id"], "rule": r}
    n = max([int(r["id"][1:]) for r in rules] + [0]) + 1
    rule = {"id": f"a{n}", **rule, "created_at": int(time.time())}
    _write(rules_path(), rules + [rule])
    return {"added": True, "rule": rule, "desc": describe(rule)}


# ------------------------------------------------------------------ evaluation

def move_tf(window_min: int) -> str:
    return "1" if window_min <= 360 else "15" if window_min <= 3 * 1440 else "60"


class Data:
    """Fetches each ticker / candle series once per check run."""

    def __init__(self, now: float):
        self.now, self._t, self._c = now, {}, {}

    def price(self, pair: str) -> float:
        if pair not in self._t:
            try:
                t = market.ticker(pair)
            except market.MarketError as e:
                raise MarketDown(str(e)) from e
            if t.get("age_s") is not None and t["age_s"] > 300:
                raise MarketDown(f"ticker {pair} basi ({t['age_s']} dtk)")
            self._t[pair] = float(t["last"])
        return self._t[pair]

    def candles(self, pair: str, tf: str, limit: int) -> list[dict]:
        key = (pair, tf)
        if key not in self._c or len(self._c[key]) < limit:
            try:
                self._c[key] = market.candles(pair, tf, limit, now=self.now)["candles"]
            except market.MarketError as e:
                raise MarketDown(str(e)) from e
        return self._c[key]


def evaluate(rule: dict, st: dict, data: Data) -> tuple[bool, str, dict]:
    """(fire?, message, new state) for one rule. `st` is this rule's previous state."""
    kind, pair, now = rule["kind"], rule["pair"], data.now
    p = label(pair)
    if kind == "price":
        px = data.price(pair)
        cond = px >= rule["level"] if rule["op"] == "above" else px <= rule["level"]
        fire = cond and not st.get("active", False)
        word = "naik ke atas" if rule["op"] == "above" else "turun ke bawah"
        return fire, f"🔔 {p} {word} {rp(rule['level'])} — sekarang {rp(px)}", {"active": cond}
    if kind == "move":
        tf = move_tf(rule["window"])
        mins = int(market.TIMEFRAMES[tf] / 60)
        cs = data.candles(pair, tf, rule["window"] // mins + 5)
        cutoff = now - rule["window"] * 60
        ref = [c for c in cs if c["time"] + mins * 60 <= cutoff]   # last candle closed at/before the window start
        if not ref:
            return False, "", st
        base, px = float(ref[-1]["close"]), data.price(pair)
        chg = (px / base - 1) * 100
        cooled = now - st.get("last_fired", 0) >= rule["cooldown"] * 60
        fire = abs(chg) >= rule["pct"] and cooled
        arrow, word = ("📈", "naik") if chg > 0 else ("📉", "turun")
        msg = f"{arrow} {p} {word} {chg:+.2f}% dalam {rule['window']} menit ({rp(base)} → {rp(px)})"
        return fire, msg, {**st, "last_fired": now if fire else st.get("last_fired", 0), "last_change_pct": round(chg, 3)}
    if kind == "rsi":
        cs = data.candles(pair, rule["tf"], max(rule["period"] * 10, 150))
        closes = [float(c["close"]) for c in cs]
        if len(closes) <= rule["period"]:
            return False, "", st
        v = analyst.rsi(closes, rule["period"])[-1]
        cond = v >= rule["level"] if rule["op"] == "above" else v <= rule["level"]
        fire = cond and not st.get("active", False)
        zone = "overbought" if rule["op"] == "above" else "oversold"
        return fire, f"⚠️ {p} RSI{rule['period']} ({rule['tf']}) = {v:.1f} — {zone} (batas {rule['level']})", \
            {"active": cond, "last_value": round(v, 2)}
    if kind == "cross":
        cs = data.candles(pair, rule["tf"], rule["slow"] * 4 + 10)
        closes = [float(c["close"]) for c in cs]
        if len(closes) < rule["slow"] + 2:
            return False, "", st
        f, s = analyst.ema(closes, rule["fast"]), analyst.ema(closes, rule["slow"])
        now_up, was_up = f[-1] > s[-1], f[-2] > s[-2]
        last_t = cs[-1]["time"]
        fire = now_up != was_up and st.get("last_cross_time") != last_t
        kind_txt = "golden cross (bullish)" if now_up else "death cross (bearish)"
        msg = f"✳️ {p} EMA{rule['fast']}/EMA{rule['slow']} {kind_txt} pada candle {rule['tf']} terakhir — harga {rp(closes[-1])}"
        return fire, msg, {**st, "last_cross_time": last_t if fire else st.get("last_cross_time"), "up": now_up}
    raise InputError(f"jenis alert tidak dikenal: {kind}")


def check(now: float | None = None, dry_run: bool = False) -> dict:
    now = time.time() if now is None else now
    rules = _read(rules_path(), [])
    state = _read(status_path(), {})
    data, fired, errors = Data(now), [], []
    for r in rules:
        try:
            fire, msg, new = evaluate(r, state.get(r["id"], {}), data)
        except MarketDown as e:
            errors.append({"id": r["id"], "error": str(e)})
            continue
        state[r["id"]] = new
        if fire:
            fired.append({"id": r["id"], "kind": r["kind"], "pair": r["pair"],
                          "message": msg + (f" · {r['note']}" if r.get("note") else "")})
    state = {k: v for k, v in state.items() if any(r["id"] == k for r in rules)}
    if not dry_run:
        _write(status_path(), state)
    if rules and len(errors) == len(rules):
        raise MarketDown("; ".join(e["error"] for e in errors[:3]))
    text = ("Indodax alert\n" + "\n".join(f["message"] for f in fired)) if fired else ""
    return {"checked_at": int(now), "rules": len(rules), "triggered": fired, "errors": errors, "text": text,
            "note": "informasi pasar, bukan perintah order"}


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Indodax price/move/RSI/cross alerts (no orders)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    ks = add.add_subparsers(dest="kind", required=True)
    for k in KINDS:
        p = ks.add_parser(k)
        p.add_argument("pair")
        p.add_argument("--note")
        p.add_argument("--pretty", action="store_true")
        if k in ("price", "rsi"):
            p.add_argument("--above", type=float)
            p.add_argument("--below", type=float)
        if k in ("rsi", "cross"):
            p.add_argument("--tf", default="60", choices=list(market.TIMEFRAMES))
        if k == "rsi":
            p.add_argument("--period", type=int, default=14)
        if k == "move":
            p.add_argument("--pct", type=float, required=True)
            p.add_argument("--window", type=int, default=60, help="menit")
            p.add_argument("--cooldown", type=int, default=60, help="menit")
        if k == "cross":
            p.add_argument("--fast", type=int, default=12)
            p.add_argument("--slow", type=int, default=26)
    sub.add_parser("list")
    rm = sub.add_parser("remove")
    rm.add_argument("id")
    ck = sub.add_parser("check")
    ck.add_argument("--dry-run", action="store_true", help="jangan simpan state (uji)")
    for name, p in sub.choices.items():
        if name != "add":
            p.add_argument("--pretty", action="store_true")
    a = ap.parse_args(argv)
    a.pretty = getattr(a, "pretty", False)
    try:
        if a.cmd == "add":
            d = add_rule(make_rule(a.kind, a))
        elif a.cmd == "list":
            rules = _read(rules_path(), [])
            d = {"rules": [{**r, "desc": describe(r)} for r in rules], "file": str(rules_path())}
        elif a.cmd == "remove":
            rules = _read(rules_path(), [])
            keep = [r for r in rules if r["id"] != a.id]
            if len(keep) == len(rules):
                raise InputError(f"alert {a.id} tidak ada")
            _write(rules_path(), keep)
            d = {"removed": a.id}
        else:
            d = check(dry_run=a.dry_run)
    except MarketDown as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (InputError, market.InputError, OSError, ValueError, KeyError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    if a.pretty:
        if a.cmd == "list":
            print("\n".join(f"{r['id']}: {r['desc']}" for r in d["rules"]) or "(belum ada alert)")
        elif a.cmd == "check":
            print(d["text"] or f"tidak ada alert ({d['rules']} aturan dicek)")
            for e in d["errors"]:
                print(f"  ! {e['id']}: {e['error']}")
        elif a.cmd == "add":
            print(("ditambahkan " if d["added"] else "sudah ada ") + f"{d['rule']['id']}: {describe(d['rule'])}")
        else:
            print(json.dumps(d, ensure_ascii=False))
    else:
        print(json.dumps(d, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
