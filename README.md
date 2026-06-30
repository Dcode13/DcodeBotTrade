# Bot Trading Otomatis XAUUSD (acuan LBMA + CRT) — MT5 + Telegram

Bot trading **emas (XAUUSD)** untuk **MetaTrader 5**, memakai **harga benchmark
LBMA Gold (AM & PM)** sebagai acuan fundamental level entry, dikonfirmasi oleh
**analisis teknikal CRT** (MSS H1 → Golden Zone → OB/FVG → CHoCH M15, port dari
EA *GridScalper_CRT*). Dikontrol & dipantau via **Telegram**, dengan pengaman
(circuit breaker + gerbang eksekusi) yang tertanam dan **tidak bisa di-bypass**.

> **Mode strategi** (`strategy_mode` di `config.yaml`):
> - `combo` (**default, emas**) — entry **momentum (strategi cent)** EMA/swing/RSI
>   (M15→M5→M1) sebagai generator utama, **tanpa wajib LBMA**; ditambah jalur LBMA
>   touch & CRT/Fibonacci. **LBMA & Fibonacci jadi acuan/konteks**, bukan syarat entry.
> - `lbma` — hanya jalur LBMA touch + CRT/Fibonacci (butuh data LBMA).
> - `legacy` — strategi scalping EMA/swing/RSI murni (tanpa LBMA/CRT/Fib).

> Default mode eksekusi = **ALERT-ONLY** (aman): bot menghitung & mengirim sinyal
> lengkap ke Telegram **tanpa** mengirim order. Eksekusi uang asli butuh dua
> langkah sengaja: `EXECUTE=true` di `.env` **DAN** `/confirm_live` di Telegram.

## 📌 Strategi LBMA (mode `lbma`)

Acuan = harga **LBMA Gold** (USD/oz) dari feed JSON resmi LBMA, riwayat **6 bulan**
di-cache ke `data/lbma_history.json` (refresh otomatis saat start & `/confirm_live`).

**Pemilihan level acuan harian:**
- LBMA **AM > PM** → level acuan = **PM**.
- LBMA **PM > AM** → level acuan = **AM** (SL **50 pips**).

**Arah entry (fade ke level):**
- Harga XAUUSD terkini **di bawah** level → tunggu harga **naik** menyentuh level → **SELL**.
- Harga XAUUSD terkini **di atas** level → tunggu harga **turun** menyentuh level → **BUY**.

**Filter konsolidasi:** bila harga-harga LBMA pada **2 hari sebelumnya berdekatan
(rentang ≤ ~300 pips / $30)** → pasar dianggap konsolidasi → **tidak entry**.

**Konfirmasi CRT:** arah entry dicek terhadap bias H1 (MSS) & CHoCH M15. Default
hanya **dilaporkan** di alert (`crt.require_confirmation=false`); set `true` agar
sinyal yang berlawanan arah CRT diblokir.

## 📊 Analisis fundamental-teknikal LBMA (spreadsheet "HARGA LBMA HARIAN")

Lapisan **konteks tambahan** dari pola fixing harian LBMA — port dari spreadsheet
analisis user. **AM = pembukaan sesi London, PM = penutupan sesi London.** Modul
`core/lbma_fundamental.py` menurunkan (lihat `/lbma_fund` atau `/fund`):

- **Metrik harian:** `DELTA` (PM−AM), `%` (DELTA/PM, skala spreadsheet), `STATUS`
  (NAIK bila PM>AM, TURUN bila PM<AM), `RASIO` (PM/AM×100), dan **grid akumulasi**
  (AM − 150/300/400) sebagai zona average-down.
- **Fibonacci AM & PM** (retracement high→low) dari jendela `fib_window_days` hari
  (default 22 hari berjalan ≈ 1 bulan; sesuaikan agar pas month-to-date sumber).
- **BIAS multi-hari** sesuai tabel interpretasi:

  | Kondisi | Interpretasi |
  |---|---|
  | PM > AM ≥ `bullish_streak_days` hari | bias bullish jangka pendek |
  | …dan PM terus *higher-high* | akumulasi / buying lebih kuat |
  | …tapi PM gagal *higher-high* | waspada **distribusi terselubung** |
  | PM < AM ≥ `bearish_streak_days` hari | tekanan jual sesi London |

Default **`lbma_fund.require_confirmation=false`** → bias ini hanya **konteks**:
tampil di alert (`[fund] ...`) & `/status`, **tidak memblok entry**. Set `true`
agar sinyal yang berlawanan arah bias fundamental dibuang. Atur di
`config.yaml` blok `lbma_fund`.

### Jalur entry (mode `combo`, default)
Semua jalur tunduk gerbang yang sama (circuit breaker, filter berita, spread).
Dicek berurutan; sinyal pertama yang valid dieksekusi. Maksimum tetap **1 posisi**.

0. **Support/Resistance M5/M15/H1 (PRIORITAS) — entry di M5.** Deteksi zona S/R dari
   swing tiap TF, gabung yang berdekatan (confluence). Harga di **support** + candle
   M5 **bullish** → BUY; di **resistance** + candle M5 **bearish** → SELL. Lihat
   `/sr`. Toggle: `sr.enabled`.
1. **MTF alignment / momentum (strategi cent) — tanpa perlu LBMA.** EMA bias (M15) → zona
   swing (M5) → momentum candle + RSI (M1). Sama seperti yang dipakai di akun cent.
   SL/TP berbasis ATR M1 (`config.strategy`).
2. **LBMA touch (fade).** Entry saat harga menyentuh level acuan LBMA (aturan di
   atas). Toggle: `lbma.enable_touch_entry`. Hanya jalur ini yang dipengaruhi
   filter konsolidasi LBMA 2-hari.
3. **CRT + Fibonacci ("market bagus").** Harga retrace ke **golden zone Fibonacci
   (0.5–0.786)** searah bias CRT + **CHoCH M15** konfirmasi → entry searah tren.
   Toggle: `crt.enable_trend_entry` + `fib.enabled`.

Pada jalur momentum, **LBMA & Fibonacci hanya ditempel sebagai acuan** di alert
(`[acuan] ...`), tidak menghalangi entry. Level Fibonacci kapan saja via `/fib`.

### Auto-set marker LBMA saat `/confirm_live`
Saat kamu kirim `/confirm_live`, bot otomatis refresh LBMA lalu **men-set marker
AM LBMA & PM LBMA** (ditampilkan di balasan + tersimpan, juga muncul di `/status`).

> **Satuan "pip" emas**: default 1 pip = `0.1` (= $0.10). Jadi SL 50 pips = **$5.0**
> dan ambang konsolidasi 300 pips = **$30.0**. Ubah `lbma.pip_size` bila konvensi
> brokermu berbeda.

> ⚠️ **Catatan risiko modal kecil ($16, akun STP):** lot minimum emas (0.01 ≈
> $1 per pergerakan $1) dengan SL $5 berarti risiko ~$5 (≈31% equity) per trade.
> Itu jauh di atas target risk %. Bot akan **SKIP** kecuali
> `risk.allow_min_lot_override=true` (default ON di config emas) — pahami bahwa ini
> memaksa entry dengan risiko di atas target. Pertimbangkan menambah modal atau
> memperkecil `lbma.sl_pips`.

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
| `MT5_PATH` | (opsional) path `terminal64.exe`; kosongkan jika MT5 sudah jalan |
| `TELEGRAM_BOT_TOKEN` | token dari @BotFather |
| `OWNER_CHAT_ID` | chat ID kamu (bot hanya merespons ID ini) |
| `NEWS_API_KEY` | (opsional) untuk kalender berita |
| `EXECUTE` | `false` = alert-only (default), `true` = izinkan eksekusi |

> **Login akun MT5 lewat Telegram.** Nomor login, password, dan server **tidak**
> lagi diisi di `.env`. Bot selalu mulai TANPA akun — kirim `/login` di Telegram
> lalu masukkan nomor login → password → server. `/logout` untuk keluar akun.

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
1. Mulai **TANPA akun** (tidak auto-login) → kirim pesan sambutan & minta `/login`.
2. Daftarkan menu perintah ke Telegram (muncul saat ketik `/`).
3. Masuk loop, melayani perintah Telegram.

Setelah `/login` (nomor login → password → server) berhasil, bot connect MT5 →
discovery simbol XAUUSD → reconcile posisi → mulai kirim **heartbeat** & alert.
Mode `lbma`: data acuan LBMA 6 bulan di-refresh & di-cache `data/lbma_history.json`.

### Ekspor LBMA ke Excel (opsional, terpisah dari bot)
```powershell
python lbma_gold_scraper.py --months 6 --output lbma_gold_6bulan.xlsx --csv
```

Hentikan dengan `Ctrl+C` (atau `/stop` di Telegram untuk kill switch eksekusi).

---

## 4. Perintah Telegram

| Perintah | Fungsi |
|---|---|
| `/start`, `/help` | sambutan & daftar perintah |
| `/login` | masuk akun MT5 (nomor login → password → server) |
| `/logout` | keluar akun MT5 yang sedang login |
| `/lbma` | acuan LBMA hari ini (AM/PM + level + status) & riwayat 10 hari + ringkasan bulanan. `/lbma YYYY-MM-DD` (per hari) atau `/lbma YYYY-MM` (per bulan) |
| `/lbma_fund` (`/fund`) | **analisis fundamental-teknikal LBMA**: bias multi-hari PM vs AM, metrik harian (DELTA/STATUS/RASIO), grid akumulasi, level Fibonacci AM & PM |
| `/fib` | level Fibonacci terkini (leg, golden zone, retracement & extension, posisi harga) |
| `/sr` | peta Support/Resistance M5/M15/H1 (level, TF confluence, kekuatan) |
| `/status` | mode (LIVE/ALERT-ONLY), acuan LBMA + marker AM/PM + bias CRT, equity, DD harian, loss beruntun, trade harian, paused |
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
| `entries_per_signal` | 1 | jumlah posisi dibuka **sekaligus** per sinyal (mis. `2`). Risiko dibagi rata antar entry; di akun mikro yang sudah lot-minimum, eksposur jadi ~n× target. |
| `max_trades_per_day` | 8 | batas overtrading |
| `max_spread_points` | 250 | **verifikasi vs broker** |
| `allow_min_lot_override` | false | true = boleh entry walau lot min > target |
| `deviation` | 50 | toleransi slippage saat order |

### `management`
| Param | Default | Arti |
|---|---|---|
| `break_even` | true | aktifkan auto-SL ke profit |
| `break_even_trigger_r` | 0.8 | saat profit ≥ R ini → SL dipindah ke profit |
| `breakeven_plus_pips` | 10 | **SL PLUS**: kunci profit sekian pips saat trigger (0 = breakeven murni) |
| `trailing_stop` | true | SL otomatis ikut harga (ATR) → profit terkunci makin besar |
| `trailing_atr_mult` | 1.5 | jarak trailing (× ATR entry TF) |
| `auto_tp` | true | TP otomatis (RR) selalu dipasang saat order |
| `entry_tp_rrs` | `[1.0, 1.5]` | RR (TP) per entry saat `entries_per_signal > 1`. Entry-1 TP rapat (cepat WIN), entry-2 lebih jauh. Kurang dari jumlah entry → entry sisa pakai nilai terakhir. |

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
│   ├── market_data.py        # tarik candle (H1/M15/M5 atau M15/M5/M1)
│   ├── indicators.py         # EMA, ATR, RSI (pure)
│   ├── lbma.py               # data LBMA AM/PM (cache 6 bln) + logika entry (pure)
│   ├── lbma_fundamental.py   # analisis fundamental LBMA: bias PM vs AM, fib AM/PM, grid (pure)
│   ├── crt_analysis.py       # port CRT: MSS/GoldenZone/OB-FVG/CHoCH (pure)
│   ├── fibonacci.py          # leg swing + retracement/extension + golden zone (pure)
│   ├── strategy.py           # sinyal multi-timeframe legacy (pure)
│   ├── risk_manager.py       # sizing, circuit breaker, validasi (pure)
│   ├── fundamentals.py       # filter berita/sentimen
│   ├── executor.py           # kirim/modify/close order
│   ├── position_manager.py   # break-even, trailing, reconcile
│   └── journal.py            # SQLite journal & state
├── telegram/
│   ├── bot.py                # polling perintah + auth
│   └── notifier.py           # alert real-time
├── backtest/backtester.py    # uji historis (terpisah dari live)
├── tests/                    # unit test (sizing, swing, trigger, breaker, LBMA, dst.)
├── lbma_gold_scraper.py      # CLI ekspor LBMA AM/PM ke Excel/CSV (mandiri)
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
