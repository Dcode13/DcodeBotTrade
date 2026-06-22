"""Koneksi MT5, reconnect, dan auto-discovery simbol (§5).

CATATAN LINGKUNGAN (WAJIB):
- Paket ``MetaTrader5`` HANYA jalan di Windows + terminal MT5 terinstall &
  login ke akun yang sama.
- Nama simbol BTCUSD JANGAN di-hardcode: dicocokkan via pola ``BTC.*USD``.
- Spesifikasi kontrak diturunkan dari API, bukan diasumsikan.

Import MT5 dibungkus agar modul lain yang hanya butuh ``SymbolSpec``/strategi
tetap bisa di-test tanpa terminal.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from core.config import Secrets
from core.risk_manager import SymbolSpec

log = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5  # type: ignore
except ImportError:  # pragma: no cover - hanya di non-Windows / belum install
    mt5 = None  # noqa: N816


class MT5Unavailable(RuntimeError):
    """Dilempar bila paket MetaTrader5 tidak tersedia."""


def _require_mt5() -> Any:
    if mt5 is None:
        raise MT5Unavailable(
            "Paket 'MetaTrader5' tidak tersedia. Jalankan di Windows dengan "
            "terminal MT5 terinstall (pip install MetaTrader5)."
        )
    return mt5


class MT5Client:
    """Pengelola koneksi terminal MT5 + discovery simbol."""

    def __init__(self, secrets: Secrets, symbol_pattern: str = "BTC.*USD") -> None:
        self.secrets = secrets
        self.symbol_pattern = symbol_pattern
        self.symbol: str | None = None
        self._connected = False

    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        """Inisialisasi & login ke terminal. True jika sukses."""
        m = _require_mt5()
        kwargs: dict[str, Any] = {}
        if self.secrets.mt5_path:
            kwargs["path"] = self.secrets.mt5_path
        if self.secrets.mt5_login:
            kwargs["login"] = self.secrets.mt5_login
            kwargs["password"] = self.secrets.mt5_password
            kwargs["server"] = self.secrets.mt5_server

        if not m.initialize(**kwargs):
            log.error("mt5.initialize() gagal: %s", m.last_error())
            return False

        info = m.account_info()
        if info is None:
            log.error("account_info() None setelah initialize: %s", m.last_error())
            m.shutdown()
            return False

        self._connected = True
        log.info(
            "MT5 terhubung. Akun=%s server=%s currency=%s",
            info.login, info.server, info.currency,
        )
        return True

    def is_connected(self) -> bool:
        if mt5 is None or not self._connected:
            return False
        return mt5.terminal_info() is not None and mt5.account_info() is not None

    def ensure_connection(self, max_retries: int = 5) -> bool:
        """Reconnect dengan exponential backoff bila putus (§15)."""
        if self.is_connected():
            return True
        m = _require_mt5()
        delay = 2.0
        for attempt in range(1, max_retries + 1):
            log.warning("Koneksi MT5 putus. Reconnect attempt %d...", attempt)
            try:
                m.shutdown()
            except Exception:  # noqa: BLE001
                pass
            if self.connect():
                if self.symbol:
                    self.select_symbol(self.symbol)
                return True
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
        log.error("Gagal reconnect MT5 setelah %d percobaan.", max_retries)
        return False

    def shutdown(self) -> None:
        if mt5 is not None and self._connected:
            mt5.shutdown()
            self._connected = False
            log.info("MT5 shutdown.")

    # ------------------------------------------------------------------ #
    # Discovery simbol (§5.3)
    # ------------------------------------------------------------------ #
    def discover_symbol(self) -> str | None:
        """Cari simbol cocok pola, tradable, lalu select & log nama persisnya."""
        m = _require_mt5()
        pattern = re.compile(self.symbol_pattern, re.IGNORECASE)
        symbols = m.symbols_get()
        if symbols is None:
            log.error("symbols_get() None: %s", m.last_error())
            return None

        candidates = [s for s in symbols if pattern.search(s.name)]
        if not candidates:
            log.error("Tidak ada simbol cocok pola '%s'.", self.symbol_pattern)
            return None

        # Prioritaskan yang tradable penuh & nama terpendek (paling "murni").
        def score(s: Any) -> tuple[int, int]:
            tradable = 1 if getattr(s, "trade_mode", 0) != 0 else 0
            return (tradable, -len(s.name))

        candidates.sort(key=score, reverse=True)
        for s in candidates:
            if self.select_symbol(s.name):
                self.symbol = s.name
                log.info(
                    "Simbol terpilih: '%s' (dari %d kandidat: %s)",
                    s.name, len(candidates), [c.name for c in candidates],
                )
                return s.name

        log.error("Tidak bisa select simbol kandidat manapun.")
        return None

    def select_symbol(self, name: str) -> bool:
        m = _require_mt5()
        if not m.symbol_select(name, True):
            log.error("symbol_select(%s) gagal: %s", name, m.last_error())
            return False
        return True

    # ------------------------------------------------------------------ #
    # Info akun & simbol
    # ------------------------------------------------------------------ #
    def account_info(self) -> Any:
        return _require_mt5().account_info()

    def equity(self) -> float:
        info = self.account_info()
        return float(info.equity) if info else 0.0

    def get_symbol_info(self, name: str | None = None) -> Any:
        m = _require_mt5()
        return m.symbol_info(name or self.symbol)

    def get_symbol_spec(self, name: str | None = None) -> SymbolSpec:
        """Bangun ``SymbolSpec`` dari ``symbol_info`` (§5.5)."""
        info = self.get_symbol_info(name)
        if info is None:
            raise MT5Unavailable(f"symbol_info({name or self.symbol}) None")
        return SymbolSpec(
            name=info.name,
            digits=int(info.digits),
            point=float(info.point),
            trade_contract_size=float(info.trade_contract_size),
            trade_tick_size=float(info.trade_tick_size),
            trade_tick_value=float(info.trade_tick_value),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            trade_stops_level=int(info.trade_stops_level),
            filling_mode=int(info.filling_mode),
        )

    def get_tick(self, name: str | None = None) -> Any:
        return _require_mt5().symbol_info_tick(name or self.symbol)

    def get_spread_points(self, name: str | None = None) -> float:
        """Spread terkini dalam point: (ask - bid) / point."""
        info = self.get_symbol_info(name)
        tick = self.get_tick(name)
        if info is None or tick is None or info.point <= 0:
            return float("inf")
        return (tick.ask - tick.bid) / info.point

    def autotrading_enabled(self) -> bool:
        """True jika tombol 'Algo Trading' di terminal MT5 aktif (trade_allowed)."""
        info = _require_mt5().terminal_info()
        return bool(getattr(info, "trade_allowed", False)) if info else False

    def is_market_open(self, name: str | None = None) -> bool:
        """Cek market open via trade_mode + ketersediaan tick segar (§5.6)."""
        m = _require_mt5()
        info = self.get_symbol_info(name)
        if info is None:
            return False
        # TRADE_MODE_DISABLED = 0 -> tidak bisa trading.
        if getattr(info, "trade_mode", 0) == getattr(m, "SYMBOL_TRADE_MODE_DISABLED", 0):
            return False
        tick = self.get_tick(name)
        return tick is not None and tick.bid > 0 and tick.ask > 0
