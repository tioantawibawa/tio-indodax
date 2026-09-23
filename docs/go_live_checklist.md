# Checklist Go-Live

Centang semuanya **berurutan**. Jangan lompat langkah. Setiap perintah dijalankan dari `~/tio-indodax`
di VPS; perintah `agent-run.sh` menjalankan modul sebagai user `indodax`.

## A. Keamanan kredensial
- [ ] API key Indodax & token Telegram yang pernah terlihat di chat/screenshot sudah **dihapus/di-revoke**
      dan diganti yang baru. Isi `.env` tidak pernah difoto atau dikirim ke mana pun.
- [ ] Key produksi: izin **view + trade**, **tanpa withdraw**, whitelist IP VPS.
- [ ] `sudo bash deploy/agent-run.sh scripts.check_private_api` → `withdraw mungkin: False`.
- [ ] `sudo ls -l /opt/indodax-agent/.env` → `-rw-------` (600), owner `indodax`.

## B. Uji eksekusi di akun DEMO (uang mainan)
1. [ ] Daftar di https://demo-indodax.com (saldo koin demo otomatis), buat API key demo (view + trade).
2. [ ] Buat `.env` demo sementara — **ganti** isi `.env` produksi setelah dicadangkan:
   ```bash
   sudo systemctl stop indodax-agent
   sudo cp /opt/indodax-agent/.env /opt/indodax-agent/.env.production.bak
   sudo -u indodax nano /opt/indodax-agent/.env
   #   AGENT_SETTINGS=config/settings.demo.yaml
   #   INDODAX_API_KEY / INDODAX_API_SECRET = key DEMO
   ```
3. [ ] `sudo bash deploy/agent-run.sh scripts.live_order_check` → **PASSED** (order tidak terisi, dibatalkan).
4. [ ] `sudo bash deploy/agent-run.sh scripts.live_order_check --fill` → **PASSED** (beli+jual minimum).
5. [ ] Kirim output + file `logs/order_check_*.json` ke developer untuk validasi format respons.
       (`sudo cat /opt/indodax-agent/logs/order_check_*.json` — isinya tanpa key/secret.)
6. [ ] Opsional: jalankan agent live di demo (`MODE=live`, `LIVE_CONFIRM=I_UNDERSTAND_THE_RISK`),
       `sudo systemctl start indodax-agent`, pastikan Telegram menerima banner **MODE LIVE** tanpa
       "Mode LIVE ditolak", lalu `/kill CONFIRM` dan `/resume` berfungsi.
7. [ ] Kembalikan `.env` produksi:
       `sudo cp /opt/indodax-agent/.env.production.bak /opt/indodax-agent/.env && sudo chmod 600 /opt/indodax-agent/.env && sudo chown indodax:indodax /opt/indodax-agent/.env`

## C. Uji di akun ASLI dengan nominal minimum (≈ Rp 10.000, biaya ≈ Rp 70)
- [ ] `sudo bash deploy/agent-run.sh scripts.live_order_check --production` → PASSED.
- [ ] `sudo bash deploy/agent-run.sh scripts.live_order_check --production --fill` → PASSED.
- [ ] Kirim output ke developer.

## D. Syarat sebelum MODE=live di akun asli
- [ ] ≥ 14 hari paper trading tercatat (`/status` → "paper days tersimpan").
- [ ] Laporan harian paper masuk setiap hari; tidak ada error berulang yang belum dijelaskan.
- [ ] `timedatectl` → `System clock synchronized: yes`; `check_private_api` → clock offset OK.
- [ ] Saldo IDR di akun ≥ modal agent (Rp 500.000). Agent **tidak** menyentuh saldo di luar modal ini.
- [ ] **Tidak ada order manual** Anda di pair whitelist (btc_idr, eth_idr, sol_idr): Deadman Switch
      membatalkan SEMUA order terbuka di pair tersebut bila agent mati.
- [ ] Mulai kecil: pertimbangkan `agent_capital_idr` lebih kecil (mis. Rp 200.000) di minggu pertama.

## E. Aktifkan live
```bash
sudo -u indodax nano /opt/indodax-agent/.env
#   MODE=live
#   LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
sudo systemctl restart indodax-agent
sudo journalctl -u indodax-agent -n 40 --no-pager
```
- [ ] Telegram menerima **⚠️ MODE LIVE** + limit aktif + "Deadman Switch aktif". Jika menerima
      **⛔ Mode LIVE ditolak**, service berhenti (tidak restart otomatis). Perbaiki semua butir yang
      disebut, lalu `sudo systemctl restart indodax-agent` — atau kembali ke `MODE=paper`.
- [ ] `/status` menunjukkan mode LIVE, RUNNING.

## F. Selama live
- Stop darurat: `/kill CONFIRM` (batalkan order agent, HALTED; posisi tetap dengan stop-loss).
- Mematikan agent (`systemctl stop`) membatalkan order agent, **tetapi stop-loss tidak dipantau**
  selama agent mati — jangan biarkan mati lama saat ada posisi.
- Kembali ke paper: `MODE=paper` lalu restart. Posisi live yang ada **tidak** ditutup otomatis.
- Alert yang harus ditindaklanjuti: Rekonsiliasi gagal, Deadman Switch gagal, Error berulang, KILL SWITCH.
