from urllib.parse import parse_qs

import httpx
import pytest
import respx

from agent.exchange.errors import IndodaxAPIError
from agent.exchange.private_client import ForbiddenOperationError, PrivateReadOnlyClient
from agent.exchange.signing import Clock, sign_sha256, sign_sha512

KEY, SECRET = "TESTKEY-AAAA", "testsecret" * 8
TAPI, V2 = "https://indodax.com/tapi", "https://api.indodax.com"


async def _nosleep(_):
    return None


@pytest.fixture
async def pc():
    c = PrivateReadOnlyClient(KEY, SECRET, clock=Clock(now=lambda: 1_700_000_000.0), sleep=_nosleep)
    yield c
    await c.aclose()


@pytest.mark.parametrize("method", ["trade", "cancelOrder", "cancelByClientOrderId", "withdrawCoin",
                                    "createVoucher", "listDownline"])
async def test_non_read_methods_refused_before_any_request(pc, method):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(TAPI)
        with pytest.raises(ForbiddenOperationError):
            await pc.legacy(method)
        assert not route.called


@pytest.mark.parametrize("path", ["/api/v2/capital/withdraw/apply", "/api/v2/fiat/withdraw", "/api/v2/xyz"])
async def test_non_read_v2_paths_refused(pc, path):
    with pytest.raises(ForbiddenOperationError):
        await pc.v2_get(path)


@respx.mock
async def test_legacy_signed_request_and_balances(pc):
    route = respx.post(TAPI).mock(return_value=httpx.Response(200, json={"success": 1, "return": {
        "balance": {"idr": 750000, "btc": "0.00100000"}, "balance_hold": {"idr": 0, "btc": "0"},
        "withdraw_status": 1}}))
    bal, ws = await pc.balances_legacy()
    req = route.calls[0].request
    body = req.content.decode()
    assert req.headers["Key"] == KEY
    assert req.headers["Sign"] == sign_sha512(SECRET, body)
    q = parse_qs(body)
    assert q["method"] == ["getInfo"] and q["timestamp"] == ["1700000000000"] and q["recvWindow"] == ["5000"]
    assert bal.free_of("IDR") == 750000 and str(bal.free_of("btc")) == "0.00100000" and ws == 1


@respx.mock
async def test_legacy_error_payload_raises(pc):
    respx.post(TAPI).mock(return_value=httpx.Response(200, json={
        "success": 0, "error": "Invalid credentials.", "error_code": "invalid_credentials"}))
    with pytest.raises(IndodaxAPIError) as ei:
        await pc.balances_legacy()
    assert ei.value.code == "invalid_credentials"


@respx.mock
async def test_v2_signed_get_and_account(pc):
    route = respx.get(url__startswith=f"{V2}/api/v2/account").mock(return_value=httpx.Response(200, json={
        "canTrade": True, "canWithdraw": False, "balances": [{"asset": "IDR", "free": "500000", "locked": "0"}]}))
    bal, can_trade, can_withdraw = await pc.account_v2()
    req = route.calls[0].request
    qs = req.url.query.decode()
    assert req.headers["X-APIKEY"] == KEY and req.headers["Sign"] == sign_sha256(SECRET, qs)
    assert "omitZeroBalances=true" in qs and can_trade is True and can_withdraw is False
    assert bal.free_of("idr") == 500000


@respx.mock
async def test_v2_error_raises(pc):
    respx.get(url__startswith=f"{V2}/api/v2/account").mock(
        return_value=httpx.Response(403, json={"code": -2015, "msg": "Access denied"}))
    with pytest.raises(IndodaxAPIError) as ei:
        await pc.account_v2()
    assert ei.value.code == -2015


def _legacy_router(withdraw_fee_response):
    def handler(request):
        body = parse_qs(request.content.decode())
        if body["method"] == ["withdrawFee"]:
            return withdraw_fee_response
        return httpx.Response(200, json={"success": 1, "return": {
            "balance": {}, "balance_hold": {}, "withdraw_status": 1}})
    return handler


@respx.mock
async def test_withdraw_probe_legacy_only_key(pc):
    respx.get(url__startswith=f"{V2}/api/v2/account").mock(
        return_value=httpx.Response(403, json={"code": -2015, "msg": "Invalid TAPI version key"}))
    respx.post(TAPI).mock(side_effect=_legacy_router(httpx.Response(200, json={
        "success": 0, "error": "No permission", "error_code": ""})))
    rep = await pc.permission_report()
    assert rep.legacy_ok and rep.v2_ok is False and rep.withdraw_possible is False
    respx.post(TAPI).mock(side_effect=_legacy_router(httpx.Response(200, json={
        "success": 1, "return": {"server_time": 1, "withdraw_fee": 0.0005, "currency": "btc"}})))
    assert (await pc.permission_report()).withdraw_possible is True
    respx.post(TAPI).mock(side_effect=_legacy_router(httpx.Response(200, json={
        "success": 0, "error": "Internal error", "error_code": "internal_server_error"})))
    assert (await pc.permission_report()).withdraw_possible is None   # unknown stays unknown


@respx.mock
async def test_permission_report_flags_withdraw_only_from_v2(pc):
    respx.post(TAPI).mock(side_effect=_legacy_router(httpx.Response(200, json={
        "success": 0, "error": "No permission", "error_code": ""})))
    respx.get(url__startswith=f"{V2}/api/v2/account").mock(return_value=httpx.Response(200, json={
        "canTrade": True, "canWithdraw": False, "balances": []}))
    rep = await pc.permission_report()
    assert rep.legacy_ok and rep.v2_ok and rep.withdraw_possible is False   # account flag is informational
    respx.get(url__startswith=f"{V2}/api/v2/account").mock(return_value=httpx.Response(200, json={
        "canTrade": True, "canWithdraw": True, "balances": []}))
    assert (await pc.permission_report()).withdraw_possible is True


@respx.mock
async def test_open_orders_both_shapes(pc):
    respx.post(TAPI).mock(return_value=httpx.Response(200, json={"success": 1, "return": {
        "orders": {"btc_idr": [{"order_id": "1"}]}}}))
    assert list(await pc.open_orders_legacy()) == ["btc_idr"]
    respx.post(TAPI).mock(return_value=httpx.Response(200, json={"success": 1, "return": {
        "orders": [{"order_id": "2"}]}}))
    assert (await pc.open_orders_legacy("btcidr"))["btc_idr"][0]["order_id"] == "2"


def test_repr_hides_credentials(pc):
    assert KEY not in repr(pc) and SECRET not in repr(pc)


def test_missing_credentials_rejected():
    with pytest.raises(ValueError):
        PrivateReadOnlyClient("", "x")


@respx.mock
async def test_half_filled_v2_pair_is_ignored():
    c = PrivateReadOnlyClient(KEY, SECRET, v2_api_key="OTHERKEY", v2_secret=None,
                              clock=Clock(now=lambda: 1_700_000_000.0), sleep=_nosleep)
    route = respx.get(url__startswith=f"{V2}/api/v2/account").mock(return_value=httpx.Response(200, json={
        "canTrade": True, "canWithdraw": False, "balances": []}))
    respx.post(TAPI).mock(return_value=httpx.Response(200, json={"success": 1, "return": {}}))
    rep = await c.permission_report()
    req = route.calls[0].request
    assert req.headers["X-APIKEY"] == KEY                      # main key, not the orphaned v2 key
    assert req.headers["Sign"] == sign_sha256(SECRET, req.url.query.decode())
    assert any("sebagian" in n for n in rep.notes)
    await c.aclose()


@respx.mock
async def test_permission_report_survives_network_errors(pc):
    respx.post(TAPI).mock(side_effect=httpx.ConnectError("down"))
    respx.get(url__startswith=f"{V2}/api/v2/account").mock(side_effect=httpx.ConnectError("down"))
    rep = await pc.permission_report()
    assert rep.legacy_ok is False and rep.v2_ok is False and rep.withdraw_possible is None
