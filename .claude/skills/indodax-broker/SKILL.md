---
name: indodax-broker
description: Eksekusi order Indodax yang sudah disetujui indodax-risk — default PAPER (dicatat di orders.json dengan saldo simulasi), LIVE via Private API (HMAC-SHA512) hanya bila INDODAX_LIVE=true DAN --confirm per order DAN hasil risk ALLOW yang masih segar. Juga baca saldo (paper/live), daftar order, open order, dan cancel. Langkah terakhir pipeline indodax-*.
---

# indodax-broker

Langkah kelima pipeline (market → analyst → budget → risk → **broker**). Skrip: `scripts/broker.py`
(Python 3 standard library).

## Gerbang keamanan (semua dicek di kode)

| Kondisi | Hasil |
|---|---|
| `INDODAX_LIVE` tidak di-set / bukan `true` | **PAPER** — dicatat di `orders.json`, saldo `paper_balance.json` diperbarui, tidak ada request ke Indodax |
| `INDODAX_LIVE=true` tanpa `--confirm` | **DITOLAK** (exit 1) — tidak pernah diam-diam jadi paper |
| `INDODAX_LIVE=true` + `--confirm` + key di env | **LIVE** limit order GTC via `/tapi` |
| Hasil risk BLOCK / > 60 dtk / fingerprint diubah / sudah pernah dipakai | **DITOLAK** |
| Kill switch (`INDODAX_HALT=1` atau file `HALT`) untuk BUY | **DITOLAK** |
| Pair maintenance/suspend, di bawah minimum Indodax setelah pembulatan | **DITOLAK** |

- Metode Private API yang bisa dipanggil hanya `getInfo`, `trade`, `openOrders`, `cancelOrder`, `getOrder`.
  **Withdraw/transfer tidak mungkin** dipanggil dari skill ini.
- Order live **tidak pernah di-retry**. Niat order dicatat di ledger **sebelum** dikirim; bila jaringan putus
  statusnya `unknown` → cek `broker.py open` sebelum mencoba lagi (persetujuan risk yang sama tidak bisa dipakai ulang).
- Harga dibulatkan ke tick Indodax ke arah aman (BUY turun, SELL naik); jumlah selalu dibulatkan ke bawah.

## Perintah

```bash
K=.claude/skills/indodax-broker/scripts/broker.py
R=.claude/skills/indodax-risk/scripts/risk.py
B=.claude/skills/indodax-budget/scripts/budget.py

python3 $K paper-init --idr 300000                     # sekali, modal simulasi
python3 $K balance > /tmp/bal.json                     # paper (default) atau --source live
python3 $B size btc_idr --balance /tmp/bal.json --stop-pct 5 \
  | python3 $R check --order - --balance /tmp/bal.json --stop-pct 5 > /tmp/risk.json
python3 $K order --risk /tmp/risk.json --pretty        # PAPER
python3 $K orders --today --pretty                     # ledger (input indodax-report)
python3 $K open                                        # open order live (read-only)
INDODAX_LIVE=true python3 $K cancel --pair btc_idr --order-id 123 --side buy --confirm
```

Exit code: `0` selesai, `1` ditolak gerbang keamanan, `2` input salah, `3` error jaringan/API.

## Kredensial

Hanya dari environment: `INDODAX_API_KEY`, `INDODAX_API_SECRET` (tidak pernah dicetak, di-log, atau ditulis
ke file). Gunakan **API key terpisah** dari agent live di VPS, izin **trade saja (tanpa withdraw)**, dan
whitelist IP bila memungkinkan. State: `$INDODAX_STATE_DIR` (default `~/.indodax-skills/`): `orders.json`,
`paper_balance.json` (lokasi ledger bisa diganti dengan `INDODAX_ORDERS_FILE`).

## Aturan yang harus diikuti agent

- **Jangan pernah** men-set `INDODAX_LIVE=true` atau menambahkan `--confirm` atas inisiatif sendiri. Order live
  hanya setelah pemilik menyetujui **order spesifik itu** (pair, sisi, jumlah, harga) di chat.
- Selalu jalankan `risk.py check` tepat sebelum `order`; jangan mengedit file hasil risk.
- Status `unknown` → laporkan ke pemilik dan cek `open`, jangan kirim ulang.
