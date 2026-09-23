# indodax-agent

Agent auto-trading kripto untuk Indodax: berjalan 24/7 di VPS, menganalisa pasar, mengeksekusi
limit order secara mandiri **di dalam batas risiko keras (hard limits) yang di-enforce oleh kode**,
dan mengirim laporan harian ke Telegram.

> ⚠️ **Status: Fase 2 dari 5 selesai** (data, analisa, strategi, risk manager, jurnal DB).
> Belum ada eksekusi order, belum ada pemanggilan endpoint private. Backtest = Fase 3.

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

## Alur keputusan per siklus (`agent/engine.py`)

1. **Data** — ticker, orderbook, candle 15m/1h/4h (hanya candle yang sudah close; cache per timeframe).
2. **Analisa** — EMA, RSI, MACD, ATR, Bollinger, ADX, rasio volume per timeframe → regime
   `trending_up / trending_down / ranging / high_volatility / no_trade`.
3. **Exit dulu** — posisi terbuka dicek: SL kena → *emergency market sell*; TP kena atau regime 1h
   berbalik turun / volatil → limit sell.
4. **Entry** — dua setup long-only (spot): *trend pullback* (4h tidak turun, 1h trending up, 15m RSI 40–65
   + MACD histogram naik) dan *range reversion* (1h ranging, 15m di bawah Bollinger bawah + RSI < 32).
   SL berbasis ATR, TP berbasis ATR / Bollinger tengah, ukuran = risiko 1% modal bila SL kena.
5. **LLM (opsional)** — hanya bisa menggeser confidence maks ±0,2 atau menahan proposal *buy*
   bila bearish ≥ 0,7. Tidak bisa membuat order, mengubah harga/qty/SL/TP, atau menyentuh limit.
   Error/timeout → siklus lanjut tanpa LLM.
6. **Risk manager** — APPROVE / RESIZE / VETO (detail di bawah).
7. **Jurnal** — setiap keputusan, termasuk VETO dan veto LLM, disimpan di tabel `decisions` beserta alasannya.

## Hard limits (`agent/risk/risk_manager.py`)

| Limit | Perilaku |
|---|---|
| Modal agent (`agent_capital_idr`) | total posisi + order pending ≤ modal; belanja ≤ kas ledger agent dan ≤ saldo IDR bebas di exchange |
| Maks per posisi 10% | proposal lebih besar di-RESIZE; posisi penuh → VETO (termasuk order pending) |
| Maks posisi terbuka 3 | pair baru ditolak bila sudah 3 (order pending dihitung) |
| Maks exposure per aset 30% | dijumlah lintas market (mis. `btc_idr` + `btc_usdt`) |
| Daily loss 3% modal | entry baru di-VETO + flag `trigger_daily_stop` (untuk alert & PAUSE) |
| Drawdown 15% dari puncak | entry di-VETO + flag `trigger_halt` (kill switch) |
| Order 10/jam, 40/hari | VETO bila tercapai |
| Hanya limit order | market order hanya untuk emergency stop-loss exit, dengan batas slippage 1% |
| Stop-loss wajib | buy tanpa SL, atau SL ≥ harga entry → VETO; tidak bisa dimatikan lewat config |
| Cooldown 30 menit setelah SL | per pair |
| Rekonsiliasi gagal | semua order di-VETO sampai rekonsiliasi |
| Biaya | expected move harus ≥ 2× biaya round-trip (fee maker+taker, pajak, kliring, spread, slippage) |
| Likuiditas | spread ≤ 0,3%, volume 24 jam ≥ minimum, slippage untuk keluar dari posisi ≤ 0,2% |
| Tanpa short | sell hanya sampai jumlah yang dipegang |

Setiap limit punya test di `tests/test_risk_manager.py`, plus fuzz test 3000 input acak yang
memverifikasi tidak ada order yang disetujui melanggar limit mana pun.

**Keputusan desain yang perlu konfirmasi pemilik:**
- *Emergency stop-loss exit dikecualikan* dari limit jumlah order per jam/hari dan tetap boleh jalan
  saat PAUSED/HALTED — karena memblokir stop-loss justru menambah risiko.
- Saat kill switch (HALTED): order terbuka dibatalkan (Fase 5), posisi **tetap dipegang** dengan SL-nya
  (emergency SL masih boleh). Alternatif: likuidasi semua posisi saat kill switch.

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
