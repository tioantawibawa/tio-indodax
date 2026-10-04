"""Final report of a paper-trading run, from the agent database (read-only).

    python -m scripts.paper_report                 # writes reports/paper_report_<date>.md and prints it
    python -m scripts.paper_report --telegram      # also sends a summary to the owner's Telegram chat
    python -m scripts.paper_report --no-live-price # value open positions at the last recorded price

The database is opened read-only; the report can be made while the agent runs or after it is stopped.
Open positions are valued at the current best bid (public API) unless --no-live-price is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.config import load_secrets, load_settings
from agent.portfolio.portfolio import Portfolio
from agent.reporting.daily_report import rp

ZERO = Decimal(0)


def _norm(reason: str) -> str:
    """Group veto reasons that only differ in numbers."""
    return re.sub(r"\d[\d.,]*", "#", reason)[:90]


@dataclass
class PaperRun:
    mode: str
    capital: Decimal
    capital_first: Decimal | None
    status: str
    pause_reason: str | None
    days: list[sqlite3.Row]
    fills: list[sqlite3.Row]
    decisions: list[sqlite3.Row]
    errors: list[sqlite3.Row]
    stoplosses: int
    portfolio: Portfolio
    marks: dict[str, Decimal] = field(default_factory=dict)
    mark_source: str = "harga tercatat terakhir"


def load_run(db_path: str, mode: str, capital: Decimal) -> PaperRun:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def state(key, default=None):
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    days = conn.execute("SELECT * FROM daily_pnl WHERE mode = ? ORDER BY date", (mode,)).fetchall()
    fills = conn.execute("SELECT * FROM fills WHERE mode = ? ORDER BY ts, id", (mode,)).fetchall()
    decisions = conn.execute("SELECT * FROM decisions WHERE mode = ? ORDER BY id", (mode,)).fetchall()
    first_ts = days[0]["date"] if days else "0000"
    errors = conn.execute("SELECT * FROM errors WHERE ts >= ? ORDER BY id", (first_ts,)).fetchall()
    stoplosses = conn.execute("SELECT COUNT(*) FROM stoploss_events WHERE mode = ?", (mode,)).fetchone()[0]

    stored_cap = state(f"{mode}:capital")
    cap = Decimal(str(stored_cap)) if stored_cap is not None else capital
    pf = Portfolio(cap)                      # same replay as agent/portfolio/persistence.py
    for r in fills:
        pf.apply_fill(r["pair"], r["side"], Decimal(r["qty"]), Decimal(r["price"]), Decimal(r["fee_idr"]),
                      datetime.fromisoformat(r["ts"]), strict=False)
    for pair, stop in (state(f"{mode}:stops", {}) or {}).items():
        if pair in pf.positions:
            pf.raise_stop(pair, Decimal(stop))
    run = PaperRun(mode, cap, Decimal(days[0]["start_equity"]) if days else None,
                   state(f"{mode}:status", "RUNNING"), state(f"{mode}:pause_reason"),
                   days, fills, decisions, errors, stoplosses, pf)
    conn.close()
    return run


def closed_trades(fills) -> list[dict]:
    """Round trips per pair (buy(s) until the position returns to zero)."""
    out, open_ = [], {}
    for f in fills:
        pair, qty = f["pair"], Decimal(f["qty"])
        t = open_.setdefault(pair, {"pair": pair, "opened": f["ts"], "qty": ZERO, "pnl": ZERO, "fees": ZERO})
        t["fees"] += Decimal(f["fee_idr"])
        if f["side"] == "buy":
            t["qty"] += qty
        else:
            t["qty"] -= qty
            t["pnl"] += Decimal(f["realized_pnl"] or 0)
            if t["qty"] <= 0:
                t["closed"] = f["ts"]
                out.append(open_.pop(pair))
    return out


def equity_curve(run: PaperRun) -> list[tuple[str, Decimal]]:
    """Capital-change-proof curve: current capital + cumulative daily PnL (end - start of each day)."""
    curve, cum = [], ZERO
    for d in run.days:
        if d["end_equity"] is None:
            continue
        cum += Decimal(d["end_equity"]) - Decimal(d["start_equity"])
        curve.append((d["date"], run.capital + cum))
    return curve


def max_drawdown_pct(curve) -> Decimal:
    peak, worst = None, ZERO
    for _, eq in curve:
        peak = eq if peak is None or eq > peak else peak
        if peak > 0:
            worst = max(worst, (peak - eq) / peak * 100)
    return worst


def build_report(run: PaperRun, tz: ZoneInfo, now: datetime) -> tuple[str, str]:
    """(full markdown report, short Telegram summary in HTML)."""
    pf = run.portfolio
    marks = {p: run.marks.get(p, pos.avg_cost) for p, pos in pf.positions.items()}
    unreal = pf.unrealized_pnl(marks)
    realized = sum((Decimal(f["realized_pnl"] or 0) for f in run.fills if f["side"] == "sell"), ZERO)
    fees = sum((Decimal(f["fee_idr"]) for f in run.fills), ZERO)
    total = realized + unreal
    equity = pf.equity(marks)
    curve = equity_curve(run)
    mdd = max_drawdown_pct(curve)
    trades = closed_trades(run.fills)
    wins = [t for t in trades if t["pnl"] > 0]
    verdicts = Counter(d["verdict"] for d in run.decisions)
    vetoes = Counter(_norm(json.loads(d["reasons_json"])[0]) for d in run.decisions
                     if d["verdict"] == "VETO" and json.loads(d["reasons_json"]))
    err_by = Counter(e["component"] for e in run.errors)
    period = f"{run.days[0]['date']} s/d {run.days[-1]['date']}" if run.days else "—"
    n_days = sum(1 for d in run.days if d["end_equity"] is not None)
    pct = lambda x: f"{float(x / run.capital * 100):+.2f}%"  # noqa: E731
    cap_note = ""
    if run.capital_first is not None and run.capital_first != run.capital:
        cap_note = f" (awal {rp(run.capital_first)}, diubah pemilik)"

    L = [f"# Laporan akhir paper trading — {now.astimezone(tz):%d %b %Y %H:%M} WIB", "",
         "## Ringkasan", "",
         "| | |", "|---|---|",
         f"| Periode | {period} ({n_days} hari tercatat) |",
         f"| Status terakhir agent | {run.status}{f' ({run.pause_reason})' if run.pause_reason else ''} |",
         f"| Modal agent | {rp(run.capital)}{cap_note} |",
         f"| Equity akhir | {rp(equity)} |",
         f"| **Total PnL** | **{rp(total)} ({pct(total)})** |",
         f"| PnL realized | {rp(realized)} |",
         f"| PnL unrealized (posisi terbuka, {run.mark_source}) | {rp(unreal)} |",
         f"| Total fee (termasuk dalam PnL) | {rp(fees)} |",
         f"| Drawdown maks (harian) | {float(mdd):.2f}% |",
         f"| Transaksi (fill) | {len(run.fills)} · trade selesai {len(trades)} "
         f"(menang {len(wins)}, kalah {len(trades) - len(wins)}) · stop-loss kena {run.stoplosses} |",
         f"| Keputusan | proposal {len(run.decisions)} · disetujui "
         f"{verdicts.get('APPROVE', 0) + verdicts.get('RESIZE', 0)} · di-veto {verdicts.get('VETO', 0)} |",
         f"| Error tercatat | {len(run.errors)} |", ""]

    L += ["## Posisi terbuka", ""]
    if pf.positions:
        L += ["| Pair | Qty | Harga beli (incl. fee) | Harga sekarang | Unrealized | Stop-loss |", "|---|---|---|---|---|---|"]
        for p, pos in pf.positions.items():
            L.append(f"| {p} | {pos.qty} | {rp(pos.avg_cost)} | {rp(marks[p])} | {rp(pos.unrealized(marks[p]))} | "
                     f"{rp(pos.stop_loss)} |")
    else:
        L.append("— tidak ada")
    L += ["", "## Semua transaksi", ""]
    if run.fills:
        L += ["| Waktu (WIB) | Pair | Sisi | Qty | Harga | Fee | PnL realized |", "|---|---|---|---|---|---|---|"]
        for f in run.fills:
            t = datetime.fromisoformat(f["ts"]).astimezone(tz)
            L.append(f"| {t:%d %b %H:%M} | {f['pair']} | {f['side'].upper()} | {f['qty']} | {rp(Decimal(f['price']))} | "
                     f"{rp(Decimal(f['fee_idr']))} | {rp(Decimal(f['realized_pnl'])) if f['realized_pnl'] else '—'} |")
    else:
        L.append("— tidak ada")
    L += ["", "## Harian", "",
          "| Tanggal | Awal hari | Akhir hari | PnL hari itu | Fee |", "|---|---|---|---|---|"]
    for d in run.days:
        end = Decimal(d["end_equity"]) if d["end_equity"] is not None else None
        start = Decimal(d["start_equity"])
        L.append(f"| {d['date']} | {rp(start)} | {rp(end)} | {rp(end - start) if end is not None else '—'} | "
                 f"{rp(Decimal(d['fees'])) if d['fees'] else 'Rp 0'} |")
    L += ["", "## Keputusan risk manager", ""]
    if vetoes:
        L += [f"- {n}× {r}" for r, n in vetoes.most_common(5)]
    else:
        L.append("- Tidak ada proposal yang di-veto.")
    L += ["", "## Error", ""]
    if run.errors:
        L += [f"- {c}: {n}×" for c, n in err_by.most_common()]
        L += ["", "Terakhir:"] + [f"- {e['ts'][:16]} {e['component']}: {e['message'][:120]}" for e in run.errors[-5:]]
    else:
        L.append("- Tidak ada error.")
    L += ["", "## Catatan", "",
          f"- Strategi trend-following harian: backtest 2018–2026 rata-rata ±1–2 trade/bulan untuk 3 pair. "
          f"{n_days} hari adalah sampel yang sangat kecil; hasil paper ini **bukan** ukuran kinerja strategi, "
          "melainkan bukti bahwa agent berjalan stabil (data, keputusan, risk manager, laporan, Telegram).",
          "- Paper trading tidak memakai uang sungguhan; fill disimulasikan di orderbook asli dengan fee + pajak + kliring.",
          "- Posisi terbuka di laporan ini dinilai dengan harga saat laporan dibuat dan **tidak dijual**."]

    tg = [f"🏁 <b>Laporan akhir paper trading</b> ({period}, {n_days} hari)",
          f"Status {run.status} · modal {rp(run.capital)} · equity {rp(equity)}",
          f"Total PnL <b>{rp(total)} ({pct(total)})</b> · realized {rp(realized)} · unrealized {rp(unreal)}",
          f"Fee {rp(fees)} · drawdown maks {float(mdd):.2f}%",
          f"Fill {len(run.fills)} · trade selesai {len(trades)} (menang {len(wins)}) · SL kena {run.stoplosses}",
          f"Proposal {len(run.decisions)} · di-veto {verdicts.get('VETO', 0)} · error {len(run.errors)}"]
    for p, pos in pf.positions.items():
        tg.append(f"Posisi {p}: {pos.qty} @ {rp(pos.avg_cost)} · sekarang {rp(marks[p])} · "
                  f"unrealized {rp(pos.unrealized(marks[p]))} · SL {rp(pos.stop_loss)}")
    return "\n".join(L) + "\n", "\n".join(tg)


async def live_marks(settings, pairs) -> dict[str, Decimal]:
    from agent.exchange.public_client import IndodaxPublicClient
    out = {}
    async with IndodaxPublicClient.from_settings(settings.exchange) as pub:
        for p in pairs:
            book = await pub.depth(p)
            if book.best_bid:
                out[p] = book.best_bid
    return out


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default="reports")
    ap.add_argument("--telegram", action="store_true", help="send the summary to TELEGRAM_CHAT_ID")
    ap.add_argument("--no-live-price", action="store_true")
    args = ap.parse_args(argv)

    sec = load_secrets(".env")
    s = load_settings(sec.AGENT_SETTINGS)
    db_path = args.db or s.storage.db_path
    if not Path(db_path).exists():
        print(f"database not found: {db_path}")
        return 2
    tz = ZoneInfo(s.reporting.timezone)
    run = load_run(db_path, args.mode, Decimal(str(s.risk.agent_capital_idr)))
    if run.portfolio.positions and not args.no_live_price:
        try:
            run.marks = await live_marks(s, list(run.portfolio.positions))
            run.mark_source = "harga bid saat laporan dibuat"
        except Exception as e:  # noqa: BLE001
            print(f"(harga live tidak tersedia: {type(e).__name__}; memakai harga beli)")
    if run.portfolio.positions and not run.marks:
        last = run.days[-1] if run.days else None
        run.mark_source = "harga beli (harga live tidak dipakai)"
        if last is not None and last["unrealized"] is not None and len(run.portfolio.positions) == 1:
            (pair, pos), = run.portfolio.positions.items()
            run.marks = {pair: pos.avg_cost + Decimal(last["unrealized"]) / pos.qty}
            run.mark_source = "harga tercatat terakhir"
    now = datetime.now(tz)
    md, tg = build_report(run, tz, now)
    Path(args.out).mkdir(parents=True, exist_ok=True)
    out = Path(args.out) / f"{args.mode}_report_{now:%Y%m%d_%H%M}.md"
    out.write_text(md)
    print(md)
    print(f"report saved to {out}")
    if args.telegram:
        if not (sec.TELEGRAM_BOT_TOKEN.get_secret_value() and sec.TELEGRAM_CHAT_ID):
            print("Telegram not configured in .env")
            return 1
        from telegram import Bot

        from agent.reporting.telegram_bot import TelegramNotifier
        async with Bot(sec.TELEGRAM_BOT_TOKEN.get_secret_value()) as bot:
            await TelegramNotifier(bot, int(sec.TELEGRAM_CHAT_ID)).send(tg)
        print("summary sent to Telegram")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
