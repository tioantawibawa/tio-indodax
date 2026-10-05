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

## Hasil revisi (kode final, spread 0,1%, slippage 0,1%, fee penuh)

| Periode | Return bersih | /tahun | PF | Max DD | Trade | Win | Fee | Sebelum fee | Buy & hold |
|---|---|---|---|---|---|---|---|---|---|
| **2018 – 2026-09** | **+57,6%** | +5,4% | **2,33** | **5,7%** | 131 | 47% | Rp 79.601 | Rp 655.743 | BTC +615%, ETH +309%, SOL −37%* |
| 2018–2019 | +9,7% | +4,8% | 2,52 | 3,7% | 18 | 44% | Rp 11.002 | Rp 108.252 | BTC −53%, ETH −85% |
| 2020–2021 | +36,9% | +17,0% | 8,05 | 3,9% | 28 | 68% | Rp 16.396 | Rp 385.544 | BTC +561%, ETH +2806% |
| 2022–2023 | +6,0% | +3,0% | 1,51 | 6,4% | 32 | 44% | Rp 19.180 | Rp 79.536 | BTC −3%, ETH −34%, SOL −38% |
| 2024–2026-09 | +4,9% | +1,8% | 1,22 | 8,5% | 53 | 40% | Rp 33.022 | Rp 82.411 | BTC +125%, ETH +35%, SOL +24% |

\* SOL sejak listing Nov 2021.

**Kriteria yang didaftarkan: LOLOS semua** — PF keseluruhan 2,33 > 1,3; return bersih positif di keempat
sub-periode; max drawdown 5,7% < 15%.

### Uji ketahanan (bukan tuning — parameter live tetap yang didaftarkan)
- Parameter tetangga (breakout 15/30, chandelier 2,5/3,5 ATR, EMA 50/200): **semua lolos** kriteria
  (return penuh +50% s/d +65%, PF 2,1–2,7). Hasil tidak bergantung pada satu titik parameter.
- Biaya dinaikkan: spread 0,2% + slippage 0,5% → +52,2%, PF 2,14; spread 0,25% + slippage 1% → +45,9%,
  PF 1,94, semua sub-periode tetap positif (2024–2026 hanya +0,3%).
- Catatan: spread ≥ 0,3% menyentuh batas `max_spread_pct` sehingga risk manager memblokir entry — itu
  perilaku filter, bukan hasil ekonomi.

### Keterbatasan yang harus dipahami
1. **Return absolut kecil (~5%/tahun) dan jauh di bawah buy & hold di pasar naik.** Penyebab utamanya
   hard limit: maks 10% modal per posisi dan 3 posisi → paling banyak ~30% modal yang bekerja. Imbalannya,
   drawdown hanya 5,7% (buy & hold BTC/ETH pernah −53% s/d −85%). Menaikkan `max_position_pct` akan
   memperbesar return *dan* drawdown — keputusan pemilik.
2. **Edge melemah di periode terakhir** (2024–2026: PF 1,22, +1,8%/tahun). Perlu dipantau di paper trading.
3. Backtest bukan jaminan. Spread/slippage historis adalah asumsi (nyata saat ini jauh lebih tipis).


## Revisi pemilik 2026-10-01 — ukuran posisi varian B

Pemilik meminta profil lebih agresif. Enam varian diuji dengan data dan kode yang sama (2018-01-01 s/d
Sep 2026, spread 0,1% + slippage 0,1%; diulang dengan 0,2% + 0,2%):

| Varian | Return | DD maks | Trade | PF |
|---|---|---|---|---|
| A awal (risiko 1%, posisi maks 10%) | +57,7% | 5,7% | 131 | 2,33 |
| **B (risiko 2%, posisi maks 20%) — dipilih** | **+115,3%** | **8,3%** | 131 | 2,33 |
| C (risiko 3%, posisi maks 30%) | +173,0% | 10,2% | 131 | 2,33 |
| D breakout 10 hari + EMA50 | +74,0% | 6,6% | 177 | 2,28 |
| E D + stop 2×ATR | +64,6% | 5,6% | 245 | 2,00 |
| F D + ukuran B | +147,9% | 9,1% | 177 | 2,28 |

Dengan biaya 2×: A +55,8%, B +111,6% (DD 8,6%), C +167,4%. B dan C hanya mengubah ukuran, sehingga sinyal
dan trade identik dengan strategi yang sudah didaftarkan (tidak ada risiko overfitting baru). D/E/F mengubah
parameter sinyal setelah melihat data, sehingga tidak dipakai. Pemilik memilih **B**:
`risk.max_position_pct: 20`, `strategy.risk_per_trade_pct: 2.0`; limit lain tidak berubah (maks 3 posisi,
exposure per aset 30%, rugi harian 3%, kill switch drawdown 15%).

## Revisi pemilik 2026-10-04 — whitelist ditambah XRP, DOGE, ADA

Kriteria ditetapkan **sebelum** backtest dilihat: (1) volume 24 jam ≥ Rp 1 miliar dan spread ≤ 0,3%
(bukan stablecoin); (2) riwayat 1D ≥ 3 tahun; (3) backtest per koin sejak 2018 / listing: PF ≥ 1,3,
return positif, ≥ 10 trade; (4) DD portofolio tetap ≤ 12%.

Likuid (4 Okt 2026): xrp, doge, ada, sui, hype, mubarak, aster, pengu. Gugur di (2): sui (2,6 th),
hype, aster, pengu, mubarak (< 1,5 th). Per koin (biaya 0,1% + 0,1%, ukuran varian B):

| Koin | Return | DD | Trade | PF | Lolos |
|---|---|---|---|---|---|
| btc (sudah ada) | +54,9% | 5,4% | 51 | 3,13 | — |
| eth (sudah ada) | +55,3% | 5,9% | 51 | 2,62 | — |
| sol (sudah ada) | +4,5% | 8,8% | 29 | 1,12 | (di bawah kriteria; tetap, keputusan pemilik) |
| **xrp** | +23,0% | 11,9% | 47 | 1,56 | ✅ |
| **doge** | +52,0% | 10,7% | 27 | 3,64 | ✅ |
| **ada** | +25,0% | 12,0% | 36 | 1,78 | ✅ |
| sui (info) | +6,3% | 6,3% | 9 | 1,17 | ❌ |

Portofolio (maks 3 posisi, modal Rp 300.000):

| Whitelist | Return | DD | Trade | PF | Biaya 2× |
|---|---|---|---|---|---|
| btc, eth, sol | +114,7% | 8,3% | 131 | 2,33 | +111,0% |
| **+ xrp, doge, ada** | **+175,3%** | **10,9%** | 192 | 2,24 | +173,8% |

## Permintaan pemilik 2026-10-05 — strategi short-term (didaftarkan SEBELUM diuji)

Data: candle 1h 2023-01-01 s/d sekarang, 6 pair whitelist (4h = resample dari 1h). Modal Rp 300.000,
ukuran varian B, maks 3 posisi, semua hard limit & cek biaya risk manager tetap. Kandidat:

| ID | TF | Gaya | Entry | Stop | TP | Time stop |
|---|---|---|---|---|---|---|
| A | 1h | st_breakout | close > high 24 bar, > EMA200 | 1,5×ATR | 3×ATR | 48 bar (2 hari) |
| B | 1h | st_breakout + trailing | close > high 24 bar, > EMA200 | Chandelier 22/3 | — | 72 bar (3 hari) |
| C | 1h | st_pullback | RSI14 < 30, > EMA200 | 2×ATR | 2×ATR | 24 bar (1 hari) |
| D | 1h | st_pullback | RSI14 < 25, > EMA200 | 2,5×ATR | 1,5×ATR | 12 bar |
| E | 4h | st_breakout | close > high 12 bar, > EMA100 | 1,5×ATR | 3×ATR | 18 bar (3 hari) |
| F | 4h | st_pullback | RSI14 < 35, > EMA100 | 2×ATR | 2,5×ATR | 12 bar (2 hari) |

Kriteria lolos (semua harus terpenuhi): PF ≥ 1,3 setelah fee; return positif di setiap tahun
(2023, 2024, 2025, 2026); tetap positif dengan biaya 2× (spread 0,2% + slippage 0,2%); ≥ 30 trade.
Bila beberapa lolos: pilih Sharpe tertinggi. Bila tidak ada yang lolos: strategi live TIDAK diganti.

### Hasil (2023-02-01 s/d 2026-10-05, 1h; kill switch 15% menghentikan backtest bila tersentuh)

| ID | Return | DD | Trade | PF | Biaya 2× | Per tahun 23/24/25/26 | Lolos |
|---|---|---|---|---|---|---|---|
| A | −15,2% | 15,6% | 126 | 0,53 | −14,7% | −15,2 / kill switch | ❌ |
| B | −13,5% | 15,0% | 137 | 0,56 | −13,7% | −13,5 / kill switch | ❌ |
| C | +3,1% | 7,8% | 87 | 1,16 | +2,0% | +2,1 / +7,0 / −3,3 / −2,4 | ❌ (PF, tahun negatif) |
| D | −1,0% | 3,1% | 9 | 0,62 | −0,5% | — | ❌ (trade < 30) |
| E | −14,0% | 15,5% | 99 | 0,61 | −13,6% | −14,0 / kill switch | ❌ |
| F | −3,5% | 6,8% | 40 | 0,78 | −4,8% | −0,3 / −3,1 / −1,5 / +1,3 | ❌ |

**Tidak ada yang lolos → strategi live tidak diganti.** Kode gaya `st_breakout` / `st_pullback` + time
stop tetap tersedia (default `style: trend_follow`) untuk pengujian berikutnya.

Temuan sampingan — strategi harian bila dimulai 2023-02-01 dengan modal Rp 300.000 (kondisi live sekarang):

| Konfigurasi | Return | DD | Trade | PF |
|---|---|---|---|---|
| 6 koin, risiko 2% / posisi 20% (live) | +2,5% | **15,4% (kill switch 2024)** | 54 | 1,05 |
| 3 koin, 2% / 20% | +26,4% | 13,8% | 74 | 1,47 |
| 6 koin, 1% / 10% | +10,6% | 9,7% | 96 | 1,27 |
| 3 koin, 1% / 10% | +13,2% | 7,9% | 74 | 1,47 |
