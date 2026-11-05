---
name: indodax-market
description: Ambil data pasar publik Indodax (ticker, orderbook, trade terakhir, daftar pair) tanpa API key. Gunakan saat perlu harga terkini, bid/ask/spread, kedalaman orderbook, atau riwayat trade sebuah pair IDR di Indodax — langkah pertama pipeline indodax-* (market → analyst → budget → risk → broker).
---

# indodax-market

Data **publik, read-only** dari Indodax Public API. Tidak butuh API key, tidak pernah mengirim order.
Skrip: `scripts/market.py` (Python 3 standard library saja).

## Perintah

```bash
python3 .claude/skills/indodax-market/scripts/market.py ticker btc_idr          # harga, bid/ask, spread, volume
python3 .claude/skills/indodax-market/scripts/market.py orderbook btc_idr --depth 10
python3 .claude/skills/indodax-market/scripts/market.py trades btc_idr --limit 50   # input indodax-analyst
python3 .claude/skills/indodax-market/scripts/market.py pairs --quote idr        # pair yang diperdagangkan
```

Semua perintah mencetak **JSON** ke stdout (agar bisa dipipe ke skill berikutnya); tambah `--pretty`
untuk teks yang mudah dibaca. Exit code: `0` sukses, `2` pair tidak dikenal / input salah, `3` gagal
jaringan / API setelah retry.

Format pair yang diterima: `btc_idr`, `BTC/IDR`, `btcidr`, `BTC-IDR` → dinormalkan ke id Indodax `btcidr`.

## Field output `ticker`

| Field | Arti |
|---|---|
| `last`, `bid`, `ask` | harga terakhir, bid terbaik (Indodax: `buy`), ask terbaik (Indodax: `sell`) |
| `spread_pct` | (ask − bid) / mid × 100 |
| `high`, `low` | 24 jam |
| `vol_base`, `vol_idr` | volume 24 jam dalam koin dan IDR |
| `server_time`, `age_s` | waktu server (epoch) dan umur data dalam detik — tolak data bila `age_s` > 60 |

## Aturan yang harus diikuti agent

- **Hanya baca.** Skill ini tidak punya dan tidak boleh diberi API key.
- Indodax mem-cache respons publik (Cloudflare, ±30 dtk). Skrip selalu menambahkan parameter anti-cache;
  tetap periksa `age_s` sebelum mengambil keputusan.
- Batas resmi 180 request/menit. Jangan memanggil dalam loop rapat; satu siklus per pair per beberapa detik cukup.
- Harga dalam IDR adalah bilangan bulat besar — skrip mengembalikan string desimal agar tidak kehilangan presisi.
