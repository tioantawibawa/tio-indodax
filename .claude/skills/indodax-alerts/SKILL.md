---
name: indodax-alerts
description: Pasang dan cek alert pasar Indodax — harga menembus level, pergerakan ≥ X% dalam N menit, RSI masuk zona overbought/oversold, dan EMA golden/death cross. Edge-triggered (tidak spam) dengan teks siap kirim ke Telegram via indodax-report. Gunakan saat pemilik minta "kabari kalau BTC tembus …", "alert kalau turun 5%", atau untuk memantau pasar secara berkala. Tidak pernah mengirim order.
---

# indodax-alerts

Skrip: `scripts/alerts.py` (Python 3 standard library; data dari `indodax-market`, RSI/EMA dari
`indodax-analyst`). **Hanya informasi, tidak pernah mengirim order.**

## Perintah

```bash
A=.claude/skills/indodax-alerts/scripts/alerts.py

python3 $A add price btc_idr --above 1500000000 --note "target TP"   # atau --below
python3 $A add move  btc_idr --pct 5 --window 60 --cooldown 60        # |perubahan| ≥ 5% dalam 60 menit
python3 $A add rsi   eth_idr --tf 240 --above 70                      # atau --below 30; --period 14
python3 $A add cross sol_idr --tf 1D --fast 12 --slow 26              # golden/death cross EMA
python3 $A list --pretty
python3 $A remove a3
python3 $A check --pretty                                             # evaluasi semua alert sekali
```

Timeframe (`--tf`): `1`, `15`, `30`, `60`, `240`, `1D`. `move` memakai candle 1 menit (≤ 6 jam),
15 menit (≤ 3 hari), atau 1 jam, dibandingkan dengan harga `last` terkini.

## Perilaku (tidak spam)

| Jenis | Kapan berbunyi |
|---|---|
| `price` | saat harga **mulai** melewati level; aktif lagi setelah harga kembali ke sisi semula |
| `rsi` | saat RSI **masuk** zona; aktif lagi setelah keluar zona |
| `move` | saat \|perubahan\| ≥ pct, lalu jeda `--cooldown` menit |
| `cross` | sekali per candle tempat persilangan terjadi (hanya candle yang sudah tutup) |

Aturan di `$INDODAX_STATE_DIR/alerts.json`, status di `alerts_state.json` (default `~/.indodax-skills/`).
Data tiap pair/timeframe diambil sekali per `check`. Bila sebagian pair gagal diambil, alert lain tetap jalan
(`errors`); bila semua gagal → exit 3.

## Output `check`

`triggered` (id, kind, pair, message), `errors`, dan `text` — gabungan pesan siap kirim (kosong bila tidak ada
alert). Kirim ke Telegram: `alerts.py check | report.py send --stdin` (indodax-report).
Jalankan berkala (mis. cron/systemd tiap 5 menit); jangan lebih sering dari 1×/menit (batas API 180 req/menit).
Exit code: `0` ok, `2` input salah, `3` data pasar tidak tersedia.

## Aturan yang harus diikuti agent

- Alert adalah **informasi**. Jangan mengubahnya menjadi order tanpa melewati budget → risk → broker dan
  persetujuan pemilik.
- Konfirmasi ke pemilik setiap alert yang ditambah/dihapus (tampilkan `list --pretty`).
