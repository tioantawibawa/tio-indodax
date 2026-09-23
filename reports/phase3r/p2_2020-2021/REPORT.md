# Backtest btc_idr, eth_idr, sol_idr

Periode: 2020-01-02 00:00 → 2022-01-01 00:00 UTC (730.0 hari)  
Asumsi: spread 0.1%, slippage exit 0.1%, fee/pajak/kliring dari config & /api/pairs.

**Hasil setelah biaya POSITIF** (Rp 369.147).

| Metrik | Nilai |
|---|---|
| Modal awal → akhir | Rp 1.000.000 → Rp 1.369.147 |
| Return total | 36.915% (disetahunkan 17.01%) |
| Max drawdown | 3.948% |
| Sharpe (harian, disetahunkan) | 2.542 |
| Trade selesai / masih terbuka | 28 / 0 |
| Win rate | 67.9% |
| Rata-rata menang / kalah | Rp 22.187 / Rp -5.822 |
| Profit factor | 8.045 |
| **PnL sebelum fee** | Rp 385.544 |
| **Total fee (fee+pajak+kliring)** | Rp 16.396 |
| **PnL bersih** | Rp 369.147 |
| Estimasi biaya spread+slippage saat exit | Rp 3.899 |

**Buy & hold (dari data pertama pair dalam periode):** btc_idr +560.92%, eth_idr +2805.85%, sol_idr -26.34%

**Alasan exit:** {'stop_loss': 28}  
**Setup:** {'trend_breakout': 28}  
**Keputusan risk manager:** {'RESIZE': 7, 'APPROVE': 21}

**Alasan veto terbanyak:**


**Event:** tidak ada
