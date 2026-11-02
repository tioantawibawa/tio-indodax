# Perbaikan laba bersih — 2 November 2026

Perubahan kode sudah diterapkan dan diuji. Parameter kandidat belum dipromosikan:
tidak ada varian yang meningkatkan return bersih di kedua periode harian, dan
periode verifikasi 2026 masih negatif. `config/settings.yaml`, modal Rp300.000,
target volatilitas 20%, dan hard limits tetap menjadi baseline.

## Perubahan

- Filter biaya membedakan limit pasif dan limit yang menyentuh ask. Untuk fill
  live hasil lookup tanpa informasi fee, estimasi memakai taker; flag non-darurat
  tidak lagi dianggap bukti maker. Fee aktual dari acknowledgement tetap dipakai
  bila tersedia. Rekonsiliasi fee aktual historis belum tersedia dari API legacy.
- `portfolio.trend_buffer_pct` menambahkan hysteresis: masuk di atas EMA + buffer,
  tetap eligible sampai close berada di bawah EMA − buffer. State eligible disimpan
  di rencana agar restart tidak menghilangkan memori tren. Default 0 mempertahankan
  aturan EMA lama.
- `portfolio.max_positions` membatasi jumlah target, menggunakan urutan inverse-vol
  yang sama dengan model asal. `minimum_aware` membuang alokasi yang tidak memenuhi
  minimum transaksi atau minimum exit pada stop. Bobot yang dibuang menjadi kas,
  bukan dialihkan otomatis ke koin lain. Default keduanya mempertahankan baseline.
- Dengan `react_to_changes`, close harian baru yang sinkron memvalidasi kembali
  rencana yang belum terlaksana tanpa menggeser jadwal mingguan. Candle lama tidak
  memperpanjang validitas. Proposal/defer/waiting dicatat dalam `pm:plan.execution`;
  fill sebenarnya tetap ada pada tabel orders/fills. Rencana lama tetap terbaca.
- `portfolio.execution_style: maker_first` mencoba bid untuk beli dan ask untuk jual,
  lalu memakai harga marketable setelah `maker_wait_minutes` (default 5). Waktu
  percobaan disimpan lintas restart. Order lama harus selesai/dibatalkan sebelum
  fallback. Maker sell juga punya expiry; cancel gagal tidak membuat order pengganti.
  Order pasif tidak dijamin maker karena buku bisa bergerak sebelum request tiba.
  Default tetap `taker`; stop darurat tetap melalui jalur exit darurat.
- Backtester dapat memakai timeframe eksekusi terpisah dari sinyal, delay sinyal,
  depth terbatas, partial fill dan turnover. Candle harian yang belum selesai atau
  masih dalam delay tidak terlihat oleh engine maupun manajer portofolio.

## Hasil harian

Baseline dan kandidat menggunakan engine yang sama setelah koreksi, bukan replay
kode versi lama. Periode dimulai dari portofolio kas baru, tanpa carry antarperiode.
Return berikut kumulatif setelah fee, bukan return per tahun.

| Varian | Feb 2023–Des 2025 | Jan–1 Nov 2026 |
|---|---:|---:|
| Baseline | +67,914% | −7,253% |
| Buffer EMA 1% | +73,524% | −10,493% |
| Minimum-aware | +59,359% | −6,432% |
| Maksimal 3 target + minimum-aware | +56,053% | −9,651% |
| Gabungan buffer 1% + 3 target + minimum-aware | +73,179% | −9,291% |

Biaya 2× memperberat maker/taker per-pair, tarif fallback, pajak, kliring, spread
dan slippage. Seluruh kandidat juga gagal screen pada kondisi ini. Hasil lengkap,
drawdown, trade count, parameter, dan hash sumber data/kode ada pada
[`daily.json`](../reports/optimization/daily.json).

Screen ditetapkan di script: return positif dan lebih tinggi dari baseline pada
kedua periode dan kedua tingkat biaya, minimal 10 trade selesai per pengujian,
drawdown tidak melebihi baseline + 2 poin persentase atau 30%.

## Pemeriksaan eksekusi intraday

Uji September dan Oktober–1 November 2026 menggunakan quote/stop 15 menit, sinyal
harian tertunda 300 detik, dan hanya 50% depth sintetis yang bisa dieksekusi.
Baseline dibandingkan kandidat gabungan dan maker-first. Hasil tersimpan pada
[`intraday.json`](../reports/optimization/intraday.json). Jumlah trade selesai pada
masing-masing pengujian kurang dari 10: hasil ini pemeriksaan integrasi eksekusi,
bukan bukti statistik keunggulan maker.

## Mengulang pengujian

```bash
cd /workspace/tio-indodax
.venv/bin/python -m pytest -ra
.venv/bin/python -m scripts.download_history --since 2017-01-01 --timeframes 1D
.venv/bin/python -m scripts.download_history --months 6 --timeframes 15
.venv/bin/python -m scripts.compare_portfolios \
  --start 2023-02-01 --split 2026-01-01 --end 2026-11-02 \
  --variants baseline buffer_1pct minimum_aware slots_3 combined \
  --out reports/optimization/daily.json
.venv/bin/python -m scripts.compare_portfolios \
  --start 2026-09-01 --split 2026-10-01 --end 2026-11-02 \
  --variants baseline combined maker_first --execution-timeframe 15 \
  --signal-delay 300 --fill-fraction 0.5 --out reports/optimization/intraday.json
```

Untuk satu kandidat, gunakan `scripts.run_backtest --settings
config/settings.research.yaml` dengan tanggal dan data yang sesuai. File ini
khusus riset; pemilihan kandidat tidak pernah mengubah settings aktif atau mode.
`--fill-fraction` adalah fraksi kapasitas depth, bukan fraksi paksa dari setiap order.

## Batas interpretasi

Data publik yang baru diunduh tersedia sejak 2017/listing untuk candle harian dan
sekitar April 2026 untuk candle 15 menit. Angka memakai snapshot metadata pair
saat download, bukan riwayat fee/minimum order pada tiap tanggal. Orderbook dan
antrean historis tidak tersedia. Limit pasif dianggap valid untuk candle eksekusi
berikutnya dan membutuhkan trade-through, dengan remainder dibatalkan; ini belum
mereplikasi expiry 5 menit di dalam candle 15 menit. Stop intrabar masih mengasumsikan
fill penuh dan tidak mereplikasi batas slippage/no-fill exit live. Karena itu hasil
tidak menjadi dasar aktivasi live.

Periode verifikasi bisa tumpang tindih dengan riset lama proyek; jangan menyebutnya
holdout yang belum pernah dilihat. Peningkatan keuntungan ke depan belum terbukti.
Tidak ada order nyata, pesan Telegram, deployment, atau publikasi environment yang
dilakukan dalam pekerjaan ini.
