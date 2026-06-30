"""Pemuatan & validasi konfigurasi.

Menggabungkan ``config/config.yaml`` (parameter strategi/risiko) dengan
secret dari environment (``.env``). Mengembalikan dataclass bertipe agar
sisa kode tidak perlu menebak struktur dict.

Modul ini SENGAJA tidak meng-import MetaTrader5 supaya bisa dipakai di
unit test & backtester tanpa terminal MT5.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


# --------------------------------------------------------------------------- #
# Dataclass parameter (non-secret)
# --------------------------------------------------------------------------- #
@dataclass
class StrategyConfig:
    ema_fast: int = 50
    ema_slow: int = 200
    atr_period: int = 14
    swing_lookback: int = 60
    swing_pivot_n: int = 3
    zone_proximity_atr_m5: float = 0.6
    min_body_ratio: float = 0.5
    rsi_period: int = 14
    rsi_filter: bool = True
    rsi_overbought: float = 75.0
    rsi_oversold: float = 25.0
    sl_buffer_atr_m1: float = 0.5
    rr_ratio: float = 1.5
    sl_min_atr_m1: float = 0.5
    sl_max_atr_m1: float = 3.0


@dataclass
class RiskConfig:
    position_sizing_mode: str = "risk"  # "risk" (dari risk_per_trade) | "fixed" (lot tetap)
    fixed_lot: float = 0.1              # dipakai bila position_sizing_mode = "fixed"
    risk_per_trade: float = 0.01
    risk_warn_threshold: float = 0.05
    max_daily_loss_pct: float = 1.00
    max_consecutive_losses: int = 3
    max_open_positions: int = 1
    entries_per_signal: int = 1   # jumlah posisi dibuka SEKALIGUS per sinyal (mis. 2)
    max_trades_per_day: int = 50
    max_spread_points: float = 250.0
    allow_min_lot_override: bool = False
    deviation: int = 50


@dataclass
class ManagementConfig:
    break_even: bool = True
    break_even_trigger_r: float = 0.8     # pindahkan SL ke profit saat profit >= R ini
    breakeven_plus_pips: float = 10.0     # SL PLUS: kunci profit sekian pips (0 = breakeven murni)
    trailing_stop: bool = True            # SL otomatis ikut harga (trailing) -> kunci profit makin besar
    trailing_atr_mult: float = 1.5
    auto_tp: bool = True                  # TP otomatis (RR) selalu dipasang saat order
    # RR (TP) per entry saat entries_per_signal > 1. Entry ke-i pakai
    # entry_tp_rrs[i] (TP = entry +/- rr * jarak SL). Default: entry pertama TP
    # rapat (cepat tercapai -> win-rate naik), entry kedua sedikit lebih jauh.
    entry_tp_rrs: list[float] = field(default_factory=lambda: [1.0, 1.5])


@dataclass
class LBMAConfig:
    """Parameter strategi & data acuan LBMA Gold (XAUUSD).

    Satuan "pip" emas default: 1 pip = 0.1 (10 point pada quote 2 desimal).
    Jadi sl_pips=50 -> SL $5.0 dan proximity_pips=300 -> $30.0.
    """

    enabled: bool = True
    enable_touch_entry: bool = True  # entry fade saat harga menyentuh level LBMA
    pip_size: float = 0.1          # nilai 1 "pip" emas (harga). 0.1 = $0.10
    sl_pips: float = 50.0          # SL untuk entry LBMA (aturan 1b: 50 pips)
    rr_ratio: float = 2.0          # TP = entry +/- RR x jarak SL
    proximity_pips: float = 300.0  # aturan 2: ambang konsolidasi
    proximity_days: int = 2        # aturan 2: jumlah hari LBMA sebelum ref
    entry_tolerance_pips: float = 20.0  # toleransi "menyentuh" level (pips)
    history_months: int = 6        # riwayat LBMA yang di-cache (bulan)
    use_latest_when_missing: bool = True  # pakai tanggal LBMA terbaru bila hari ini belum rilis


@dataclass
class LBMAFundamentalConfig:
    """Analisis fundamental-teknikal LBMA (port spreadsheet 'HARGA LBMA HARIAN').

    Membaca riwayat fixing AM (pembukaan sesi London) & PM (penutupan) lalu
    menurunkan metrik harian (DELTA/STATUS/RASIO/grid akumulasi), level
    Fibonacci AM & PM, serta BIAS multi-hari (PM vs AM beruntun). Dipakai sebagai
    konteks/konfirmasi lunak — TIDAK memblok entry kecuali ``require_confirmation``.
    """

    enabled: bool = True
    require_confirmation: bool = False   # true = bias fundamental berlawanan -> blok entry
    fib_window_days: int = 22            # jendela hari utk high/low fib, rata-rata, median
    recent_days: int = 10                # baris metrik harian yang ditampilkan di /lbma_fund
    bullish_streak_days: int = 3         # PM>AM beruntun >= ini -> bias bullish
    bearish_streak_days: int = 3         # PM<AM beruntun >= ini -> bias bearish
    # Offset grid akumulasi/average-down (harga, BUKAN pips). AM - tiap offset.
    grid_offsets: list[float] = field(default_factory=lambda: [150.0, 300.0, 400.0])


@dataclass
class MTFConfig:
    """Entry alignment multi-timeframe (H1/M15/M5 searah + trigger momentum M1).

    Tanpa perlu LBMA / tanpa menunggu zona swing: kalau bias semua TF sepakat
    buy/sell dan ada candle momentum M1 -> langsung entry searah tren.
    """

    enabled: bool = True
    # Mode penentuan arah:
    #   "all"      = H1,M15,M5 wajib semua searah (paling ketat)
    #   "majority" = >=2 dari H1/M15/M5 searah & H1 tak berlawanan
    #   "h1"       = ikuti bias H1 saja (paling agresif)
    mode: str = "majority"


@dataclass
class RegimeConfig:
    """Filter rezim pasar (anti-sideways) berbasis ADX + alignment multi-TF.

    Sebelum entry apa pun, bot menilai H1/M15/M5/M3:
    - SEMUA timeframe ``adx`` >= ``adx_min`` (tren cukup kuat, bukan ranging), dan
    - SEMUA timeframe sepakat arah (bias EMA + arah +DI/-DI) -> tren jelas.
    Bila tidak terpenuhi -> dianggap SIDEWAYS dan SEMUA entry diblok.
    Saat trending, entry hanya diizinkan SEARAH tren (sinyal lawan arah dibuang).
    """

    enabled: bool = True
    timeframes: list[str] = field(default_factory=lambda: ["H1", "M15", "M5", "M3"])
    adx_period: int = 14
    adx_min: float = 22.0           # ambang kekuatan tren; < ini = sideways
    require_all_aligned: bool = True  # true = semua TF wajib searah; false = mayoritas
    di_confirms_direction: bool = True  # +DI/-DI harus searah bias EMA per TF


@dataclass
class SRConfig:
    """Support/Resistance multi-timeframe (M5/M15/H1) + entry di M5.

    Satuan pips emas = lbma.pip_size (default 0.1 = $0.10).
    """

    enabled: bool = True
    pivot_n: int = 3               # lebar fractal deteksi swing S/R
    lookback: int = 150            # candle yang dipindai per TF
    cluster_pips: float = 40.0     # gabungkan level berjarak <= ini jadi 1 zona
    touch_pips: float = 30.0       # toleransi "harga di zona"
    sl_buffer_pips: float = 20.0   # SL di luar zona
    rr_ratio: float = 2.0          # TP = entry +/- RR x jarak SL
    min_strength: int = 2          # kekuatan minimum zona agar dipakai
    require_m5_candle: bool = True  # wajib candle M5 searah (bounce/rejection)
    min_body_ratio: float = 0.2    # body minimum candle konfirmasi M5


@dataclass
class FibConfig:
    """Analitik Fibonacci (retracement/extension) + entry golden zone."""

    enabled: bool = True
    pivot_n: int = 3               # lebar fractal deteksi swing leg
    lookback: int = 120            # jumlah candle untuk cari leg swing
    gz_start: float = 0.50         # awal golden zone (retracement)
    gz_end: float = 0.786          # akhir golden zone
    proximity_pips: float = 30.0   # jarak dianggap "di level fib" (pips)


@dataclass
class CRTConfig:
    """Parameter lapisan konfirmasi teknikal CRT (port dari EA GridScalper_CRT)."""

    enabled: bool = True
    require_confirmation: bool = False  # true = CRT berlawanan -> blok entry LBMA
    enable_trend_entry: bool = True     # entry "market bagus": CRT+Fib trend continuation
    swing_h1: int = 5
    body_ratio_h1: float = 0.60
    h1_scan_bars: int = 150
    gz_start: float = 0.50
    gz_end: float = 0.786
    require_ob_fvg: bool = False   # (info) tampilkan OB/FVG; tidak memblok by default
    fvg_min_points: float = 30.0
    swing_m15: int = 3
    m15_scan_bars: int = 80
    l3_strict: bool = False
    body_ratio_m15: float = 0.60


@dataclass
class FundamentalsConfig:
    enabled: bool = True
    no_trade_window_minutes: int = 30
    fail_mode: str = "skip"  # "skip" | "continue"
    fear_greed_filter: bool = False
    fear_greed_min: int = 10
    fear_greed_max: int = 90
    calendar_url: str = ""
    calendar_cache_minutes: int = 15   # cache hasil kalender (hindari fetch tiap loop)
    fear_greed_url: str = "https://api.alternative.me/fng/?limit=1"
    http_timeout_sec: int = 10


@dataclass
class BarbarConfig:
    """Aggressive XAUUSD M1 hedged martingale grid.

    Disabled by default. Enable at runtime with Telegram ``/barbar on``.
    Distances are price units for XAUUSD, so ``1.00`` means USD 1.00.
    """

    enabled: bool = False
    timeframe: str = "M1"
    entry_mode: str = "STRADDLE"       # STRADDLE | MARKET
    straddle_distance: float = 0.50
    base_lot: float = 0.01
    lot_multiplier: float = 2.0
    grid_step: float = 1.00
    max_grid_levels: int = 4
    recovery_mode: str = "HEDGE"       # HEDGE | AVERAGE
    take_profit_usd: float = 5.0
    auto_take_profit: bool = True
    per_position_tp: float = 0.0
    per_position_sl: float = 0.0
    candle_follow: bool = True
    candle_follow_sl_buffer: float = 0.10
    candle_follow_tp_distance: float = 5.0  # 50 pips emas (pip 0.1 = $5.00); 0 = jarak TP otomatis
    stop_and_reverse: bool = True  # pasang stop order lawan di level SL candle (exit + balik arah)
    stop_reverse_lot_mode: str = "BASE"  # BASE = base_lot | MATCH = volume posisi yang ditutup | FIXED = stop_reverse_lot
    stop_reverse_lot: float = 0.0  # lot tetap untuk order reverse saat mode FIXED
    trailing_stop: bool = True
    trailing_start: float = 0.50
    trailing_distance: float = 0.50
    trailing_step: float = 0.10
    breakeven_plus: float = 0.10
    trailing_lock_profit_only: bool = True
    profit_lock_percent: float = 1.0
    quick_profit_lock: float = 0.10
    trailing_when_loss: bool = True
    magic_number: int = 20260629
    max_total_lots: float = 0.20
    equity_stop_percent: float = 20.0
    max_basket_loss_usd: float = 50.0
    max_spread: float = 0.40
    one_position_per_bar: bool = True
    trade_hours_utc: str = ""          # e.g. "00:05-21:55,23:05-23:55"
    cooldown_after_stop: int = 300
    cancel_opposite_pending: bool = False
    deviation: int = 50


@dataclass
class StraddleM1Config:
    """StraddleM1 EA (XAUUSD M1): straddle candle sebelumnya + trailing + reverse.

    Strategi 1 posisi (netting), TANPA TP/averaging/martingale. Semua jarak dalam
    POINT (XAUUSD 2-digit: 1 point = 0.01; 100 point = $1.00). State machine:
    FLAT_IDLE -> STRADDLE_PENDING -> IN_POSITION -> REVERSE_PENDING (stop-and-reverse).
    Aktifkan runtime via Telegram ``/straddle on``.
    """

    enabled: bool = False
    timeframe: str = "M1"
    lot: float = 0.01
    magic_number: int = 770017
    deviation: int = 30                # deviasi maks (points) untuk eksekusi market
    max_spread_pts: float = 30.0       # 0 = off; spread maks (points) untuk entry baru
    offset_pts: float = 30.0           # BUY STOP=High[1]+offset, SELL STOP=Low[1]-offset
    sl_pts: float = 50.0               # SL tiap leg dari harga entry (TANPA TP)
    trail_dist_pts: float = 20.0       # jarak SL di belakang harga saat trailing
    trail_step_pts: float = 20.0       # langkah minimal update SL/reverse (throttle)
    rev_gap_pts: float = 20.0          # jarak order reverse dari SL posisi
    reverse_timeout_bars: int = 0      # 0 = simpan reverse sampai fill; >0 = batalkan setelah N bar
    use_time_filter: bool = False      # filter jam (WAKTU SERVER) untuk blok entry baru
    block_start_hour: int = 15         # mulai blok (inklusif)
    block_end_hour: int = 17           # selesai blok (eksklusif); tangani wrap-around
    max_trades_per_day: int = 0        # 0 = off; rem jumlah straddle baru per hari
    max_daily_loss: float = 0.0        # 0 = off; rem realized loss harian (mata uang akun)


# Menit per timeframe (untuk validasi urutan; tanpa dependensi MT5).
TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M10": 10,
    "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440,
}


@dataclass
class TimeframesConfig:
    """Stack timeframe strategi (bisa diganti).

    - ``trend`` : TF penentu bias tren (EMA fast/slow).
    - ``zone``  : TF penentu zona swing/pivot.
    - ``entry`` : TF trigger/eksekusi (momentum candle, ATR untuk SL).

    Urutan WAJIB: menit(trend) >= menit(zone) >= menit(entry).
    """

    trend: str = "M15"
    zone: str = "M5"
    entry: str = "M1"

    def validate(self) -> list[str]:
        """Kembalikan daftar error (kosong = valid)."""
        errors: list[str] = []
        for label, tf in (("trend", self.trend), ("zone", self.zone), ("entry", self.entry)):
            if tf not in TIMEFRAME_MINUTES:
                errors.append(
                    f"timeframe {label}='{tf}' tidak valid. Pilihan: "
                    f"{', '.join(TIMEFRAME_MINUTES)}"
                )
        if errors:
            return errors
        mt, mz, me = (TIMEFRAME_MINUTES[self.trend], TIMEFRAME_MINUTES[self.zone],
                      TIMEFRAME_MINUTES[self.entry])
        if not (mt >= mz >= me):
            errors.append(
                f"urutan timeframe salah: trend({self.trend}) >= zone({self.zone}) "
                f">= entry({self.entry}) tidak terpenuhi."
            )
        return errors


@dataclass
class LoopConfig:
    loop_sleep_sec: int = 3
    candles: int = 300
    heartbeat_minutes: int = 60


@dataclass
class Secrets:
    """Secret runtime (TIDAK di-log, TIDAK di-commit)."""

    # Kredensial login MT5 di-set saat runtime lewat /login di Telegram
    # (bukan dari .env). Bot selalu mulai tanpa akun.
    mt5_login: int | None = None
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_path: str = ""
    telegram_bot_token: str = ""
    owner_chat_id: str = ""
    news_api_key: str = ""
    execute: bool = False  # gerbang eksekusi level .env

    def redacted(self) -> dict[str, str]:
        """Ringkasan aman untuk log (tanpa membocorkan nilai)."""

        def mask(value: Any) -> str:
            return "SET" if value not in (None, "", 0) else "EMPTY"

        return {
            "MT5_PATH": mask(self.mt5_path),
            "TELEGRAM_BOT_TOKEN": mask(self.telegram_bot_token),
            "OWNER_CHAT_ID": mask(self.owner_chat_id),
            "NEWS_API_KEY": mask(self.news_api_key),
            "EXECUTE": str(self.execute).lower(),
        }


@dataclass
class AppConfig:
    symbol_pattern: str = "BTC.*USD"
    magic: int = 770120
    # Mode strategi: "combo" (momentum cent + LBMA/CRT/Fib acuan, UTAMA emas) |
    # "lbma" (hanya jalur LBMA touch + CRT/Fib) | "legacy" (EMA/swing/RSI murni).
    strategy_mode: str = "combo"
    cent_account: bool = False  # true = balance/equity dalam sen (akun cent)
    timeframes: TimeframesConfig = field(default_factory=TimeframesConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    lbma: LBMAConfig = field(default_factory=LBMAConfig)
    lbma_fund: LBMAFundamentalConfig = field(default_factory=LBMAFundamentalConfig)
    crt: CRTConfig = field(default_factory=CRTConfig)
    fib: FibConfig = field(default_factory=FibConfig)
    mtf: MTFConfig = field(default_factory=MTFConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    sr: SRConfig = field(default_factory=SRConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    management: ManagementConfig = field(default_factory=ManagementConfig)
    fundamentals: FundamentalsConfig = field(default_factory=FundamentalsConfig)
    barbar: BarbarConfig = field(default_factory=BarbarConfig)
    straddle_m1: StraddleM1Config = field(default_factory=StraddleM1Config)
    loop: LoopConfig = field(default_factory=LoopConfig)
    secrets: Secrets = field(default_factory=Secrets)


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #
def _filter_known(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Ambil hanya key yang dikenal dataclass (abaikan ekstra di yaml)."""
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return {k: v for k, v in (data or {}).items() if k in known}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_secrets() -> Secrets:
    """Muat secret dari environment (.env sudah di-load oleh load_config).

    Kredensial login MT5 (login/password/server) TIDAK dibaca dari env — akun
    di-set saat runtime lewat /login di Telegram. Hanya ``MT5_PATH`` (lokasi
    terminal64.exe) yang masih dipakai untuk attach ke terminal.
    """
    return Secrets(
        mt5_path=os.getenv("MT5_PATH", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        owner_chat_id=os.getenv("OWNER_CHAT_ID", ""),
        news_api_key=os.getenv("NEWS_API_KEY", ""),
        execute=_env_bool("EXECUTE", False),
    )


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    env_path: str | os.PathLike[str] | None = None,
    load_env: bool = True,
) -> AppConfig:
    """Muat konfigurasi penuh (yaml + env).

    Args:
        config_path: lokasi config.yaml. Default ``config/config.yaml`` relatif
            ke root project.
        env_path: lokasi file .env. Default ``.env`` di root project.
        load_env: jika False, lewati pemuatan .env (berguna untuk test).
    """
    root = Path(__file__).resolve().parent.parent

    if config_path is None:
        config_path = root / "config" / "config.yaml"
    if env_path is None:
        env_path = root / ".env"

    if load_env:
        # override=False -> env yang sudah di-set OS tetap menang.
        load_dotenv(dotenv_path=env_path, override=False)

    raw: dict[str, Any] = {}
    cfg_file = Path(config_path)
    if cfg_file.exists():
        with cfg_file.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    return AppConfig(
        symbol_pattern=raw.get("symbol_pattern", "BTC.*USD"),
        magic=int(raw.get("magic", 770120)),
        strategy_mode=str(raw.get("strategy_mode", "combo")).lower(),
        cent_account=bool(raw.get("cent_account", False)),
        timeframes=TimeframesConfig(**_filter_known(TimeframesConfig, raw.get("timeframes", {}))),
        strategy=StrategyConfig(**_filter_known(StrategyConfig, raw.get("strategy", {}))),
        lbma=LBMAConfig(**_filter_known(LBMAConfig, raw.get("lbma", {}))),
        lbma_fund=LBMAFundamentalConfig(
            **_filter_known(LBMAFundamentalConfig, raw.get("lbma_fund", {}))
        ),
        crt=CRTConfig(**_filter_known(CRTConfig, raw.get("crt", {}))),
        fib=FibConfig(**_filter_known(FibConfig, raw.get("fib", {}))),
        mtf=MTFConfig(**_filter_known(MTFConfig, raw.get("mtf", {}))),
        regime=RegimeConfig(**_filter_known(RegimeConfig, raw.get("regime", {}))),
        sr=SRConfig(**_filter_known(SRConfig, raw.get("sr", {}))),
        risk=RiskConfig(**_filter_known(RiskConfig, raw.get("risk", {}))),
        management=ManagementConfig(**_filter_known(ManagementConfig, raw.get("management", {}))),
        fundamentals=FundamentalsConfig(
            **_filter_known(FundamentalsConfig, raw.get("fundamentals", {}))
        ),
        barbar=BarbarConfig(**_filter_known(BarbarConfig, raw.get("barbar", {}))),
        straddle_m1=StraddleM1Config(
            **_filter_known(StraddleM1Config, raw.get("straddle_m1", {}))
        ),
        loop=LoopConfig(**_filter_known(LoopConfig, raw.get("loop", {}))),
        secrets=load_secrets() if load_env else Secrets(),
    )
