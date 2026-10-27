# Eksperimen model prediksi harga (ML) — didaftarkan SEBELUM diuji (2026-10-27)

Permintaan pemilik: apakah model prediksi harga dapat memperbaiki sistem? Eksperimen riset saja —
tidak menyentuh live. Kode: `scripts/research_ml.py`. Data: candle harian Indodax 6 koin whitelist
(BTC sejak 2015; ETH/XRP 2017; ADA/DOGE 2018; SOL 2021) sampai 2026-10-26.

## Model
- Target: return 7 hari ke depan > 0 (klasifikasi), per koin per hari.
- Fitur (hanya data <= hari t): return 1/3/7/14/30/60/90 hari; jarak ke EMA20/50/100; volatilitas
  10/30 hari dan rasionya; RSI14; z-score volume 30 hari; drawdown dari high 90 hari; return BTC
  7/30 hari; return rata-rata pasar (6 koin) 7/30 hari.
- Model utama: HistGradientBoostingClassifier (max_depth 3, learning_rate 0,05, 200 iterasi,
  min_samples_leaf 100, l2 1,0). Pembanding: regresi logistik.
- Walk-forward: latih ulang tiap 90 hari dengan seluruh data sebelumnya, dengan jeda 7 hari agar label
  masa depan tidak bocor (purge). Prediksi out-of-sample 2020-01-01 s/d 2026-10-26.

## Strategi yang dibandingkan (simulasi harian, biaya 0,442% per IDR yang ditransaksikan)
- A — baseline = model Nadia sekarang: koin di atas EMA100, bobot inverse-vol, target volatilitas
  portofolio 20% (kovarians 60 hari), maks 45% per koin, rebalance mingguan + saat koin masuk/keluar tren.
- B — A + filter ML: koin hanya eligible bila di atas EMA100 **dan** p(naik 7 hari) >= 0,5.
- C — ML saja: eligible bila p >= 0,55 (tanpa filter tren), bobot & target volatilitas sama.

## Kriteria lolos (semua harus terpenuhi untuk B dipertimbangkan)
1. Prediksi: AUC out-of-sample keseluruhan >= 0,53 **dan** > 0,50 di setiap tahun 2020–2026.
2. B mengalahkan A (return setelah biaya) di >= 5 dari 7 tahun **dan** secara total.
3. Sharpe B > Sharpe A, dan drawdown maks B <= drawdown maks A + 5 poin.
4. Tetap berlaku dengan biaya 2×.
Bila lolos: diuji lagi di mesin backtest asli lalu paper trading sebelum ada keputusan live.
Bila gagal: model tidak dipakai; sistem tetap seperti sekarang.

## Hasil (dijalankan 2026-10-27, out-of-sample 2020-01-01 s/d 2026-10-26, 18.356 baris)

**Prediksi** (naik dalam 7 hari): GBM AUC 0,523, akurasi 52,2% (basis naik 48,9%); per tahun 2020 0,478 ·
2021 0,517 · 2022 0,475 · 2023 0,532 · 2024 0,559 · 2025 0,536 · 2026 0,559. Logistik AUC 0,518.
Dua tahun (2020, 2022) lebih buruk dari acak.

**Strategi** (simulasi sederhana harian — tanpa batas modal agent dan tanpa ambang rebalance, sehingga
lebih peka biaya daripada mesin asli; perbandingan A/B/C tetap setara):

| | Total 2020+ | DD | Sharpe | Biaya 2× (total / DD) |
|---|---|---|---|---|
| A baseline EMA100 (model Nadia) | +292,9% | 33,3% | 1,06 | +121,7% / 45,7% |
| B EMA100 + filter ML | +70,1% | 35,3% | 0,55 | −47,4% / 66,7% |
| C ML saja | +77,2% | 32,7% | 0,57 | −60,2% / 72,9% |

B hanya unggul di 1 dari 7 tahun. Filter ML membuat sinyal berganti-ganti (probabilitas sering melintasi
0,5) → transaksi dan biaya jauh lebih banyak, sementara keunggulan prediksinya terlalu kecil.

| Kriteria | Hasil |
|---|---|
| 1. AUC ≥ 0,53 dan > 0,5 tiap tahun | ❌ 0,523; 2020 & 2022 < 0,5 |
| 2. B > A di ≥ 5/7 tahun dan total | ❌ 1/7 |
| 3. Sharpe B > A, DD B ≤ DD A + 5 | ❌ 0,55 vs 1,06 |
| 4. Tetap unggul dengan biaya 2× | ❌ |

**Kesimpulan: TIDAK LOLOS — model prediksi tidak dipakai; sistem live tidak diubah.**
