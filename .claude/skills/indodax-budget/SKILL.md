---
name: indodax-budget
description: Hitung ukuran posisi (sizing) untuk order Indodax dari saldo IDR dan holding, dengan cash buffer, cap per koin, sizing berbasis risiko (stop-loss), fee, dan minimum order. Gunakan sebelum indodax-risk/indodax-broker saat perlu tahu "boleh beli/jual berapa?" atau melihat sisa ruang anggaran per koin. Hanya usulan, tidak pernah mengirim order.
---

# indodax-budget

Langkah ketiga pipeline (market → analyst → **budget** → risk → broker). Skrip: `scripts/budget.py`
(Python 3 standard library). Harga diambil lewat `indodax-market` (ask untuk beli, bid untuk jual, last
untuk menilai holding) kecuali diberi `--price`. **Tidak butuh API key dan tidak pernah mengirim order.**

## Perintah

```bash
B=.claude/skills/indodax-budget/scripts/budget.py

python3 $B size btc_idr --idr 300000 --pretty                       # maks yang boleh dibeli
python3 $B size btc_idr --idr 300000 --hold eth=0.002 --stop-pct 5   # + batas risiko 1% equity di stop 5%
python3 $B size eth_idr --balance balance.json --want 50000          # minta Rp 50.000 → dipotong bila melebihi batas
python3 $B size sol_idr --balance balance.json --side sell --pct 50  # jual 50% holding
python3 $B plan --balance balance.json --pairs btc_idr,eth_idr --pretty   # ruang beli per koin
```

`balance.json` (atau `--balance -` dari stdin): `{"idr": 300000, "holdings": {"btc": "0.0001"}}`, atau
format `getInfo` Indodax `{"balance": {"idr": "...", "btc": "..."}}` — nanti dihasilkan `indodax-broker`.

## Aturan sizing (beli)

`order = min(` semua batas `)`, dibulatkan ke bawah ke rupiah:

| Batas | Rumus | Default |
|---|---|---|
| `deployable` | kas IDR − cash buffer (`--cash-buffer-pct` × equity) | buffer 20% |
| `coin_cap` | `--max-coin-pct` × equity − nilai koin itu sekarang | 25% |
| `requested` | `--want` IDR atau `--pct` × equity | — |
| `risk_per_trade` | (`--risk-pct` × equity) / (stop% + 2 × fee) — hanya bila `--stop-pct` diberi | 1% |

Equity = kas IDR + nilai semua holding (harga `last`). Fee default 0,3422%/sisi (taker 0,2 + pajak 0,12 +
clearing 0,0222). Bila hasil < `--min-order-idr` (default Rp 10.000) → `ok: false`, `order_idr: 0`,
`reason` menjelaskan batas mana yang mengikat (`limited_by`).

Jual: `est_amount` = holding × `--pct` (atau senilai `--want`), tidak pernah melebihi holding.

## Output

JSON berisi `order_idr`, `est_amount` (koin, sudah dikurangi fee), `est_fee_idr`, `price`, `limited_by`,
`limits_idr`, `equity_idr`, `position_idr/pct`, `reserve_idr`, `cap_idr`, `ok`, `reason`, `config`.
Angka uang berupa string desimal. Exit code: `0` (juga bila `ok: false`), `2` input salah, `3` harga tidak tersedia/basi.

## Aturan yang harus diikuti agent

- Hasilnya **usulan**. Selalu teruskan ke `indodax-risk` sebelum `indodax-broker`; broker tetap paper kecuali
  `INDODAX_LIVE=true` dan konfirmasi eksplisit pemilik.
- Jangan menaikkan `--max-coin-pct`, `--risk-pct`, atau menurunkan `--cash-buffer-pct` tanpa izin pemilik di chat.
- Saldo nyata hanya dari broker/pemilik; jangan mengarang saldo. Sebutkan bila memakai `--price` manual.
