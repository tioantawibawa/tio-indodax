#!/usr/bin/env python3
"""indodax-cycle: one automatic PAPER trading cycle through the whole skill pipeline (stdlib only).

    cycle.py run [--pairs btc_idr,eth_idr,sol_idr] [--tf 240] [--stop-pct 5] [--dry-run] [--notify] [--pretty]
    cycle.py status [--pretty]                     # tracked paper positions with entry and stop

Per run: analyst → (sell first) budget → risk → broker, then buys the same way.
  SELL a held coin when price <= its stop, or when the analyst bias turns bearish.
  BUY a coin not yet held when bias is bullish and RSI is not overbought; stop = entry × (1 − stop%).
  After a stop-loss exit the coin is not bought again for --reentry-hours (default 24).
This is a simple, UNVALIDATED trend rule meant for paper testing of the pipeline — not a proven strategy.

PAPER ONLY BY CONSTRUCTION: the cycle never passes --confirm to the broker and refuses to start when
INDODAX_LIVE=true. Live orders always need a human to confirm each order.
A lock file prevents overlapping runs. Exit codes: 0 ok, 1 refused (live armed / locked), 2 bad input,
3 market data unavailable for every pair.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

SKILLS = Path(__file__).resolve().parents[2]
for s in ("indodax-report", "indodax-broker", "indodax-analyst", "indodax-market"):
    sys.path.insert(0, str(SKILLS / s / "scripts"))
import analyst  # noqa: E402
import broker  # noqa: E402
import market  # noqa: E402
import report  # noqa: E402

risk, budget = broker.risk, broker.risk.budget
D = budget.D


class Refused(Exception):
    pass


def state_path() -> Path:
    return risk.state_dir() / "cycle_state.json"


def load_state() -> dict:
    return broker._read(state_path(), {})


def load_positions() -> dict:
    return load_state().get("positions", {})


def save_state(pos: dict, cooldown: dict, last_run: dict) -> None:
    broker._write(state_path(), {"positions": pos, "cooldown_until": cooldown, "last_run": last_run})


def get_signal(pair: str, tf: str) -> dict:
    data = market.candles(pair, tf, 200)
    return analyst.analyse(*analyst.series_from(data))


def paper_balance() -> tuple[Decimal, dict]:
    bal = broker._read(broker.paper_path(), None)
    if bal is None:
        raise Refused("saldo paper belum ada — jalankan: broker.py paper-init --idr <modal>")
    return budget.parse_balance({"idr": bal["idr"], "holdings": bal.get("holdings", {})})


def execute(order_prop: dict, side: str, stop_pct: Decimal | None, px: dict, dry: bool) -> dict:
    """budget proposal → risk.evaluate → broker.cmd_order (paper). Returns a result row."""
    idr, hold = paper_balance()
    coin = budget.coin_of(order_prop["pair"])
    order = {"pair": order_prop["pair"], "coin": coin, "side": side,
             "idr": Decimal(order_prop["order_idr"]).quantize(Decimal(1)),
             "amount": Decimal(order_prop["est_amount"]).quantize(Decimal("0.00000001")),
             "price": Decimal(order_prop["price"]), "stop_pct": stop_pct}
    eq, _ = budget.equity_of(idr, hold, px)
    _, start = risk.day_start(eq, time.time())
    r = risk.evaluate(order, idr, hold, px, dict(risk.LIMITS), start, risk.halted())
    if r["decision"] != "ALLOW":
        return {"status": "blocked", "failed": r["failed"]}
    if dry:
        return {"status": "dry-run", "idr": str(order["idr"]), "amount": str(order["amount"])}
    r["checked_at"] = int(time.time())
    tmp = risk.state_dir() / "cycle_risk.json"
    broker._write(tmp, r)
    try:
        res = broker.cmd_order(SimpleNamespace(risk=str(tmp), confirm=False, max_age=60))
    finally:
        tmp.unlink(missing_ok=True)
    return {"status": res["status"], "mode": res["mode"], "price": res["price"], "qty": res["qty"], "idr": res["idr"],
            "fee_idr": res.get("fee_idr")}


def run(pairs: list[str], tf: str, stop_pct: Decimal, dry: bool, signal_fn=None,
        reentry_hours: float = 24) -> dict:
    signal_fn = signal_fn or get_signal
    if broker.live_armed():
        raise Refused("INDODAX_LIVE=true — siklus otomatis hanya untuk PAPER; order live wajib dikonfirmasi manusia")
    now = int(time.time())
    idr, hold = paper_balance()
    st = load_state()
    positions = st.get("positions", {})
    cooldown = {c: t for c, t in st.get("cooldown_until", {}).items() if t > now}
    coins = {budget.coin_of(p) for p in pairs} | set(hold)
    px = budget.fetch_prices(coins)
    rows, failures = [], 0
    signals = {}
    for p in sorted({f"{c}_idr" for c in coins}):
        try:
            signals[p] = signal_fn(p, tf)
        except (market.MarketError, analyst.InputError, market.InputError) as e:
            signals[p] = None
            failures += 1
            rows.append({"pair": p, "action": "skip", "reason": f"data analyst tidak ada: {e}"})

    # 1) exits first (frees cash, reduces risk)
    for coin in sorted(hold):
        p = f"{coin}_idr"
        sig, price = signals.get(p), px[coin]["bid"]
        pos = positions.get(coin, {})
        reason, stopped = None, False
        if pos.get("stop") and price <= D(pos["stop"]):
            reason, stopped = f"harga {price} ≤ stop {pos['stop']}", True
        elif sig and sig["bias"] == "bearish":
            reason = "bias bearish: " + "; ".join(sig["reasons"])
        if not reason:
            rows.append({"pair": p, "action": "hold", "reason": f"bias {sig['bias'] if sig else '?'}"
                         + (f", stop {pos['stop']}" if pos.get("stop") else "")})
            continue
        prop = budget.size(coin, "sell", idr, hold, px, dict(budget.DEFAULTS), want_pct=D(100))
        if not prop["ok"]:
            rows.append({"pair": p, "action": "sell", "reason": reason, "result": {"status": "dust", "detail": prop["reason"]}})
            continue
        res = _safe(execute, prop, "sell", None, px, dry)
        rows.append({"pair": p, "action": "sell", "reason": reason, "result": res})
        if res.get("status") == "filled":
            positions.pop(coin, None)
            if stopped:
                cooldown[coin] = now + int(reentry_hours * 3600)
            idr, hold = paper_balance()

    # 2) entries
    for p in pairs:
        coin, sig = budget.coin_of(p), signals.get(p)
        if sig is None or coin in hold:
            continue
        if coin in cooldown:
            rows.append({"pair": p, "action": "wait", "reason": "jeda setelah stop-loss sampai "
                         + time.strftime("%d-%m %H:%M", time.localtime(cooldown[coin]))})
            continue
        if sig["bias"] != "bullish" or sig["signals"]["rsi_zone"] == "overbought":
            rows.append({"pair": p, "action": "wait",
                         "reason": f"bias {sig['bias']}, RSI {sig['signals']['rsi_zone']}"})
            continue
        prop = budget.size(coin, "buy", idr, hold, px, dict(budget.DEFAULTS), stop_pct=stop_pct)
        if not prop["ok"]:
            rows.append({"pair": p, "action": "buy", "reason": "bias bullish",
                         "result": {"status": "no-budget", "detail": prop["reason"]}})
            continue
        res = _safe(execute, prop, "buy", stop_pct, px, dry)
        rows.append({"pair": p, "action": "buy", "reason": "bias bullish: " + "; ".join(sig["reasons"]), "result": res})
        if res.get("status") == "filled":
            entry = D(res["price"])
            positions[coin] = {"entry": res["price"], "stop": str((entry * (1 - stop_pct / 100)).quantize(Decimal(1))),
                               "qty": res["qty"], "opened_at": now}
            idr, hold = paper_balance()

    if signals and failures == len(signals):
        raise budget.MarketDown("data analyst tidak tersedia untuk semua pair")
    eq, _ = budget.equity_of(idr, hold, px)
    summary = {"ran_at": now, "tf": tf, "dry_run": dry, "equity_idr": int(eq), "idr": int(idr),
               "orders": sum(1 for r in rows if r.get("result", {}).get("status") == "filled"),
               "problems": [r for r in rows if r.get("result", {}).get("status") in ("blocked", "error", "refused")]}
    if not dry:
        positions = {c: v for c, v in positions.items() if c in hold}
        save_state(positions, cooldown, summary)
    return {**summary, "rows": rows, "positions": positions, "text": render(summary, rows),
            "note": "siklus PAPER otomatis — aturan tren sederhana yang belum divalidasi"}


def _safe(fn, *a) -> dict:
    try:
        return fn(*a)
    except broker.Refused as e:
        return {"status": "refused", "detail": str(e)}
    except (broker.ApiError, budget.MarketDown, budget.InputError) as e:
        return {"status": "error", "detail": str(e)}


def render(summary: dict, rows: list[dict]) -> str:
    L = [f"🤖 Siklus paper ({summary['tf']}) · equity {report.rp(summary['equity_idr'])} · kas {report.rp(summary['idr'])}"
         + (" · DRY-RUN" if summary["dry_run"] else "")]
    icon = {"buy": "🟢", "sell": "🔴", "hold": "⏸", "wait": "·", "skip": "⚠️"}
    for r in rows:
        res = r.get("result")
        tail = ""
        if res:
            tail = f" → {res['status']}"
            if res.get("qty"):
                tail += f" {res['qty']} @ {report.rp(res['price'])}"
            if res.get("failed"):
                tail += f" ({', '.join(res['failed'])})"
            if res.get("detail"):
                tail += f" ({res['detail']})"
        L.append(f"{icon.get(r['action'], '')} {r['pair'].upper()} {r['action'].upper()}: {r['reason']}{tail}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Automatic PAPER cycle for the indodax-* pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--pairs", default="btc_idr,eth_idr,sol_idr")
    r.add_argument("--tf", default="240", choices=list(market.TIMEFRAMES))
    r.add_argument("--stop-pct", default="5")
    r.add_argument("--reentry-hours", type=float, default=24, help="jeda beli ulang setelah kena stop")
    r.add_argument("--dry-run", action="store_true", help="hitung keputusan tanpa order paper")
    r.add_argument("--notify", action="store_true", help="kirim ke Telegram bila ada order/masalah")
    st = sub.add_parser("status")
    for p in (r, st):
        p.add_argument("--pretty", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            d = broker._read(state_path(), {"positions": {}})
            print(json.dumps(d, ensure_ascii=False, indent=1 if a.pretty else None))
            return 0
        pairs = [market.ticker_name(market.pair_id(x)) for x in a.pairs.split(",") if x.strip()]
        if not pairs or len(pairs) > 20 or any(not p.endswith("_idr") for p in pairs):
            raise budget.InputError("--pairs: 1..20 pair IDR")
        stop = D(a.stop_pct, "--stop-pct")
        if not D(1) <= stop <= D(30):
            raise budget.InputError("--stop-pct 1..30")
        risk.state_dir().mkdir(parents=True, exist_ok=True)
        with open(risk.state_dir() / "cycle.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused("siklus lain masih berjalan") from None
            if not 0 <= a.reentry_hours <= 24 * 30:
                raise budget.InputError("--reentry-hours 0..720")
            d = run(pairs, a.tf, stop, a.dry_run, reentry_hours=a.reentry_hours)
        if a.notify and (d["orders"] or d["problems"]):
            try:
                d["notified"] = report.send_telegram(d["text"])
            except (report.NotConfigured, report.SendError) as e:
                d["notify_error"] = str(e)
    except Refused as e:
        print(json.dumps({"refused": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except budget.MarketDown as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (budget.InputError, broker.ApiError, OSError, ValueError, KeyError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(d["text"] if a.pretty else json.dumps(d, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
