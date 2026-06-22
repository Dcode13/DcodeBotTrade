# Bot Trading Otomatis BTCUSD (MT5 + Telegram)

Bot scalping **multi-timeframe** (M15 → M5 → M1) untuk **MetaTrader 5**, fokus
satu simbol **BTCUSD**, dikontrol & dipantau via **Telegram**, dengan pengaman
(circuit breaker + gerbang eksekusi) yang tertanam dan **tidak bisa di-bypass**.

> Default mode = **ALERT-ONLY** (aman): bot menghitung & mengirim sinyal lengkap
> ke Telegram **tanpa** mengirim order. Eksekusi uang asli butuh dua langkah
> sengaja: `EXECUTE=true` di `.env` **DAN** `/confirm_live` di Telegram.

---

## ⚠️ DISCLAIMER RISIKO (WAJIB DIBACA)

- Bot ini **BUKAN jaminan profit**. Scalping BTC M1 dengan modal kecil punya
  **probabilitas tinggi kehilangan modal** — spread & slippage mendominasi, dan
  candle M1 sangat *noisy*.
- Klaim return ekstrem (mis. "ribuan persen") adalah cherry-picking /
  survivorship bias, **bukan** ekspektasi.
- Ini **bukan nasihat finansial berlisensi**.
- Pengaman di bot ini membatasi kerugian akibat **bug kode**, **bukan** menjamin
  profit. Risiko terbesar pada uang asli adalah bug (salah arah/lot/loop) —
  karena itu **jalankan mode ALERT-ONLY dulu beberapa hari** untuk memastikan
  logika & eksekusi waras sebelum menyalakan live.

---

## 1. Kendala Lingkungan (WAJIB dipahami)

1. **Paket `MetaTrader5` HANYA jalan di Windows.** Tidak ada build resmi
   Linux/Mac. Untuk operasi 24/7 → gunakan **Windows VPS**.
2. **Terminal MT5 harus terinstall & login** ke akun yang sama (Juno Markets)
   di mesin yang sama. Paket Python menempel ke terminal yang sedang berjalan.
3. **Nama simbol BTCUSD TIDAK di-hardcode.** Bot mencocokkan pola `BTC.*USD`,
   memilih yang tradable, lalu me-log nama persisnya.
4. **Akun Cent:** balance/equity tampil dalam **sen**. Verifikasi via `/balance`
   bahwa angka masuk akal (mis. modal $20 ≈ 2000 sen).
5. **Spesifikasi kontrak diturunkan dari API** (`symbol_info`), bukan diasumsikan.

---

## 2. Instalasi

### a. Install MetaTrader 5 + login
1. Install terminal MT5 dari broker (Juno Markets).
2. Login ke akun **STP Cent real** kamu di terminal.
3. Di MT5: **Tools → Options → Expert Advisors** → centang *Allow Algo Trading*.
4. Biarkan terminal **tetap berjalan & login**.

### b. Install Python + dependencies (Windows)
```powershell
python --version          # butuh 3.11+
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### c. Isi konfigurasi rahasia
```powershell
copy .env.example .env
notepad .env
```
Isi field berikut:

| Variabel | Keterangan |
|---|---|
| `MT5_LOGIN` | nomor login akun MT5 |
| `MT5_PASSWORD` | password akun |
| `MT5_SERVER` | nama server broker (mis. `JunoMarkets-Live`) |
| `MT5_PATH` | (opsional) path `terminal64.exe`; kosongkan jika MT5 sudah jalan |
| `TELEGRAM_BOT_TOKEN` | token dari @BotFather |
| `OWNER_CHAT_ID` | chat ID kamu (bot hanya merespons ID ini) |
| `NEWS_API_KEY` | (opsional) untuk kalender berita |
| `EXECUTE` | `false` = alert-only (default), `true` = izinkan eksekusi |

> Mendapatkan `OWNER_CHAT_ID`: kirim pesan ke bot, lalu buka
> `https://api.telegram.org/bot<TOKEN>/getUpdates` dan lihat `chat.id`.

---

## 3. Menjalankan

```powershell
.venv\Scripts\activate
python main.py
```
Opsi: `python main.py --config path\config.yaml --env path\.env`

Saat start, bot akan:
1. Connect MT5 → discovery simbol BTCUSD → log nama & spesifikasi kontrak.
2. Reconcile posisi (sinkron dengan posisi nyata).
3. Masuk loop, mengirim **heartbeat** & alert ke Telegram.

Hentikan dengan `Ctrl+C` (atau `/stop` di Telegram untuk kill switch eksekusi).

---

## 4. Perintah Telegram

| Perintah | Fungsi |
|---|---|
| `/start`, `/help` | daftar perintah |
| `/status` | mode (LIVE/ALERT-ONLY), bias, equity, DD harian, loss beruntun, trade harian, paused |
| `/positions` | posisi terbuka (entry, SL, TP, floating P/L) |
| `/balance` | balance & equity (penjelasan satuan cent) |
| `/risk` | parameter risiko aktif |
| `/set_risk <pct>` | ubah risk per trade (mis. `/set_risk 1` = 1%); WARNING jika > 5% |
| `/pause`, `/resume` | hentikan/lanjut entry baru (resume mereset loss beruntun) |
| `/stop` | kill switch: matikan eksekusi + pause |
| `/confirm_live` | aktifkan eksekusi uang asli (hanya jika `EXECUTE=true`) |
| `/disable_exec` | kembali ke alert-only |
| `/report` | ringkasan performa dari journal |

### Cara menyalakan LIVE (saat sudah yakin)
1. Set `EXECUTE=true` di `.env`, restart bot.
2. Kirim `/confirm_live` di Telegram.
3. `/status` harus menampilkan `mode: LIVE`.
4. Awasi run pertama dengan `/stop` siap.

---

## 5. Backtester

```powershell
# Tarik data langsung dari MT5 (Windows):
python -m backtest.backtester --days 60 --spread 100 --slippage 20 --out equity.csv

# Atau dari CSV (kolom: time,open,high,low,close) di folder berisi M1.csv/M5.csv/M15.csv:
python -m backtest.backtester --csv-dir data\hist --spread 100 --slippage 20
```
Output: jumlah trade, win rate, average R, **profit factor**, **max drawdown**,
equity curve.

> **Jujur:** hasil backtest M1 sering jauh lebih bagus daripada live.
> Perlakukan dengan skeptis. Acceptance saran (bukan jaminan): profit factor
> > 1.2 & drawdown terkontrol.

---

## 6. ✅ DAFTAR YANG WAJIB DIVERIFIKASI

Sebelum live, verifikasi ini terhadap broker/akun kamu:

- [ ] **Nama simbol** persis (lihat log saat start / `/status`) — pastikan benar
      `BTCUSD` versi broker, bukan instrumen lain yang cocok pola.
- [ ] **Spesifikasi kontrak** (`trade_contract_size`, `trade_tick_size`,
      `trade_tick_value`, `volume_min/max/step`, `point`, `digits`,
      `trade_stops_level`) — tercetak di log saat start.
- [ ] **Spread** khas BTCUSD broker → sesuaikan `max_spread_points` di
      `config.yaml` (default 250 mungkin tidak cocok).
- [ ] **Filling mode** diterima broker (bot mendeduksi FOK/IOC/RETURN; jika
      order ditolak `INVALID_FILL`, periksa log).
- [ ] **Satuan cent** masuk akal via `/balance`.
- [ ] **Kasus min-lot:** pada modal $20 + BTC, sering `lot minimum > target
      risiko` → bot **SKIP** (itu jujur, bukan bug). Ubah `allow_min_lot_override`
      hanya bila paham risikonya.

---

## 7. Penjelasan Parameter (`config/config.yaml`)

### `strategy`
| Param | Default | Arti |
|---|---|---|
| `ema_fast` / `ema_slow` | 50 / 200 | EMA penentu bias tren di M15 |
| `atr_period` | 14 | periode ATR (semua TF) |
| `swing_lookback` | 60 | jumlah candle M5 untuk cari swing |
| `swing_pivot_n` | 3 | lebar fractal (bar = ekstrem di `[i-n, i+n]`) |
| `zone_proximity_atr_m5` | 0.6 | jarak maksimum harga ke zona (× ATR M5) |
| `min_body_ratio` | 0.5 | rasio body/range minimum candle trigger M1 |
| `rsi_period` | 14 | periode RSI |
| `rsi_filter` | true | aktifkan filter RSI di trigger |
| `rsi_overbought` / `rsi_oversold` | 75 / 25 | tolak BUY/SELL di ekstrem RSI |
| `sl_buffer_atr_m1` | 0.5 | buffer SL di luar ekstrem (× ATR M1) |
| `rr_ratio` | 1.5 | risk:reward (TP = entry ± RR × jarak SL) |
| `sl_min_atr_m1` / `sl_max_atr_m1` | 0.5 / 3.0 | clamp jarak SL (× ATR M1) |

### `risk`
| Param | Default | Arti |
|---|---|---|
| `risk_per_trade` | 0.01 | **1%** per trade. JANGAN naikkan ke 0.2–0.3. |
| `risk_warn_threshold` | 0.05 | > nilai ini → WARNING wajib tampil |
| `max_daily_loss_pct` | 0.05 | DD harian ≥ 5% → stop entry sampai hari berganti |
| `max_consecutive_losses` | 3 | loss beruntun → auto-pause, butuh `/resume` |
| `max_open_positions` | 1 | tanpa stacking/averaging |
| `max_trades_per_day` | 8 | batas overtrading |
| `max_spread_points` | 250 | **verifikasi vs broker** |
| `allow_min_lot_override` | false | true = boleh entry walau lot min > target |
| `deviation` | 50 | toleransi slippage saat order |

### `management`
| Param | Default | Arti |
|---|---|---|
| `break_even` | true | geser SL ke entry setelah profit `break_even_trigger_r` |
| `break_even_trigger_r` | 1.0 | trigger break-even pada R ini |
| `trailing_stop` | false | trailing berbasis ATR(M1) |
| `trailing_atr_mult` | 1.5 | jarak trailing (× ATR M1) bila aktif |

### `fundamentals`
| Param | Default | Arti |
|---|---|---|
| `enabled` | true | aktifkan filter berita/sentimen |
| `no_trade_window_minutes` | 30 | blackout sebelum/sesudah event high-impact |
| `fail_mode` | skip | `skip` = jangan entry saat API ragu; `continue` = lanjut + log |
| `fear_greed_filter` | false | skip saat sentimen ekstrem (opsional) |
| `calendar_url` | "" | endpoint JSON kalender ekonomi (kosong = nonaktif) |

### `loop`
| Param | Default | Arti |
|---|---|---|
| `loop_sleep_sec` | 3 | jeda loop utama |
| `candles` | 300 | jumlah candle ditarik per TF |
| `heartbeat_minutes` | 60 | interval heartbeat |

---

## 8. Struktur Proyek

```
.
├── config/config.yaml        # parameter strategi & risiko (non-secret)
├── core/
│   ├── config.py             # loader yaml + env -> dataclass
│   ├── mt5_client.py         # connect, reconnect, symbol discovery
│   ├── market_data.py        # tarik candle M15/M5/M1
│   ├── indicators.py         # EMA, ATR, RSI (pure)
│   ├── strategy.py           # sinyal multi-timeframe (pure)
│   ├── risk_manager.py       # sizing, circuit breaker, validasi (pure)
│   ├── fundamentals.py       # filter berita/sentimen
│   ├── executor.py           # kirim/modify/close order
│   ├── position_manager.py   # break-even, trailing, reconcile
│   └── journal.py            # SQLite journal & state
├── telegram/
│   ├── bot.py                # polling perintah + auth
│   └── notifier.py           # alert real-time
├── backtest/backtester.py    # uji historis (terpisah dari live)
├── tests/                    # unit test (sizing, swing, trigger, breaker, dst.)
├── main.py                   # orchestrator / loop utama
├── requirements.txt
├── .env.example
└── README.md
```

---

## 9. Test

```powershell
python -m pytest -q
```
Mencakup: position sizing (+ kasus min-lot), deteksi swing & pemilihan zona,
confirmation candle M1, bias M15, circuit breaker, validasi stops, indikator,
dan smoke test backtester.

---

## 10. Anti-Pattern (DILARANG di bot ini)

- ❌ Martingale / averaging down.
- ❌ Balik arah otomatis "balas dendam" setelah SL (sinyal baru tetap harus lolos
  aturan §7 secara independen & tunduk `max_consecutive_losses`).
- ❌ `risk_per_trade > 5%` tanpa WARNING.
- ❌ Kirim order live tanpa `EXECUTE=true` + `/confirm_live`.
- ❌ Hardcode nama simbol / spesifikasi kontrak / secret.
- ❌ Lebih dari 1 posisi / stacking.

---

*Catatan penutup: ini perkakas teknis, bukan nasihat finansial. Trading BTC M1
dengan modal sangat kecil berpotensi besar rugi. Bangun rapi, jalankan
alert-only dulu, dan awasi run pertama dengan kill switch siap.*
