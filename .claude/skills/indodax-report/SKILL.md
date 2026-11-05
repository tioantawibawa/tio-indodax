---
name: indodax-report
description: Ringkas aktivitas harian pipeline indodax-* (saldo & equity, P&L harian vs batas risk, order hari ini dari orders.json termasuk yang perlu dicek, kill switch, alert aktif) dan kirim teks apa pun ke Telegram lewat Bot API. Gunakan untuk laporan harian, mengirim output indodax-alerts, atau saat pemilik minta ringkasan/kiriman Telegram.
---

# indodax-report

Skrip: `scripts/report.py` (Python 3 standard library). Membaca (read-only) state skill lain:
`orders.json` & saldo dari `indodax-broker`, `risk_state.json` & kill switch dari `indodax-risk`,
`alerts.json` dari `indodax-alerts`, harga dari `indodax-market`.

## Perintah

```bash
P=.claude/skills/indodax-report/scripts/report.py

python3 $P daily --pretty                         # lihat laporan hari ini (WIB)
python3 $P daily --date 2026-11-04 --pretty
python3 $P daily --send --dry-run                 # pratinjau pesan Telegram
python3 $P daily --send                           # kirim
python3 .claude/skills/indodax-alerts/scripts/alerts.py check | python3 $P send --stdin   # kirim alert (kosong = tidak kirim)
python3 $P send --text "pesan bebas"
```

Sumber saldo: `--source paper|live`; default `live` hanya bila `INDODAX_LIVE=true` (baca `getInfo`, butuh
`INDODAX_API_KEY/SECRET`), selain itu saldo paper.

## Isi laporan harian

Equity, kas, nilai & porsi tiap koin · P&L hari ini vs equity awal hari (dari indodax-risk) dan tanda bila
batas harian tercapai · status kill switch · jumlah order (per mode), total beli/jual, fee, 10 order terakhir ·
**order berstatus `unknown`/`rejected`/`submitting` ditandai ⚠️ untuk dicek** · daftar alert aktif ·
peringatan bila data tidak tersedia (laporan tetap dibuat).

## Telegram

- Kredensial hanya dari env `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID`; token tidak pernah dicetak
  (pesan error disensor).
- Teks polos (tanpa parse mode, aman dari karakter khusus), dipecah otomatis per 4000 karakter.
- Retry untuk 429 (menghormati `retry_after`) dan 5xx, maksimal 3×; 4xx lain tidak di-retry.
- Input `send --stdin` boleh teks biasa atau JSON dengan field `text` (output `alerts.py check`); teks kosong → tidak kirim.

Exit code: `0` ok (termasuk "tidak ada yang dikirim"), `1` Telegram belum dikonfigurasi, `2` input salah,
`3` gagal kirim.

## Aturan yang harus diikuti agent

- Laporan hanya membaca; tidak pernah mengubah saldo, order, limit, atau alert.
- Gunakan `--dry-run` dulu saat pemilik belum pernah melihat format pesannya.
- Pakai bot/chat Telegram yang memang ditujukan untuk ini; jangan mengirim ke chat lain tanpa izin.
