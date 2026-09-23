"""Shared builders for Phase 2 tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from agent.config import Settings
from agent.exchange.models import OrderBook, PairInfo, Ticker
from agent.risk.risk_manager import AgentStatus, MarketView, RiskContext
from agent.strategy.strategy import TradeProposal

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc)
D = Decimal


def settings(**overrides) -> Settings:
    raw = yaml.safe_load((ROOT / "config/settings.yaml").read_text())
    raw["market"]["whitelist"] = ["btc_idr", "eth_idr", "sol_idr", "xrp_idr", "btc_usdt"]
    for section, values in overrides.items():
        raw[section].update(values)
    return Settings.model_validate(raw)


def pair_info(pair: str = "btc_idr", tick: str = "1000", step: str = "0.00000001",
              min_quote: str = "10000", min_base: str = "0.00001", **kw) -> PairInfo:
    base, quote = pair.split("_")
    return PairInfo(
        ticker_id=pair, pair_id=base + quote, symbol=(base + quote).upper(), base=base, quote=quote,
        price_tick=D(tick), qty_step=D(step), min_quote=D(min_quote), min_base=D(min_base),
        maker_fee_pct=D("0.1"), taker_fee_pct=D("0.2"), **kw,
    )


def book(pair: str = "btc_idr", bid: str = "999000000", ask: str = "1000000000",
         depth_qty: str = "5", levels: int = 5, tick: str = "1000") -> OrderBook:
    b, a, t = D(bid), D(ask), D(tick)
    return OrderBook(
        pair=pair,
        bids=tuple((b - i * t, D(depth_qty)) for i in range(levels)),
        asks=tuple((a + i * t, D(depth_qty)) for i in range(levels)),
    )


def ticker(pair: str = "btc_idr", last: str = "999500000", bid: str = "999000000",
           ask: str = "1000000000", vol_quote: str = "50000000000") -> Ticker:
    return Ticker(pair=pair, last=D(last), bid=D(bid), ask=D(ask), high=D(ask), low=D(bid),
                  vol_base=D("50"), vol_quote=D(vol_quote), server_time=int(NOW.timestamp()))


def market_view(pair: str = "btc_idr", **kw) -> MarketView:
    return MarketView(pair_info(pair), ticker(pair, **kw.pop("ticker", {})), kw.pop("book", book(pair)))


def ctx(**kw) -> RiskContext:
    base = dict(
        now=NOW, status=AgentStatus.RUNNING, reconciliation_ok=True,
        equity_idr=D(1_000_000), peak_equity_idr=D(1_000_000), day_start_equity_idr=D(1_000_000),
        cash_idr=D(1_000_000), position_qty={}, position_value_idr={}, pending_buy_idr={},
        orders_last_hour=0, orders_today=0, last_stoploss_at={}, exchange_free_idr=None,
    )
    base.update(kw)
    return RiskContext(**base)


def buy(pair: str = "btc_idr", price: str = "999000000", qty: str = "0.0001",
        sl: str | None = "979000000", tp: str | None = "1040000000", **kw) -> TradeProposal:
    return TradeProposal(
        pair=pair, side="buy", order_type=kw.pop("order_type", "limit"), price=D(price), qty=D(qty),
        intent="entry", reason="test", confidence=0.7,
        stop_loss=D(sl) if sl is not None else None, take_profit=D(tp) if tp is not None else None, **kw,
    )


def sell(pair: str = "btc_idr", price: str = "999000000", qty: str = "0.0001",
         emergency: bool = False, order_type: str | None = None) -> TradeProposal:
    return TradeProposal(
        pair=pair, side="sell", order_type=order_type or ("market" if emergency else "limit"),
        price=D(price), qty=D(qty), intent="exit", reason="test", confidence=1.0,
        is_emergency_exit=emergency,
    )


def with_position(c: RiskContext, pair: str, qty: str, value: str) -> RiskContext:
    q = dict(c.position_qty); q[pair] = D(qty)
    v = dict(c.position_value_idr); v[pair] = D(value)
    return replace(c, position_qty=q, position_value_idr=v)


# ------------------------------------------------------------- candle series

def candles(close: np.ndarray, spread: float = 0.002, volume: float = 10.0,
            freq: str = "1h") -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    idx = pd.date_range("2026-01-01", periods=len(close), freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(len(close), volume)}, index=idx)


def uptrend(n: int = 200, start: float = 1e9, drift: float = 0.004, noise: float = 0.002, seed: int = 1):
    rng = np.random.default_rng(seed)
    return candles(start * np.cumprod(1 + drift + rng.normal(0, noise, n)))


def downtrend(n: int = 200, **kw):
    return uptrend(n, drift=-0.004, **kw)


def ranging(n: int = 200, start: float = 1e9, amp: float = 0.01, seed: int = 2):
    rng = np.random.default_rng(seed)
    x = np.arange(n)
    return candles(start * (1 + amp * np.sin(x / 3) + rng.normal(0, 0.001, n)))
