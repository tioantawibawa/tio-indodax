# Catatan API Indodax

Sumber: `https://github.com/btcid/indodax-official-api-docs` (dibaca 2026-09-23, branch `master`).
File yang dibaca: `Public-RestAPI.md`, `Private-RestAPI.md`, `INDODAX-TradeAPI-2.md`,
`tapi-v2/enums.md`, `Marketdata-websocket.md`, `Private-websocket.md`, `Deadman-switch.md`,
`Self-Trade Prevention-TradeAPI.md`.

Tanda: ✅ = sudah diverifikasi (contoh signature dihitung ulang), ⚠️ = ambigu / perlu konfirmasi,
❓ = pertanyaan untuk pemilik akun.

---

## 1. Ringkasan permukaan API

| API | Base URL | Auth | Dipakai agent untuk |
|---|---|---|---|
| Public REST | `https://indodax.com` | tidak ada | ticker, orderbook, trades, OHLC, info pair |
| Private REST (legacy "TAPI") | `https://indodax.com/tapi` (POST) | header `Key` + `Sign` = HMAC-**SHA512** | `trade`, `cancelOrder`, `getOrderByClientOrderId`, `openOrders`, `getInfo` |
| Trade API 2.0 ("TAPIv2") | `https://api.indodax.com` ⚠️ (dok. STP menyebut `https://tapi.indodax.com`) | header `X-APIKEY` + `Sign` = HMAC-**SHA256** | `/api/v2/myTrades`, `/api/v2/order/histories`, `/api/v2/account`, order v2 |
| Deadman Switch | `https://indodax.com/tapi/countdownCancelAll` | header `Key` + `Sign` = HMAC-SHA512 | auto-cancel order jika agent mati |
| Market Data WS | `wss://ws3.indodax.com/ws/` | static token publik (JSON-RPC ala Centrifugo) | ticker/orderbook real-time (opsional) |
| Private WS | `wss://pws.indodax.com/ws/?cf_ws_frame_ping_pong=true` | token dari `POST /api/private_ws/v1/generate_token` | event order/fill real-time |

Demo/sandbox: dokumentasi menyebut `https://demo-indodax.com`, tetapi **akun demo tidak tersedia untuk
pengguna umum** (dikonfirmasi pemilik, 2026-09-23). Validasi eksekusi dilakukan di akun asli dengan
order minimum (`scripts/live_order_check.py --production`).

---

## 2. Format pair (penting — tiap endpoint beda!)

Format kanonik internal agent: **`btc_idr`** (sama dengan whitelist di config).
Konversi di `agent/exchange/pairs.py`.

| Tempat | Format | Contoh |
|---|---|---|
| `/api/pairs` field `ticker_id` | `base_quote` | `btc_idr` |
| `/api/pairs` field `id` | `basequote` | `btcidr` |
| `/api/pairs` field `symbol` | `BASEQUOTE` | `BTCIDR` |
| `/api/ticker/{pair_id}`, `/api/trades/{pair_id}`, `/api/depth/{pair_id}` | `id` | `btcidr` |
| `/api/summaries`, `/api/ticker_all` → key `tickers` | `ticker_id` | `btc_idr` |
| `/api/summaries` → key `prices_24h`, `prices_7d` | `id` | `btcidr` |
| `/api/price_increments` key | `ticker_id` | `btc_idr` |
| `/tradingview/history_v2?symbol=` | `symbol` | `BTCIDR` |
| Legacy `/tapi` param `pair` | `ticker_id` | `btc_idr` |
| TAPIv2 param `symbol` | `symbol` (atau lower-case) | `BTCIDR` / `btcidr` |
| Deadman `pair` (boleh comma-separated) | `ticker_id` | `btc_idr,eth_idr` |
| WS channel | `id` | `market:order-book-btcidr` |

---

## 3. Public REST

- Base `https://indodax.com`, semua GET, JSON.
- **Rate limit: 180 request/menit** (per IP). Klien membatasi diri ke default 150/menit (margin).
- Timestamp: dok. bilang "milliseconds", **tetapi contoh respons** `ticker.server_time`, `trades.date`,
  OHLC `Time` dalam **detik**; `/api/server_time.server_time` dalam **ms**. ⚠️ Parser menangani keduanya
  (heuristik: nilai > 10^12 = ms).
- Angka sering dikirim sebagai **string** → parser selalu konversi ke `Decimal`.
- ⚠️ **Cache CDN (terverifikasi 2026-09-23):** semua endpoint publik mengirim
  `cache-control: public, max-age=30` (OHLC `max-age=60`) dan Cloudflare menyajikan salinan cache —
  `server_time` basi membuat estimasi jam meleset hingga ~800 ms, dan ticker/orderbook bisa basi hingga 30 dtk.
  Klien menambahkan parameter unik `_=<ns>` + header `Cache-Control: no-cache` di setiap request
  (hasil: `cf-cache-status: MISS`, data segar). Ticker yang lebih tua dari 60 dtk ditolak (tidak dipakai trading).

| Endpoint | Isi |
|---|---|
| `GET /api/server_time` | `{"timezone":"UTC","server_time": <ms>}` — dipakai untuk cek clock drift |
| `GET /api/pairs` | list info pair: `id`, `ticker_id`, `symbol`, `traded_currency`, `base_currency`, `price_round`, `price_precision`, `pricescale`, `quantity_increment`, `volume_precision`, `trade_min_base_currency` (min IDR), `trade_min_traded_currency` (min koin), `trade_fee_percent{,_maker,_taker}`, `is_maintenance`, `is_market_suspended` |
| `GET /api/price_increments` | `{"increments": {"btc_idr": "1000", ...}}` — **tick size harga** |
| `GET /api/summaries` | ticker semua pair + harga 24 jam & 7 hari lalu |
| `GET /api/ticker/{pair_id}` | `high, low, vol_<coin>, vol_idr, last, buy (best bid), sell (best ask), server_time` |
| `GET /api/ticker_all` | seperti ticker, untuk semua pair |
| `GET /api/trades/{pair_id}` | trade publik terbaru: `date, price, amount, tid, type` |
| `GET /api/depth/{pair_id}` | orderbook: `buy` (bid, desc) & `sell` (ask, asc) berupa `[price, amount_coin]` |
| `GET /tradingview/history_v2?from=&to=&symbol=&tf=` | OHLC. `from`/`to` unix **detik**. `tf` ∈ `1, 15, 30, 60, 240, 1D, 3D, 1W` |

Catatan OHLC:
- ⚠️ **Tidak ada timeframe 5 menit.** Untuk siklus 5 menit, gunakan candle 15m/1h/4h (multi-timeframe)
  + ticker/orderbook live. Jika perlu 5m, bisa di-resample dari candle 1m.
- ⚠️ **Terverifikasi di API sungguhan (2026-09-23, tidak ada di dokumentasi):** untuk timeframe intraday
  (`1`, `15`, `30`, `60`, `240`) server hanya mengembalikan **maksimal 7 hari yang berakhir di `to`**,
  berapa pun `from`-nya (mis. minta 50 hari 1h → dapat 169 candle terakhir). Riwayat lama tetap ada bila
  diminta per jendela ≤ 7 hari. Untuk `1D` batasnya **~730 candle (2 tahun)** yang berakhir di `to`.
  Riwayat 1D tersedia sejak 2015-09 (BTC), 2017-08 (ETH), 2021-11 (SOL); candle harian mulai 00:00 UTC. Klien memecah rentang: maks 1000 candle
  maks 6 hari per request intraday dan maks 700 candle untuk 1D, lalu menggabungkan hasil (dedupe by `Time`).
- ✅ `Volume` pada candle = volume **koin** (base asset): Σ volume 1h selama 24 jam ≈ `vol_btc` ticker,
  dan Σ volume×close ≈ `vol_idr`.
- ✅ Spread nyata (2026-09-23): btc_idr ~0,000%, eth_idr ~0,002%, sol_idr ~0,000% (1 tick); depth 150 level.
- Field `Volume` berupa string, `Open/High/Low/Close` berupa number.

### Presisi & minimum order
- **Tick harga**: pakai `/api/price_increments` (paling eksplisit). Field `price_precision`/`pricescale`
  di `/api/pairs` semantiknya tidak konsisten antar contoh (btc: `1000`, cat: `0.000001`) ⚠️ —
  dipakai hanya sebagai fallback.
- **Step quantity**: `quantity_increment` (contoh btc `"0.00000001"`, cat `1`).
- **Minimum order**: `trade_min_base_currency` (nilai IDR, contoh btc Rp 50.000 — pada contoh lain Rp 10.000)
  **dan** `trade_min_traded_currency` (jumlah koin). Order harus memenuhi keduanya.
- Pembulatan: harga beli dibulatkan **ke bawah** ke tick, harga jual **ke atas**; qty selalu **ke bawah**
  ke step (tidak pernah membeli/menjual lebih dari yang diizinkan risk manager).

### Fee (penting untuk model biaya)
- `/api/pairs` contoh: `trade_fee_percent_maker: 0.1`, `trade_fee_percent_taker: 0.2` (persen).
- Event fill di Private WS menunjukkan komponen tambahan per fill:
  `feeRate 0.002` + **`taxRate 0.0012`** (pajak) + **`clearingRate 0.000222`** (biaya kliring).
  → biaya efektif taker ≈ **0,342%**, maker ≈ 0,1% + pajak + kliring ≈ **0,242%** (estimasi).
- `myTrades.commission` = "trading fees **and any applicable taxes**".
- Model biaya agent: `fee_pair (dari /api/pairs) + tax_rate + clearing_rate` (tax & clearing dari config,
  default 0,12% dan 0,0222%). Round-trip taker ≈ 0,68% + spread + slippage. ❓ Mohon cek tier fee akun Anda.

---

## 4. Private REST legacy (`/tapi`) — HMAC-SHA512

- `POST https://indodax.com/tapi`, body `application/x-www-form-urlencoded`.
- Header: `Key: <api_key>`, `Sign: hex(HMAC_SHA512(secret, body))`.
- Wajib: `method` + (`nonce` **atau** `timestamp` [+ `recvWindow`]).
- ✅ Contoh dokumentasi diverifikasi: body
  `method=getInfo&timestamp=1578304294000&recvWindow=1578303937000` dengan secret contoh →
  `bab004e5a5…8cae99` (lihat `tests/test_signing.py`).
- Error: `{"success": 0, "error": "...", "error_code": "..."}`. Sukses: `{"success": 1, "return": {...}}`.
- Izin key: `view` (getInfo, transHistory, tradeHistory, openOrders, orderHistory, getOrder,
  getOrderByClientOrderId), `trade` (trade, cancelOrder, cancelByClientOrderId), `withdraw`
  (withdrawFee, withdrawCoin).
- ✅ **Deteksi izin withdraw pada key legacy** (terverifikasi 2026-09-23 dengan key legacy pemilik: v2 menolak
  dengan `-2015 Invalid TAPI version key`): agent memanggil `withdrawFee` (hanya info biaya, tidak memindahkan
  dana; butuh izin withdraw). Berhasil → key PUNYA izin withdraw → live ditolak. "No permission" → aman.
- **Cek izin withdraw (lama):** `getInfo.return.withdraw_status` (1 = user bisa withdraw) — ⚠️ ini status akun,
  bukan izin key. TAPIv2 `GET /api/v2/account` memberi `canWithdraw` (lebih relevan). Agent akan
  memanggil keduanya bila tersedia dan **menolak mode live bila ada indikasi izin withdraw**.
- Rate limit `trade`: **20 req/detik per akun per pair**; lewat batas → blok 5 detik, HTTP 429
  `error_code: too_many_requests`. `cancelOrder`: 30 req/detik.

### Timing (nonce/timestamp)
Server menolak jika `timestamp >= serverTime + 1000` **atau** `serverTime - timestamp > recvWindow`
(default 5000 ms). Artinya jam VPS **tidak boleh lebih cepat > 1 detik** dari server → wajib NTP/chrony.
Agent mengukur offset via `/api/server_time` saat startup & berkala, dan menolak jalan bila
|offset| > 500 ms (konfigurabel).

⚠️ Contoh dokumentasi mengisi `recvWindow=1578303937000` (angka seperti timestamp) — jelas salah ketik;
kita kirim `recvWindow` dalam ms (mis. `5000`).

### Method yang relevan
| method | Param utama | Catatan |
|---|---|---|
| `getInfo` | – | saldo `balance` & `balance_hold` (per koin, IDR int), `withdraw_status` |
| `trade` | `pair`, `type` (buy/sell), `price`, `<coin>` (qty koin) atau `idr`, `order_type` (limit/market), `client_order_id` (≤36, `[A-Za-z0-9_-]`), `time_in_force` (GTC/MOC), `smp_cancel` | **Limit buy wajib pakai qty koin** (param bernama koin, mis. `btc=0.001`) — `idr` + limit ditolak/berisiko under-fill. Market buy hanya dengan `idr`. |
| `openOrders` | `pair` (opsional) | bentuk respons beda bila `pair` diisi (list) vs tidak (dict per pair) |
| `getOrder` | `pair`, `order_id` | |
| `getOrderByClientOrderId` | `client_order_id` | **kunci idempotensi**: sebelum re-submit, cek order ini |
| `cancelOrder` | `pair`, `order_id`, `type` | |
| `cancelByClientOrderId` | `client_order_id` | |
| `tradeHistory`, `orderHistory` | – | ⚠️ **Sudah decommissioned sejak 7 Apr 2026** → pakai TAPIv2 `/api/v2/myTrades` & `/api/v2/order/histories` |

`client_order_id` duplikat ditolak: `"client order id ... already exists"` → ini dimanfaatkan sebagai
pelindung double-order (ID deterministik per proposal).

Order `MOC` (maker-or-cancel) ditolak bila akan langsung match: `"Order cancelled because it's not maker."`.

---

## 5. Trade API 2.0 (TAPIv2) — HMAC-SHA256

- Base: `https://api.indodax.com` ⚠️ (dok. STP: `https://tapi.indodax.com` / `https://tapi.btcapi.net`).
- Header: `Accept: application/json`, `X-APIKEY`, `Sign` (atau param `signature`).
  POST pakai `Content-Type: application/x-www-form-urlencoded`. GET/DELETE: param di query string.
- Signature: `hex(HMAC_SHA256(secret, query_string_or_body))`.
  ⚠️ **Contoh hash di dokumentasi v2 salah** — nilainya identik dengan contoh SHA512 legacy
  (panjang 128 hex, bukan 64). Hash SHA256 yang benar untuk contoh
  `symbol=btcidr&limit=100&timestamp=1578304294000&recvWindow=1578303937000` adalah
  `eeb688c782ef4cad36908b54ab4ff8fa259a44c40f7e573529f9d88c1070ae66` (dihitung sendiri). Perlu diuji ke
  server sebelum Fase 4.
- **Butuh API key khusus TAPIv2** (key TAPI lama tidak bisa dipakai). Key dibuat di
  `https://indodax.com/trade_api`. Izin Trade **wajib** whitelist IP.
- Error: `{"code": -1121, "msg": "Invalid symbol"}` dengan HTTP status (400/401/403/404/429/500).
  Kode penting: `-1021` timestamp di luar recvWindow, `-1022` signature salah, `-1003` rate limit,
  `-2010` order ditolak (saldo kurang / client id duplikat), `-2013` order tidak ada, `-1016` market suspended,
  `-2015` akses ditolak (IP tidak di-whitelist / tanpa izin).
- Rate limit per IP: order/cancel/openOrders/getOrder/account/myTrades/histories **300/menit**;
  create order juga **20/detik per pair**; cancel **30/detik**.

| Endpoint | Fungsi | Catatan |
|---|---|---|
| `POST /api/v2/order` | buat order | `symbol`, `side` BUY/SELL, `type` LIMIT/MARKET, `price`, `quantity` (base) / `quoteOrderQty` (market buy, IDR), `newClientOrderId`, `timeInForce` GTC/MOC, `selfTradePreventionMode` |
| `DELETE /api/v2/order` | cancel | `symbol` + `orderId` atau `origClientOrderId` |
| `GET /api/v2/openOrders` | order terbuka | `symbol` opsional |
| `GET /api/v2/order` | detail order | `orderId` (numerik) atau `origClientOrderId`. Order baru bisa **belum tersedia** sesaat setelah dibuat (`-2013`) → retry dengan jeda, jangan asumsikan gagal |
| `GET /api/v2/account` | saldo `free`/`locked`, `canTrade`, `canWithdraw` | |
| `GET /api/v2/order/histories` | riwayat order per `symbol` | rentang maks 7 hari, default 24 jam |
| `GET /api/v2/myTrades` | fill per `symbol` (`commission` termasuk pajak) | rentang maks 7 hari; `orderId` di sini = format penuh `btcidr-limit-123` |

Status order: `NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`
(Private WS memakai `FILL` untuk event fill).

---

## 6. Deadman Switch — `POST /tapi/countdownCancelAll`

- Base: `https://indodax.com/tapi` (alternatif `https://btcapi.net/tapi`; demo `https://demo-indodax.com/tapi`).
- Header `Key`, `Sign` (HMAC-SHA512), `Content-Type: text/plain`.
- Param: `pair` (wajib, boleh `btc_idr,eth_idr`), `countdownTime` ms (default 5000, `0` = matikan timer),
  `timestamp` + `recvWindow` atau `nonce`.
- Semua order terbuka di pair tersebut dibatalkan bila tidak ada heartbeat dalam `countdownTime`.
  Rekomendasi resmi: heartbeat tiap 30 dtk dengan countdown 120 dtk.
- Rate limit: **10 request / 10 detik per IP**.
- ✅ Dok. menulis "Use your `Key` as the key" — **salah tulis**: contoh hash hanya cocok bila memakai
  **secret key** (sudah diverifikasi, `b4f03574…` untuk body, `29ff8937…` untuk query string URL-encoded).
- Respons HTTP 200 baik sukses maupun gagal → harus cek field `success`.
- Rencana agent: heartbeat tiap 30 dtk, `countdownTime=120000`, untuk semua pair whitelist yang punya
  order terbuka. Gagal heartbeat berturut-turut → alert + stop entry baru.

---

## 7. Market Data WebSocket

- `wss://ws3.indodax.com/ws/`, protokol Centrifugo JSON.
- Autentikasi: `{"params":{"token":"<static token publik di dok>"},"id":1}`.
- Ping: `{"method":7,"id":n}`. Subscribe: `method: 1`, unsubscribe: `method: 2`.
- Channel: `chart:tick-<pair_id>`, `market:summary-24h`, `market:trade-activity-<pair_id>`,
  `market:order-book-<pair_id>`.
- Recovery: subscribe dengan `recover: true, offset: <terakhir>` setelah reconnect.
- Server bisa memutus koneksi karena rebalancing → wajib auto-reconnect.
- Rencana: Fase 1–3 cukup REST (siklus 5 menit). WS dipertimbangkan di Fase 4/5 untuk harga real-time
  saat monitoring stop-loss.

## 8. Private WebSocket

- Token: `POST https://indodax.com/api/private_ws/v1/generate_token` body `client=tapi&tapi_key=<key>`,
  header `Sign` HMAC-SHA512 dari body. Respons: `connToken`, `channel` (`pws:#...`).
- Connect `wss://pws.indodax.com/ws/?cf_ws_frame_ping_pong=true`,
  kirim `{"connect":{"token":"..."},"id":1}`, lalu `{"subscribe":{"channel":"pws:#..."},"id":2}`.
- Event `order_update`: `orderId`, `clientOrderId`, `status` (NEW/FILL/CANCELLED/REJECTED),
  `executedQty`, `unfilledQty`, `fillInformation` (`participant`, `fee`, `tax`, `clearing`, ...).
- Token kedaluwarsa (`code 109`) → generate ulang.
- Rencana: dipakai di Fase 5 untuk konfirmasi fill cepat; REST polling tetap jadi sumber kebenaran.

## 9. Self-Trade Prevention
- Berlaku sejak 14 Jul 2026. Legacy param `smp_cancel` (`MAKER`/`TAKER`/`BOTH`, default MAKER);
  v2 `selfTradePreventionMode` (`EXPIRE_MAKER` default). Agent tidak akan pernah punya order buy & sell
  yang saling silang di pair sama (risk manager), jadi default cukup.

---

## 9b. Diverifikasi di akun asli (order minimum, 2026-09-23)

- Minimum order dicek **di harga limit order itu sendiri**: `qty × price ≥ Rp 10.000`
  (limit di 90% bid → "Minimum order is 0.00000735 BTC").
- `trade` (buy, limit, tidak terisi) → `{"receive_btc":0,"spend_rp":0,"fee":0,"remain_rp":10534,
  "order_id":268678770,"remain":10534,"client_order_id":"..."}`.
- `getOrderByClientOrderId` (buy) → `order_id, client_order_id, price, type, submit_time, finish_time,
  status ("open"), fee, order_rp, remain_rp, receive_btc`. Order buy berdenominasi IDR, dan
  `order_rp` **termasuk cadangan fee** (nilai 10.510,92 → order_rp 10.534, ≈ +0,22%), jadi qty dari
  `order_rp/price` sedikit kebesaran. Fill buy diambil dari `receive_btc`.
- Entri `openOrders` **tidak punya field `status`**, qty buy di `order_idr`/`remain_idr` → dianggap open.
- Cancel buy: getOrder → `status "cancelled"`, `remain_rp` tetap = sisa yang tidak terisi.
- Buy marketable yang terisi penuh (Tes B): respons `trade` melaporkan **0 terisi**
  (`receive_btc 0, spend_rp 0`); getOrder → `status "filled"`, `remain_rp "0"`, `refund_idr "40"`,
  **`receive_btc 0` dan `fee 0`**. Saldo BTC bertambah **tepat** qty yang diorder (0,00000768),
  sedangkan `order_rp/price` = 0,0000076964 (kebesaran 0,2%). → fill buy dibukukan sebagai
  qty yang dikirim × fraksi terisi (`OrderState.filled_for`), dibulatkan ke bawah.
- Sell terisi: `trade` juga melaporkan 0 terisi; getOrder → `order_btc`, `remain_btc`, `sold_btc`
  (benar), `receive_idr 0`, `fee 0`. → fee & hasil IDR tidak dilaporkan: agent memakai estimasi
  konservatif (harga limit, fee taker+pajak+kliring); saldo IDR asli dicek oleh rekonsiliasi.

## 10. Pertanyaan / ambiguitas untuk Anda ❓

1. **Legacy `/tapi` (SHA512) vs TAPIv2 (SHA256)?** Brief Anda menyebut HMAC-SHA512 (= legacy).
   Legacy masih melayani `trade`/`cancelOrder`/`getOrderByClientOrderId` dan **Deadman Switch hanya ada
   di legacy**, tetapi `tradeHistory`/`orderHistory` legacy sudah dimatikan dan dok. menyatakan legacy
   akan di-decommission. Usulan saya: **order & deadman via legacy `/tapi`**, **riwayat trade/order & saldo
   via TAPIv2**, keduanya di balik satu interface sehingga mudah pindah penuh ke v2 nanti.
   Konsekuensinya Anda mungkin perlu **dua key** (TAPI lama + TAPIv2) — atau cek apakah key TAPIv2 Anda
   diterima oleh `/tapi`. Mohon konfirmasi key mana yang akan Anda buat.
2. **Base URL TAPIv2**: `api.indodax.com` (dok. utama) vs `tapi.indodax.com` (dok. STP). Saya set di config,
   default `https://tapi.indodax.com`? atau `api.`? — akan diuji dengan smoke test read-only di VPS.
3. **Tier fee akun** Anda (maker/taker) — agar model biaya akurat. Default: maker 0,1%, taker 0,2%,
   + pajak 0,12% + kliring 0,0222%.
4. **Nilai placeholder** di brief: modal maksimum agent (Rp ...) dan volume 24 jam minimum (Rp X).
   Default sementara di `config/settings.yaml`: modal Rp 1.000.000, volume min Rp 1 miliar.
5. Sandbox ini **tidak bisa mengakses indodax.com** (diblokir network policy lingkungan cloud), sehingga
   test Fase 1 memakai respons contoh dari dokumentasi (mock). Jalankan `python -m scripts.smoke_public`
   di VPS untuk validasi terhadap API sungguhan. Untuk backtest Fase 3 saya butuh data OHLC historis:
   bisa (a) Anda jalankan script downloader di VPS lalu commit/upload CSV-nya, atau (b) izinkan domain
   `indodax.com` di network policy environment ini.
