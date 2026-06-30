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

import glob
import logging
import os
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
        self._last_error: Any = None
        self.login_timeout_ms = 60000  # tunggu koneksi server broker (ms)

    # ------------------------------------------------------------------ #
    def last_error_str(self) -> str:
        """Error MT5 terakhir (kode + deskripsi) untuk ditampilkan ke user."""
        if not self._last_error:
            return "tidak ada detail (cek apakah terminal MT5 berjalan)"
        try:
            code, desc = self._last_error
            return f"[{code}] {desc}"
        except (TypeError, ValueError):
            return str(self._last_error)

    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        """Hubungkan MT5 & login akun. True jika sukses.

        Login ke broker APA PUN (real/demo) hanya dengan nomor login, password,
        & server — TANPA set path manual. Bila kredensial diberikan (via /login),
        bot mencoba: terminal yang sedang berjalan, lalu SEMUA terminal MT5 yang
        terpasang di PC (auto-discovery), sampai ada yang menerima login (server
        broker tsb dikenal oleh terminal itu).
        """
        m = _require_mt5()
        self._last_error = None

        if not self.secrets.mt5_login:
            return self._init_only(m)

        # Kandidat terminal: path eksplisit (opsional) -> terminal berjalan ->
        # semua terminal MT5 terpasang.
        candidates: list[str | None] = []
        if self.secrets.mt5_path:
            candidates.append(self.secrets.mt5_path)
        candidates.append(None)
        for p in self.discover_terminal_paths():
            if p not in candidates:
                candidates.append(p)

        last_err: Any = None
        for cand in candidates:
            if self._try_login(m, cand):
                if cand:
                    # Ingat terminal yang berhasil untuk reconnect berikutnya.
                    self.secrets.mt5_path = cand
                return True
            last_err = self._last_error
            try:
                m.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self._last_error = last_err
        log.error("Login gagal di semua terminal (%d kandidat). Error: %s",
                  len(candidates), self.last_error_str())
        return False

    def _try_login(self, m: Any, path: str | None) -> bool:
        """Coba initialize(path) + login akun. True bila akun aktif."""
        init_kwargs: dict[str, Any] = {"timeout": self.login_timeout_ms}
        if path:
            init_kwargs["path"] = path
        if not m.initialize(**init_kwargs):
            self._last_error = m.last_error()
            log.info("initialize(%s) gagal: %s", path or "default", self._last_error)
            return False
        authorized = m.login(
            int(self.secrets.mt5_login),
            password=self.secrets.mt5_password,
            server=self.secrets.mt5_server,
            timeout=self.login_timeout_ms,
        )
        if not authorized:
            self._last_error = m.last_error()
            log.info("login(server=%s) via %s gagal: %s",
                     self.secrets.mt5_server, path or "default", self._last_error)
            return False
        info = m.account_info()
        if info is None:
            self._last_error = m.last_error()
            return False
        self._connected = True
        log.info("MT5 terhubung via %s. Akun=%s server=%s currency=%s",
                 path or "(terminal berjalan)", info.login, info.server, info.currency)
        return True

    def _init_only(self, m: Any) -> bool:
        """Attach terminal tanpa login eksplisit (mode belum punya akun)."""
        init_kwargs: dict[str, Any] = {"timeout": self.login_timeout_ms}
        if self.secrets.mt5_path:
            init_kwargs["path"] = self.secrets.mt5_path
        if not m.initialize(**init_kwargs):
            self._last_error = m.last_error()
            log.error("mt5.initialize() gagal: %s", self._last_error)
            return False
        info = m.account_info()
        if info is None:
            self._last_error = m.last_error()
            m.shutdown()
            return False
        self._connected = True
        log.info("MT5 terhubung (tanpa login eksplisit). Akun=%s server=%s",
                 info.login, info.server)
        return True

    @staticmethod
    def discover_terminal_paths() -> list[str]:
        """Cari semua terminal64.exe MT5 yang terpasang di PC (Windows).

        Memindai Program Files, Program Files (x86), LocalAppData, dan folder
        data MetaQuotes. Dipakai agar login bisa ke broker mana pun tanpa
        menyebut path secara manual (cukup satu terminal generic terpasang).
        """
        bases = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        patterns: list[str] = []
        for base in bases:
            if not base:
                continue
            patterns.append(os.path.join(base, "*", "terminal64.exe"))
            patterns.append(os.path.join(base, "*", "*", "terminal64.exe"))
        appdata = os.environ.get("APPDATA")
        if appdata:
            patterns.append(os.path.join(
                appdata, "MetaQuotes", "Terminal", "*", "terminal64.exe"))
        found: list[str] = []
        seen: set[str] = set()
        for pat in patterns:
            for path in glob.glob(pat):
                if path not in seen:
                    seen.add(path)
                    found.append(path)
        return found

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
        was_connected = self._connected
        self._connected = False
        if mt5 is not None:
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass
            if was_connected:
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
