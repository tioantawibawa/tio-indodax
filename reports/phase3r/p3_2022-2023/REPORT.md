# Backtest btc_idr, eth_idr, sol_idr

Periode: 2022-01-02 00:00 → 2024-01-01 00:00 UTC (729.0 hari)  
Asumsi: spread 0.1%, slippage exit 0.1%, fee/pajak/kliring dari config & /api/pairs.

**Hasil setelah biaya POSITIF** (Rp 60.355).

| Metrik | Nilai |
|---|---|
| Modal awal → akhir | Rp 1.000.000 → Rp 1.060.355 |
| Return total | 6.036% (disetahunkan 2.98%) |
| Max drawdown | 6.383% |
| Sharpe (harian, disetahunkan) | 0.51 |
| Trade selesai / masih terbuka | 32 / 0 |
| Win rate | 43.8% |
| Rata-rata menang / kalah | Rp 12.718 / Rp -6.539 |
| Profit factor | 1.513 |
| **PnL sebelum fee** | Rp 79.536 |
| **Total fee (fee+pajak+kliring)** | Rp 19.180 |
| **PnL bersih** | Rp 60.355 |
| Estimasi biaya spread+slippage saat exit | Rp 4.270 |

**Buy & hold (dari data pertama pair dalam periode):** btc_idr -3.03%, eth_idr -33.82%, sol_idr -37.70%

**Alasan exit:** {'stop_loss': 32}  
**Setup:** {'trend_breakout': 32}  
**Keputusan risk manager:** {'APPROVE': 14, 'RESIZE': 18, 'VETO': 3}

**Alasan veto terbanyak:**

- 3× #h volume # IDR < min #

**Event:** tidak ada
