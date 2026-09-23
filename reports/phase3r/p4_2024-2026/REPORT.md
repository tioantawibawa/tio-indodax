# Backtest btc_idr, eth_idr, sol_idr

Periode: 2024-01-02 00:00 → 2026-09-23 00:00 UTC (995.0 hari)  
Asumsi: spread 0.1%, slippage exit 0.1%, fee/pajak/kliring dari config & /api/pairs.

**Hasil setelah biaya POSITIF** (Rp 49.390).

| Metrik | Nilai |
|---|---|
| Modal awal → akhir | Rp 1.000.000 → Rp 1.049.390 |
| Return total | 4.939% (disetahunkan 1.78%) |
| Max drawdown | 8.508% |
| Sharpe (harian, disetahunkan) | 0.325 |
| Trade selesai / masih terbuka | 53 / 3 |
| Win rate | 39.6% |
| Rata-rata menang / kalah | Rp 11.369 / Rp -6.096 |
| Profit factor | 1.224 |
| **PnL sebelum fee** | Rp 82.411 |
| **Total fee (fee+pajak+kliring)** | Rp 33.022 |
| **PnL bersih** | Rp 49.390 |
| Estimasi biaya spread+slippage saat exit | Rp 7.243 |

**Buy & hold (dari data pertama pair dalam periode):** btc_idr +124.73%, eth_idr +34.88%, sol_idr +24.48%

**Alasan exit:** {'stop_loss': 53, 'open at end': 3}  
**Setup:** {'trend_breakout': 56}  
**Keputusan risk manager:** {'RESIZE': 25, 'APPROVE': 31}

**Alasan veto terbanyak:**


**Event:** tidak ada
