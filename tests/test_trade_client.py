from decimal import Decimal as D
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from agent.exchange.errors import IndodaxAPIError, IndodaxNetworkError
from agent.exchange.private_client import ForbiddenOperationError
from agent.exchange.signing import Clock, sign_sha512
from agent.exchange.trade_client import LiveTradeClient, fmt, is_not_found, parse_order

KEY, SECRET = "TESTKEY-AAAA", "testsecret" * 8
TAPI = "https://indodax.com/tapi"


async def _nosleep(_):
    return None


@pytest.fixture
async def tc():
    c = LiveTradeClient(KEY, SECRET, clock=Clock(now=lambda: 1_700_000_000.0), sleep=_nosleep)
    yield c
    await c.aclose()


def body(call):
    return parse_qs(call.request.content.decode())


def test_fmt_never_scientific():
    assert fmt(D("1.5420E+9")) == "1542000000"
    assert fmt(D("0.00014285000")) == "0.00014285"
    assert fmt(D("0")) == "0"


@pytest.mark.parametrize("method", ["withdrawCoin", "createVoucher", "withdrawFeeX", "cancelByClientOrderId"])
async def test_forbidden_methods(tc, method):
    with pytest.raises(ForbiddenOperationError):
        await tc.legacy(method)


@respx.mock
async def test_place_limit_buy_params_and_immediate_fill(tc):
    route = respx.post(TAPI).mock(return_value=httpx.Response(200, json={"success": 1, "return": {
        "receive_btc": "0.00010000", "spend_rp": 154205, "fee": 346, "remain_rp": 0,
        "order_id": 59632813, "client_order_id": "agx-1"}}))
    ack = await tc.place_limit("btc_idr", "buy", D("1542050000"), D("0.0001"), "agx-1")
    b = body(route.calls[0])
    assert b["method"] == ["trade"] and b["pair"] == ["btc_idr"] and b["type"] == ["buy"]
    assert b["price"] == ["1542050000"] and b["btc"] == ["0.0001"]          # coin-denominated limit buy
    assert b["order_type"] == ["limit"] and b["time_in_force"] == ["GTC"] and b["client_order_id"] == ["agx-1"]
    assert "idr" not in b
    req = route.calls[0].request
    assert req.headers["Sign"] == sign_sha512(SECRET, req.content.decode())
    assert ack.order_id == "59632813" and ack.filled_qty == D("0.0001") and ack.filled_quote == 154205
    assert ack.fee_idr == 346


@respx.mock
async def test_trade_is_never_retried_on_network_error(tc):
    route = respx.post(TAPI).mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(IndodaxNetworkError):
        await tc.place_limit("btc_idr", "buy", D("1"), D("1"), "agx-2")
    assert route.call_count == 1


@respx.mock
async def test_reads_are_retried(tc):
    route = respx.post(TAPI).mock(side_effect=[httpx.ReadTimeout("t"), httpx.Response(200, json={
        "success": 1, "return": {"order": {"order_id": "5", "price": "100", "type": "buy", "order_btc": "2",
                                           "remain_btc": "0.5", "status": "open", "client_order_id": "agx-3"}}})])
    st = await tc.get_order_by_coid("agx-3", "btc_idr")
    assert route.call_count == 2 and st.filled_qty == D("1.5") and st.is_open


@respx.mock
async def test_order_not_found_returns_none(tc):
    respx.post(TAPI).mock(return_value=httpx.Response(200, json={
        "success": 0, "error": "Order not found", "error_code": "order_not_found"}))
    assert await tc.get_order_by_coid("agx-4", "btc_idr") is None


def test_invalid_client_order_id_rejected_locally(tc):
    import asyncio
    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(tc.place_limit("btc_idr", "buy", D(1), D(1), "bad id!"))


@pytest.mark.parametrize("raw,filled,status", [
    ({"order_id": "1", "price": "100", "type": "sell", "order_btc": "0.02", "remain_btc": "0.005",
      "status": "open"}, D("0.015"), "open"),
    ({"order_id": "2", "price": "100", "type": "buy", "order_rp": "1000", "remain_rp": "0",
      "status": "filled"}, D("10"), "filled"),
    ({"order_id": "3", "price": "100", "type": "buy", "order_btc": "1", "remain_btc": "1",
      "status": "cancelled"}, D("0"), "cancelled"),
])
def test_parse_order_shapes(raw, filled, status):
    st = parse_order(raw, "btcidr")
    assert st.filled_qty == filled and st.status == status and st.pair == "btc_idr"


def test_parse_real_buy_orders_book_submitted_qty_not_fee_inflated_rp():
    # real getOrderByClientOrderId responses (2026-09-23)
    resting = {"order_id": "268679410", "client_order_id": "chk-d66d2063", "price": "1350774000", "type": "buy",
               "submit_time": "1790173736", "finish_time": "0", "status": "open", "fee": 0,
               "order_rp": "10532", "remain_rp": "10532", "receive_btc": 0}
    st = parse_order(resting, "btc_idr")
    assert st.is_open and st.filled_for(D("0.00000778"), D("0.00000001")) == 0
    filled = {"order_id": "268679416", "client_order_id": "chk-6c0d31a9", "price": "1506036000", "type": "buy",
              "submit_time": "1790173739", "finish_time": "1790173739", "status": "filled", "fee": 0,
              "order_rp": "11591", "remain_rp": "0", "refund_idr": "40", "receive_btc": 0}
    st = parse_order(filled, "btc_idr")
    assert st.filled_qty > D("0.00000768")                                # order_rp/price is inflated
    assert st.filled_for(D("0.00000768"), D("0.00000001")) == D("0.00000768")   # balance credited
    part = {**resting, "remain_rp": "5266"}
    assert parse_order(part, "btc_idr").filled_for(D("0.00000778"), D("0.00000001")) == D("0.00000389")
    got = {**part, "receive_btc": "0.0000038"}   # a positive receive_btc caps the booked qty
    assert parse_order(got, "btc_idr").filled_for(D("0.00000778"), D("0.00000001")) == D("0.0000038")


def test_parse_real_sell_order():
    raw = {"order_id": "268679420", "client_order_id": "chk-8d23fadc", "price": "1497858000", "type": "sell",
           "submit_time": "1790173741", "finish_time": "1790173741", "status": "filled", "fee": 0,
           "receive_idr": 0, "order_btc": "0.00000768", "remain_btc": "0.00000000", "sold_btc": "0.00000768"}
    st = parse_order(raw, "btc_idr")
    assert st.status == "filled" and st.filled_qty == D("0.00000768")
    assert st.filled_for(D("0.00000768")) == D("0.00000768")


def test_open_orders_entries_without_status_are_open():
    raw = {"order_id": "1", "client_order_id": "x", "price": "100", "type": "buy", "order_idr": "1000",
           "remain_idr": "1000"}
    assert parse_order(raw, "btc_idr", default_status="open").is_open


def test_parse_order_unknown_status_raises():
    from agent.exchange.errors import IndodaxResponseFormatError
    with pytest.raises(IndodaxResponseFormatError):
        parse_order({"order_id": "1", "price": "1", "order_btc": "1", "remain_btc": "0", "status": "weird"}, "btc_idr")


@respx.mock
async def test_cancel_not_found_is_ok(tc):
    route = respx.post(TAPI).mock(return_value=httpx.Response(200, json={
        "success": 0, "error": "Order not found"}))
    await tc.cancel("btc_idr", "123", "buy")
    b = body(route.calls[0])
    assert b["method"] == ["cancelOrder"] and b["order_id"] == ["123"] and b["type"] == ["buy"]


@respx.mock
async def test_deadman_request_matches_docs(tc):
    route = respx.post(f"{TAPI}/countdownCancelAll").mock(return_value=httpx.Response(200, json={"success": 1}))
    await tc.countdown_cancel_all(["btc_idr", "ethidr"], 120000)
    req = route.calls[0].request
    raw = req.content.decode()
    assert raw.startswith("pair=btc_idr,eth_idr&countdownTime=120000&timestamp=1700000000000&recvWindow=5000")
    assert req.headers["Content-Type"] == "text/plain" and req.headers["Key"] == KEY
    assert req.headers["Sign"] == sign_sha512(SECRET, raw)


@respx.mock
async def test_deadman_http200_error_raises(tc):
    respx.post(f"{TAPI}/countdownCancelAll").mock(return_value=httpx.Response(200, json={
        "success": 0, "error": "Invalid pair", "error_code": "bad_request"}))
    with pytest.raises(IndodaxAPIError):
        await tc.countdown_cancel_all(["btc_idr"], 120000)


def test_is_not_found():
    assert is_not_found(IndodaxAPIError("x", code="order_not_found"))
    assert not is_not_found(IndodaxAPIError("insufficient balance"))
