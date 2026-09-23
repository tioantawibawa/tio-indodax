"""Risk manager: the single gate between a TradeProposal and the exchange.

Every hard limit from ``config/settings.yaml`` → ``risk:`` is enforced here,
in code. The limits object is frozen and loaded once at startup; nothing
passed to :meth:`RiskManager.evaluate` can change it. The LLM analyst has no
access to this module — it can only adjust a proposal's confidence or
suppress a proposal before it gets here.

Verdicts:
- APPROVE: send as proposed (after exchange rounding).
- RESIZE:  send with a smaller quantity (limits capped the size).
- VETO:    do not send. ``reasons`` explains why.

Design decisions (documented in README, confirm with owner):
- Emergency stop-loss exits (market sell of an existing position) are exempt
  from the hourly/daily order-count limits and from PAUSED / daily-loss /
  drawdown states, because blocking a stop-loss increases risk. They are still
  subject to the emergency slippage cap, whitelist, no-shorting and
  reconciliation checks.
- In HALTED only emergency exits pass; ordinary exits wait for `/resume`.
- Percent limits are measured against ``agent_capital_idr`` (the agent's
  allocation), drawdown against the agent's own peak equity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from agent.analysis.costs import CostModel
from agent.config import FeeSettings, MarketSettings, RiskLimits
from agent.exchange.models import OrderBook, PairInfo, Ticker
from agent.exchange.pairs import base_asset
from agent.strategy.strategy import TradeProposal

ZERO = Decimal(0)
HUNDRED = Decimal(100)


class Verdict(str, Enum):
    APPROVE = "APPROVE"
    RESIZE = "RESIZE"
    VETO = "VETO"


class AgentStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"      # /pause or daily loss limit: no new entries
    HALTED = "HALTED"      # kill switch / drawdown: needs manual /resume


@dataclass(frozen=True)
class RiskContext:
    """Snapshot of everything the risk manager needs. Built fresh each cycle
    from the portfolio ledger, the DB and the exchange."""

    now: datetime
    status: AgentStatus
    reconciliation_ok: bool
    equity_idr: Decimal                     # agent ledger equity (marked to bid)
    peak_equity_idr: Decimal                # highest equity ever recorded by the agent
    day_start_equity_idr: Decimal           # equity at 00:00 Asia/Jakarta
    cash_idr: Decimal                       # agent ledger free cash
    position_qty: Mapping[str, Decimal]     # pair -> qty held
    position_value_idr: Mapping[str, Decimal]  # pair -> marked value
    pending_buy_idr: Mapping[str, Decimal]  # pair -> value of open (unfilled) buy orders
    orders_last_hour: int
    orders_today: int
    last_stoploss_at: Mapping[str, datetime]
    exchange_free_idr: Decimal | None = None  # live: actual free IDR on the exchange


@dataclass(frozen=True)
class MarketView:
    info: PairInfo
    ticker: Ticker
    orderbook: OrderBook


@dataclass(frozen=True)
class RiskDecision:
    verdict: Verdict
    proposal: TradeProposal
    qty: Decimal                 # approved quantity (0 on VETO)
    price: Decimal               # approved (rounded) price
    reasons: tuple[str, ...]
    trigger_halt: bool = False
    trigger_daily_stop: bool = False
    metrics: dict = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.verdict in (Verdict.APPROVE, Verdict.RESIZE)


class RiskManager:
    def __init__(self, limits: RiskLimits, market: MarketSettings, fees: FeeSettings,
                 whitelist: tuple[str, ...] | None = None):
        # Pydantic models are frozen: attribute assignment raises.
        self._limits = limits
        self._market = market
        self._fees = fees
        self._whitelist = frozenset(whitelist if whitelist is not None else market.whitelist)
        self._costs = CostModel(fees)

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    # ------------------------------------------------------------ helpers

    def _capital(self) -> Decimal:
        return Decimal(str(self._limits.agent_capital_idr))

    def _pct(self, x: float) -> Decimal:
        return Decimal(str(x))

    def drawdown_pct(self, ctx: RiskContext) -> Decimal:
        if ctx.peak_equity_idr <= 0:
            return ZERO
        return max(ZERO, (ctx.peak_equity_idr - ctx.equity_idr) / ctx.peak_equity_idr * HUNDRED)

    def daily_loss_pct(self, ctx: RiskContext) -> Decimal:
        return max(ZERO, (ctx.day_start_equity_idr - ctx.equity_idr) / self._capital() * HUNDRED)

    def check_account(self, ctx: RiskContext) -> tuple[bool, bool, list[str]]:
        """(trigger_halt, trigger_daily_stop, reasons) — also used by the main
        loop every cycle, independent of any proposal."""
        reasons = []
        dd = self.drawdown_pct(ctx)
        dl = self.daily_loss_pct(ctx)
        halt = dd >= self._pct(self._limits.max_drawdown_pct)
        daily = dl >= self._pct(self._limits.daily_loss_limit_pct)
        if halt:
            reasons.append(f"KILL SWITCH: drawdown {dd:.2f}% >= {self._limits.max_drawdown_pct}%")
        if daily:
            reasons.append(f"daily loss {dl:.2f}% >= limit {self._limits.daily_loss_limit_pct}%")
        return halt, daily, reasons

    # ------------------------------------------------------------ evaluate

    def evaluate(self, p: TradeProposal, ctx: RiskContext, mv: MarketView) -> RiskDecision:
        info = mv.info
        L = self._limits
        halt, daily_stop, acct_reasons = self.check_account(ctx)
        metrics: dict = {
            "drawdown_pct": str(self.drawdown_pct(ctx)),
            "daily_loss_pct": str(self.daily_loss_pct(ctx)),
        }

        def veto(*why: str) -> RiskDecision:
            return RiskDecision(Verdict.VETO, p, ZERO, p.price, tuple(why),
                                trigger_halt=halt, trigger_daily_stop=daily_stop, metrics=metrics)

        # ---- 1. structural checks (apply to every order)
        if p.pair not in self._whitelist:
            return veto(f"pair {p.pair} not in whitelist")
        if info.ticker_id != p.pair:
            return veto(f"market data is for {info.ticker_id}, proposal is for {p.pair}")
        if p.side not in ("buy", "sell") or p.qty <= 0 or p.price <= 0:
            return veto("malformed proposal (side/qty/price)")
        if not info.tradable:
            return veto(f"{p.pair} is in maintenance or suspended")
        if not ctx.reconciliation_ok:
            return veto("exchange balances/orders do not match local DB — reconcile first")

        if p.side == "sell" and ctx.position_qty.get(p.pair, ZERO) <= 0:
            return veto(f"no {p.pair} position to sell (shorting not allowed)")

        is_emergency = (p.is_emergency_exit and p.side == "sell" and p.intent == "exit")
        if p.is_emergency_exit and not is_emergency:
            return veto("emergency flag only valid on sell exits")

        # ---- 2. order type: limit only; market only for emergency SL exits
        if p.order_type == "market":
            if not is_emergency:
                return veto("market orders are only allowed for emergency stop-loss exits")
            if not mv.orderbook.bids:
                return veto("emergency exit: no bids in orderbook — fall back to limit order")
            held = ctx.position_qty.get(p.pair, ZERO)
            est_qty = min(p.qty, held)
            est = mv.orderbook.estimate_fill("sell", est_qty * mv.orderbook.best_bid)  # type: ignore[operator]
            metrics["emergency_slippage_pct"] = str(est.slippage_pct)
            if (not est.fully_filled or est.slippage_pct is None
                    or est.slippage_pct > self._pct(L.emergency_exit_max_slippage_pct)):
                return veto(f"emergency exit slippage {est.slippage_pct}% exceeds "
                            f"{L.emergency_exit_max_slippage_pct}% (or book too thin) — use limit order")
        elif p.order_type != "limit":
            return veto(f"order type {p.order_type!r} not allowed")

        # ---- 3. agent status
        if ctx.status == AgentStatus.HALTED and not is_emergency:
            return veto("agent HALTED — manual /resume required")
        if ctx.status == AgentStatus.PAUSED and p.side == "buy":
            return veto("agent PAUSED — no new entries")

        # ---- 4. order rate limits (emergency exits exempt, see module doc)
        if not is_emergency:
            if ctx.orders_last_hour >= L.max_orders_per_hour:
                return veto(f"order limit: {ctx.orders_last_hour} orders in last hour >= {L.max_orders_per_hour}")
            if ctx.orders_today >= L.max_orders_per_day:
                return veto(f"order limit: {ctx.orders_today} orders today >= {L.max_orders_per_day}")

        if p.side == "sell":
            return self._evaluate_sell(p, ctx, mv, is_emergency, halt, daily_stop, metrics)
        # ---- buys only below
        if halt:
            return veto(*acct_reasons)
        if daily_stop:
            return veto(*acct_reasons)
        return self._evaluate_buy(p, ctx, mv, metrics)

    # ---------------------------------------------------------------- sell

    def _evaluate_sell(self, p: TradeProposal, ctx: RiskContext, mv: MarketView, is_emergency: bool,
                       halt: bool, daily_stop: bool, metrics: dict) -> RiskDecision:
        info = mv.info
        held = ctx.position_qty.get(p.pair, ZERO)

        def veto(why: str) -> RiskDecision:
            return RiskDecision(Verdict.VETO, p, ZERO, p.price, (why,), halt, daily_stop, metrics)

        if held <= 0:
            return veto(f"no {p.pair} position to sell (shorting not allowed)")
        qty = info.round_qty(min(p.qty, held))
        price = info.round_price(p.price, "sell") if p.order_type == "limit" else p.price
        if p.order_type == "limit" and mv.orderbook.best_bid is not None:
            floor = mv.orderbook.best_bid * (1 - self._pct(self._market.max_slippage_pct) / HUNDRED)
            if price < floor:
                return veto(f"sell limit {price} too far below best bid {mv.orderbook.best_bid}")
        why = info.min_order_violation(price, qty)
        if why:
            return veto(f"below exchange minimum: {why}")
        verdict = Verdict.APPROVE if qty == p.qty else Verdict.RESIZE
        reasons = ("emergency stop-loss exit" if is_emergency else "exit approved",)
        if verdict == Verdict.RESIZE:
            reasons += (f"qty capped to held/rounded {qty}",)
        return RiskDecision(verdict, p, qty, price, reasons, halt, daily_stop, metrics)

    # ----------------------------------------------------------------- buy

    def _evaluate_buy(self, p: TradeProposal, ctx: RiskContext, mv: MarketView, metrics: dict) -> RiskDecision:
        L = self._limits
        info, book = mv.info, mv.orderbook
        cap = self._capital()

        def veto(why: str) -> RiskDecision:
            return RiskDecision(Verdict.VETO, p, ZERO, p.price, (why,), False, False, metrics)

        # stop-loss / take-profit
        if L.require_stop_loss and (p.stop_loss is None or p.stop_loss <= 0):
            return veto("stop-loss is mandatory for every position")
        if p.stop_loss is not None and p.stop_loss >= p.price:
            return veto(f"stop-loss {p.stop_loss} must be below entry {p.price}")
        ref = p.take_profit if p.take_profit is not None else p.target
        if ref is None or ref <= p.price:
            return veto("take-profit or target above entry is required to evaluate expected move")

        # cooldown after stop-loss on the same pair
        last_sl = ctx.last_stoploss_at.get(p.pair)
        if last_sl is not None:
            until = last_sl + timedelta(minutes=L.stoploss_cooldown_minutes)
            if ctx.now < until:
                return veto(f"cooldown after stop-loss on {p.pair} until {until.isoformat()}")

        # max concurrent positions (a pending buy on a new pair counts as a position)
        open_pairs = {k for k, q in ctx.position_qty.items() if q > 0}
        open_pairs |= {k for k, v in ctx.pending_buy_idr.items() if v > 0}
        if p.pair not in open_pairs and len(open_pairs) >= L.max_open_positions:
            return veto(f"max open positions reached ({len(open_pairs)}/{L.max_open_positions})")

        # price sanity: never bid above best ask + max slippage
        price = info.round_price(p.price, "buy")
        if book.best_ask is None or book.best_bid is None:
            return veto("orderbook empty")
        ceiling = book.best_ask * (1 + self._pct(self._market.max_slippage_pct) / HUNDRED)
        if price > ceiling:
            return veto(f"buy limit {price} too far above best ask {book.best_ask}")

        # ---- sizing caps (RESIZE)
        pos_val = ctx.position_value_idr.get(p.pair, ZERO) + ctx.pending_buy_idr.get(p.pair, ZERO)
        asset = base_asset(p.pair)
        asset_val = sum(
            (v for k, v in ctx.position_value_idr.items() if base_asset(k) == asset), ZERO
        ) + sum((v for k, v in ctx.pending_buy_idr.items() if base_asset(k) == asset), ZERO)
        deployed = sum(ctx.position_value_idr.values(), ZERO) + sum(ctx.pending_buy_idr.values(), ZERO)
        fee_mult = 1 + self._costs.leg_fee_pct(info, maker=True) / HUNDRED
        committed_cash = sum(ctx.pending_buy_idr.values(), ZERO)
        caps = {
            "max_position": cap * self._pct(L.max_position_pct) / HUNDRED - pos_val,
            "max_asset_exposure": cap * self._pct(L.max_asset_exposure_pct) / HUNDRED - asset_val,
            "agent_capital": cap - deployed,
            "agent_cash": (ctx.cash_idr - committed_cash) / fee_mult,
        }
        if ctx.exchange_free_idr is not None:
            caps["exchange_free_idr"] = ctx.exchange_free_idr / fee_mult
        allowed = min(caps.values())
        binding = min(caps, key=caps.get)  # type: ignore[arg-type]
        metrics["caps_idr"] = {k: str(v) for k, v in caps.items()}
        if allowed <= 0:
            return veto(f"no room under {binding} limit")
        qty = info.round_qty(p.qty)
        resized = False
        if qty * price > allowed:
            qty = info.round_qty(allowed / price)
            resized = True
        if qty <= 0:
            return veto(f"size rounds to zero under {binding} limit")
        why = info.min_order_violation(price, qty)
        if why:
            return veto(f"below exchange minimum after sizing ({binding}): {why}")
        notional = qty * price

        # ---- liquidity on the final size
        spread = book.spread_pct
        if spread is None or spread > self._pct(self._market.max_spread_pct):
            return veto(f"spread {spread}% > max {self._market.max_spread_pct}%")
        vol = mv.ticker.vol_quote
        if vol is None or vol < Decimal(str(self._market.min_volume_24h_idr)):
            return veto(f"24h volume {vol} IDR < min {self._market.min_volume_24h_idr:,.0f}")
        est_exit = book.estimate_fill("sell", notional)   # can we get out again?
        if not est_exit.fully_filled or est_exit.slippage_pct is None:
            return veto("orderbook too thin to exit this size")
        if est_exit.slippage_pct > self._pct(self._market.max_slippage_pct):
            return veto(f"exit slippage {est_exit.slippage_pct:.3f}% > max {self._market.max_slippage_pct}%")

        # ---- cost vs expected move (computed here, not trusted from strategy)
        costs = self._costs.round_trip(info, spread, est_exit.slippage_pct)
        expected = (ref - price) / price * HUNDRED
        need = costs.total_pct * self._pct(self._fees.min_edge_multiple)
        metrics.update(expected_move_pct=str(expected), round_trip_cost_pct=str(costs.total_pct))
        if expected < need:
            return veto(f"expected move {expected:.3f}% < {self._fees.min_edge_multiple}x round-trip cost "
                        f"{costs.total_pct:.3f}%")

        if resized:
            return RiskDecision(Verdict.RESIZE, p, qty, price,
                                (f"resized {p.qty} -> {qty} by {binding} limit",), metrics=metrics)
        return RiskDecision(Verdict.APPROVE, p, qty, price, ("all checks passed",), metrics=metrics)
