"""Why did (or didn't) the agent trade? Read-only diagnosis of the agent database.

    python -m scripts.trade_diagnostics            # last 7 days, mode from the latest heartbeat
    python -m scripts.trade_diagnostics --days 14

Shows the funnel proposal -> risk verdict -> order -> fill, the grouped veto reasons with a plain
explanation, orders that were cancelled/rejected, what the portfolio manager is skipping right now
(target vs held) and errors. Never writes the database, never needs API keys.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.config import load_secrets, load_settings

EXPLAIN = [
    ("too small to exit at its stop", "posisi terlalu kecil: nilai di harga stop < 1,2× minimum order Indodax "
                                      "(stop-loss tidak akan bisa dijual). Modal kecil + stop lebar."),
    ("volume", "volume 24 jam koin di bawah batas market.min_volume_24h_idr (likuiditas kurang)."),
    ("spread", "spread bid/ask lebih lebar dari market.max_spread_pct."),
    ("slippage", "orderbook tipis: perkiraan slippage melebihi batas."),
    ("below exchange minimum", "ukuran order di bawah minimum order Indodax."),
    ("cooldown after stop-loss", "masa tunggu 30 menit setelah stop-loss di pair yang sama."),
    ("orders per hour", "batas jumlah order per jam tercapai."),
    ("orders per day", "batas jumlah order per hari tercapai."),
    ("max open positions", "jumlah posisi maksimum tercapai."),
    ("max_position", "batas ukuran per posisi tercapai."),
    ("max_asset_exposure", "batas eksposur per aset tercapai."),
    ("no room under", "tidak ada ruang di bawah salah satu batas (modal/kas/saldo IDR)."),
    ("exchange_free_idr", "saldo IDR bebas di Indodax tidak cukup."),
    ("agent_cash", "kas agent tidak cukup."),
    ("daily loss", "batas rugi harian tercapai — entry dihentikan sampai besok."),
    ("KILL SWITCH", "kill switch drawdown aktif (HALTED)."),
    ("PAUSED", "agent sedang PAUSED (/pause, rugi harian, jam VPS, rekonsiliasi atau deadman)."),
    ("HALTED", "agent HALTED — butuh /resume."),
    ("reconcile", "saldo exchange tidak cocok dengan catatan agent — order diblokir."),
    ("expected move", "target keuntungan < 2× biaya transaksi."),
    ("maintenance", "pair sedang maintenance/suspend di Indodax."),
]


def norm(reason: str) -> str:
    return re.sub(r"\d[\d.,]*", "#", reason)[:110]


def explain(reason: str) -> str:
    low = reason.lower()
    for key, text in EXPLAIN:
        if key.lower() in low:
            return text
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    sec = load_secrets(".env")
    s = load_settings(sec.AGENT_SETTINGS)
    path = args.db or s.storage.db_path
    if not Path(path).exists():
        print(f"database not found: {path}")
        return 2
    tz = ZoneInfo(s.reporting.timezone)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row

    def state(key, default=None):
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    hb = state("office:heartbeat") or {}
    mode = hb.get("mode") or ("live" if str(sec.MODE.value) == "live" else "paper")
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    print(f"# Diagnosis transaksi — mode {mode.upper()}, {args.days} hari terakhir "
          f"(sejak {datetime.fromisoformat(since).astimezone(tz):%d %b %H:%M} WIB)\n")

    # ---- current status
    status = state(f"{mode}:status", "RUNNING")
    reason = state(f"{mode}:pause_reason")
    hb_age = None
    if hb.get("ts"):
        hb_age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb["ts"])).total_seconds() / 60
    print(f"Status agent: {status}{f' ({reason})' if reason else ''} · siklus terakhir "
          f"{f'{hb_age:.0f} menit lalu' if hb_age is not None else '—'} · rekonsiliasi "
          f"{'OK' if hb.get('reconcile_ok', True) else 'TIDAK COCOK'}")
    if hb.get("exchange_free_idr") is not None:
        print(f"IDR bebas di Indodax: Rp {float(hb['exchange_free_idr']):,.0f}".replace(",", "."))

    # ---- funnel
    dec = conn.execute("SELECT * FROM decisions WHERE mode = ? AND ts >= ? ORDER BY id", (mode, since)).fetchall()
    orders = conn.execute("SELECT * FROM orders WHERE mode = ? AND ts_created >= ?", (mode, since)).fetchall()
    fills = conn.execute("SELECT * FROM fills WHERE mode = ? AND ts >= ?", (mode, since)).fetchall()
    verdicts = Counter(d["verdict"] for d in dec)
    ost = Counter(o["status"] for o in orders)
    print("\n## Corong keputusan")
    print(f"Proposal {len(dec)} → disetujui {verdicts.get('APPROVE', 0) + verdicts.get('RESIZE', 0)} "
          f"(di-RESIZE {verdicts.get('RESIZE', 0)}) · di-veto {verdicts.get('VETO', 0)}")
    print(f"Order dikirim {len(orders)} → FILLED {ost.get('FILLED', 0)} · PARTIAL {ost.get('PARTIALLY_FILLED', 0)} · "
          f"CANCELLED {ost.get('CANCELLED', 0)} · REJECTED {ost.get('REJECTED', 0)} · "
          f"terbuka {ost.get('NEW', 0) + ost.get('PENDING_SUBMIT', 0)}")
    print(f"Fill tercatat {len(fills)}")

    # ---- veto reasons
    vetoes = [d for d in dec if d["verdict"] == "VETO"]
    if vetoes:
        print("\n## Alasan veto (manajer risiko)")
        by = Counter()
        pairs = defaultdict(Counter)
        for d in vetoes:
            r = (json.loads(d["reasons_json"]) or [""])[0]
            by[norm(r)] += 1
            pairs[norm(r)][d["pair"]] += 1
        for r, n in by.most_common(10):
            ps = ", ".join(f"{p.split('_')[0].upper()}×{c}" for p, c in pairs[r].most_common(4))
            print(f"- {n}× {r}  [{ps}]")
            e = explain(r)
            if e:
                print(f"    → {e}")

    # ---- orders that did not fill
    bad = [o for o in orders if o["status"] in ("CANCELLED", "REJECTED")]
    if bad:
        errs = conn.execute("SELECT * FROM errors WHERE component = 'executor' AND ts >= ?", (since,)).fetchall()
        print("\n## Order yang tidak terisi")
        for o in bad[-15:]:
            t = datetime.fromisoformat(o["ts_created"]).astimezone(tz)
            why = next((e["message"] for e in errs if o["client_order_id"] in e["message"]), "")
            if not why and o["status"] == "CANCELLED":
                why = "tidak terisi dalam batas waktu (entry TTL 120 dtk) atau dibatalkan saat rebalance/kill"
            print(f"- {t:%d %b %H:%M} {o['pair']} {o['side'].upper()} {o['qty']} @ {o['price']} → {o['status']}"
                  f"{f' · {why[:140]}' if why else ''}")

    # ---- portfolio manager: what it is skipping right now
    plan = state(f"{mode}:pm:plan")
    if plan:
        print("\n## Manajer portofolio — target vs dipegang (saat ini)")
        made = datetime.fromisoformat(plan["made_at"]).astimezone(tz)
        print(f"Rencana {made:%d %b %H:%M} WIB · alasan: {plan.get('reason', '—')}")
        held = defaultdict(float)
        for f in conn.execute("SELECT pair, side, qty FROM fills WHERE mode = ?", (mode,)):
            held[f["pair"]] += float(f["qty"]) * (1 if f["side"] == "buy" else -1)
        notes = {p: d.get("note", "") for p, d in (hb.get("pairs") or {}).items()}
        for p, w in sorted(plan["weights"].items(), key=lambda kv: -kv[1]):
            h = "dipegang" if held.get(p, 0) > 1e-12 else "tidak dipegang"
            print(f"- {p.split('_')[0].upper()}: target {w * 100:.0f}% · {h}"
                  f"{f' · {notes[p]}' if notes.get(p) else ''}")

    # ---- errors
    errs = conn.execute("SELECT component, message FROM errors WHERE ts >= ?", (since,)).fetchall()
    if errs:
        print("\n## Error")
        for (comp, msg), n in Counter((e["component"], norm(e["message"])[:100]) for e in errs).most_common(8):
            print(f"- {n}× [{comp}] {msg}")
    print("\nKirim seluruh output ini ke developer bila perlu dianalisis.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
