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
    max_daily_loss_pct: float = 0.05
    max_consecutive_losses: int = 3
    max_open_positions: int = 1
    max_trades_per_day: int = 8
    max_spread_points: float = 250.0
    allow_min_lot_override: bool = False
    deviation: int = 50


@dataclass
class ManagementConfig:
    break_even: bool = True
    break_even_trigger_r: float = 1.0
    trailing_stop: bool = False
    trailing_atr_mult: float = 1.5


@dataclass
class FundamentalsConfig:
    enabled: bool = True
    no_trade_window_minutes: int = 30
    fail_mode: str = "skip"  # "skip" | "continue"
    fear_greed_filter: bool = False
    fear_greed_min: int = 10
    fear_greed_max: int = 90
    calendar_url: str = ""
    fear_greed_url: str = "https://api.alternative.me/fng/?limit=1"
    http_timeout_sec: int = 10


# Menit per timeframe (untuk validasi urutan; tanpa dependensi MT5).
TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440,
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
            "MT5_LOGIN": mask(self.mt5_login),
            "MT5_PASSWORD": mask(self.mt5_password),
            "MT5_SERVER": mask(self.mt5_server),
            "TELEGRAM_BOT_TOKEN": mask(self.telegram_bot_token),
            "OWNER_CHAT_ID": mask(self.owner_chat_id),
            "NEWS_API_KEY": mask(self.news_api_key),
            "EXECUTE": str(self.execute).lower(),
        }


@dataclass
class AppConfig:
    symbol_pattern: str = "BTC.*USD"
    magic: int = 770120
    timeframes: TimeframesConfig = field(default_factory=TimeframesConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    management: ManagementConfig = field(default_factory=ManagementConfig)
    fundamentals: FundamentalsConfig = field(default_factory=FundamentalsConfig)
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


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def load_secrets() -> Secrets:
    """Muat secret dari environment (.env sudah di-load oleh load_config)."""
    return Secrets(
        mt5_login=_env_int("MT5_LOGIN"),
        mt5_password=os.getenv("MT5_PASSWORD", ""),
        mt5_server=os.getenv("MT5_SERVER", ""),
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
        timeframes=TimeframesConfig(**_filter_known(TimeframesConfig, raw.get("timeframes", {}))),
        strategy=StrategyConfig(**_filter_known(StrategyConfig, raw.get("strategy", {}))),
        risk=RiskConfig(**_filter_known(RiskConfig, raw.get("risk", {}))),
        management=ManagementConfig(**_filter_known(ManagementConfig, raw.get("management", {}))),
        fundamentals=FundamentalsConfig(
            **_filter_known(FundamentalsConfig, raw.get("fundamentals", {}))
        ),
        loop=LoopConfig(**_filter_known(LoopConfig, raw.get("loop", {}))),
        secrets=load_secrets() if load_env else Secrets(),
    )
