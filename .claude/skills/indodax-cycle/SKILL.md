---
name: indodax-cycle
description: Jalankan satu siklus trading PAPER otomatis lewat seluruh pipeline indodax-* (analyst → budget → risk → broker) untuk daftar pair, dengan stop-loss, jeda beli ulang setelah stop, dan notifikasi Telegram; plus pemasang timer systemd (user, tanpa sudo) untuk siklus per jam, alert tiap 5 menit, dan laporan harian. Default paper; LIVE otomatis hanya dengan INDODAX_LIVE=true + INDODAX_LIVE_AUTO + saldo dipagari (lewat scripts/skills.py).
---

# indodax-cycle

Skrip: `scripts/cycle.py` (Python 3 standard library) + `systemd/` (unit timer user dan `install.sh`).

## Aturan siklus (sederhana, BELUM divalidasi — untuk menguji pipeline secara paper)

Per pair (default `btc_idr,eth_idr,sol_idr`, candle `--tf 240`):

1. **Jual dulu** koin yang dipegang bila harga ≤ stop, atau bias analyst `bearish`.
2. **Beli** koin yang belum dipegang bila bias `bullish` dan RSI tidak overbought. Ukuran dari `indodax-budget`
   (stop `--stop-pct`, default 5%), lalu `indodax-risk` harus ALLOW, lalu `indodax-broker` (paper).
3. Setelah kena stop, koin itu tidak dibeli lagi selama `--reentry-hours` (default 24 jam).
4. Tidak menambah posisi yang sudah ada (tanpa pyramiding).

## Perintah

```bash
C=.claude/skills/indodax-cycle/scripts/cycle.py
python3 $C run --dry-run --pretty                 # lihat keputusan tanpa order
python3 $C run --pairs btc_idr,eth_idr,sol_idr,xrp_idr,doge_idr,ada_idr --pretty
python3 $C run --notify                           # kirim ke Telegram bila ada order/masalah
python3 $C status --pretty                        # posisi paper: entry, stop, jeda
```

Exit code: `0` ok, `1` ditolak (INDODAX_LIVE=true / siklus lain masih jalan), `2` input salah, `3` data pasar
tidak ada untuk semua pair. State tambahan: `cycle_state.json` (posisi, jeda, ringkasan run terakhir), `cycle.lock`.

## Jadwal otomatis (systemd user, tanpa sudo)

```bash
python3 .claude/skills/indodax-broker/scripts/broker.py paper-init --idr 300000   # sekali
.claude/skills/indodax-cycle/systemd/install.sh --pairs btc_idr,eth_idr,sol_idr --dry-run   # pratinjau
.claude/skills/indodax-cycle/systemd/install.sh --pairs btc_idr,eth_idr,sol_idr
systemctl --user list-timers 'indodax-skills-*'
journalctl --user -u indodax-skills-cycle -n 50
.claude/skills/indodax-cycle/systemd/install.sh --uninstall
```

| Timer | Jadwal | Isi |
|---|---|---|
| `indodax-skills-cycle` | tiap jam menit :07 | `cycle.py run --tf 240 --notify` |
| `indodax-skills-alerts` | tiap 5 menit | `alerts.py check \| report.py send --stdin` |
| `indodax-skills-report` | 21:05 WIB | `report.py daily --source paper --send` |

Rahasia Telegram di `~/.config/indodax-skills/env` (dibuat `chmod 600` oleh installer, di luar repo).
Agar jalan 24/7 tanpa login, admin perlu sekali `sudo loginctl enable-linger <user>` — **minta izin pemilik dulu**.
Timer ini terpisah dari agent live di VPS (unit, state, dan bot Telegram berbeda) dan tidak menyentuhnya.

## Mode LIVE otomatis (modal dipagari)

`cycle.py run|stops --live --balance-file F` hanya jalan bila `INDODAX_LIVE=true` **dan**
`INDODAX_LIVE_AUTO=I_ACCEPT_AUTOMATIC_LIVE_TRADING` (izin tetap pemilik, menggantikan `--confirm` per order),
dengan file saldo dipagari (umur ≤ 3 menit) dari `scripts/skills.py` di repo agent. Batas broker tetap berlaku
(modal ≤ Rp 100.000, order ≤ Rp 50.000, sisa order dibatalkan). Equity buku −15% → file HALT.
`cycle.py stops` = cek stop-loss saja (dipanggil tiap 5 menit). Detail: `deploy/README_DEPLOY.md`.

## Aturan yang harus diikuti agent

- Jangan pernah menyalakan live (env `INDODAX_LIVE_AUTO`, `SKILLS_LIVE=true`) atau menaikkan batas modal/order atas inisiatif sendiri — hanya pemilik.
- Jangan memasang timer di VPS atau menjalankan `sudo` tanpa izin eksplisit pemilik di chat.
- Hasil paper dari aturan sederhana ini bukan bukti strategi menguntungkan; nilai setelah cukup lama (≥ beberapa minggu).
