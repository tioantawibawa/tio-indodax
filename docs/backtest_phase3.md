# Hasil Backtest Fase 3 (2026-09-23)

Data: candle Indodax asli 15m/1h/4h, btc_idr, eth_idr, sol_idr, 27 Mar – 23 Sep 2026 (180 hari),
+1 bulan warm-up. Modal Rp 1.000.000, hard limits & fee sesuai `config/settings.yaml`.
Asumsi simulasi: spread 0,1% (nyata ~0,00–0,002%, jadi konservatif), slippage exit 0,1%.

## Kesimpulan: strategi saat ini TIDAK layak live

| | Baseline |
|---|---|
| PnL sebelum fee | **Rp −26.344** (tidak ada edge bahkan tanpa biaya) |
| Total fee+pajak+kliring | Rp 82.787 |
| PnL bersih | **Rp −109.131 (−10,9%)**, max DD 11,7%, profit factor 0,36, win rate 33% |
| Buy & hold periode sama | BTC +32%, ETH +40%, SOL +44% |

Dengan spread nyata yang lebih tipis (0,01%) hasilnya tetap negatif (−12,8%; lebih banyak trade lolos filter).

### Dari mana ruginya
- **Range reversion**: win rate 16%, rugi sebelum fee Rp −21.087 → setup ini buruk di pasar yang sedang naik.
- **Trend pullback**: hampir impas sebelum fee (Rp −7.025) tapi fee Rp 56.662.
- Fee rata-rata **0,58% dari notional per round-trip**; 141 trade × ~Rp 584 ≈ 8% modal dalam 6 bulan.
- Take-profit tetap (3×ATR 1h ≈ +2%) memotong kenaikan besar; stop-loss rata-rata −1,4%.
- 584 proposal di-veto karena expected move < 2× biaya → filter biaya bekerja, tapi yang lolos pun belum cukup.

### Uji varian (ditetapkan sebelum melihat hasil; dipecah 2 × 90 hari)

| Varian | Mar–Jun bersih | Jun–Sep bersih | PF |
|---|---|---|---|
| Baseline | −6,26% | −4,65% | 0,34 / 0,39 |
| A: tanpa range reversion | −3,60% | −2,63% | 0,45 / 0,50 |
| B: A + SL 2,5 / TP 6 ATR | −2,76% | −2,76% | 0,61 / 0,60 |

Tidak ada varian yang positif setelah biaya. Varian tidak di-tuning lebih jauh untuk menghindari overfitting.

## Usulan perbaikan (butuh keputusan pemilik)
1. **Ganti gaya ke trend-following jangka lebih panjang** (sinyal 4h/1D, trailing stop, tanpa TP tetap):
   jauh lebih sedikit trade → fee drag turun drastis, dan kenaikan besar tidak dipotong.
   Data 1D Indodax tidak terkena batas 7 hari, sehingga bisa diuji pada **beberapa tahun**
   (termasuk pasar turun) — jauh lebih kuat daripada 6 bulan yang kebetulan bullish.
2. **Hapus setup range reversion.**
3. **Exit take-profit sebagai limit order maker** (fee 0,1% alih-alih 0,2%).
4. Tetapkan kriteria lolos sebelum uji: mis. PF > 1,3 dan return bersih > 0 di **setiap** sub-periode,
   serta drawdown < 15%. Kalau tidak tercapai, jangan lanjut ke live.

---

# Revisi Fase 3 — desain & kriteria ditetapkan SEBELUM uji (2026-09-23)

Disetujui pemilik: poin 1–4 di atas.

## Strategi `trend_follow` (long-only, candle harian 1D, 00:00 UTC)
Parameter ditetapkan di depan (nilai klasik, tidak di-tuning ke data):
- **Entry**: close harian > high tertinggi 20 hari sebelumnya (Donchian breakout) **dan** close > EMA100.
  Order: limit di best ask (langsung terisi, fee taker) segera setelah candle harian close.
- **Stop awal & trailing (Chandelier exit)**: SL = high tertinggi 22 hari − 3 × ATR(20).
  SL hanya boleh naik, tidak pernah turun. Stop dicek tiap siklus 5 menit (live) / intrabar (backtest).
- **Tanpa take-profit tetap** — posisi dibiarkan jalan sampai trailing stop kena.
  (Karena tidak ada TP, poin 3 "TP sebagai maker" tidak berlaku; semua exit adalah stop.)
- **Target untuk cek biaya**: entry + 4 × ATR(20) — hanya dipakai risk manager untuk aturan
  "expected move ≥ 2× biaya", bukan untuk exit.
- Ukuran: risiko 1% modal bila SL kena, lalu dibatasi hard limit (maks 10% per posisi).
- Setup range reversion dihapus.

## Periode & kriteria lolos
Data: 2017-01 s/d 2026-09-23 (warm-up dari data sebelumnya). Pair masuk sejak listing (SOL 2021-11).
Sub-periode: **2018–2019, 2020–2021, 2022–2023, 2024–2026-09**.

Lolos hanya jika **semua** terpenuhi (asumsi spread 0,1%, slippage 0,1%, fee penuh):
1. Profit factor keseluruhan > 1,3
2. Return bersih > 0 di **setiap** sub-periode
3. Max drawdown < 15%

Jika tidak lolos: dilaporkan apa adanya, tidak lanjut ke live.
