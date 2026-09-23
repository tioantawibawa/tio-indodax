# indodax-agent

Agent auto-trading kripto untuk Indodax: berjalan 24/7 di VPS, menganalisa pasar, mengeksekusi
limit order secara mandiri **di dalam batas risiko keras (hard limits) yang di-enforce oleh kode**,
dan mengirim laporan harian ke Telegram.

> ⚠️ **Status: Fase 1 dari 5 selesai** (riset API + kerangka + klien public API).
> Belum ada logika trading, belum ada pemanggilan endpoint private, belum ada order.

## Risiko — baca dulu

- Trading kripto bisa menghabiskan modal. Backtest/paper yang bagus **tidak menjamin** hasil live.
- Hard limits membatasi kerugian per hari / drawdown, tetapi tidak melindungi dari gap harga ekstrem,
  gangguan exchange, atau bug. Gunakan modal yang siap hilang.
- API key harus **tanpa izin withdraw** dan di-whitelist ke IP VPS. Agent menolak mode live jika
  terdeteksi izin withdraw (Fase 5).

## Arsitektur

```
data → analisa → strategy (TradeProposal) → risk_manager (APPROVE/RESIZE/VETO) → executor → DB → Telegram
```

| Modul | Tanggung jawab | Fase |
|---|---|---|
| `agent/exchange/` | klien REST/WS Indodax, signature, rate limit, retry, format pair | 1 (public) · 4–5 (private) |
| `agent/data/` | ticker, orderbook, trades, OHLC, cache | 2 |
| `agent/analysis/` | indikator, sinyal, regime, LLM analyst opsional | 2 |
| `agent/strategy/` | menghasilkan `TradeProposal` | 2 |
| `agent/risk/` | hard limits — satu-satunya penentu boleh/tidaknya order | 2 |
| `agent/execution/` | limit order, cancel/replace, konfirmasi fill, deadman switch | 5 |
| `agent/portfolio/` | saldo, posisi, PnL | 2/4 |
| `agent/storage/` | SQLite: orders, fills, decisions, daily_pnl, errors | 2 |
| `agent/reporting/` | bot Telegram, laporan harian | 4 |
| `agent/backtest/` | backtester, paper broker | 3/4 |

Detail API Indodax (endpoint, signature, rate limit, format pair, presisi, fee, ambiguitas):
[`docs/indodax_api_notes.md`](docs/indodax_api_notes.md).

## Konfigurasi

- `config/settings.yaml` — non-rahasia, termasuk **hard limits** di bagian `risk:`. Dimuat sekali saat
  start ke objek immutable; key tak dikenal / nilai tidak konsisten → agent menolak start.
- `.env` — rahasia & mode. Salin dari `.env.example`, lalu `chmod 600 .env`. **Jangan pernah di-commit**
  (sudah di `.gitignore`).
- `MODE=backtest|paper|live`. Live butuh `LIVE_CONFIRM=I_UNDERSTAND_THE_RISK` dan ≥14 hari hasil paper
  di DB (gate kedua diimplementasi Fase 5).

## Menjalankan (dev)

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # unit test (tanpa jaringan)
.venv/bin/python -m scripts.smoke_public   # uji read-only ke API publik Indodax sungguhan
```

`smoke_public` tidak butuh API key dan tidak mengirim order. Ia memeriksa semua endpoint publik yang
dipakai, format pair, tick/step/minimum order pair whitelist, dan **selisih jam VPS vs server Indodax**
(harus < 500 ms; kalau tidak, aktifkan chrony/NTP).

Panduan deploy VPS (`deploy/`) dibuat di Fase 5.
