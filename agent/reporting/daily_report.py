"""Daily report (Telegram HTML). Pure function of a ReportData snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .telegram_bot import esc


@dataclass(frozen=True)
class FillLine:
    ts_local: datetime
    pair: str
    side: str
    price: Decimal
    qty: Decimal
    fee: Decimal
    pnl: Decimal | None
    reason: str


@dataclass(frozen=True)
class PositionLine:
    pair: str
    qty: Decimal
    avg_cost: Decimal
    mark: Decimal
    stop: Decimal | None
    take_profit: Decimal | None

    @property
    def unrealized(self) -> Decimal:
        return (self.mark - self.avg_cost) * self.qty


@dataclass(frozen=True)
class ReportData:
    mode: str
    status: str
    pause_reason: str | None
    uptime: timedelta
    now_local: datetime
    day_start_equity: Decimal
    equity: Decimal
    peak_equity: Decimal
    realized_today: Decimal
    fees_today: Decimal
    fills: list[FillLine]
    positions: list[PositionLine]
    proposals: int
    approved: int
    vetoed: int
    top_veto_reasons: list[tuple[str, int]]
    errors: list[tuple[datetime, str, str]]
    outlook: str
    title: str = "Laporan harian"
    notes: list[str] = field(default_factory=list)


def rp(x: Decimal | float | None) -> str:
    if x is None:
        return "—"
    v = float(x)
    s = f"{abs(v):,.0f}".replace(",", ".")
    return f"{'-' if v < 0 else ''}Rp {s}"


def _pct(a: Decimal, b: Decimal) -> str:
    return f"{float(a / b * 100):+.2f}%" if b else "—"


def _uptime(td: timedelta) -> str:
    d, rem = divmod(int(td.total_seconds()), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}h {h}j {rem // 60}m" if d else f"{h}j {rem // 60}m"


def build_daily_report(r: ReportData) -> str:
    unreal = sum((p.unrealized for p in r.positions), Decimal(0))
    dd = (r.peak_equity - r.equity) / r.peak_equity * 100 if r.peak_equity > 0 else Decimal(0)
    status = r.status + (f" ({esc(r.pause_reason)})" if r.pause_reason else "")
    out = [
        f"<b>📊 {esc(r.title)} — {r.now_local:%d %b %Y %H:%M} WIB</b>",
        f"Mode <b>{esc(r.mode.upper())}</b> · Status <b>{status}</b> · Uptime {_uptime(r.uptime)}",
        "",
        "<b>Modal</b>",
        f"Awal hari {rp(r.day_start_equity)} → sekarang {rp(r.equity)} ({_pct(r.equity - r.day_start_equity, r.day_start_equity)})",
        f"PnL realized hari ini {rp(r.realized_today)} · unrealized {rp(unreal)}",
        f"Drawdown dari puncak {float(dd):.2f}% (puncak {rp(r.peak_equity)})",
        "",
        f"<b>Transaksi hari ini ({len(r.fills)})</b>",
    ]
    if not r.fills:
        out.append("— tidak ada")
    for f in r.fills:
        pnl = f" · PnL {rp(f.pnl)}" if f.pnl is not None and f.side == "sell" else ""
        out.append(f"{f.ts_local:%H:%M} {esc(f.pair)} <b>{f.side.upper()}</b> {f.qty} @ {rp(f.price)} "
                   f"· fee {rp(f.fee)}{pnl} · {esc(f.reason)}")
    out += ["", f"<b>Posisi terbuka ({len(r.positions)})</b>"]
    if not r.positions:
        out.append("— tidak ada")
    for p in r.positions:
        tp = f" · TP {rp(p.take_profit)}" if p.take_profit else " · TP: trailing"
        out.append(f"{esc(p.pair)} {p.qty} @ {rp(p.avg_cost)} · harga {rp(p.mark)} · "
                   f"unrealized {rp(p.unrealized)} · SL {rp(p.stop)}{tp}")
    out += ["", "<b>Keputusan</b>",
            f"Proposal {r.proposals} · disetujui {r.approved} · di-veto {r.vetoed}"]
    for reason, n in r.top_veto_reasons[:3]:
        out.append(f"  {n}× {esc(reason)}")
    out += ["", f"<b>Total fee hari ini</b> {rp(r.fees_today)}", "", f"<b>Error/anomali ({len(r.errors)})</b>"]
    if not r.errors:
        out.append("— tidak ada")
    for ts, comp, msg in r.errors[-5:]:
        out.append(f"{ts:%H:%M} [{esc(comp)}] {esc(msg[:150])}")
    out += ["", "<b>Pandangan pasar</b>", esc(r.outlook)]
    for n in r.notes:
        out.append(f"ℹ️ {esc(n)}")
    return "\n".join(out)
