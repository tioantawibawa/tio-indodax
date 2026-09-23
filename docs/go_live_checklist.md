# Checklist Go-Live

Centang semuanya **berurutan**. Jangan lompat langkah. Setiap perintah dijalankan dari `~/tio-indodax`
di VPS; perintah `agent-run.sh` menjalankan modul sebagai user `indodax`.

## A. Keamanan kredensial
- [ ] API key Indodax & token Telegram yang pernah terlihat di chat/screenshot sudah **dihapus/di-revoke**
      dan diganti yang baru. Isi `.env` tidak pernah difoto atau dikirim ke mana pun.
- [ ] Key produksi: izin **view + trade**, **tanpa withdraw**, whitelist IP VPS.
- [ ] `sudo bash deploy/agent-run.sh scripts.check_private_api` → `withdraw mungkin: False`.
- [ ] `sudo ls -l /opt/indodax-agent/.env` → `-rw-------` (600), owner `indodax`.

## B. Validasi eksekusi di akun ASLI dengan nominal minimum
Akun demo Indodax tidak tersedia untuk pengguna umum, jadi format respons order divalidasi langsung di
akun asli dengan order sekecil mungkin. Script ini **tidak** memakai strategi dan **tidak** menyentuh
posisi agent; order-nya berawalan `chk-` (bukan order agent).

✅ **Selesai 2026-09-23** — Tes A dan Tes B lulus; temuan format respons sudah ditangani di kode
(lihat `docs/indodax_api_notes.md` §9b). Ulangi hanya bila developer memintanya.

1. [ ] Pastikan tidak ada order manual Anda di `btc_idr` (agar tidak tertukar saat membaca hasil).
2. [ ] **Tes A — biaya nol:** limit buy 10% di bawah harga (tidak akan terisi) → dibaca ulang → dibatalkan.
   ```bash
   sudo bash deploy/agent-run.sh scripts.live_order_check --production
   ```
   Harus berakhir `ORDER CHECK PASSED`. Butuh saldo IDR ≥ ±Rp 11.000 (order ditahan sebentar lalu dibatalkan).
3. [ ] Kirim seluruh output + isi file hasil ke developer:
   `sudo bash -c 'ls -t /opt/indodax-agent/logs/order_check_*.json | head -1 | xargs cat'` (tanpa key/secret).
4. [ ] **Tes B — biaya ±Rp 100** (setelah developer mengonfirmasi hasil Tes A): beli jumlah minimum
   (±Rp 10.000) lalu langsung jual kembali.
   ```bash
   sudo bash deploy/agent-run.sh scripts.live_order_check --production --fill
   ```
   Kirim output-nya juga. Batas keras script: Rp 20.000 per order.

## C. Canary: live dengan modal kecil
- [ ] Minggu pertama live boleh memakai `agent_capital_idr` lebih kecil, **minimal Rp 250.000**.
      Di bawah itu, posisi maks (10% modal) terlalu dekat dengan minimum order Indodax Rp 10.000:
      risk manager menolak entry yang nilai jualnya di harga stop-loss < 1,2 × minimum (agar stop-loss
      selalu bisa dieksekusi), sehingga agent hampir tidak akan pernah trading. Setelah seminggu tanpa
      anomali, naikkan ke Rp 500.000. (`install.sh` menimpa config dari repo — minta developer mengubahnya.)

## D. Syarat sebelum MODE=live di akun asli
- [ ] ≥ 14 hari paper trading tercatat (`/status` → "paper days tersimpan").
- [ ] Laporan harian paper masuk setiap hari; tidak ada error berulang yang belum dijelaskan.
- [ ] `timedatectl` → `System clock synchronized: yes`; `check_private_api` → clock offset OK.
- [ ] Saldo IDR di akun ≥ modal agent (Rp 500.000). Agent **tidak** menyentuh saldo di luar modal ini.
- [ ] **Tidak ada order manual** Anda di pair whitelist (btc_idr, eth_idr, sol_idr): Deadman Switch
      membatalkan SEMUA order terbuka di pair tersebut bila agent mati.
- [ ] Mulai kecil: lihat bagian C (minimal Rp 250.000).

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
