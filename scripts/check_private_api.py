"""Read-only check of the Indodax API key in .env (sends NO orders).

    python -m scripts.check_private_api

Reports: clock offset, which API backend accepts the key (legacy /tapi and/or
Trade API 2.0), whether withdrawals look possible, and the IDR balance vs the
configured agent capital. Never prints the key or secret.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

from agent.config import env_file_permission_problem, load_secrets, load_settings
from agent.exchange.private_client import PrivateReadOnlyClient
from agent.exchange.public_client import IndodaxPublicClient
from agent.exchange.signing import Clock


async def main() -> int:
    s, sec = load_settings(), load_secrets(".env")
    problem = env_file_permission_problem(".env")
    if problem:
        print("⚠️", problem)
    key, secret = sec.INDODAX_API_KEY.get_secret_value(), sec.INDODAX_API_SECRET.get_secret_value()
    if not (key and secret):
        print("INDODAX_API_KEY / INDODAX_API_SECRET kosong di .env")
        return 1
    async with IndodaxPublicClient.from_settings(s.exchange) as pub:
        offset = await pub.clock_offset_ms()
    print(f"clock offset: {offset} ms ({'OK' if abs(offset) <= s.exchange.max_clock_offset_ms else 'TERLALU BESAR - cek chrony'})")
    async with PrivateReadOnlyClient(
        key, secret, tapi_url=s.exchange.tapi_url, v2_base_url=s.exchange.tapi_v2_base_url,
        v2_api_key=sec.INDODAX_V2_API_KEY.get_secret_value() or None,
        v2_secret=sec.INDODAX_V2_API_SECRET.get_secret_value() or None,
        recv_window_ms=s.exchange.recv_window_ms, clock=Clock(offset_ms=offset),
    ) as pc:
        rep = await pc.permission_report()
        print(f"legacy /tapi: {'OK' if rep.legacy_ok else 'tidak bisa'} | Trade API 2.0: {'OK' if rep.v2_ok else 'tidak bisa'}")
        print(f"canTrade: {rep.can_trade} | withdraw mungkin: {rep.withdraw_possible}")
        for n in rep.notes:
            print(" -", n)
        bal = None
        if rep.v2_ok:
            bal, _, _ = await pc.account_v2()
        elif rep.legacy_ok:
            bal, _ = await pc.balances_legacy()
        if bal is not None:
            idr = bal.free_of("idr")
            cap = Decimal(str(s.risk.agent_capital_idr))
            print(f"IDR bebas: {idr:,.0f} | modal agent: {cap:,.0f} -> {'cukup' if idr >= cap else 'KURANG dari modal agent'}")
    if rep.withdraw_possible:
        print("‼️ Key ini bisa withdraw. Buat key baru TANPA izin withdraw sebelum mode live.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
