---
name: indodax-risk
description: Cek setiap order Indodax terhadap limit keras sebelum boleh ke broker — max loss per trade (wajib stop-loss), max exposure per koin, max total exposure, daily loss limit, kill switch, kas cukup, minimum order, dan harga vs pasar. Hasil ALLOW/BLOCK beserta alasan per cek. Gunakan setelah indodax-budget dan sebelum indodax-broker. Tidak pernah mengirim order.
---

# indodax-risk

Langkah keempat pipeline (market → analyst → budget → **risk** → broker). Skrip: `scripts/risk.py`
(Python 3 standard library; memakai parsing saldo & harga dari `indodax-budget`). **Tidak mengirim order.**

## Perintah

```bash
R=.claude/skills/indodax-risk/scripts/risk.py
B=.claude/skills/indodax-budget/scripts/budget.py

python3 $R check btc_idr --idr-order 50000 --stop-pct 5 --balance balance.json --pretty
python3 $R check btc_idr --idr-order 50000 --stop-price 1390000000 --idr 300000      # stop sebagai harga
python3 $B size eth_idr --balance balance.json --stop-pct 4 \
  | python3 $R check --order - --balance balance.json --stop-pct 4                     # pipe dari budget
python3 $R check sol_idr --side sell --amount 0.5 --balance balance.json
python3 $R status --balance balance.json --pretty                                      # P&L harian & kill switch
```

Exit code: **`0` ALLOW**, **`1` BLOCK**, `2` input salah (mis. saldo tidak diberikan), `3` harga tidak tersedia/basi.

## Cek

| Cek | BUY | SELL | Default |
|---|---|---|---|
| `kill_switch` — env `INDODAX_HALT=1` atau file `HALT` di state dir | ✔ | – | – |
| `stop_loss_set` — BUY wajib punya `--stop-pct`/`--stop-price` | ✔ | – | – |
| `max_loss_per_trade` — order × (stop% + 2 × fee) ≤ % equity | ✔ | – | 1% |
| `max_coin_exposure` — nilai koin setelah order ≤ % equity | ✔ | – | 25% |
| `max_total_exposure` — semua koin setelah order ≤ % equity | ✔ | – | 80% |
| `daily_loss_limit` — equity vs awal hari (WIB) | ✔ | – | −3% |
| `enough_idr` / `enough_coin` | ✔ | ✔ | – |
| `min_order` | ✔ | ✔ | Rp 10.000 |
| `price_vs_market` — harga order vs ask/bid sekarang | ✔ | ✔ | ≤ 2% |

SELL selalu boleh saat daily limit/kill switch aktif karena mengurangi risiko. Limit bisa diubah per panggilan
(`--max-coin-pct`, `--daily-loss-limit-pct`, …) — **hanya dengan izin pemilik**.

**Daily loss:** equity pada cek pertama tiap tanggal WIB disimpan di `$INDODAX_STATE_DIR/risk_state.json`
(default `~/.indodax-skills/`). Setoran/penarikan IDR di tengah hari ikut mengubah equity — pakai
`--day-start-equity` untuk mengoreksi.

## Output

`decision` (ALLOW/BLOCK), `failed` (daftar cek gagal), `checks` (nilai, batas, keterangan per cek), `order`
(pair, side, idr, amount, price, stop_pct), `order_fingerprint`, `checked_at`, `equity_idr`, `day_pnl_pct`, `limits`.
`indodax-broker` hanya menerima order yang punya hasil risk **ALLOW** yang masih segar dan fingerprint-nya cocok.

## Aturan yang harus diikuti agent

- Jangan pernah meneruskan order BLOCK ke broker, dan jangan "mengakali" BLOCK dengan menaikkan limit sendiri.
- Saldo wajib nyata (dari broker/pemilik) — skrip menolak berjalan tanpa saldo.
- Untuk menghentikan semua BUY seketika: `touch ~/.indodax-skills/HALT` (hapus file untuk melanjutkan).
