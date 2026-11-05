#!/usr/bin/env python3
"""indodax-report: daily activity summary and Telegram delivery for the indodax-* skills (stdlib only).

    report.py daily [--date 2026-11-05] [--source paper|live] [--pretty]      # build the summary (JSON)
    report.py daily --send [--dry-run]                                        # ... and send it to Telegram
    alerts.py check | report.py send --stdin [--dry-run]                      # send any text / alerts JSON
    report.py send --text "halo"

Telegram credentials only from env TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID — never printed or logged.
Sources (read-only): broker ledger orders.json, paper_balance.json or live getInfo (via indodax-broker),
risk_state.json + HALT (indodax-risk), alerts.json (indodax-alerts), tickers (indodax-market).
Exit codes: 0 ok (also "nothing to send"), 1 Telegram not configured, 2 bad input, 3 network/API error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "indodax-broker" / "scripts"))
sys.path.insert(0, str(SKILLS / "indodax-alerts" / "scripts"))
import alerts  # noqa: E402
import broker  # noqa: E402  (brings risk, budget, market)

risk, budget = broker.risk, broker.risk.budget
WIB = risk.WIB
TG_LIMIT = 4000                      # Telegram hard limit is 4096 chars per message


class NotConfigured(Exception):
    pass


class SendError(Exception):
    pass


def rp(x) -> str:
    return "Rp " + f"{Decimal(x):,.0f}".replace(",", ".")


# ------------------------------------------------------------------ data collection (read-only)

def collect(date: str, source: str, now: float) -> dict:
    out: dict = {"date": date, "source": source, "generated_at": int(now), "warnings": []}

    # balance + valuation
    try:
        if source == "live":
            bal = broker.parse_getinfo(broker.tapi("getInfo", offset_ms=broker.server_offset_ms()))
        else:
            bal = broker._read(broker.paper_path(), None)
            if bal is None:
                raise broker.Refused("saldo paper belum ada (broker.py paper-init)")
        idr, hold = budget.parse_balance({"idr": bal["idr"], "holdings": bal.get("holdings", {})})
        px = budget.fetch_prices(hold) if hold else {}
        eq, values = budget.equity_of(idr, hold, px)
        out["balance"] = {"idr": int(idr), "equity_idr": int(eq),
                          "coins": [{"coin": c, "qty": str(q), "value_idr": int(values[c]),
                                     "pct": round(float(values[c] / eq * 100), 1) if eq else 0.0}
                                    for c, q in sorted(hold.items(), key=lambda kv: -values[kv[0]])]}
    except (broker.Refused, broker.ApiError, budget.MarketDown, budget.InputError) as e:
        out["balance"] = None
        out["warnings"].append(f"saldo: {e}")

    # daily P&L vs the equity recorded by indodax-risk at the first check of the day
    try:
        st = broker._read(risk.state_dir() / "risk_state.json", {})
    except budget.InputError as e:
        st = {}
        out["warnings"].append(str(e))
    if out["balance"] and st.get("date") == date and st.get("start_equity_idr"):
        start = Decimal(st["start_equity_idr"])
        pnl = Decimal(out["balance"]["equity_idr"]) - start
        out["day"] = {"start_equity_idr": int(start), "pnl_idr": int(pnl),
                      "pnl_pct": round(float(pnl / start * 100), 2) if start else 0.0,
                      "limit_pct": float(risk.LIMITS["daily_loss_limit_pct"])}
    else:
        out["day"] = None
    out["halt"] = risk.halted()

    # orders of the day
    try:
        rows = [r for r in broker.load_ledger() if r.get("date_wib") == date]
    except budget.InputError as e:
        rows = []
        out["warnings"].append(str(e))
    done = [r for r in rows if r.get("status") in ("filled", "submitted")]
    out["orders"] = {
        "count": len(rows),
        "by_status": {s: sum(1 for r in rows if r.get("status") == s) for s in sorted({r.get("status") for r in rows})},
        "by_mode": {m: sum(1 for r in rows if r.get("mode") == m) for m in sorted({r.get("mode") for r in rows})},
        "buy_idr": int(sum(Decimal(r["idr"]) for r in done if r["side"] == "buy")),
        "sell_idr": int(sum(Decimal(r["idr"]) for r in done if r["side"] == "sell")),
        "fees_idr": int(sum(Decimal(r.get("fee_idr") or 0) for r in done)),
        "attention": [r for r in rows if r.get("status") in ("unknown", "rejected", "submitting")],
        "items": rows[-10:],
    }

    # alert rules
    try:
        rules = alerts._read(alerts.rules_path(), [])
        out["alerts"] = [f"{r['id']}: {alerts.describe(r)}" for r in rules]
    except alerts.InputError as e:
        out["alerts"] = []
        out["warnings"].append(str(e))
    return out


def render(d: dict) -> str:
    L = [f"📊 Laporan Indodax {d['date']} ({d['source'].upper()})"]
    b = d["balance"]
    if b:
        L.append(f"Equity {rp(b['equity_idr'])} · kas {rp(b['idr'])}")
        for c in b["coins"]:
            L.append(f"  {c['coin'].upper():5s} {rp(c['value_idr'])} ({c['pct']}%) · {c['qty']}")
    if d["day"]:
        x = d["day"]
        flag = " ⛔ batas harian tercapai" if x["pnl_pct"] <= -x["limit_pct"] else ""
        L.append(f"P&L hari ini {'+' if x['pnl_idr'] >= 0 else '−'}{rp(abs(x['pnl_idr']))} ({x['pnl_pct']:+.2f}%, "
                 f"batas −{x['limit_pct']:g}%){flag}")
    if d["halt"]:
        L.append(f"🛑 Kill switch AKTIF: {d['halt']}")
    o = d["orders"]
    if o["count"]:
        modes = ", ".join(f"{m} {n}" for m, n in o["by_mode"].items())
        L.append(f"\nOrder: {o['count']} ({modes}) · beli {rp(o['buy_idr'])} · jual {rp(o['sell_idr'])} · fee {rp(o['fees_idr'])}")
        for r in o["items"]:
            t = dt.datetime.fromtimestamp(r["ts"], WIB).strftime("%H:%M")
            L.append(f"  {t} {r['mode'][0].upper()} {r['side'].upper():4s} {r['pair'].split('_')[0].upper()} "
                     f"{r['qty']} @ {rp(r['price'])} → {r['status']}")
        for r in o["attention"]:
            L.append(f"  ⚠️ cek {r['pair']} {r['side']} {r.get('client_order_id')}: {r['status']} {r.get('error', '')}".rstrip())
    else:
        L.append("\nOrder: tidak ada hari ini")
    if d["alerts"]:
        L.append(f"\nAlert aktif ({len(d['alerts'])}): " + "; ".join(a.split(": ", 1)[1] for a in d["alerts"][:8])
                 + (" …" if len(d["alerts"]) > 8 else ""))
    for w in d["warnings"]:
        L.append(f"ℹ️ {w}")
    return "\n".join(L)


# ------------------------------------------------------------------ Telegram

def tg_credentials() -> tuple[str, str]:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not tok or not chat:
        raise NotConfigured("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tidak ada di environment")
    return tok, chat


def chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines():
        while len(line) > limit:                               # pathological single line
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def send_telegram(text: str, *, opener=None, sleep=time.sleep) -> int:
    tok, chat = tg_credentials()
    prefix = os.environ.get("INDODAX_SKILLS_TG_PREFIX", "").strip()[:40]
    if prefix:
        text = f"{prefix}\n{text}"
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    sent = 0
    for part in chunks(text):
        body = urllib.parse.urlencode({"chat_id": chat, "text": part, "disable_web_page_preview": "true"}).encode()
        for attempt in range(3):
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with (opener or urllib.request.urlopen)(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
                if not data.get("ok"):
                    raise SendError(f"Telegram menolak: {str(data.get('description', '?'))[:200]}")
                sent += 1
                break
            except urllib.error.HTTPError as e:
                retry = e.code == 429 or e.code >= 500
                desc = ""
                try:
                    j = json.loads(e.read().decode())
                    desc = str(j.get("description", ""))[:200]
                    wait = float((j.get("parameters") or {}).get("retry_after", 2))
                except (ValueError, AttributeError, OSError):
                    wait = 2.0
                if not retry or attempt == 2:
                    raise SendError(f"Telegram HTTP {e.code} {desc}".replace(tok, "***").strip()) from None
                sleep(min(wait, 30))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 2:
                    raise SendError(f"Telegram tidak terjangkau ({type(e).__name__})") from None
                sleep(2 ** attempt)
    return sent


# ------------------------------------------------------------------ CLI

def text_from_stdin(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(d, dict) and "text" in d:
            return str(d["text"] or "")
    return raw


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daily report + Telegram sender for indodax-* skills")
    sub = ap.add_subparsers(dest="cmd", required=True)
    dy = sub.add_parser("daily")
    dy.add_argument("--date", help="YYYY-MM-DD (WIB), default hari ini")
    dy.add_argument("--source", choices=["paper", "live"], help="default: live bila INDODAX_LIVE=true, selain itu paper")
    dy.add_argument("--send", action="store_true")
    sd = sub.add_parser("send")
    g = sd.add_mutually_exclusive_group(required=True)
    g.add_argument("--stdin", action="store_true", help="teks atau JSON ber-field 'text' (mis. output alerts.py check)")
    g.add_argument("--text")
    for p in (dy, sd):
        p.add_argument("--dry-run", action="store_true", help="tampilkan saja, jangan kirim")
        p.add_argument("--pretty", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "daily":
            now = time.time()
            date = a.date or dt.datetime.fromtimestamp(now, WIB).date().isoformat()
            dt.date.fromisoformat(date)
            d = collect(date, a.source or ("live" if broker.live_armed() else "paper"), now)
            d["text"] = render(d)
            if a.send:
                d["sent"] = 0 if a.dry_run else send_telegram(d["text"])
            res = d
        else:
            text = text_from_stdin(sys.stdin.read()) if a.stdin else a.text
            if len(text) > 20 * TG_LIMIT:
                raise ValueError("teks terlalu panjang")
            if not text.strip():
                res = {"sent": 0, "text": "", "note": "tidak ada yang dikirim (teks kosong)"}
            else:
                res = {"sent": 0 if a.dry_run else send_telegram(text), "text": text, "dry_run": a.dry_run}
    except NotConfigured as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except SendError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (ValueError, OSError, KeyError, budget.InputError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    if a.pretty or a.dry_run:
        print(res["text"] or res.get("note", ""))
        if a.dry_run:
            print("\n(dry-run: tidak dikirim)")
    else:
        print(json.dumps(res, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
