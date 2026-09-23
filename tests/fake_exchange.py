"""In-memory stand-in for LiveTradeClient: an order book that fills orders
according to a scripted behaviour, for executor/reconciliation tests."""

from __future__ import annotations

from decimal import Decimal

from agent.exchange.errors import IndodaxAPIError, IndodaxNetworkError
from agent.exchange.private_client import Balances, PermissionReport
from agent.exchange.trade_client import OrderAck, OrderState, parse_order

D = Decimal


class FakeExchange:
    def __init__(self):
        self.orders: dict[str, dict] = {}       # client_order_id -> order
        self.submits: list[str] = []            # every trade request that reached the exchange
        self.cancels: list[str] = []
        self.balances = {"idr": D(1_000_000), "btc": D(0), "eth": D(0), "sol": D(0)}
        self.locked: dict[str, Decimal] = {}
        self.fill_ratio = D(1)                  # share of qty filled immediately on submit
        self.timeout_after_accept = False       # accept the order, then drop the response
        self.fail_lookups = 0
        self.deadman_calls: list[tuple[list[str], int]] = []
        self.deadman_fail = False
        self.foreign_orders: dict[str, list[OrderState]] = {}
        self.withdraw = False
        self.seq = 100
        # respond like the real legacy API (verified 2026-09-23): buys IDR-denominated with a
        # ~0.22% fee reserve in order_rp/remain_rp, receive_btc always 0, trade ack reports no fill
        self.indodax_shapes = False

    # --- trading
    async def place_limit(self, pair, side, price, qty, client_order_id):
        if client_order_id in self.orders:
            raise IndodaxAPIError(f"client order id {client_order_id} already exists")
        self.seq += 1
        filled = (qty * self.fill_ratio).quantize(D("0.00000001"))
        o = dict(order_id=str(self.seq), coid=client_order_id, pair=pair, side=side, price=price, qty=qty,
                 filled=filled, status="filled" if filled >= qty else "open")
        self.orders[client_order_id] = o
        self.submits.append(client_order_id)
        base = pair.split("_")[0]
        if side == "buy":
            self.balances["idr"] -= filled * price
            self.balances[base] = self.balances.get(base, D(0)) + filled
        else:
            self.balances[base] -= filled
            self.balances["idr"] += filled * price
        if self.timeout_after_accept:
            raise IndodaxNetworkError("read timeout")
        if self.indodax_shapes:
            return OrderAck(o["order_id"], client_order_id, D(0), D(0), D(0))
        return OrderAck(o["order_id"], client_order_id, filled, filled * price, None)

    def fill_more(self, client_order_id, qty):
        o = self.orders[client_order_id]
        o["filled"] = min(o["qty"], o["filled"] + qty)
        base = o["pair"].split("_")[0]
        if o["side"] == "buy":
            self.balances[base] = self.balances.get(base, D(0)) + qty
        if o["filled"] >= o["qty"]:
            o["status"] = "filled"

    def _state(self, o) -> OrderState:
        if self.indodax_shapes:
            base = o["pair"].split("_")[0]
            if o["side"] == "buy":
                rp = lambda q: str((q * o["price"] * D("1.0022")).to_integral_value())  # noqa: E731
                raw = {"order_id": o["order_id"], "client_order_id": o["coid"], "price": str(o["price"]),
                       "type": "buy", "status": o["status"], "fee": 0, "order_rp": rp(o["qty"]),
                       "remain_rp": rp(o["qty"] - o["filled"]), "receive_btc": 0}
            else:
                raw = {"order_id": o["order_id"], "client_order_id": o["coid"], "price": str(o["price"]),
                       "type": "sell", "status": o["status"], "fee": 0, "receive_idr": 0,
                       f"order_{base}": str(o["qty"]), f"remain_{base}": str(o["qty"] - o["filled"]),
                       f"sold_{base}": str(o["filled"])}
            return parse_order(raw, o["pair"])
        return OrderState(o["order_id"], o["coid"], o["pair"], o["side"], o["price"], o["qty"],
                          o["qty"] - o["filled"], o["status"], {})

    async def get_order_by_coid(self, client_order_id, pair):
        if self.fail_lookups:
            self.fail_lookups -= 1
            raise IndodaxNetworkError("lookup timeout")
        o = self.orders.get(client_order_id)
        return self._state(o) if o else None

    async def cancel(self, pair, order_id, side):
        for o in self.orders.values():
            if o["order_id"] == order_id and o["status"] == "open":
                o["status"] = "cancelled"
                self.cancels.append(o["coid"])

    async def open_orders(self, pair):
        mine = [self._state(o) for o in self.orders.values() if o["pair"] == pair and o["status"] == "open"]
        return mine + self.foreign_orders.get(pair, [])

    async def balances_legacy(self):
        return Balances(dict(self.balances), dict(self.locked)), 1

    async def countdown_cancel_all(self, pairs, countdown_ms):
        if self.deadman_fail:
            raise IndodaxNetworkError("deadman down")
        self.deadman_calls.append((list(pairs), countdown_ms))

    async def permission_report(self):
        return PermissionReport(legacy_ok=True, v2_ok=False, withdraw_possible=self.withdraw,
                                notes=[])
