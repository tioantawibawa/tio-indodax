---
name: indodax-analyst
description: Hitung indikator teknikal dasar (SMA, EMA, RSI) dan bias tren untuk pair Indodax dari candle OHLC atau trade history. Gunakan setelah indodax-market saat perlu membaca tren/momentum sebuah koin — langkah kedua pipeline indodax-* (market → analyst → budget → risk → broker). Hanya analisis, tidak pernah mengirim order.
---

# indodax-analyst

Indikator teknikal dari data publik Indodax. Skrip: `scripts/analyst.py` (Python 3 standard library),
memakai `indodax-market/scripts/market.py` untuk mengambil data. **Tidak pernah mengirim order.**

## Perintah

```bash
A=.claude/skills/indodax-analyst/scripts/analyst.py
M=.claude/skills/indodax-market/scripts/market.py

python3 $A btc_idr --pretty                          # candle harian (default 1D, 300 candle)
python3 $A eth_idr --tf 240 --sma 20,50 --ema 12,26 --rsi 14
python3 $M candles btc_idr --tf 60 --limit 500 | python3 $A --input -     # pipe dari market
python3 $M trades btc_idr --limit 1000 | python3 $A --input - --sma 20 --ema 9,21   # sangat jangka pendek
```

Timeframe (`--tf`): `1`, `15`, `30`, `60`, `240` (menit) atau `1D`. Output JSON (atau `--pretty`).
Exit code: `0` sukses, `2` input salah / data kurang, `3` data pasar tidak tersedia.

> Trade history Indodax hanya berisi trade beberapa menit terakhir (BTC: ±1000 trade). Untuk tren
> gunakan **candle**; trade history hanya untuk momentum sangat jangka pendek.

## Definisi (sama dengan TA-Lib / TradingView)

| Indikator | Rumus |
|---|---|
| SMA(n) | rata-rata n close terakhir |
| EMA(n) | diawali SMA n close pertama, lalu α = 2/(n+1) |
| RSI(n) | Wilder: rata-rata awal = mean n perubahan pertama, lalu `avg = (avg·(n−1) + x)/n` |

Hanya candle yang **sudah tutup** yang dipakai (candle berjalan dibuang oleh market.py).

## Output

- `indicators`: `smaN`, `emaN`, `rsiN` pada candle terakhir.
- `signals`: `price_vs_smaN` (above/below), `ema_cross` (bullish/bearish EMA cepat vs lambat),
  `fresh_cross` (baru menyilang di candle terakhir), `rsi_zone` (overbought ≥ 70 / oversold ≤ 30 / neutral).
- `bias`: skor = ±1 (EMA cepat vs lambat) ±1 (harga vs EMA lambat) → `bullish` (+2), `bearish` (−2), selain itu `neutral`.
- `reasons`: alasan dalam bahasa Indonesia; `note`: pengingat bahwa ini bukan perintah order.

## Aturan yang harus diikuti agent

- `bias` adalah **informasi**, bukan sinyal beli/jual. Keputusan ukuran posisi dan izin order tetap lewat
  `indodax-budget` → `indodax-risk` → `indodax-broker` (paper-mode default).
- Jangan menyimpulkan dari data yang terlalu sedikit; skrip menolak bila titik < periode terpanjang.
- Sebutkan timeframe dan sumber data (`source`) saat melaporkan hasil.
