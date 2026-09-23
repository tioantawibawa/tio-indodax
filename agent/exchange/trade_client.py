"""Live trading client (legacy ``/tapi``, HMAC-SHA512) + Deadman Switch.

Only limit orders are sent. "Market" emergency exits are sent as marketable
limit sells with a price floor (best bid − max slippage), which gives the
same immediacy with a hard slippage bound.

Response field names of legacy ``trade``/``getOrder*`` are only partially
documented; parsing is defensive and is validated on the real account with
minimum-size orders (scripts/live_order_check.py, docs/go_live_checklist.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import structlog

from .errors import IndodaxAPIError, IndodaxResponseFormatError
from .models import D
from .pairs import base_asset, to_ticker_id
from .private_client import LEGACY_PROBE_METHODS, LEGACY_READ_METHODS, PrivateReadOnlyClient
from .signing import encode_params, sign_sha512

log = structlog.get_logger(__name__)

ZERO = Decimal(0)
OPEN_STATUSES = {"open", "new", "partially_filled", "pending"}


def fmt(d: Decimal) -> str:
    """Plain decimal string (never scientific notation), no trailing zeros."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def is_not_found(e: IndodaxAPIError) -> bool:
    text = f"{e} {e.code or ''}".lower()
    return "not found" in text or "not_found" in text or "not exist" in text or "invalid order" in text


@dataclass(frozen=True)
class OrderAck:
    order_id: str
    client_order_id: str
    filled_qty: Decimal          # immediately filled base qty (0 if unknown)
    filled_quote: Decimal        # IDR spent/received for the immediate fill
    fee_idr: Decimal | None      # reported fee, if any
    raw: dict | None = None      # raw response (no credentials), for validation


@dataclass(frozen=True)
class OrderState:
    order_id: str
    client_order_id: str
    pair: str
    side: str
    price: Decimal
    orig_qty: Decimal
    remaining_qty: Decimal
    status: str                  # normalised: open | filled | cancelled
    raw: dict
    received_qty: Decimal | None = None  # buys: receive_<base> when > 0 (Indodax often reports 0)

    @property
    def filled_qty(self) -> Decimal:
        """Raw estimate from order/remain fields. For IDR-denominated buys this is inflated by the
        fee reserve — use ``filled_for`` with the submitted quantity to book fills."""
        return max(self.orig_qty - self.remaining_qty, ZERO)

    @property
    def filled_fraction(self) -> Decimal:
        if self.orig_qty <= 0:
            return ZERO
        return min(max((self.orig_qty - self.remaining_qty) / self.orig_qty, ZERO), Decimal(1))

    def filled_for(self, submitted_qty: Decimal, step: Decimal | None = None) -> Decimal:
        """Cumulative filled coin for an order we submitted with ``submitted_qty``.

        Verified on the real account: a fully filled buy of 0.00000768 BTC credited exactly
        0.00000768 while getOrder reported receive_btc=0 and order_rp/price=0.0000076964
        (fee reserve). So: filled -> submitted qty; partial -> fraction x submitted, rounded
        down (never book more coin than the exchange holds); capped by receive_<base> if given.
        """
        if self.status == "filled":
            filled = submitted_qty
        else:
            filled = submitted_qty * self.filled_fraction
            if step:
                filled = (filled / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if self.received_qty is not None and self.received_qty > 0:
            filled = min(filled, self.received_qty)
        return max(min(filled, submitted_qty), ZERO)

    @property
    def is_open(self) -> bool:
        return self.status == "open"


def _num(d: dict, *keys: str) -> Decimal | None:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            try:
                return D(d[k])
            except IndodaxResponseFormatError:
                continue
    return None


def parse_order(raw: dict, pair: str, default_status: str | None = None) -> OrderState:
    """Parse a legacy order. Verified on the real account (2026-09-23):
    - buy orders are IDR-denominated; ``order_rp``/``remain_rp`` INCLUDE the fee reserve
      (order 10510.92 IDR -> order_rp 10534), so qty derived from them is slightly too high.
      ``receive_btc`` stays 0 even after a full fill, and ``fee``/``receive_idr`` are 0 too.
      Fills are therefore booked with ``OrderState.filled_for(submitted_qty)``.
    - ``openOrders`` entries carry no ``status`` field (pass ``default_status="open"``).
    """
    base = base_asset(pair)
    price = _num(raw, "price") or ZERO
    orig = _num(raw, f"order_{base}")
    remain = _num(raw, f"remain_{base}")
    if orig is None:  # IDR-denominated order: convert via price
        orig_rp = _num(raw, "order_rp", "order_idr")
        remain_rp = _num(raw, "remain_rp", "remain_idr")
        if orig_rp is not None and price > 0:
            orig = orig_rp / price
            remain = (remain_rp or ZERO) / price
    if orig is None:
        raise IndodaxResponseFormatError(f"order {raw.get('order_id')}: no quantity fields")
    status_raw = str(raw.get("status") or default_status or "").lower()
    if status_raw in ("filled", "done", "fill"):
        status = "filled"
    elif status_raw in ("cancelled", "canceled", "rejected", "expired"):
        status = "cancelled"
    elif status_raw in OPEN_STATUSES:
        status = "open"
    else:
        raise IndodaxResponseFormatError(f"order {raw.get('order_id')}: unknown status {status_raw!r}")
    side = str(raw.get("type", "")).lower()
    received = _num(raw, f"receive_{base}") if side == "buy" else None
    if received is not None and received <= 0:
        received = None  # 0 is reported even for filled buys -> carries no information
    return OrderState(str(raw.get("order_id", "")), str(raw.get("client_order_id", "")), to_ticker_id(pair),
                      side, price, orig, remain if remain is not None else ZERO, status, raw, received)


class LiveTradeClient(PrivateReadOnlyClient):
    LEGACY_ALLOWED = LEGACY_READ_METHODS | LEGACY_PROBE_METHODS | frozenset({"trade", "cancelOrder"})

    def __repr__(self) -> str:
        return "LiveTradeClient(<credentials hidden>)"

    async def place_limit(self, pair: str, side: str, price: Decimal, qty: Decimal,
                          client_order_id: str) -> OrderAck:
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if qty <= 0 or price <= 0:
            raise ValueError("price and qty must be positive")
        if not (1 <= len(client_order_id) <= 36) or not all(c.isalnum() or c in "-_" for c in client_order_id):
            raise ValueError("client_order_id: 1-36 chars of [A-Za-z0-9_-]")
        base = base_asset(pair)
        r = await self.legacy(
            "trade", pair=to_ticker_id(pair), type=side, price=fmt(price), order_type="limit",
            time_in_force="GTC", client_order_id=client_order_id, **{base: fmt(qty)},
        )
        order_id = str(r.get("order_id", ""))
        if not order_id:
            raise IndodaxResponseFormatError("trade: response has no order_id")
        if side == "buy":
            filled = _num(r, f"receive_{base}") or ZERO
            quote = _num(r, "spend_rp", "spend_idr") or ZERO
        else:
            filled = _num(r, f"spend_{base}", f"sold_{base}") or ZERO
            quote = _num(r, "receive_rp", "receive_idr") or ZERO
        log.info("order_submitted", pair=pair, side=side, price=fmt(price), qty=fmt(qty),
                 client_order_id=client_order_id, order_id=order_id, immediate_fill=fmt(filled))
        return OrderAck(order_id, client_order_id, filled, quote, _num(r, "fee"), dict(r))

    async def get_order_by_coid(self, client_order_id: str, pair: str) -> OrderState | None:
        try:
            r = await self.legacy("getOrderByClientOrderId", client_order_id=client_order_id)
        except IndodaxAPIError as e:
            if is_not_found(e):
                return None
            raise
        return parse_order(r.get("order", r), pair)

    async def cancel(self, pair: str, order_id: str, side: str) -> None:
        try:
            await self.legacy("cancelOrder", pair=to_ticker_id(pair), order_id=order_id, type=side,
                              order_type="limit")
        except IndodaxAPIError as e:
            if is_not_found(e):  # already filled/cancelled
                log.info("cancel_not_found", order_id=order_id)
                return
            raise
        log.info("order_cancelled", pair=pair, order_id=order_id)

    async def open_orders(self, pair: str) -> list[OrderState]:
        by_pair = await self.open_orders_legacy(pair)
        return [parse_order(o, pair, default_status="open") for o in by_pair.get(to_ticker_id(pair), [])]

    # ------------------------------------------------------------ deadman

    async def countdown_cancel_all(self, pairs: list[str], countdown_ms: int) -> None:
        """Deadman Switch heartbeat: cancel all open orders of ``pairs`` unless
        called again within ``countdown_ms``. ``countdown_ms=0`` disables the timer."""
        url = self.tapi_url.rstrip("/") + "/countdownCancelAll"

        async def send():
            body = encode_params({"pair": ",".join(to_ticker_id(p) for p in pairs),
                                  "countdownTime": countdown_ms, "timestamp": self.clock.timestamp_ms(),
                                  "recvWindow": self.recv_window_ms}).replace("%2C", ",")
            headers = {"Key": self._key, "Sign": sign_sha512(self._secret, body), "Content-Type": "text/plain"}
            return await self._http.post(url, content=body, headers=headers)

        resp = await self._retrying("deadman", send)
        try:
            data: Any = resp.json()
        except ValueError as e:
            raise IndodaxResponseFormatError(f"deadman: non-JSON response (HTTP {resp.status_code})") from e
        if not isinstance(data, dict) or str(data.get("success")) != "1":
            raise IndodaxAPIError(f"deadman: {data.get('error') if isinstance(data, dict) else data}",
                                  status=resp.status_code,
                                  code=data.get("error_code") if isinstance(data, dict) else None)
