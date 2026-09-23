"""Backtest report: Markdown summary + trades/equity CSVs."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .backtester import BacktestResult


def _idr(x) -> str:
    return "—" if x is None else f"Rp {x:,.0f}".replace(",", ".")


def verdict_text(m: dict) -> str:
    net, before = m["net_pnl_idr"], m["pnl_before_fees_idr"]
    if m["trades_closed"] == 0:
        return "**Tidak ada trade yang selesai** — strategi terlalu selektif atau data kurang; hasil belum bisa dinilai."
    if net > 0:
        return f"**Hasil setelah biaya POSITIF** ({_idr(net)})."
    if before > 0:
        return (f"**Hasil setelah biaya NEGATIF** ({_idr(net)}). Sebelum fee strategi untung {_idr(before)}, "
                "jadi fee memakan seluruh edge.")
    return f"**Hasil NEGATIF bahkan sebelum fee** ({_idr(before)} sebelum fee, {_idr(net)} setelah fee)."


def write_report(r: BacktestResult, out_dir: str | Path, title: str = "Backtest") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    m = r.metrics
    with (out / "trades.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pair", "setup", "entry_time", "entry_price", "qty", "exit_time", "exit_price",
                    "exit_reason", "fees_idr", "pnl_idr"])
        for t in r.trades:
            w.writerow([t.pair, t.setup, t.entry_time.isoformat(), t.entry_price, t.qty,
                        t.exit_time.isoformat() if t.exit_time else "", t.exit_price or "",
                        t.exit_reason, round(t.fees, 2), "" if t.pnl is None else round(t.pnl, 2)])
    r.equity.rename("equity_idr").to_csv(out / "equity.csv", index_label="time")
    (out / "metrics.json").write_text(json.dumps(m, indent=1, default=str))

    lines = [
        f"# {title}",
        "",
        f"Periode: {r.start:%Y-%m-%d %H:%M} → {r.end:%Y-%m-%d %H:%M} UTC ({m['period_days']} hari)  ",
        f"Asumsi: spread {r.config.spread_pct}%, slippage exit {r.config.slippage_pct}%, "
        "fee/pajak/kliring dari config & /api/pairs.",
        "",
        verdict_text(m),
        "",
        "| Metrik | Nilai |",
        "|---|---|",
        f"| Modal awal → akhir | {_idr(m['initial_equity_idr'])} → {_idr(m['final_equity_idr'])} |",
        f"| Return total | {m['total_return_pct']}% (disetahunkan {m['annualized_return_pct']}%) |",
        f"| Max drawdown | {m['max_drawdown_pct']}% |",
        f"| Sharpe (harian, disetahunkan) | {m['sharpe_daily_annualized']} |",
        f"| Trade selesai / masih terbuka | {m['trades_closed']} / {m['trades_open_at_end']} |",
        f"| Win rate | {m['win_rate_pct']}% |",
        f"| Rata-rata menang / kalah | {_idr(m['avg_win_idr'])} / {_idr(m['avg_loss_idr'])} |",
        f"| Profit factor | {m['profit_factor']} |",
        f"| **PnL sebelum fee** | {_idr(m['pnl_before_fees_idr'])} |",
        f"| **Total fee (fee+pajak+kliring)** | {_idr(m['total_fees_idr'])} |",
        f"| **PnL bersih** | {_idr(m['net_pnl_idr'])} |",
        f"| Estimasi biaya spread+slippage saat exit | {_idr(m['spread_slippage_cost_idr'])} |",
        "",
        "**Buy & hold (dari data pertama pair dalam periode):** " + ", ".join(f"{k} {v:+.2f}%" for k, v in m["buy_and_hold_pct"].items()),
        "",
        f"**Alasan exit:** {m['exit_reasons']}  ",
        f"**Setup:** {m['setups']}  ",
        f"**Keputusan risk manager:** {m['verdicts']}",
        "",
        "**Alasan veto terbanyak:**",
        "",
        *[f"- {n}× {reason}" for reason, n in m["top_veto_reasons"]],
        "",
        "**Event:** " + ("; ".join(f"{t} {e}" for t, e in m["events"]) or "tidak ada"),
        "",
    ]
    path = out / "REPORT.md"
    path.write_text("\n".join(lines))
    return path
