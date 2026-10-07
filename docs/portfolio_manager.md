# Manajer Portofolio & Reviewer (keputusan pemilik 2026-10-07)

Permintaan: agent yang mengalokasikan anggaran Rp 300.000 agar terserap, plus review berkala atas
kualitas dan kinerja modelnya.

## Riset (sederhana, harga penutupan harian, biaya 0,44% per IDR yang ditransaksikan)

| Model | Sejak | Return total | DD maks | Modal terpakai rata-rata |
|---|---|---|---|---|
| Beli & tahan 6 koin | 2023-02 | +228% | 67% | 100% |
| Tren EMA100 + inverse-vol, vol portofolio 30% | 2023-02 | +168% | 37% | 47% |
| **… vol portofolio 20% (dipilih)** | 2023-02 | +103% | 26% | 31% |
| … vol portofolio 10% | 2023-02 | +46% | 14% | 16% |

Pemilik memilih target volatilitas 20%. Hard limit diubah sesuai pilihan itu: maks 6 posisi, maks 45%
per posisi dan per aset, rugi harian 6%, kill switch drawdown 40%.

## Model (agent/portfolio/manager.py)

1. Koin eligible bila close harian > EMA100.
2. Bobot inverse-volatility (stdev return harian 30 hari).
3. Seluruh buku diskalakan agar volatilitas portofolio (kovarians 60 hari, disetahunkan) = 20%, tanpa
   leverage, maks 45% per koin; sisanya kas.
4. Rencana dibuat tiap 7 hari. Jual dulu; beli di siklus berikutnya setelah penjualan selesai.
   Selisih < 5% modal (min Rp 15.000) tidak ditransaksikan; order di bawah minimum Indodax dilewati.
5. Stop darurat tetap entry − 6×ATR (tidak di-trailing); keluar normal lewat filter tren mingguan.
6. Setiap order tetap lewat risk manager (hard limit, biaya, minimum, aturan stop-loss).

Catatan implementasi: versi pertama memakai trailing Chandelier 4×ATR sebagai stop wajib → 140 dari 192
posisi tertutup oleh stop (bukan oleh model) dan return 2023+ hanya +28%. Stop diganti menjadi stop
darurat lebar agar kode setia pada model (bukan penyesuaian parameter terhadap hasil).

## Validasi di mesin backtest yang sama dengan live (modal Rp 300.000, 6 koin)

| Sejak | Return | DD maks | Trade | PF | Biaya 2× | Kill switch |
|---|---|---|---|---|---|---|
| 2018-06 | +117,1% | 22,3% | 175 | 2,07 | +111,1% | tidak pernah |
| 2023-02 | +46,5% | 22,1% | 103 | 1,78 | +43,9% | tidak pernah |

Pembanding strategi sebelumnya (trend_follow 1%/10%): 2018+ +105,7% (DD 6,3%), 2023+ +10,6% (DD 9,7%).
Modal terpakai di mesin asli: rata-rata 26% (2023+), sering 46%, puncak 66% — lebih rendah dari riset
karena posisi dibatasi modal agent (tidak compounding melebihi Rp 300.000) dan ambang rebalance.
Hasil sensitif terhadap detail eksekusi (versi antara memberi +52% / DD 14,5%); anggap angka di atas
sebagai perkiraan, bukan janji.

## Reviewer (agent/portfolio/review.py)

Mingguan (Senin 21:30 WIB) + `/review`. Mengukur sejak model mulai dan 28 hari terakhir: return, DD,
volatilitas nyata vs target, pemakaian modal, biaya/30 hari, turnover, **selisih vs simulasi bayangan
model** (kesetiaan eksekusi), pembanding tahan-6-koin dan BTC. Vonis BAIK / PERHATIAN / BURUK / AWAL
dengan ambang di config `portfolio.review_*`. Reviewer tidak pernah mengubah pengaturan — hanya
melapor dan merekomendasikan; keputusan di pemilik.
