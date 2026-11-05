# Skill Indodax (pipeline modular, paper-mode default)

| Urutan | Skill | Fungsi | Status |
|---|---|---|---|
| 1 | `indodax-market` | ticker / orderbook / trades / pairs (Public API, read-only) | ✅ |
| 2 | `indodax-analyst` | SMA, EMA, RSI dari trade history | rencana |
| 3 | `indodax-budget` | sizing dari saldo IDR, cash buffer, cap per koin | rencana |
| 4 | `indodax-risk` | max loss per trade, max exposure per koin, daily loss limit | rencana |
| 5 | `indodax-broker` | order via Private API (HMAC-SHA512) **hanya bila `INDODAX_LIVE=true`**, selain itu paper trade di `orders.json` | rencana |
| 6 | `indodax-alerts` | alert harga / pergerakan signifikan | rencana |
| 7 | `indodax-report` | ringkasan harian ke Telegram (Bot API) | rencana |

Aturan bersama: Python 3 standard library; output JSON agar bisa dipipe antar skill; kredensial hanya
dari environment variable (`INDODAX_API_KEY`, `INDODAX_API_SECRET`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`), tidak pernah di file config dan tidak pernah dicetak.
Tes per skill: `python3 .claude/skills/<skill>/scripts/test_*.py`.
