# Evaluasi skills LIVE — kriteria ditetapkan SEBELUM melihat hasil

Ditetapkan 2026-11-05 (hari pertama live skills, modal dipagari Rp 100.000; agent live tetap berjalan terpisah).
Evaluasi pertama: **2026-11-19** (2 minggu). Kriteria tidak boleh diubah setelah melihat hasil.

## Data yang dikumpulkan (dari VPS, tanpa rahasia)

```bash
sudo bash deploy/agent-run.sh scripts.skills doctor
sudo bash deploy/agent-run.sh scripts.skills run --live broker balance --source book
sudo bash deploy/agent-run.sh scripts.skills run --live broker orders --limit 200 --pretty
sudo bash deploy/agent-run.sh scripts.skills run --live report daily --source book --pretty
sudo bash deploy/agent-run.sh scripts.trade_diagnostics            # agent live, periode yang sama
sudo journalctl -u indodax-agent --since 2026-11-05 --no-pager | grep -c '"reconcile_ok": false'
sudo journalctl -u 'indodax-skills-*' --since 2026-11-05 --no-pager | grep -ciE 'error|dilewati|unknown'
```

## Kriteria (semua harus lolos untuk LANJUT)

| # | Kriteria | Ambang |
|---|---|---|
| K1 | Keamanan: tidak ada order `unknown`/`open` tertinggal, tidak ada `reconcile_ok: false` pada agent akibat skills | 0 kejadian |
| K2 | Modal: equity buku skills (harga saat evaluasi) | ≥ Rp 97.000 (rugi ≤ 3%) |
| K3 | Relatif: return skills vs return agent pada periode yang sama | tidak lebih buruk > 5 poin persen |
| K4 | Disiplin risiko: kill switch drawdown (HALT) tidak terpicu; tidak ada order > Rp 50.000 | 0 kejadian |
| K5 | Operasional: siklus jam-an berjalan ≥ 90% (Satpam tidak melapor skills macet berulang) | ≥ 90% |

## Keputusan

- **Semua lolos** → lanjut sampai evaluasi 4 minggu (2026-12-03) dengan modal **tetap** Rp 100.000
  (tidak menaikkan modal sebelum 4 minggu).
- **K1 atau K4 gagal** → matikan live segera (`SKILLS_LIVE=false`), analisis penyebab.
- **K2 atau K3 gagal** → matikan live (kembali paper); aturan skills perlu diriset/backtest dulu sebelum live lagi.
- **Hanya K5 gagal** → perbaiki operasional, lanjut 2 minggu lagi.

Catatan: 2 minggu adalah sampel kecil (±336 siklus, beberapa trade). Hasil bagus belum membuktikan
aturannya unggul; hasil buruk dalam batas ambang juga belum membuktikan sebaliknya. Kriteria ini
terutama menjaga keamanan dan membatasi kerugian, bukan membuktikan edge.
