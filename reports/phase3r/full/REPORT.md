# Backtest btc_idr, eth_idr, sol_idr

Periode: 2018-01-02 00:00 → 2026-09-23 00:00 UTC (3186.0 hari)  
Asumsi: spread 0.1%, slippage exit 0.1%, fee/pajak/kliring dari config & /api/pairs.

**Hasil setelah biaya POSITIF** (Rp 576.143).

| Metrik | Nilai |
|---|---|
| Modal awal → akhir | Rp 1.000.000 → Rp 1.576.143 |
| Return total | 57.614% (disetahunkan 5.35%) |
| Max drawdown | 5.733% |
| Sharpe (harian, disetahunkan) | 1.132 |
| Trade selesai / masih terbuka | 131 / 3 |
| Win rate | 47.3% |
| Rata-rata menang / kalah | Rp 16.125 / Rp -6.222 |
| Profit factor | 2.329 |
| **PnL sebelum fee** | Rp 655.743 |
| **Total fee (fee+pajak+kliring)** | Rp 79.601 |
| **PnL bersih** | Rp 576.143 |
| Estimasi biaya spread+slippage saat exit | Rp 18.110 |

**Buy & hold (dari data pertama pair dalam periode):** btc_idr +614.69%, eth_idr +309.34%, sol_idr -37.04%

**Alasan exit:** {'stop_loss': 131, 'open at end': 3}  
**Setup:** {'trend_breakout': 134}  
**Keputusan risk manager:** {'APPROVE': 76, 'RESIZE': 58, 'VETO': 3}

**Alasan veto terbanyak:**

- 3× #h volume # IDR < min #

**Event:** tidak ada
