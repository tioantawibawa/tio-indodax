"""Read-only private API client (Phase 4).

Two backends, see docs/indodax_api_notes.md §4–5:
- legacy ``POST /tapi`` (HMAC-SHA512, headers ``Key``/``Sign``)
- Trade API 2.0 ``GET /api/v2/...`` (HMAC-SHA256, headers ``X-APIKEY``/``Sign``)

SAFETY: only methods/paths in the READ allow-lists can be called. Anything
that places, cancels or withdraws is refused locally, before any request is
signed, so this client cannot move funds even with a trade/withdraw key.
Keys, secrets and signatures are never logged.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import structlog

from .errors import IndodaxAPIError, IndodaxNetworkError, IndodaxResponseFormatError
from .models import D
from .pairs import to_symbol, to_ticker_id
from .rate_limiter import AsyncRateLimiter
from .signing import Clock, encode_params, sign_sha256, sign_sha512

log = structlog.get_logger(__name__)

LEGACY_READ_METHODS = frozenset({
    "getInfo", "openOrders", "getOrder", "getOrderByClientOrderId", "transHistory",
})
# Probe only: withdrawFee returns fee info and moves no funds, but it requires the
# key's withdraw permission -> a safe way to detect that permission on legacy keys.
LEGACY_PROBE_METHODS = frozenset({"withdrawFee"})
V2_READ_PATHS = frozenset({
    "/api/v2/account", "/api/v2/openOrders", "/api/v2/order", "/api/v2/myTrades", "/api/v2/order/histories",
})


class ForbiddenOperationError(RuntimeError):
    """Raised when code tries to call a non read-only endpoint through this client."""


@dataclass(frozen=True)
class Balances:
    free: dict[str, Decimal]
    locked: dict[str, Decimal]

    def free_of(self, asset: str) -> Decimal:
        return self.free.get(asset.lower(), Decimal(0))


@dataclass(frozen=True)
class PermissionReport:
    legacy_ok: bool | None = None           # None = not tested (no key)
    v2_ok: bool | None = None
    withdraw_possible: bool | None = None   # from v2 canWithdraw; None = unknown
    can_trade: bool | None = None
    notes: list[str] = field(default_factory=list)


def _parse_balance_map(d: dict[str, Any]) -> dict[str, Decimal]:
    out = {}
    for k, v in (d or {}).items():
        try:
            out[k.lower()] = D(v)
        except Exception:  # noqa: BLE001 - odd values for exotic assets are skipped
            continue
    return out


class PrivateReadOnlyClient:
    def __init__(
        self,
        api_key: str,
        secret: str,
        *,
        tapi_url: str = "https://indodax.com/tapi",
        v2_base_url: str = "https://api.indodax.com",
        v2_api_key: str | None = None,
        v2_secret: str | None = None,
        recv_window_ms: int = 5000,
        clock: Clock | None = None,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        http: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
    ):
        if not api_key or not secret:
            raise ValueError("api_key and secret are required")
        self._key, self._secret = api_key, secret
        # a dedicated v2 key is only used together with its own secret; a half-filled pair would
        # produce invalid signatures, so fall back to the main credentials instead
        self.v2_pair_incomplete = bool(v2_api_key) != bool(v2_secret)
        if v2_api_key and v2_secret:
            self._v2_key, self._v2_secret = v2_api_key, v2_secret
        else:
            self._v2_key, self._v2_secret = api_key, secret
        self.tapi_url = tapi_url
        self.v2_base_url = v2_base_url.rstrip("/")
        self.recv_window_ms = recv_window_ms
        self.clock = clock or Clock()
        self.max_retries = max_retries
        self._sleep = sleep
        self._limiter = AsyncRateLimiter(120, 60.0, sleep=sleep)
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": "indodax-agent/0.1"})

    def __repr__(self) -> str:  # never expose credentials
        return "PrivateReadOnlyClient(<credentials hidden>)"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    # --------------------------------------------------------------- core

    async def _retrying(self, name: str, send):
        for attempt in range(self.max_retries + 1):
            await self._limiter.acquire()
            try:
                return await send()
            except httpx.TransportError as e:
                log.warning("private_api_network_error", call=name, attempt=attempt, error=type(e).__name__)
                if attempt >= self.max_retries:
                    raise IndodaxNetworkError(f"{name} failed: {type(e).__name__}") from e
                await self._sleep(min(0.5 * 2**attempt, 8) * (0.5 + random.random() / 2))
        raise IndodaxNetworkError(name)  # pragma: no cover

    async def legacy(self, method: str, **params: Any) -> dict:
        if method not in LEGACY_READ_METHODS | LEGACY_PROBE_METHODS:
            raise ForbiddenOperationError(f"legacy method {method!r} is not read-only; refused")

        async def send():
            # timestamp regenerated per attempt so a retry is never outside recvWindow
            body = encode_params({"method": method, "timestamp": self.clock.timestamp_ms(),
                                  "recvWindow": self.recv_window_ms, **params})
            headers = {"Key": self._key, "Sign": sign_sha512(self._secret, body),
                       "Content-Type": "application/x-www-form-urlencoded"}
            return await self._http.post(self.tapi_url, content=body, headers=headers)

        resp = await self._retrying(f"legacy:{method}", send)
        try:
            data = resp.json()
        except ValueError as e:
            raise IndodaxResponseFormatError(f"legacy {method}: non-JSON response (HTTP {resp.status_code})") from e
        if not isinstance(data, dict) or str(data.get("success")) != "1":
            err = data.get("error") if isinstance(data, dict) else None
            code = data.get("error_code") if isinstance(data, dict) else None
            raise IndodaxAPIError(f"legacy {method}: {err or 'unknown error'}", status=resp.status_code, code=code)
        return data.get("return", {})

    async def v2_get(self, path: str, **params: Any) -> Any:
        if path not in V2_READ_PATHS:
            raise ForbiddenOperationError(f"v2 path {path!r} is not read-only; refused")

        async def send():
            qs = encode_params({**params, "timestamp": self.clock.timestamp_ms(),
                                "recvWindow": self.recv_window_ms})
            headers = {"Accept": "application/json", "X-APIKEY": self._v2_key,
                       "Sign": sign_sha256(self._v2_secret, qs)}
            return await self._http.get(f"{self.v2_base_url}{path}?{qs}", headers=headers)

        resp = await self._retrying(f"v2:{path}", send)
        try:
            data = resp.json()
        except ValueError as e:
            raise IndodaxResponseFormatError(f"v2 {path}: non-JSON response (HTTP {resp.status_code})") from e
        if resp.status_code >= 400 or (isinstance(data, dict) and "code" in data and "msg" in data):
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg") if isinstance(data, dict) else str(data)[:200]
            raise IndodaxAPIError(f"v2 {path}: {msg}", status=resp.status_code, code=code)
        return data

    # ---------------------------------------------------------- endpoints

    async def balances_legacy(self) -> tuple[Balances, int | None]:
        r = await self.legacy("getInfo")
        ws = r.get("withdraw_status")
        return (Balances(_parse_balance_map(r.get("balance", {})), _parse_balance_map(r.get("balance_hold", {}))),
                int(ws) if ws is not None else None)

    async def account_v2(self) -> tuple[Balances, bool | None, bool | None]:
        r = await self.v2_get("/api/v2/account", omitZeroBalances="true")
        free, locked = {}, {}
        for b in r.get("balances", []):
            a = str(b.get("asset", "")).lower()
            free[a], locked[a] = D(b.get("free", 0)), D(b.get("locked", 0))
        return Balances(free, locked), r.get("canTrade"), r.get("canWithdraw")

    async def open_orders_legacy(self, pair: str | None = None) -> dict[str, list[dict]]:
        """Open orders keyed by ticker_id (normalises both documented response shapes)."""
        r = await self.legacy("openOrders", **({"pair": to_ticker_id(pair)} if pair else {}))
        orders = r.get("orders") or {}
        if isinstance(orders, list):
            return {to_ticker_id(pair): orders} if pair else {}
        return {to_ticker_id(k): v for k, v in orders.items()}

    async def my_trades_v2(self, pair: str, start_ms: int | None = None, end_ms: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"symbol": to_symbol(pair).lower()}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        r = await self.v2_get("/api/v2/myTrades", **params)
        return r.get("data", []) if isinstance(r, dict) else r

    async def permission_report(self) -> PermissionReport:
        """Probe both backends; report what works and whether withdrawals look possible."""
        notes: list[str] = []
        if self.v2_pair_incomplete:
            notes.append("INDODAX_V2_API_KEY/SECRET hanya terisi sebagian — diabaikan; memakai key utama")
        legacy_ok = v2_ok = None
        withdraw = can_trade = None
        try:
            _, ws = await self.balances_legacy()
            legacy_ok = True
            if ws == 1:
                # account-level flag, not the key's permission -> informational only
                notes.append("legacy getInfo: withdraw_status=1 (account-level; says nothing about this key)")
        except IndodaxAPIError as e:
            legacy_ok = False
            notes.append(f"legacy /tapi not usable: {e}")
        if legacy_ok:
            try:
                await self.legacy("withdrawFee", currency="btc")
                withdraw = True
                notes.append("legacy withdrawFee berhasil -> key ini PUNYA izin withdraw")
            except IndodaxAPIError as e:
                text = str(e).lower()
                if "permission" in text or "no_permission" in str(e.code).lower():
                    withdraw = False
                    notes.append("legacy withdrawFee ditolak (no permission) -> key TANPA izin withdraw")
                else:
                    notes.append(f"legacy withdrawFee: hasil tidak pasti ({e})")
        try:
            _, can_trade, can_withdraw = await self.account_v2()
            v2_ok = True
            if can_withdraw:
                withdraw = True
                notes.append("v2 /account: canWithdraw=true")
            elif can_withdraw is False and withdraw is None:
                withdraw = False
        except IndodaxAPIError as e:
            v2_ok = False
            notes.append(f"v2 API not usable: {e}")
        return PermissionReport(legacy_ok, v2_ok, withdraw, can_trade, notes)
