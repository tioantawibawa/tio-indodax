# Skill Indodax (pipeline modular, paper-mode default)

| Urutan | Skill | Fungsi | Status |
|---|---|---|---|
| 1 | `indodax-market` | ticker / orderbook / trades / candles / pairs (Public API, read-only) | ✅ |
| 2 | `indodax-analyst` | SMA, EMA, RSI + bias tren dari candle atau trade history | ✅ |
| 3 | `indodax-budget` | sizing dari saldo IDR, cash buffer, cap per koin, sizing berbasis stop, fee & minimum order | ✅ |
| 4 | `indodax-risk` | ALLOW/BLOCK: max loss per trade (stop wajib), exposure per koin & total, daily loss limit, kill switch | ✅ |
| 5 | `indodax-broker` | order via Private API (HMAC-SHA512) **hanya bila `INDODAX_LIVE=true`**, selain itu paper trade di `orders.json`; live juga butuh `--confirm` + risk ALLOW segar | ✅ |
| 6 | `indodax-alerts` | alert harga, pergerakan ≥ X%, RSI zona, EMA cross — edge-triggered, teks siap Telegram | ✅ |
| 7 | `indodax-report` | ringkasan harian (saldo, P&L harian, order, alert) + kirim ke Telegram (Bot API) | ✅ |

Aturan bersama: Python 3 standard library; output JSON agar bisa dipipe antar skill; kredensial hanya
dari environment variable (`INDODAX_API_KEY`, `INDODAX_API_SECRET`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`), tidak pernah di file config dan tidak pernah dicetak.
Tes per skill: `python3 .claude/skills/<skill>/scripts/test_*.py`.

## Contoh alur lengkap (paper)

```bash
S=.claude/skills
python3 $S/indodax-broker/scripts/broker.py paper-init --idr 300000          # sekali
python3 $S/indodax-analyst/scripts/analyst.py btc_idr --pretty                # tren
python3 $S/indodax-broker/scripts/broker.py balance > /tmp/bal.json
python3 $S/indodax-budget/scripts/budget.py size btc_idr --balance /tmp/bal.json --stop-pct 5 \
  | python3 $S/indodax-risk/scripts/risk.py check --order - --balance /tmp/bal.json --stop-pct 5 > /tmp/risk.json \
  && python3 $S/indodax-broker/scripts/broker.py order --risk /tmp/risk.json --pretty
python3 $S/indodax-report/scripts/report.py daily --pretty
```

State bersama di `$INDODAX_STATE_DIR` (default `~/.indodax-skills/`): `orders.json`, `paper_balance.json`,
`risk_state.json`, `alerts.json`, `alerts_state.json`, dan file `HALT` (kill switch BUY).
Order **live** hanya bila `INDODAX_LIVE=true` + `--confirm` per order + hasil risk ALLOW ≤ 60 dtk.
