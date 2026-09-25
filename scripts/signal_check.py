"""Show why the agent is (not) buying right now — read-only, public data only.

    python -m scripts.signal_check

For every whitelisted pair: the agent's own decision (same code path as the
live cycle) and an independent pandas recomputation of the entry rule over the
last closed daily candles. Daily candles close at 00:00 UTC = 07:00 WIB.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from agent.analysis.signals import compute_features
from agent.config import load_secrets, load_settings
from agent.data.market_data import MarketDataService
from agent.exchange.public_client import IndodaxPublicClient
from agent.strategy.strategy import Strategy


async def main() -> int:
    sec = load_secrets(".env")
    s = load_settings(sec.AGENT_SETTINGS)
    st = s.strategy
    tf = st.timeframes[0]
    print(f"now {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · rule: close > highest high of prior "
          f"{st.breakout_bars} days AND close > EMA{st.trend_ema} (closed {tf} candles only)")
    async with IndodaxPublicClient.from_settings(s.exchange) as pub:
        md = MarketDataService(pub, st.timeframes, st.lookback_bars)
        strat = Strategy(st, Decimal(str(s.risk.agent_capital_idr)))
        for pair in s.market.whitelist:
            m = await md.fetch(pair)
            df = m.candles[tf]
            f = compute_features(df, tf, st)
            prop, note = strat.propose_entry(pair, m.info, m.orderbook, {tf: f})
            c, h = df["close"], df["high"]
            t = pd.DataFrame({"close": c, f"prev{st.breakout_bars}high": h.shift(1).rolling(st.breakout_bars).max(),
                              f"ema{st.trend_ema}": c.ewm(span=st.trend_ema, adjust=False).mean()}).tail(5)
            t["breakout"] = (t.iloc[:, 0] > t.iloc[:, 1]) & (t.iloc[:, 0] > t.iloc[:, 2])
            print(f"\n=== {pair} · last closed candle {df.index[-1]:%Y-%m-%d} · ask now {m.orderbook.best_ask:,.0f}")
            print(t.to_string(float_format=lambda x: f"{x:,.0f}"))
            print("agent decision:", f"PROPOSE BUY {prop.qty} @ {prop.price:,.0f} SL {prop.stop_loss:,.0f}"
                  if prop else note)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
