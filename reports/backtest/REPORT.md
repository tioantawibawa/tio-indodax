# Backtest btc_idr, eth_idr, sol_idr

Periode: 2026-03-27 06:30 → 2026-09-23 06:15 UTC (180.0 hari)  
Asumsi: spread 0.1%, slippage exit 0.1%, fee/pajak/kliring dari config & /api/pairs.

**Hasil NEGATIF bahkan sebelum fee** (Rp -26.344 sebelum fee, Rp -109.131 setelah fee).

| Metrik | Nilai |
|---|---|
| Modal awal → akhir | Rp 1.000.000 → Rp 890.869 |
| Return total | -10.913% (disetahunkan -20.89%) |
| Max drawdown | 11.661% |
| Sharpe (harian, disetahunkan) | -6.323 |
| Trade selesai / masih terbuka | 141 / 2 |
| Win rate | 33.3% |
| Rata-rata menang / kalah | Rp 1.339 / Rp -1.844 |
| Profit factor | 0.363 |
| **PnL sebelum fee** | Rp -26.344 |
| **Total fee (fee+pajak+kliring)** | Rp 82.787 |
| **PnL bersih** | Rp -109.131 |
| Estimasi biaya spread+slippage saat exit | Rp 8.995 |

**Buy & hold periode yang sama:** btc_idr +32.21%, eth_idr +40.29%, sol_idr +44.09%

**Alasan exit:** {'stop_loss': 82, 'take_profit': 46, 'regime_exit': 13, 'open at end': 2}  
**Setup:** {'trend_pullback': 99, 'range_reversion': 44}  
**Keputusan risk manager:** {'RESIZE': 202, 'VETO': 617, 'APPROVE': 17}

**Alasan veto terbanyak:**

- 584× expected move # < #x round-trip cost #
- 27× cooldown after stop-loss on <pair> until #
- 6× orderbook too thin to exit this size

**Event:** tidak ada
