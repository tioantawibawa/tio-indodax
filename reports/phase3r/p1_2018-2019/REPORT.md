# Backtest btc_idr, eth_idr, sol_idr

Periode: 2018-01-02 00:00 → 2020-01-01 00:00 UTC (729.0 hari)  
Asumsi: spread 0.1%, slippage exit 0.1%, fee/pajak/kliring dari config & /api/pairs.

**Hasil setelah biaya POSITIF** (Rp 97.250).

| Metrik | Nilai |
|---|---|
| Modal awal → akhir | Rp 1.000.000 → Rp 1.097.250 |
| Return total | 9.725% (disetahunkan 4.76%) |
| Max drawdown | 3.697% |
| Sharpe (harian, disetahunkan) | 0.977 |
| Trade selesai / masih terbuka | 18 / 0 |
| Win rate | 44.4% |
| Rata-rata menang / kalah | Rp 20.178 / Rp -6.417 |
| Profit factor | 2.515 |
| **PnL sebelum fee** | Rp 108.252 |
| **Total fee (fee+pajak+kliring)** | Rp 11.002 |
| **PnL bersih** | Rp 97.250 |
| Estimasi biaya spread+slippage saat exit | Rp 2.698 |

**Buy & hold (dari data pertama pair dalam periode):** btc_idr -53.31%, eth_idr -84.97%

**Alasan exit:** {'stop_loss': 18}  
**Setup:** {'trend_breakout': 18}  
**Keputusan risk manager:** {'APPROVE': 10, 'RESIZE': 8}

**Alasan veto terbanyak:**


**Event:** tidak ada
