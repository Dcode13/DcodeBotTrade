"""Orchestrator / loop utama bot scalping BTCUSD (§6).

Menyatukan semua modul:
- koneksi MT5 + discovery simbol
- penarikan data & evaluasi strategi (M15->M5->M1)
- gerbang eksekusi live (§16), circuit breaker (§8.2), filter berita (§9)
- eksekusi & manajemen posisi
- kontrol & alert via Telegram (§12)

Default = ALERT-ONLY (aman). Eksekusi uang asli butuh EXECUTE=true di .env
DAN /confirm_live di Telegram.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pandas as pd

from core import crt_analysis
from core import fibonacci
from core import lbma as lbma_mod
from core import lbma_fundamental as lbma_fund_mod
from core import regime as regime_mod
from core import strategy as strat_mod
from core import support_resistance as sr_mod
from core.barbar import BarbarCycleResult, BarbarGrid, StraddleM1
from core.config import AppConfig, load_config
from core.executor import Executor
from core.fundamentals import FundamentalsFilter
from core.lbma import LBMAStore
from core.journal import Journal, TradeRecord
from core.market_data import MarketData
from core.mt5_client import MT5Client, MT5Unavailable
from core.position_manager import PositionManager
from core.risk_manager import (
    DayState,
    SymbolSpec,
    check_circuit_breakers,
    check_spread,
    high_risk_warning,
    size_position,
    validate_stops,
)
from telegram.bot import TelegramBot
from telegram.notifier import Notifier

log = logging.getLogger("btc_bot")

# Pesan sambutan saat /start & saat bot mulai.
WELCOME = "🎉 Selamat Datang di Bot Trading Dcode!, jangan maruk!!! 🎯"

# Menu perintah yang muncul saat user mengetik '/' di Telegram (setMyCommands).
TELEGRAM_COMMANDS: list[tuple[str, str]] = [
    ("start", "Mulai bot & perkenalan"),
    ("help", "Daftar semua perintah"),
    ("login", "Login akun MT5 (nomor, password, server)"),
    ("logout", "Putuskan akun MT5 yang sedang login"),
    ("setpath", "Set path terminal broker (opsional, jarang perlu)"),
    ("terminals", "Lihat terminal MT5 yang terdeteksi di PC"),
    ("cancel", "Batalkan proses login"),
    ("status", "Status mode, equity, bias, drawdown"),
    ("positions", "Posisi terbuka"),
    ("balance", "Balance & equity akun"),
    ("risk", "Parameter risiko"),
    ("set_risk", "Ubah risk per trade (mis. /set_risk 1)"),
    ("lbma", "Acuan LBMA hari ini & riwayat"),
    ("lbma_fund", "Analisis fundamental LBMA"),
    ("fib", "Level Fibonacci (golden zone)"),
    ("sr", "Peta Support/Resistance"),
    ("barbar", "Mode Gold M1 hedged-martingale grid"),
    ("straddle", "EA StraddleM1: straddle + trailing + stop-and-reverse"),
    ("pause", "Hentikan entry baru"),
    ("resume", "Lanjutkan entry baru"),
    ("rebase", "Reset baseline equity hari ini"),
    ("confirm_live", "Aktifkan eksekusi uang asli"),
    ("disable_exec", "Kembali ke alert-only"),
    ("report", "Ringkasan performa"),
    ("stop", "Kill switch (matikan eksekusi + pause)"),
]

# Perintah yang butuh akun MT5 sudah login (akses akun/market). Sebelum /login
# berhasil, perintah ini dibalas dengan instruksi untuk login dulu.
NEEDS_ACCOUNT = frozenset({
    "status", "positions", "balance", "rebase", "confirm_live", "barbar", "straddle",
})


# --------------------------------------------------------------------------- #
def setup_logging(log_dir: str = "logs") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    # Konsol Windows default cp1252 -> paksa UTF-8 agar emoji di log tidak crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fh = TimedRotatingFileHandler(
        Path(log_dir) / "bot.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fmt_px(v: float | None) -> str:
    """Format harga LBMA (atau '-' bila None)."""
    return f"{v:.2f}" if v is not None else "-"


# --------------------------------------------------------------------------- #
class TradingBot:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.journal = Journal()
        self.notifier = Notifier(cfg.secrets.telegram_bot_token, cfg.secrets.owner_chat_id)
        self.client = MT5Client(cfg.secrets, cfg.symbol_pattern)
        self.data = MarketData(self.client)
        self.executor = Executor(self.client, cfg.magic, cfg.risk.deviation)
        self.position_mgr = PositionManager(
            self.client, self.executor, self.journal,
            cfg.management, cfg.strategy, cfg.magic,
            pip_size=cfg.lbma.pip_size,
        )
        self.fundamentals = FundamentalsFilter(cfg.fundamentals, cfg.secrets.news_api_key)
        self.lbma = LBMAStore(cfg.lbma)
        self.barbar = BarbarGrid(self.client, cfg.barbar)
        self.straddle = StraddleM1(self.client, cfg.straddle_m1)
        self.tg = TelegramBot(
            cfg.secrets.telegram_bot_token, cfg.secrets.owner_chat_id,
            self.notifier, self.handle_command,
        )

        self.spec: SymbolSpec | None = None
        self.tfs = cfg.timeframes
        # Runtime state (dipersist).
        self.paused: bool = bool(self.journal.get_state("paused", False))
        self.live_confirmed: bool = bool(self.journal.get_state("live_confirmed", False))
        self.last_bar_time: str | None = self.journal.get_state("last_bar_time")
        self.barbar_enabled: bool = bool(
            self.journal.get_state("barbar_enabled", cfg.barbar.enabled)
        )
        self.barbar.last_bar_time = self.journal.get_state("barbar_last_bar_time")
        self.barbar.cooldown_until = float(
            self.journal.get_state("barbar_cooldown_until", 0.0) or 0.0
        )
        # StraddleM1 berjalan berdampingan (magic terpisah), bukan mode eksklusif.
        self.straddle_enabled: bool = bool(
            self.journal.get_state("straddle_enabled", cfg.straddle_m1.enabled)
        )
        self.straddle.last_bar_time = self.journal.get_state("straddle_last_bar_time")
        self.straddle.trade_day = str(self.journal.get_state("straddle_trade_day", "") or "")
        self.straddle.trades_today = int(
            self.journal.get_state("straddle_trades_today", 0) or 0
        )
        # Marker AM/PM LBMA yang di-set otomatis (saat /confirm_live & start).
        self.lbma_markers: dict | None = self.journal.get_state("lbma_markers")
        self._last_heartbeat = 0.0
        self.day_state: DayState | None = self.journal.load_day_state()
        self._running = True
        # State flow /login (None = tidak sedang login). Berisi langkah & data
        # yang sudah dikumpulkan: {"step": "login|password|server", "data": {...}}.
        self._login_state: dict | None = None
        # True bila MT5 terhubung & simbol siap. Saat False, loop hanya melayani
        # perintah Telegram (mis. /login) tanpa mencoba trading.
        self.ready: bool = False

    # ------------------------------------------------------------------ #
    # Gerbang eksekusi (§16)
    # ------------------------------------------------------------------ #
    @property
    def execution_enabled(self) -> bool:
        return bool(self.cfg.secrets.execute) and self.live_confirmed and not self.paused

    @property
    def mode_str(self) -> str:
        return "LIVE" if self.execution_enabled else "ALERT-ONLY"

    def _save_runtime(self) -> None:
        self.journal.set_state("paused", self.paused)
        self.journal.set_state("live_confirmed", self.live_confirmed)
        self.journal.set_state("last_bar_time", self.last_bar_time)
        self.journal.set_state("barbar_enabled", self.barbar_enabled)
        self.journal.set_state("barbar_last_bar_time", self.barbar.last_bar_time)
        self.journal.set_state("barbar_cooldown_until", self.barbar.cooldown_until)
        self.journal.set_state("straddle_enabled", self.straddle_enabled)
        self.journal.set_state("straddle_last_bar_time", self.straddle.last_bar_time)
        self.journal.set_state("straddle_trade_day", self.straddle.trade_day)
        self.journal.set_state("straddle_trades_today", self.straddle.trades_today)
        if self.day_state:
            self.journal.save_day_state(self.day_state)

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #
    def setup(self) -> bool:
        # Validasi timeframe stack sebelum apa pun.
        tf_errors = self.tfs.validate()
        if tf_errors:
            for e in tf_errors:
                log.error("Config timeframe: %s", e)
            return False
        log.info("Timeframe stack: trend=%s zone=%s entry=%s",
                 self.tfs.trend, self.tfs.zone, self.tfs.entry)

        # Daftarkan menu perintah supaya '/' memunculkan semua perintah.
        self.tg.register_commands(TELEGRAM_COMMANDS)

        # Bot SELALU mulai TANPA akun. Kredensial .env TIDAK dipakai untuk
        # auto-login: akun trading hanya didapat setelah user /login dan
        # memasukkan akun sendiri (nomor login, password, server).
        self.cfg.secrets.mt5_login = None
        self.cfg.secrets.mt5_password = ""
        self.cfg.secrets.mt5_server = ""
        self.ready = False
        self.spec = None
        self.notifier.send(
            f"{WELCOME}\n\n"
            "🤖 Bot AKTIF — BELUM ada akun yang login.\n"
            "Kirim /login untuk masuk: nomor login → password → server MT5.\n"
            "Ketik /help untuk daftar perintah."
        )
        return True

    def _prepare_symbol(self) -> bool:
        """Discovery simbol + bangun spec + reconcile + reset harian.

        Set ``self.ready=True`` & kembalikan True bila simbol siap dipakai.
        """
        try:
            symbol = self.client.discover_symbol()
            if not symbol:
                log.error("Simbol cocok pola '%s' tidak ditemukan.", self.cfg.symbol_pattern)
                return False
            self.spec = self.client.get_symbol_spec(symbol)
            log.info("Spesifikasi simbol: %s", self.spec)
            self.position_mgr.reconcile(symbol)
            self.reset_day_if_needed()
        except Exception as exc:  # noqa: BLE001
            log.error("Gagal siapkan simbol: %s", exc)
            return False
        self.ready = True
        return True

    def _currency(self) -> str:
        info = self.client.account_info()
        return info.currency if info else "?"

    def _equity_line(self) -> str:
        eq = self.client.equity()
        if self.cfg.cent_account:
            return (f"equity={eq:.2f} {self._currency()} "
                    f"(akun cent: ~{eq / 100:.2f} unit mata uang)")
        return f"equity={eq:.2f} {self._currency()}"

    def _refresh_lbma_safe(self) -> str:
        """Pastikan data LBMA segar; kembalikan ringkasan 1 baris (untuk alert)."""
        try:
            ok = self.lbma.ensure_fresh(self.cfg.lbma.history_months)
        except Exception as exc:  # noqa: BLE001
            log.warning("Refresh LBMA error: %s", exc)
            ok = False
        if not self.lbma.has_data():
            return "🏷️ LBMA: data belum tersedia (cek koneksi)."
        latest = self.lbma.latest_date()
        am, pm = self.lbma.get(latest)
        ref = self.lbma.reference_for(latest)
        net = "fresh" if ok else "cache"
        base = f"🏷️ LBMA {latest} ({net}): AM={_fmt_px(am)} PM={_fmt_px(pm)}"
        if ref:
            base += f" | acuan {ref.level_name}={ref.level:.2f}"
        return base

    def _set_lbma_markers(self) -> str:
        """Set otomatis marker AM LBMA & PM LBMA (dipanggil saat /confirm_live).

        Menyimpan AM/PM + level acuan ke state, lalu kembalikan teks penanda.
        """
        if not self.lbma.has_data():
            return "🏷️ LBMA: data belum tersedia (cek koneksi)."
        today = datetime.now(timezone.utc).date()
        am, pm = self.lbma.get(today)
        ref_date = today
        if am is None and pm is None:
            latest = self.lbma.latest_date()
            if latest is not None:
                ref_date = latest
                am, pm = self.lbma.get(latest)
        ref = self.lbma.reference_for(ref_date)
        self.lbma_markers = {
            "date": ref_date.isoformat(), "am": am, "pm": pm,
            "level_name": ref.level_name if ref else None,
            "level": ref.level if ref else None,
        }
        self.journal.set_state("lbma_markers", self.lbma_markers)

        rule = ("AM>PM→PM" if (am and pm and am > pm) else
                "PM>AM→AM" if (am and pm and pm > am) else "AM=PM→AM")
        lines = [
            f"🏷️ LBMA acuan di-SET otomatis (tgl {ref_date.isoformat()}):",
            f"• AM LBMA = {_fmt_px(am)}",
            f"• PM LBMA = {_fmt_px(pm)}",
        ]
        if ref:
            lines.append(f"→ acuan aktif: {ref.level_name} = {ref.level:.2f} "
                         f"({rule}) | SL {ref.sl_pips:.0f}p")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Reset harian (§6 langkah 3)
    # ------------------------------------------------------------------ #
    def reset_day_if_needed(self) -> None:
        today = _today_str()
        equity = self.client.equity()
        if self.day_state is None or self.day_state.day != today:
            self.day_state = DayState(
                day=today,
                start_equity=equity,
                trades_today=0,
                consecutive_losses=0,
                paused=self.paused,
            )
            self.journal.save_day_state(self.day_state)
            log.info("Hari baru %s, start_equity=%.2f", today, equity)

    # ------------------------------------------------------------------ #
    # Loop utama
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        log.info("Memasuki loop utama (sleep=%ds).", self.cfg.loop.loop_sleep_sec)
        while self._running:
            try:
                self._loop_once()
            except Exception as exc:  # noqa: BLE001 - loop harus tetap hidup (§15)
                log.exception("Error di loop utama")
                self.notifier.send(f"⚠️ Error loop: {exc}")
            time.sleep(self.cfg.loop.loop_sleep_sec)

    def _loop_once(self) -> None:
        # 1. Perintah Telegram (selalu dilayani, termasuk saat belum login).
        self.tg.poll_and_process()

        # Belum terhubung ke MT5 (belum /login) -> jangan coba trading/reconnect.
        if not self.ready:
            return

        # 2. Koneksi sehat?
        if not self.client.ensure_connection():
            self.notifier.send("⚠️ MT5 terputus, gagal reconnect. Skip siklus.")
            return
        assert self.spec is not None
        symbol = self.spec.name

        # 3. Reset harian.
        self.reset_day_if_needed()

        # 4. Kelola posisi (break-even/trailing + deteksi tutup).
        df_stack = self.data.get_stack(self.tfs, self.cfg.loop.candles, symbol)
        df_entry = df_stack["entry"]
        manage = self.position_mgr.manage(self.spec, df_entry if not df_entry.empty else None)
        self._handle_manage_result(manage)

        # StraddleM1 jalan berdampingan (magic terpisah) — tiap loop, lepas dari
        # BARBAR/normal. Tidak mengambil alih engine entry yang lain.
        if self.straddle_enabled:
            s_status = self._run_straddle_cycle(notify=True)
            if s_status:
                log.info("straddle: %s", s_status)

        # BARBAR mode owns the entry engine while active. Normal positions are
        # still managed above, but no normal strategy entry is evaluated here.
        if self.barbar_enabled:
            status = self._run_barbar_cycle(notify=True)
            if status:
                log.info("barbar: %s", status)
            self._heartbeat()
            return

        # 5/6. Idempotency (per candle entry TF) + 1 posisi saja.
        if df_entry.empty or len(df_entry) < 3:
            return
        bar_time = str(df_entry.index[-2])
        is_new_bar = bar_time != self.last_bar_time
        if not is_new_bar:
            self._heartbeat()
            return
        self.last_bar_time = bar_time
        self.journal.set_state("last_bar_time", bar_time)

        # Hanya 1 posisi per simbol (§6.6).
        if self.position_mgr.get_open_positions(symbol):
            self._heartbeat()
            return

        status = self._evaluate_entry(df_stack)
        if status:
            log.info("eval[%s]: %s", self.tfs.entry, status)
        self._heartbeat()

    # ------------------------------------------------------------------ #
    def _evaluate_entry(self, df_stack: dict[str, pd.DataFrame]) -> str:
        """Evaluasi & (jika LIVE + sinyal valid) eksekusi. Return status singkat.

        - "combo" (default emas): momentum (strategi cent) + LBMA touch + CRT/Fib,
          LBMA & Fibonacci sebagai acuan (tidak wajib).
        - "lbma": hanya jalur LBMA touch + CRT/Fib.
        - "legacy": EMA/swing/RSI murni.
        """
        mode = self.cfg.strategy_mode
        if mode == "legacy":
            return self._evaluate_entry_legacy(df_stack)
        if mode == "lbma":
            return self._evaluate_entry_lbma(df_stack)
        return self._evaluate_entry_combo(df_stack)

    def _evaluate_entry_legacy(self, df_stack: dict[str, pd.DataFrame]) -> str:
        """Evaluasi & (jika LIVE + sinyal valid) eksekusi. Return status singkat."""
        assert self.spec is not None and self.day_state is not None
        equity = self.client.equity()

        # 7. Circuit breaker.
        cb = check_circuit_breakers(self.day_state, equity, self.cfg.risk)
        if not cb.allowed:
            log.info("Entry diblok circuit breaker: %s", cb.reason)
            return f"⛔ Diblok circuit breaker: {cb.reason}"

        # 8. Filter berita.
        fund = self.fundamentals.is_trading_allowed()
        if not fund.allowed:
            log.info("Entry diblok fundamental: %s", fund.reason)
            return f"⛔ Diblok filter berita: {fund.reason}"

        # Market open?
        if not self.client.is_market_open(self.spec.name):
            log.info("Market tutup / simbol disabled. Skip.")
            return "⛔ Market tutup / simbol disabled."

        # 9. Spread.
        spread = self.client.get_spread_points(self.spec.name)
        sp = check_spread(spread, self.cfg.risk)
        if not sp.allowed:
            log.info("Entry diblok: %s (spread=%.0f)", sp.reason, spread)
            return f"⛔ {sp.reason}"

        # 10. Strategi.
        tick = self.client.get_tick(self.spec.name)
        bid = tick.bid if tick else None
        ask = tick.ask if tick else None
        signal, reason = strat_mod.evaluate(
            df_stack["trend"], df_stack["zone"], df_stack["entry"], self.cfg.strategy, bid, ask
        )
        if signal is None:
            log.info("Tidak ada sinyal: %s", reason)
            return f"😴 Belum ada sinyal valid: {reason}"

        # Validasi jarak SL/TP vs stops_level.
        vs = validate_stops(signal, self.spec)
        if not vs.allowed:
            self.notifier.send(f"⏭️ Sinyal {signal.direction} dilewati: {vs.reason}")
            return f"⏭️ Sinyal {signal.direction} dilewati: {vs.reason}"

        # Sizing (mode "fixed" = lot tetap, atau "risk" = dari risk_per_trade).
        sizing = size_position(equity, signal.sl_distance, self.spec, self.cfg.risk)
        if not sizing.ok:
            self.notifier.send(
                f"⏭️ Sinyal {signal.direction} @ {signal.entry:.{self.spec.digits}f} "
                f"TIDAK dieksekusi: {sizing.reason}"
            )
            return f"⏭️ Sinyal {signal.direction} tak dieksekusi: {sizing.reason}"

        self._emit_signal(signal, sizing.lots, sizing.warning, spread)

        # 11. Eksekusi atau alert-only (§16).
        if not self.execution_enabled:
            log.info("ALERT-ONLY: order tidak dikirim (mode=%s).", self.mode_str)
            return "📣 Sinyal dikirim (ALERT-ONLY, order tidak dikirim)."

        # Cek tombol Algo Trading terminal sebelum coba order (hindari 10027).
        if not self.client.autotrading_enabled():
            msg = ("⚠️ AutoTrading OFF di terminal MT5. Order TIDAK dikirim. "
                   "Aktifkan tombol 'Algo Trading' (jadi hijau) di MT5.")
            log.warning(msg)
            self.notifier.send(msg)
            return msg

        ok = self._execute(signal, sizing.lots)
        return (f"✅ Order {signal.direction} {sizing.lots} lot terkirim."
                if ok else "❌ Order gagal (lihat alert).")

    # ------------------------------------------------------------------ #
    # Helper bersama untuk evaluasi entry emas (mode lbma & combo)
    # ------------------------------------------------------------------ #
    def _pre_entry_gates(self) -> tuple[bool, str, float]:
        """Gerbang umum: circuit breaker, berita, market, spread. (ok, msg, spread)."""
        assert self.spec is not None and self.day_state is not None
        equity = self.client.equity()
        cb = check_circuit_breakers(self.day_state, equity, self.cfg.risk)
        if not cb.allowed:
            return False, f"⛔ Diblok circuit breaker: {cb.reason}", 0.0
        fund = self.fundamentals.is_trading_allowed()
        if not fund.allowed:
            return False, f"⛔ Diblok filter berita: {fund.reason}", 0.0
        if not self.client.is_market_open(self.spec.name):
            return False, "⛔ Market tutup / simbol disabled.", 0.0
        spread = self.client.get_spread_points(self.spec.name)
        sp = check_spread(spread, self.cfg.risk)
        if not sp.allowed:
            return False, f"⛔ {sp.reason}", spread
        return True, "", spread

    def _lbma_context(self):
        """Acuan LBMA untuk display/jalur touch. Return (ref, analysis, teks)."""
        if not self.lbma.has_data():
            self._refresh_lbma_safe()
        if not self.lbma.has_data():
            return None, None, "LBMA: data belum ada"
        today = datetime.now(timezone.utc).date()
        am, pm = self.lbma.get(today)
        ref_date = today
        if am is None and pm is None:
            latest = self.lbma.latest_date()
            if self.cfg.lbma.use_latest_when_missing and latest is not None:
                ref_date = latest
            else:
                return None, None, "LBMA: hari ini belum rilis"
        analysis = self.lbma.analyze_for(ref_date)
        ref = analysis.reference
        if ref is None:
            return None, analysis, "LBMA: -"
        am2, pm2 = self.lbma.get(ref_date)
        text = (f"LBMA {ref.level_name}={ref.level:.2f} "
                f"(AM={_fmt_px(am2)} PM={_fmt_px(pm2)}) tgl {ref_date.isoformat()}")
        return ref, analysis, text

    def _lbma_fundamental(self):
        """Analisis fundamental-teknikal LBMA (bias PM vs AM, fib AM/PM, grid).

        Return ``LBMAFundamental`` atau ``None`` (modul off / data LBMA kosong).
        Memakai tanggal hari ini; bila belum rilis, pakai LBMA terbaru (sesuai
        ``lbma.use_latest_when_missing``).
        """
        fc = self.cfg.lbma_fund
        if not fc.enabled or not self.lbma.has_data():
            return None
        today = datetime.now(timezone.utc).date()
        am, pm = self.lbma.get(today)
        ref_date = today
        if am is None and pm is None:
            latest = self.lbma.latest_date()
            if self.cfg.lbma.use_latest_when_missing and latest is not None:
                ref_date = latest
            else:
                return None
        return lbma_fund_mod.analyze(self.lbma.am_map, self.lbma.pm_map, ref_date, fc)

    def _crt_and_fib(self):
        """Hitung konteks CRT (H1+M15) + Fibonacci. Return (ctx, fib, fib_src)."""
        candles = self.cfg.loop.candles
        df_h1 = self.data.get_rates("H1", candles)
        df_m15 = self.data.get_rates("M15", candles)
        ctx = None
        if self.cfg.crt.enabled and not df_h1.empty and not df_m15.empty:
            point = self.spec.point if self.spec else 0.01
            ctx = crt_analysis.analyze(df_h1, df_m15, self.cfg.crt, point=point)
        fib = None
        src = ""
        if self.cfg.fib.enabled:
            if ctx is not None and ctx.bias != 0:
                fib = fibonacci.compute(ctx.leg_low, ctx.leg_high, ctx.bias)
                src = f"CRT H1 ({ctx.bias_str()})"
            elif not df_m15.empty:
                fib = fibonacci.from_df(df_m15, self.cfg.fib)
                src = "swing M15"
        return ctx, fib, src

    def _fib_text(self, fib, src: str, price: float) -> str:
        if fib is None or fib.rng <= 0:
            return "fib: -"
        gz_lo, gz_hi = fib.golden_zone(self.cfg.fib)
        nr, npx, _ = fib.nearest(price)
        inz = "di GZ" if fib.in_golden_zone(price, self.cfg.fib) else "luar GZ"
        return f"fib({src}) GZ {gz_lo:.2f}-{gz_hi:.2f} [{inz}] ~{nr:.3f}@{npx:.2f}"

    def _gen_lbma_touch(self, ref, analysis, bid, ask, last_close, ctx, sig_time):
        """JALUR LBMA touch (fade). Return (Signal|None, extra, pending_reason)."""
        lc = self.cfg.lbma
        if analysis is not None and analysis.blocked:
            return None, "", f"lbma-touch: {analysis.reason}"
        touch, treason = lbma_mod.touch_signal(ref, bid, ask, last_close, lc)
        if touch is None:
            return None, "", f"lbma-touch: {treason}"
        crt_msg = "CRT off"
        if ctx is not None:
            confirmed, crt_msg = crt_analysis.confirms(touch.direction, ctx, self.cfg.crt)
            if self.cfg.crt.require_confirmation and not confirmed:
                return None, "", f"lbma-touch CRT blokir: {crt_msg}"
        signal = strat_mod.Signal(
            direction=touch.direction, entry=touch.entry, sl=touch.sl, tp=touch.tp,
            sl_distance=touch.sl_distance, zone=ref.level, atr_m1=touch.sl_distance,
            bias=("UP" if touch.direction == "BUY" else "DOWN"),
            signal_bar_time=sig_time,
            body_ratio=(ctx.choch_body_ratio if ctx else 0.0), rsi_m1=0.0,
            reason=touch.reason,
        )
        prox = analysis.proximity_range if analysis else 0.0
        extra = f"sisi harga: {touch.side} | konsolidasi2hr={prox:.2f} | {crt_msg}"
        return signal, extra, ""

    def _gen_crt_fib(self, ctx, fib, bid, ask, last_close, sig_time):
        """JALUR CRT+Fibonacci trend continuation. Return (Signal|None, extra, pending)."""
        lc = self.cfg.lbma
        if ctx is None or ctx.bias == 0:
            return None, "", "trend: bias CRT NONE"
        if fib is None or fib.rng <= 0:
            return None, "", "trend: fib belum ada"
        direction = "BUY" if ctx.bias > 0 else "SELL"
        in_gz = fib.in_golden_zone(last_close, self.cfg.fib)
        momentum = bool(ctx.choch_dir == ctx.bias and ctx.momentum_ok)
        obfvg_ok = (not self.cfg.crt.require_ob_fvg) or ctx.has_ob or ctx.has_fvg
        if not (in_gz and momentum and obfvg_ok):
            why = []
            if not in_gz:
                why.append("luar GZ fib")
            if not momentum:
                why.append("tanpa CHoCH searah")
            if not obfvg_ok:
                why.append("tanpa OB/FVG")
            return None, "", "trend: " + ", ".join(why)
        sl_dist = lc.sl_pips * lc.pip_size
        if direction == "BUY":
            entry, sl, tp = ask, ask - sl_dist, ask + lc.rr_ratio * sl_dist
        else:
            entry, sl, tp = bid, bid + sl_dist, bid - lc.rr_ratio * sl_dist
        signal = strat_mod.Signal(
            direction=direction, entry=entry, sl=sl, tp=tp, sl_distance=sl_dist,
            zone=fib._retr(self.cfg.fib.gz_end), atr_m1=sl_dist,
            bias=("UP" if direction == "BUY" else "DOWN"),
            signal_bar_time=sig_time, body_ratio=ctx.choch_body_ratio, rsi_m1=0.0,
            reason="CRT+Fib trend continuation (market bagus)",
        )
        gz_lo, gz_hi = fib.golden_zone(self.cfg.fib)
        nr, npx, _ = fib.nearest(last_close)
        extra = (f"setup: CRT+Fibonacci trend continuation\n"
                 f"fib leg {fib.low:.2f}-{fib.high:.2f} GZ {gz_lo:.2f}-{gz_hi:.2f} ✅ ~{nr:.3f}@{npx:.2f}")
        return signal, extra, ""

    # ------------------------------------------------------------------ #
    def _evaluate_entry_lbma(self, df_stack: dict[str, pd.DataFrame]) -> str:
        """Mode 'lbma' MURNI: hanya jalur LBMA touch + CRT/Fib (butuh data LBMA)."""
        assert self.spec is not None and self.day_state is not None
        ok, msg, spread = self._pre_entry_gates()
        if not ok:
            return msg
        tick = self.client.get_tick(self.spec.name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            return "😴 Tick tidak tersedia."
        df_entry = df_stack["entry"]
        if df_entry.empty or len(df_entry) < 2:
            return f"😴 Data {self.tfs.entry} belum cukup."
        last_close = float(df_entry["close"].iloc[-2])
        sig_time = df_entry.index[-2]

        ref, analysis, lbma_text = self._lbma_context()
        if ref is None:
            return f"😴 {lbma_text}"
        ctx, fib, _ = self._crt_and_fib()
        crt_summary = ctx.summary if ctx else "CRT off"
        fund = self._lbma_fundamental()
        fund_line = f"\n[fund] {fund.summary}" if fund is not None else ""

        pending: list[str] = []
        if self.cfg.lbma.enable_touch_entry:
            sig, extra, pend = self._gen_lbma_touch(
                ref, analysis, tick.bid, tick.ask, last_close, ctx, sig_time)
            if sig is not None:
                return self._finalize_signal(
                    sig, spread, f"{lbma_text}\n{extra}\n{crt_summary}{fund_line}", "LBMA")
            pending.append(pend)
        if self.cfg.crt.enable_trend_entry:
            sig, extra, pend = self._gen_crt_fib(
                ctx, fib, tick.bid, tick.ask, last_close, sig_time)
            if sig is not None:
                return self._finalize_signal(
                    sig, spread, f"{extra}\n{crt_summary}\n{lbma_text}{fund_line}", "CRT+FIB")
            pending.append(pend)
        return "😴 " + " | ".join(p for p in pending if p)

    # ------------------------------------------------------------------ #
    def _evaluate_entry_combo(self, df_stack: dict[str, pd.DataFrame]) -> str:
        """Mode 'combo' (UTAMA emas): entry momentum (strategi cent) + LBMA touch +
        CRT/Fib. LBMA & Fibonacci hanya ACUAN untuk jalur momentum (tidak memblok).
        """
        assert self.spec is not None and self.day_state is not None
        ok, msg, spread = self._pre_entry_gates()
        if not ok:
            return msg
        tick = self.client.get_tick(self.spec.name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            return "😴 Tick tidak tersedia."
        bid, ask = tick.bid, tick.ask
        df_entry = df_stack["entry"]
        if df_entry.empty or len(df_entry) < 3:
            return f"😴 Data {self.tfs.entry} belum cukup."
        last_close = float(df_entry["close"].iloc[-2])
        sig_time = df_entry.index[-2]

        # Acuan (TIDAK memblok jalur momentum).
        ref, analysis, lbma_text = self._lbma_context()
        ctx, fib, fib_src = self._crt_and_fib()
        crt_summary = ctx.summary if ctx else "CRT off"
        fund = self._lbma_fundamental()
        ref_extra = (f"[acuan] {lbma_text}\n"
                     f"[acuan] {self._fib_text(fib, fib_src, last_close)}\n"
                     f"[acuan] {crt_summary}")
        if fund is not None:
            ref_extra += f"\n[fund] {fund.summary}"

        pending: list[str] = []
        df_h1 = self.data.get_rates("H1", self.cfg.loop.candles)
        df_m15 = df_stack["trend"]
        df_m5 = df_stack["zone"]

        # GERBANG REGIME (anti-sideways): analisis H1/M15/M5/M3 sekaligus.
        # Bila pasar SIDEWAYS / arah tak sepakat -> blok SEMUA entry.
        # Bila trending -> hanya izinkan sinyal SEARAH tren (regime_dir).
        regime_dir = None
        if self.cfg.regime.enabled:
            df_by_tf = {"H1": df_h1, "M15": df_m15, "M5": df_m5}
            for tf in self.cfg.regime.timeframes:
                if tf not in df_by_tf:
                    df_by_tf[tf] = self.data.get_rates(tf, self.cfg.loop.candles)
            reg = regime_mod.assess(df_by_tf, self.cfg.strategy, self.cfg.regime)
            if reg.direction is None:
                log.info("Entry diblok regime: %s | %s", reg.reason, reg.summary)
                return f"😴 {reg.reason} [{reg.summary}]"
            regime_dir = reg.direction
            ref_extra = f"[regime] {reg.reason}\n{ref_extra}"

        def _wrong_way(sig) -> bool:
            """True bila sinyal melawan arah tren regime (harus dibuang)."""
            return regime_dir is not None and sig.direction != regime_dir

        # GEN 0 (PRIORITAS): Support/Resistance M5/M15/H1 -> entry di M5.
        if self.cfg.sr.enabled:
            sr_map = sr_mod.detect_levels(
                {"M5": df_m5, "M15": df_m15, "H1": df_h1}, self.cfg.sr,
                self.cfg.lbma.pip_size)
            sig, reason = sr_mod.evaluate_sr(
                sr_map, df_m5, bid, ask, self.cfg.sr, self.cfg.lbma.pip_size)
            if sig is not None and _wrong_way(sig):
                pending.append(f"sr: {sig.direction} lawan tren {regime_dir} -> skip")
            elif sig is not None:
                extra = (f"setup: Support/Resistance M5/M15/H1 (entry M5)\n"
                         f"body M5 {sig.body_ratio:.2f}\n{ref_extra}")
                return self._finalize_signal(sig, spread, extra, "S/R")
            else:
                pending.append(f"sr: {reason}")

        # GEN 1: alignment multi-TF H1/M15/M5 + momentum M1 - tanpa perlu LBMA
        # & tanpa menunggu zona. Kalau TF searah -> langsung entry.
        if self.cfg.mtf.enabled:
            sig, reason = strat_mod.evaluate_mtf(
                df_h1, df_stack["trend"], df_stack["zone"], df_stack["entry"],
                self.cfg.strategy, self.cfg.mtf, bid, ask,
            )
            if sig is not None and _wrong_way(sig):
                pending.append(f"mtf: {sig.direction} lawan tren {regime_dir} -> skip")
            elif sig is not None:
                extra = (f"setup: MTF align H1/M15/M5 + momentum M1\n"
                         f"bias {sig.bias} | body {sig.body_ratio:.2f} | "
                         f"RSI {sig.rsi_m1:.1f}\n{ref_extra}")
                return self._finalize_signal(sig, spread, extra, "MTF")
            else:
                pending.append(f"mtf: {reason}")

        # GEN 2: momentum strategi cent (EMA/swing/RSI, pullback ke zona) - tanpa LBMA.
        signal, reason = strat_mod.evaluate(
            df_stack["trend"], df_stack["zone"], df_stack["entry"],
            self.cfg.strategy, bid, ask,
        )
        if signal is not None and _wrong_way(signal):
            pending.append(f"momentum: {signal.direction} lawan tren {regime_dir} -> skip")
        elif signal is not None:
            extra = (f"setup: momentum {self.tfs.entry} (strategi cent)\n"
                     f"bias {signal.bias} | zona {signal.zone:.2f} | "
                     f"body {signal.body_ratio:.2f} | RSI {signal.rsi_m1:.1f}\n{ref_extra}")
            return self._finalize_signal(signal, spread, extra, "MOMENTUM")
        else:
            pending.append(f"momentum: {reason}")

        # GEN 2: LBMA touch (acuan jadi sinyal bila harga menyentuh level).
        if self.cfg.lbma.enable_touch_entry and ref is not None:
            sig, extra, pend = self._gen_lbma_touch(
                ref, analysis, bid, ask, last_close, ctx, sig_time)
            if sig is not None and _wrong_way(sig):
                pending.append(f"lbma: {sig.direction} lawan tren {regime_dir} -> skip")
            elif sig is not None:
                return self._finalize_signal(
                    sig, spread, f"{lbma_text}\n{extra}\n{crt_summary}", "LBMA")
            else:
                pending.append(pend)

        # GEN 3: CRT + Fibonacci trend continuation.
        if self.cfg.crt.enable_trend_entry:
            sig, extra, pend = self._gen_crt_fib(ctx, fib, bid, ask, last_close, sig_time)
            if sig is not None and _wrong_way(sig):
                pending.append(f"crt+fib: {sig.direction} lawan tren {regime_dir} -> skip")
            elif sig is not None:
                return self._finalize_signal(
                    sig, spread, f"{extra}\n{crt_summary}\n{lbma_text}", "CRT+FIB")
            else:
                pending.append(pend)

        return "😴 " + " | ".join(p for p in pending if p)

    def _finalize_signal(
        self, signal: strat_mod.Signal, spread: float, extra: str, kind: str
    ) -> str:
        """Validasi stops + sizing + emit alert + (jika LIVE) eksekusi. Dipakai kedua jalur."""
        assert self.spec is not None

        # Konfirmasi fundamental LBMA (opsional). Hanya memblok bila
        # require_confirmation=true DAN bias fundamental berlawanan arah sinyal.
        if self.cfg.lbma_fund.enabled and self.cfg.lbma_fund.require_confirmation:
            fund = self._lbma_fundamental()
            if fund is not None:
                ok_f, why_f = lbma_fund_mod.confirms(signal.direction, fund, self.cfg.lbma_fund)
                if not ok_f:
                    self.notifier.send(f"⏭️ Sinyal {signal.direction} ({kind}) dilewati: {why_f}")
                    return f"⏭️ Sinyal {signal.direction} dilewati: {why_f}"

        vs = validate_stops(signal, self.spec)
        if not vs.allowed:
            self.notifier.send(f"⏭️ Sinyal {signal.direction} ({kind}) dilewati: {vs.reason}")
            return f"⏭️ Sinyal {signal.direction} dilewati: {vs.reason}"

        sizing = size_position(self.client.equity(), signal.sl_distance, self.spec, self.cfg.risk)
        if not sizing.ok:
            self.notifier.send(
                f"⏭️ Sinyal {signal.direction} ({kind}) @ {signal.entry:.{self.spec.digits}f} "
                f"TIDAK dieksekusi: {sizing.reason}"
            )
            return f"⏭️ Sinyal {signal.direction} tak dieksekusi: {sizing.reason}"

        self._emit_signal_lbma(signal, sizing.lots, sizing.warning, spread, extra, kind)

        if not self.execution_enabled:
            log.info("ALERT-ONLY: order tidak dikirim (mode=%s).", self.mode_str)
            return f"📣 Sinyal {kind} dikirim (ALERT-ONLY, order tidak dikirim)."
        if not self.client.autotrading_enabled():
            msg = ("⚠️ AutoTrading OFF di terminal MT5. Order TIDAK dikirim. "
                   "Aktifkan tombol 'Algo Trading' (jadi hijau) di MT5.")
            log.warning(msg)
            self.notifier.send(msg)
            return msg
        ok = self._execute(signal, sizing.lots)
        return (f"✅ Order {kind} {signal.direction} {sizing.lots} lot terkirim."
                if ok else "❌ Order gagal (lihat alert).")

    def _entries_note(self) -> str:
        """Baris info 'entry sekaligus' untuk alert (kosong bila 1 entry)."""
        n = max(1, int(self.cfg.risk.entries_per_signal))
        if n <= 1:
            return ""
        rrs = self.cfg.management.entry_tp_rrs or []
        if self.cfg.management.auto_tp and rrs:
            rr_txt = " / ".join(f"{rrs[min(i, len(rrs) - 1)]:.2f}" for i in range(n))
            return f"\n🎯 {n} entry SEKALIGUS | TP RR per entry: {rr_txt}"
        return f"\n🎯 {n} entry SEKALIGUS"

    def _emit_signal_lbma(
        self, signal: strat_mod.Signal, lots: float, warning: str, spread: float,
        extra: str, kind: str = "LBMA",
    ) -> None:
        assert self.spec is not None
        d = self.spec.digits
        rr = (abs(signal.tp - signal.entry) / signal.sl_distance) if signal.sl_distance else 0.0
        txt = (
            f"📣 SINYAL {kind} {signal.direction} ({self.mode_str})\n"
            f"simbol: {self.spec.name}\n"
            f"entry≈ {signal.entry:.{d}f} | lot: {lots}\n"
            f"SL: {signal.sl:.{d}f} | TP: {signal.tp:.{d}f}\n"
            f"jarak SL: {signal.sl_distance:.{d}f} | RR≈ {rr:.2f}\n"
            f"spread: {spread:.0f} pts\n"
            f"{extra}"
        )
        txt += self._entries_note()
        if warning:
            txt += f"\n{warning}"
        self.notifier.send(txt)

    # ------------------------------------------------------------------ #
    def _try_immediate_entry(self) -> str:
        """Cek & eksekusi entry SEKARANG (dipakai oleh /confirm_live).

        Melewati gerbang 'satu sinyal per candle' agar bisa langsung bertindak,
        tetapi tetap tunduk pada SEMUA aturan strategi, filter, & circuit breaker.
        """
        if not self.execution_enabled:
            return "Mode bukan LIVE — tidak membuka posisi."
        if not self.client.ensure_connection():
            return "MT5 terputus — tidak bisa cek entry."
        assert self.spec is not None
        symbol = self.spec.name
        if self.position_mgr.get_open_positions(symbol):
            return "Sudah ada posisi terbuka (maks 1) — tidak buka baru."
        df_stack = self.data.get_stack(self.tfs, self.cfg.loop.candles, symbol)
        df_entry = df_stack["entry"]
        if df_entry.empty or len(df_entry) < 3:
            return f"Data {self.tfs.entry} belum cukup — tidak bisa cek."
        # Tandai bar ini sudah diproses agar loop tak dobel-evaluasi bar yang sama.
        self.last_bar_time = str(df_entry.index[-2])
        self.journal.set_state("last_bar_time", self.last_bar_time)
        return self._evaluate_entry(df_stack)

    # ------------------------------------------------------------------ #
    def _split_entry_lots(self, total_lots: float, n: int) -> list[float]:
        """Bagi total lot ke ``n`` entry agar TOTAL risiko ~ target (tak berlipat).

        Tiap entry minimal ``volume_min``. Bila akun terlalu kecil sehingga total
        tak bisa dibagi (sudah di lot minimum), tiap entry tetap ``volume_min``
        -> eksposur jadi ~n x target (di-warning lewat sizing override).
        """
        from core.risk_manager import _round_to_step
        assert self.spec is not None
        step, vmin, vmax = self.spec.volume_step, self.spec.volume_min, self.spec.volume_max
        if n <= 1:
            return [total_lots]
        per = _round_to_step(total_lots / n, step)
        if per < vmin:
            per = vmin
        lots = [min(per, vmax) for _ in range(n)]
        # Sisa pembulatan dilekatkan ke entry pertama.
        remainder = _round_to_step(total_lots - per * n, step)
        if remainder > 0:
            lots[0] = min(_round_to_step(lots[0] + remainder, step), vmax)
        return lots

    def _entry_tp(self, signal: strat_mod.Signal, idx: int, n: int) -> float:
        """TP untuk entry ke-``idx`` (0-based). Single entry -> TP sinyal asli."""
        if not self.cfg.management.auto_tp:
            return 0.0
        rrs = self.cfg.management.entry_tp_rrs or []
        if n <= 1 or not rrs:
            return signal.tp
        rr = rrs[min(idx, len(rrs) - 1)]
        if signal.direction == "BUY":
            return signal.entry + rr * signal.sl_distance
        return signal.entry - rr * signal.sl_distance

    def _execute(self, signal: strat_mod.Signal, lots: float) -> bool:
        """Buka N posisi SEKALIGUS (entries_per_signal). Tiap entry bisa TP berbeda."""
        from dataclasses import replace
        assert self.spec is not None and self.day_state is not None
        n = max(1, int(self.cfg.risk.entries_per_signal))
        lot_list = self._split_entry_lots(lots, n)
        d = self.spec.digits
        sent = 0
        for i in range(n):
            lot_i = lot_list[i]
            tp_i = round(self._entry_tp(signal, i, n), d)
            sig_i = replace(signal, tp=tp_i)
            result = self.executor.open_position(sig_i, lot_i, self.spec)
            if not result.ok:
                self.notifier.send(f"❌ Order entry {i + 1}/{n} GAGAL: {result.comment}")
                continue

            ticket = result.ticket or 0
            self.journal.record_open(TradeRecord(
                ticket=ticket, symbol=self.spec.name, direction=sig_i.direction,
                lots=lot_i, entry=result.price or sig_i.entry, sl=sig_i.sl, tp=sig_i.tp,
                open_time=datetime.now(timezone.utc).isoformat(),
                sl_distance=sig_i.sl_distance, reason=sig_i.reason,
                retcode=result.retcode, magic=self.cfg.magic,
            ))
            self.position_mgr._known_tickets.add(ticket)
            self.day_state.trades_today += 1
            sent += 1
            tp_txt = f"{tp_i:.{d}f}" if tp_i else "-"
            self.notifier.send(
                f"✅ ORDER {i + 1}/{n} TERKIRIM {sig_i.direction} {lot_i} lot @ "
                f"{result.price or sig_i.entry:.{d}f} | TP {tp_txt} ticket={ticket}"
            )
        self.journal.save_day_state(self.day_state)
        return sent > 0

    # ------------------------------------------------------------------ #
    def _emit_signal(self, signal: strat_mod.Signal, lots: float, warning: str, spread: float) -> None:
        assert self.spec is not None
        d = self.spec.digits
        txt = (
            f"📣 SINYAL {signal.direction} ({self.mode_str})\n"
            f"simbol: {self.spec.name}\n"
            f"entry≈ {signal.entry:.{d}f} | lot: {lots}\n"
            f"SL: {signal.sl:.{d}f} | TP: {signal.tp:.{d}f}\n"
            f"jarak SL: {signal.sl_distance:.{d}f} | RR: {self.cfg.strategy.rr_ratio}\n"
            f"bias M15: {signal.bias} | zona M5: {signal.zone:.{d}f}\n"
            f"body_ratio: {signal.body_ratio:.2f} | RSI M1: {signal.rsi_m1:.1f}\n"
            f"spread: {spread:.0f} pts | alasan: {signal.reason}"
        )
        txt += self._entries_note()
        if warning:
            txt += f"\n{warning}"
        self.notifier.send(txt)

    # ------------------------------------------------------------------ #
    def _handle_manage_result(self, manage) -> None:
        assert self.day_state is not None
        for msg in manage.modified:
            log.info(msg)
            self.notifier.send(f"🔧 {msg}")
        for ev in manage.closed:
            if ev.is_win:
                self.day_state.consecutive_losses = 0
            else:
                self.day_state.consecutive_losses += 1
            self.journal.save_day_state(self.day_state)
            self.notifier.send(
                f"🏁 Posisi tutup {ev.direction} ticket={ev.ticket} | "
                f"P/L={ev.profit:.2f} | R={ev.r_multiple:.2f} | "
                f"loss beruntun={self.day_state.consecutive_losses}"
            )
            # Auto-pause jika loss beruntun mencapai batas (§8.2).
            if self.day_state.consecutive_losses >= self.cfg.risk.max_consecutive_losses:
                self.paused = True
                self.day_state.paused = True
                self._save_runtime()
                self.notifier.send(
                    f"🛑 Loss beruntun {self.day_state.consecutive_losses} "
                    f"-> AUTO-PAUSE. Kirim /resume untuk lanjut."
                )

    # ------------------------------------------------------------------ #
    def _heartbeat(self) -> None:
        now = time.time()
        interval = self.cfg.loop.heartbeat_minutes * 60
        if now - self._last_heartbeat >= interval:
            self._last_heartbeat = now
            log.info("[heartbeat] mode=%s equity=%.2f paused=%s",
                     self.mode_str, self.client.equity(), self.paused)

    # ------------------------------------------------------------------ #
    # BARBAR command/runtime helpers
    # ------------------------------------------------------------------ #
    def _run_barbar_cycle(self, notify: bool = False) -> str:
        """Run one BARBAR cycle and optionally notify account-touching events."""
        assert self.spec is not None and self.day_state is not None

        allow_new = True
        block_reason = ""
        cb = check_circuit_breakers(self.day_state, self.client.equity(), self.cfg.risk)
        if not cb.allowed:
            allow_new = False
            block_reason = f"circuit breaker: {cb.reason}"
        else:
            fund = self.fundamentals.is_trading_allowed()
            if not fund.allowed:
                allow_new = False
                block_reason = f"filter berita: {fund.reason}"

        tf = (self.cfg.barbar.timeframe or "M1").upper()
        try:
            df_m1 = self.data.get_rates(tf, self.cfg.loop.candles, self.spec.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("BARBAR gagal ambil data %s: %s", tf, exc)
            df_m1 = pd.DataFrame()
        bar_time = str(df_m1.index[-2]) if not df_m1.empty and len(df_m1) >= 2 else None

        result = self.barbar.cycle(
            self.spec,
            df_m1,
            execution_enabled=self.execution_enabled,
            autotrading_enabled=self.client.autotrading_enabled(),
            allow_new_entries=allow_new,
            block_reason=block_reason,
            bar_time=bar_time,
        )
        self._handle_barbar_result(result, notify=notify)
        self._save_runtime()
        return self._barbar_result_summary(result)

    def _handle_barbar_result(self, result: BarbarCycleResult, notify: bool) -> None:
        assert self.day_state is not None
        if result.opened:
            self.day_state.trades_today += result.opened
            self.journal.save_day_state(self.day_state)
        if result.exit_profit is not None:
            if result.exit_profit >= 0:
                self.day_state.consecutive_losses = 0
            else:
                self.day_state.consecutive_losses += 1
            self.journal.save_day_state(self.day_state)
            if self.day_state.consecutive_losses >= self.cfg.risk.max_consecutive_losses:
                self.paused = True
                self.day_state.paused = True
                self._save_runtime()

        for msg in result.events:
            log.info("BARBAR: %s", msg)
            if notify:
                self.notifier.send(f"BARBAR: {msg}")
        for msg in result.errors:
            log.warning("BARBAR: %s", msg)
            if notify:
                self.notifier.send(f"BARBAR ERROR: {msg}")
        if result.exit_reason and notify:
            profit = result.exit_profit if result.exit_profit is not None else 0.0
            self.notifier.send(
                f"BARBAR basket exit: {result.exit_reason} | P/L={profit:.2f} | "
                f"loss beruntun={self.day_state.consecutive_losses}"
            )

    def _barbar_result_summary(self, result: BarbarCycleResult) -> str:
        parts: list[str] = []
        if result.events:
            parts.extend(result.events)
        if result.errors:
            parts.extend(f"ERROR: {e}" for e in result.errors)
        if result.blocked:
            parts.append(result.blocked)
        if not parts:
            return "no action"
        return " | ".join(parts)

    # ------------------------------------------------------------------ #
    # StraddleM1 command/runtime helpers (berjalan berdampingan dengan BARBAR)
    # ------------------------------------------------------------------ #
    def _run_straddle_cycle(self, notify: bool = False) -> str:
        """Run one StraddleM1 cycle; share the same news/circuit-breaker gate."""
        assert self.spec is not None and self.day_state is not None

        allow_new = True
        block_reason = ""
        cb = check_circuit_breakers(self.day_state, self.client.equity(), self.cfg.risk)
        if not cb.allowed:
            allow_new = False
            block_reason = f"circuit breaker: {cb.reason}"
        else:
            fund = self.fundamentals.is_trading_allowed()
            if not fund.allowed:
                allow_new = False
                block_reason = f"filter berita: {fund.reason}"

        tf = (self.cfg.straddle_m1.timeframe or "M1").upper()
        try:
            df_m1 = self.data.get_rates(tf, self.cfg.loop.candles, self.spec.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("StraddleM1 gagal ambil data %s: %s", tf, exc)
            df_m1 = pd.DataFrame()
        bar_time = str(df_m1.index[-2]) if not df_m1.empty and len(df_m1) >= 2 else None

        result = self.straddle.cycle(
            self.spec,
            df_m1,
            execution_enabled=self.execution_enabled,
            autotrading_enabled=self.client.autotrading_enabled(),
            allow_new_entries=allow_new,
            block_reason=block_reason,
            bar_time=bar_time,
        )
        self._handle_straddle_result(result, notify=notify)
        self._save_runtime()
        return self._barbar_result_summary(result)

    def _handle_straddle_result(self, result: BarbarCycleResult, notify: bool) -> None:
        for msg in result.events:
            log.info("StraddleM1: %s", msg)
            if notify:
                self.notifier.send(f"StraddleM1: {msg}")
        for msg in result.errors:
            log.warning("StraddleM1: %s", msg)
            if notify:
                self.notifier.send(f"StraddleM1 ERROR: {msg}")

    def _cmd_straddle(self, args: list[str]) -> str:
        if not self.spec:
            return "Simbol belum siap."
        action = (args[0].lower() if args else "status").strip()

        if action in {"help", "?"}:
            return (
                "StraddleM1 commands:\n"
                "/straddle status - lihat state machine, posisi & pending\n"
                "/straddle on - aktifkan EA (berdampingan dgn BARBAR/normal)\n"
                "/straddle off - matikan EA (tidak close posisi)\n"
                "/straddle once - jalankan 1 cycle sekarang\n"
                "/straddle close - close posisi & cancel pending StraddleM1\n"
                "Magic terpisah; tanpa TP; 1 posisi; stop-and-reverse otomatis."
            )

        if action in {"status", "info"}:
            state = "ON" if self.straddle_enabled else "OFF"
            return f"StraddleM1: {state} ({self.mode_str})\n{self.straddle.status_text(self.spec)}"

        if action in {"on", "start"}:
            self.straddle_enabled = True
            self._save_runtime()
            status = self._run_straddle_cycle(notify=True)
            live_note = ""
            if not self.execution_enabled:
                live_note = (
                    "\nMode masih ALERT-ONLY: order tidak dikirim sebelum "
                    "EXECUTE=true + /confirm_live."
                )
            return (
                "StraddleM1 ON. Berjalan berdampingan dengan BARBAR/strategi normal "
                "(magic terpisah).\n"
                f"Cycle sekarang: {status}{live_note}"
            )

        if action in {"off", "stop"}:
            self.straddle_enabled = False
            self._save_runtime()
            positions = self.straddle.positions(self.spec.name)
            pending = self.straddle.pending_orders(self.spec.name)
            note = ""
            if positions or pending:
                note = (
                    f"\nMasih ada {len(positions)} posisi dan {len(pending)} pending StraddleM1. "
                    "Gunakan /straddle close untuk menutup/cancel semuanya."
                )
            return f"StraddleM1 OFF (posisi tidak ditutup otomatis).{note}"

        if action in {"once", "run"}:
            status = self._run_straddle_cycle(notify=True)
            return f"StraddleM1 cycle: {status}"

        if action in {"close", "closeall", "kill"}:
            if not self.execution_enabled:
                return "Tidak bisa close otomatis: mode belum LIVE (butuh EXECUTE=true + /confirm_live)."
            if not self.client.autotrading_enabled():
                return "Tidak bisa close otomatis: AutoTrading OFF di terminal MT5."
            result = self.straddle.close_all(self.spec)
            self._handle_straddle_result(result, notify=True)
            self._save_runtime()
            return "StraddleM1 close-all: " + self._barbar_result_summary(result)

        return "Subcommand tidak dikenal. Pakai /straddle help."

    def _cmd_barbar(self, args: list[str]) -> str:
        if not self.spec:
            return "Simbol belum siap."
        action = (args[0].lower() if args else "status").strip()

        if action in {"help", "?"}:
            return (
                "BARBAR commands:\n"
                "/barbar status - lihat basket dan pending order\n"
                "/barbar on - aktifkan Gold M1 hedged-martingale grid\n"
                "/barbar off - matikan mode (tidak close posisi)\n"
                "/barbar once - jalankan 1 cycle sekarang\n"
                "/barbar close - close semua posisi & cancel pending BARBAR\n"
                "/barbar reset - hapus cooldown/marker bar\n"
                "Trailing SL aktif via barbar.trailing_*; TP otomatis via barbar.auto_take_profit"
            )

        if action in {"status", "info"}:
            state = "ON" if self.barbar_enabled else "OFF"
            live = self.mode_str
            return f"BARBAR: {state} ({live})\n{self.barbar.status_text(self.spec)}"

        if action in {"on", "start"}:
            self.barbar_enabled = True
            self._save_runtime()
            status = self._run_barbar_cycle(notify=True)
            live_note = ""
            if not self.execution_enabled:
                live_note = "\nMode masih ALERT-ONLY: order tidak akan dikirim sebelum EXECUTE=true + /confirm_live."
            return (
                "BARBAR ON. Normal entry strategy dihentikan selama mode ini aktif.\n"
                "PERINGATAN: martingale grid sangat agresif; guardrail tetap aktif.\n"
                f"Cycle sekarang: {status}{live_note}"
            )

        if action in {"off", "stop"}:
            self.barbar_enabled = False
            self._save_runtime()
            positions = self.barbar.positions(self.spec.name)
            pending = self.barbar.pending_orders(self.spec.name)
            note = ""
            if positions or pending:
                note = (
                    f"\nMasih ada {len(positions)} posisi dan {len(pending)} pending BARBAR. "
                    "Gunakan /barbar close untuk menutup/cancel semuanya."
                )
            return f"BARBAR OFF. Normal strategy aktif lagi pada candle berikutnya.{note}"

        if action in {"once", "run"}:
            status = self._run_barbar_cycle(notify=True)
            return f"BARBAR cycle: {status}"

        if action == "reset":
            self.barbar.cooldown_until = 0.0
            self.barbar.last_bar_time = None
            self._save_runtime()
            return "BARBAR cooldown dan marker one-position-per-bar direset."

        if action in {"close", "closeall", "kill"}:
            if not self.execution_enabled:
                return "Tidak bisa close otomatis: mode belum LIVE (butuh EXECUTE=true + /confirm_live)."
            if not self.client.autotrading_enabled():
                return "Tidak bisa close otomatis: AutoTrading OFF di terminal MT5."
            profit_before = self.barbar.basket_profit(self.barbar.positions(self.spec.name))
            result = self.barbar.close_all(self.spec, reason="manual")
            result.exit_profit = profit_before
            result.exit_reason = "manual"
            self._handle_barbar_result(result, notify=True)
            self._save_runtime()
            return "BARBAR close-all: " + self._barbar_result_summary(result)

        return "Subcommand tidak dikenal. Pakai /barbar help."

    # ------------------------------------------------------------------ #
    # Telegram command handler
    # ------------------------------------------------------------------ #
    def handle_command(self, cmd: str, args: list[str]) -> str:
        handlers = {
            "start": self._cmd_start, "help": self._cmd_help,
            "login": self._cmd_login_start, "logout": self._cmd_logout,
            "cancel": self._cmd_login_cancel,
            "setpath": lambda: self._cmd_setpath(args),
            "terminals": self._cmd_terminals,
            "__text__": lambda: self._handle_free_text(args),
            "status": self._cmd_status, "positions": self._cmd_positions,
            "balance": self._cmd_balance, "risk": self._cmd_risk,
            "set_risk": lambda: self._cmd_set_risk(args),
            "pause": self._cmd_pause, "resume": self._cmd_resume,
            "rebase": self._cmd_rebase, "reset_day": self._cmd_rebase,
            "stop": self._cmd_stop, "confirm_live": self._cmd_confirm_live,
            "disable_exec": self._cmd_disable_exec, "report": self._cmd_report,
            "lbma": lambda: self._cmd_lbma(args),
            "lbma_fund": self._cmd_lbma_fund, "fund": self._cmd_lbma_fund,
            "lbmaf": self._cmd_lbma_fund,
            "fib": self._cmd_fib, "fibonacci": self._cmd_fib,
            "sr": self._cmd_sr, "support": self._cmd_sr,
            "barbar": lambda: self._cmd_barbar(args),
            "straddle": lambda: self._cmd_straddle(args),
        }
        if not self.ready and cmd in NEEDS_ACCOUNT:
            return "🔒 Belum ada akun MT5 yang login. Kirim /login dulu."
        fn = handlers.get(cmd)
        if fn is None:
            return f"Perintah tidak dikenal: /{cmd}. Ketik /help."
        return fn()

    def _cmd_start(self) -> str:
        """Perkenalan saat /start (pesan sambutan + daftar perintah)."""
        return f"{WELCOME}\n\n{self._cmd_help()}"

    def _cmd_help(self) -> str:
        return (
            "🤖 Bot Trading XAUUSD (acuan LBMA + CRT)\n"
            "/login - login akun MT5 (nomor login, password, server)\n"
            "/logout - putuskan akun MT5 yang sedang login\n"
            "/terminals - lihat terminal MT5 yang terdeteksi di PC\n"
            "/setpath - set path terminal broker (opsional, jarang perlu)\n"
            "/status - mode, acuan LBMA, bias CRT, equity, DD, loss beruntun\n"
            "/lbma - acuan LBMA hari ini + riwayat (hari/bulan)\n"
            "    /lbma YYYY-MM-DD | /lbma YYYY-MM\n"
            "/lbma_fund (/fund) - analisis fundamental LBMA: bias PM vs AM, fib AM/PM, grid\n"
            "/fib - level Fibonacci (golden zone) terkini\n"
            "/sr - peta Support/Resistance M5/M15/H1\n"
            "/barbar - mode Gold M1 hedged-martingale grid\n"
            "/straddle - EA StraddleM1 (straddle + trailing + stop-and-reverse)\n"
            "/positions - posisi terbuka\n"
            "/balance - balance & equity\n"
            "/risk - parameter risiko\n"
            "/set_risk <pct> - ubah risk per trade (mis. /set_risk 1 = 1%)\n"
            "/pause /resume - hentikan/lanjut entry baru\n"
            "/rebase - reset baseline equity hari ini (setelah deposit/tarik dana)\n"
            "/stop - kill switch (matikan eksekusi + pause)\n"
            "/confirm_live - aktifkan eksekusi uang asli (refresh LBMA + cek entry)\n"
            "/disable_exec - kembali ke alert-only\n"
            "/report - ringkasan performa"
        )

    # ------------------------------------------------------------------ #
    # Login akun MT5 via Telegram (flow 3 langkah)
    # ------------------------------------------------------------------ #
    def _cmd_login_start(self) -> str:
        """Mulai flow login: minta nomor login, lalu password, lalu server."""
        self._login_state = {"step": "login", "data": {}}
        return (
            "🔐 LOGIN AKUN MT5\n"
            "Langkah 1/3 — kirim NOMOR LOGIN akun MT5 kamu (angka).\n"
            "Ketik /cancel kapan saja untuk batal."
        )

    def _cmd_login_cancel(self) -> str:
        if self._login_state is None:
            return "Tidak ada proses login yang berjalan."
        self._login_state = None
        return "❌ Proses login dibatalkan."

    def _handle_free_text(self, args: list[str]) -> str:
        """Tangani pesan biasa (non-perintah). Hanya bermakna saat flow /login."""
        if self._login_state is None:
            return ""  # abaikan teks biasa di luar flow login
        text = (args[0] if args else "").strip()
        if not text:
            return ""
        step = self._login_state["step"]
        data = self._login_state["data"]

        if step == "login":
            try:
                data["login"] = int(text.replace(" ", "").replace(",", ""))
            except ValueError:
                return "⚠️ Nomor login harus angka. Kirim ulang, atau /cancel."
            self._login_state["step"] = "password"
            return "Langkah 2/3 — kirim PASSWORD akun MT5."
        if step == "password":
            data["password"] = text
            self._login_state["step"] = "server"
            return ("Langkah 3/3 — kirim NAMA SERVER MT5 "
                    "(mis. Exness-MT5Real, ICMarketsSC-Demo).")
        if step == "server":
            data["server"] = text
            self._login_state = None
            return self._apply_login(data)
        # Step tak dikenal -> reset aman.
        self._login_state = None
        return "⚠️ State login tidak valid, dibatalkan. Mulai lagi dengan /login."

    def _apply_login(self, data: dict) -> str:
        """Terapkan kredensial baru: reconnect MT5 + re-discovery simbol.

        Kredensial hanya dipakai untuk sesi berjalan (tidak ditulis ke disk).
        Bila gagal, kredensial lama dipulihkan agar bot tetap bisa jalan.
        """
        s = self.cfg.secrets
        old = (s.mt5_login, s.mt5_password, s.mt5_server)
        s.mt5_login = data["login"]
        s.mt5_password = data["password"]
        s.mt5_server = data["server"]
        self.ready = False
        try:
            self.client.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            connected = self.client.connect()
        except MT5Unavailable as exc:
            s.mt5_login, s.mt5_password, s.mt5_server = old
            return (f"❌ Login GAGAL: {exc}\n"
                    "Pastikan dijalankan di Windows dengan terminal MT5 terinstall.")
        if not connected:
            err = self.client.last_error_str()
            s.mt5_login, s.mt5_password, s.mt5_server = old
            # Pulihkan sesi lama hanya bila sebelumnya memang ada akun login.
            if old[0]:
                try:
                    if self.client.connect():
                        self._prepare_symbol()
                except Exception:  # noqa: BLE001
                    pass
            n_term = len(self.client.discover_terminal_paths())
            if n_term == 0:
                return (
                    "❌ LOGIN GAGAL — tidak ada terminal MT5 di PC.\n"
                    f"Sebab (MT5): {err}\n\n"
                    "Install MetaTrader 5 SATU kali (versi generic dari "
                    "metatrader5.com sudah cukup untuk SEMUA broker — tidak perlu "
                    "terminal bermerek Finex). Lalu /login lagi."
                    + ("\nKredensial sebelumnya dipulihkan." if old[0] else "")
                )
            return (
                "❌ LOGIN GAGAL\n"
                f"Sebab (MT5): {err}\n"
                f"(sudah dicoba {n_term} terminal MT5 yang terpasang)\n\n"
                "Cek:\n"
                "• Nomor login, password, & server harus PERSIS "
                "(perhatikan huruf besar/kecil, mis. FinexBisnisSolusi-Demo).\n"
                "• Kalau server belum dikenal terminal: buka MT5 sekali → "
                "File → Open an Account → cari nama broker (mis. Finex) → server "
                "ter-cache. Tidak perlu install terminal bermerek broker.\n"
                "• Pastikan ada koneksi internet & akun belum kedaluwarsa.\n"
                "• Lihat terminal terdeteksi: /terminals\n"
                + ("Kredensial sebelumnya dipulihkan." if old[0] else "")
            )

        if not self._prepare_symbol():
            info = self.client.account_info()
            acc = f"{info.login} @ {info.server}" if info else f"{s.mt5_login} @ {s.mt5_server}"
            return (f"⚠️ Login BERHASIL ({acc}) tapi simbol cocok pola "
                    f"'{self.cfg.symbol_pattern}' tidak ditemukan / gagal disiapkan. "
                    "Cek Market Watch MT5.")

        info = self.client.account_info()
        acc_login = info.login if info else s.mt5_login
        acc_server = info.server if info else s.mt5_server
        return (
            "✅ LOGIN BERHASIL\n"
            f"akun: {acc_login}\n"
            f"server: {acc_server}\n"
            f"simbol: {self.spec.name if self.spec else '?'}\n"
            f"{self._equity_line()}\n"
            "ℹ️ Kredensial dipakai untuk sesi ini saja (tidak disimpan permanen)."
        )

    def _cmd_setpath(self, args: list[str]) -> str:
        """Set path terminal64.exe broker tertentu (untuk login lintas-broker).

        Berguna bila server broker tidak dikenal terminal yang sedang berjalan:
        tunjuk ke terminal64.exe milik broker itu, lalu /login.
        """
        if not args:
            cur = self.cfg.secrets.mt5_path or "(kosong → attach ke terminal yang berjalan)"
            return (
                "🗂️ PATH TERMINAL MT5\n"
                f"Sekarang: {cur}\n\n"
                "Set: /setpath <path ke terminal64.exe>\n"
                "Contoh: /setpath C:\\Program Files\\FINEX MT5\\terminal64.exe\n"
                "Kosongkan: /setpath clear"
            )
        path = " ".join(args).strip().strip('"')
        if path.lower() == "clear":
            self.cfg.secrets.mt5_path = ""
            return "✅ Path terminal dikosongkan (akan attach ke terminal yang berjalan). Lalu /login."
        self.cfg.secrets.mt5_path = path
        return (f"✅ Path terminal di-set:\n{path}\n"
                "Sekarang kirim /login untuk masuk ke akun di terminal broker ini.")

    def _cmd_terminals(self) -> str:
        """Tampilkan terminal MT5 yang terdeteksi di PC (untuk /login otomatis)."""
        paths = self.client.discover_terminal_paths()
        if not paths:
            return (
                "🖥️ Tidak ada terminal MT5 terdeteksi di PC.\n"
                "Install MetaTrader 5 SATU kali saja — versi generic dari "
                "metatrader5.com sudah cukup untuk SEMUA broker (tidak perlu "
                "terminal bermerek tiap broker). Lalu kirim /login."
            )
        lines = [f"🖥️ {len(paths)} terminal MT5 terdeteksi:"]
        lines += [f"• {p}" for p in paths]
        lines.append("\nSaat /login, bot otomatis memilih terminal yang mengenal "
                     "server broker kamu — tidak perlu sebut path.")
        return "\n".join(lines)

    def _cmd_logout(self) -> str:
        """Putuskan koneksi akun MT5 yang sedang login."""
        self._login_state = None
        self.ready = False
        try:
            self.client.shutdown()
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ Gagal shutdown koneksi: {exc}"
        # Matikan eksekusi demi keamanan setelah logout.
        self.live_confirmed = False
        self.paused = True
        if self.day_state:
            self.day_state.paused = True
        self._save_runtime()
        return ("👋 Logout: koneksi MT5 diputus & eksekusi dimatikan.\n"
                "Gunakan /login untuk masuk lagi dengan akun lain.")

    def _cmd_status(self) -> str:
        ds = self.day_state
        dd = ds.drawdown_pct(self.client.equity()) * 100 if ds else 0.0
        # Baris bias bergantung mode strategi.
        bias_line = self._status_bias_line()
        return (
            f"📊 STATUS\n"
            f"mode: {self.mode_str} (EXECUTE={str(self.cfg.secrets.execute).lower()}, "
            f"live_confirmed={self.live_confirmed})\n"
            f"strategi: {self.cfg.strategy_mode}\n"
            f"barbar: {'ON' if self.barbar_enabled else 'OFF'}\n"
            f"simbol: {self.spec.name if self.spec else '?'}\n"
            f"TF: {self.tfs.trend}→{self.tfs.zone}→{self.tfs.entry}\n"
            f"{self._regime_line()}\n"
            f"{bias_line}\n"
            f"equity: {self.client.equity():.2f} {self._currency()}\n"
            f"DD harian: {dd:.2f}%\n"
            f"loss beruntun: {ds.consecutive_losses if ds else 0}\n"
            f"trade hari ini: {ds.trades_today if ds else 0}/{self.cfg.risk.max_trades_per_day}\n"
            f"paused: {self.paused}"
        )

    def _regime_line(self) -> str:
        """Ringkasan rezim pasar (anti-sideways) untuk /status."""
        if not self.cfg.regime.enabled:
            return "regime: OFF (filter sideways nonaktif)"
        try:
            df_by_tf = {
                tf: self.data.get_rates(tf, self.cfg.loop.candles)
                for tf in self.cfg.regime.timeframes
            }
            reg = regime_mod.assess(df_by_tf, self.cfg.strategy, self.cfg.regime)
            if reg.direction is None:
                return f"regime: ⛔ {reg.reason}\n  [{reg.summary}]"
            return f"regime: ✅ {reg.reason}"
        except Exception as exc:  # noqa: BLE001
            return f"regime: (gagal nilai: {exc})"

    def _status_bias_line(self) -> str:
        if self.cfg.strategy_mode in ("lbma", "combo"):
            if not self.lbma.has_data():
                line = "LBMA: data belum ada (jalankan /lbma)"
            else:
                latest = self.lbma.latest_date()
                am, pm = self.lbma.get(latest) if latest else (None, None)
                ref = self.lbma.reference_for(latest) if latest else None
                line = f"LBMA {latest}: AM={_fmt_px(am)} PM={_fmt_px(pm)}"
                if ref:
                    line += f" | acuan {ref.level_name}={ref.level:.2f}"
                if self.lbma_markers:
                    line += f"\nmarker di-set: {self.lbma_markers.get('date')} (AM/PM ✅)"
            try:
                ctx, _, _ = self._crt_and_fib()
                if ctx is not None:
                    line += f"\nCRT bias: {ctx.bias_str()}"
            except Exception:  # noqa: BLE001
                pass
            try:
                fund = self._lbma_fundamental()
                if fund is not None:
                    line += (f"\nFund LBMA: {fund.bias_str()} "
                             f"(PM>AM {fund.pm_gt_am_streak}h/PM<AM {fund.am_gt_pm_streak}h)")
            except Exception:  # noqa: BLE001
                pass
            return line
        # legacy
        bias = "?"
        try:
            df_trend = self.data.get_rates(self.tfs.trend, self.cfg.loop.candles)
            if not df_trend.empty:
                bias = strat_mod.compute_bias(df_trend, self.cfg.strategy)
        except Exception:  # noqa: BLE001
            pass
        return f"bias {self.tfs.trend}: {bias}"

    def _cmd_positions(self) -> str:
        if not self.spec:
            return "Simbol belum siap."
        positions = self.position_mgr.get_open_positions(self.spec.name)
        if not positions:
            return "Tidak ada posisi terbuka."
        d = self.spec.digits
        lines = ["📈 POSISI TERBUKA:"]
        for p in positions:
            side = "BUY" if p.type == 0 else "SELL"
            lines.append(
                f"#{p.ticket} {side} {p.volume} lot @ {p.price_open:.{d}f} "
                f"SL={p.sl:.{d}f} TP={p.tp:.{d}f} P/L={p.profit:.2f}"
            )
        return "\n".join(lines)

    def _cmd_balance(self) -> str:
        info = self.client.account_info()
        if not info:
            return "account_info tidak tersedia."
        txt = (
            f"💰 BALANCE\n"
            f"balance: {info.balance:.2f} {info.currency}\n"
            f"equity: {info.equity:.2f} {info.currency}"
        )
        if self.cfg.cent_account:
            txt += f"\n⚠️ Akun CENT: angka dalam sen. ~{info.equity/100:.2f} unit mata uang riil."
        return txt

    def _cmd_risk(self) -> str:
        r = self.cfg.risk
        sizing = (f"lot tetap {r.fixed_lot}" if r.position_sizing_mode == "fixed"
                  else f"risk {r.risk_per_trade*100:.2f}%/trade")
        return (
            f"⚙️ RISIKO\n"
            f"sizing: {r.position_sizing_mode} ({sizing})\n"
            f"risk_per_trade: {r.risk_per_trade*100:.2f}%\n"
            f"max_daily_loss: {r.max_daily_loss_pct*100:.1f}%\n"
            f"max_consecutive_losses: {r.max_consecutive_losses}\n"
            f"max_trades_per_day: {r.max_trades_per_day}\n"
            f"max_spread_points: {r.max_spread_points}\n"
            f"allow_min_lot_override: {r.allow_min_lot_override}"
        )

    def _cmd_set_risk(self, args: list[str]) -> str:
        if not args:
            return "Pakai: /set_risk <pct>  (mis. /set_risk 1 untuk 1%)"
        try:
            pct = float(args[0])
        except ValueError:
            return f"Nilai tidak valid: {args[0]}"
        fraction = pct / 100.0
        self.cfg.risk.risk_per_trade = fraction
        msg = f"risk_per_trade -> {pct:.2f}% ({fraction})"
        warn = high_risk_warning(self.cfg.risk)
        if warn:
            msg += f"\n{warn}"
        return msg

    def _cmd_pause(self) -> str:
        self.paused = True
        if self.day_state:
            self.day_state.paused = True
        self._save_runtime()
        return "⏸️ Entry baru DIHENTIKAN (posisi tetap dikelola)."

    def _cmd_resume(self) -> str:
        self.paused = False
        if self.day_state:
            self.day_state.paused = False
            self.day_state.consecutive_losses = 0
        self._save_runtime()
        return "▶️ Resume. Loss beruntun direset ke 0."

    def _cmd_rebase(self) -> str:
        """Re-baseline equity awal hari ke equity SEKARANG.

        Pakai setelah deposit/tarik dana agar circuit breaker daily-loss tidak
        salah-blokir (penurunan equity karena tarik dana, bukan rugi trading).
        """
        eq = self.client.equity()
        old = self.day_state.start_equity if self.day_state else 0.0
        if self.day_state:
            self.day_state.start_equity = eq
            self.day_state.consecutive_losses = 0
            self.day_state.paused = False
        self.paused = False
        self._save_runtime()
        if self.day_state:
            self.journal.save_day_state(self.day_state)
        return (f"🔄 Baseline harian di-reset: start_equity {old:.2f} → {eq:.2f}. "
                f"DD harian = 0% (cocok setelah deposit/tarik dana). "
                f"Circuit breaker daily-loss bersih, entry diizinkan lagi.")

    def _cmd_stop(self) -> str:
        self.live_confirmed = False
        self.paused = True
        if self.day_state:
            self.day_state.paused = True
        self._save_runtime()
        return "🛑 KILL SWITCH: eksekusi dimatikan + pause. (/resume + /confirm_live untuk live lagi)"

    def _cmd_confirm_live(self) -> str:
        if not self.cfg.secrets.execute:
            return ("❌ EXECUTE=false di .env. Tidak bisa live. "
                    "Set EXECUTE=true lalu restart, baru /confirm_live.")
        self.live_confirmed = True
        self.paused = False
        if self.day_state:
            self.day_state.paused = False
        self._save_runtime()

        # Scrape/refresh acuan LBMA saat konfirmasi live + SET marker AM/PM otomatis.
        lbma_line = ""
        if self.cfg.strategy_mode == "lbma" and self.cfg.lbma.enabled:
            self._refresh_lbma_safe()
            lbma_line = self._set_lbma_markers() + "\n"

        # Langsung cek entry sekarang (buka posisi jika ada sinyal valid).
        try:
            status = self._try_immediate_entry()
        except Exception as exc:  # noqa: BLE001 - jangan biarkan crash handler
            log.exception("Immediate entry error")
            status = f"⚠️ Gagal cek entry langsung: {exc}"
        return (f"🔴 LIVE AKTIF (mode={self.mode_str}). Order uang asli akan dikirim "
                f"saat sinyal valid.\n{lbma_line}Cek entry sekarang → {status}")

    def _cmd_disable_exec(self) -> str:
        self.live_confirmed = False
        self._save_runtime()
        return "🟢 Kembali ke ALERT-ONLY."

    def _cmd_report(self) -> str:
        s = self.journal.performance_summary()
        if s.get("trades", 0) == 0:
            return "📑 Belum ada trade tertutup."
        pf = s["profit_factor"]
        pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
        return (
            f"📑 REPORT\n"
            f"trades: {s['trades']} (W:{s['wins']} L:{s['losses']})\n"
            f"win rate: {s['win_rate']:.1f}%\n"
            f"net P/L: {s['net_profit']:.2f}\n"
            f"avg R: {s['avg_r']:.2f}\n"
            f"profit factor: {pf_str}"
        )

    # ------------------------------------------------------------------ #
    # /LBMA - cek acuan LBMA hari ini & riwayat (hari/bulan/tahun)
    # ------------------------------------------------------------------ #
    def _cmd_lbma(self, args: list[str]) -> str:
        if not self.cfg.lbma.enabled:
            return "LBMA nonaktif (set lbma.enabled=true di config)."
        try:
            self.lbma.ensure_fresh(self.cfg.lbma.history_months)
        except Exception as exc:  # noqa: BLE001
            log.warning("Refresh LBMA (/lbma) gagal: %s", exc)
        if not self.lbma.has_data():
            return "Data LBMA belum tersedia (cek koneksi internet)."

        if args:
            q = args[0].strip()
            try:
                d = date.fromisoformat(q)
                return self._lbma_day_report(d)
            except ValueError:
                pass
            if len(q) == 7 and q[4] == "-":  # YYYY-MM
                return self._lbma_month_report(q)
            return "Format: /lbma | /lbma YYYY-MM-DD | /lbma YYYY-MM"
        return self._lbma_overview()

    def _lbma_overview(self) -> str:
        latest = self.lbma.latest_date()
        am, pm = self.lbma.get(latest)
        ref = self.lbma.reference_for(latest)
        analysis = self.lbma.analyze_for(latest)
        lines = ["🏷️ LBMA GOLD (USD/oz)"]
        lines.append(f"Terbaru {latest}: AM={_fmt_px(am)} PM={_fmt_px(pm)}")
        if ref:
            rule = "AM>PM→PM" if (am and pm and am > pm) else (
                "PM>AM→AM" if (am and pm and pm > am) else "AM=PM→AM")
            lines.append(f"Level acuan: {ref.level_name}={ref.level:.2f} "
                         f"({rule}) | SL {ref.sl_pips:.0f}p")
        lines.append("Status: " + (("⛔ " + analysis.reason) if analysis.blocked
                                    else ("✅ " + analysis.reason)))

        # Hint arah dari harga terkini vs level acuan.
        if ref and self.spec:
            tick = self.client.get_tick(self.spec.name)
            if tick and tick.bid > 0:
                px = (tick.bid + tick.ask) / 2.0
                if px < ref.level:
                    lines.append(f"Harga {px:.2f} DI BAWAH level → tunggu naik ke "
                                 f"{ref.level:.2f} → SELL")
                elif px > ref.level:
                    lines.append(f"Harga {px:.2f} DI ATAS level → tunggu turun ke "
                                 f"{ref.level:.2f} → BUY")
                else:
                    lines.append(f"Harga {px:.2f} tepat di level (tunggu)")

        lines.append("")
        lines.append("📅 10 hari terakhir (AM | PM):")
        for iso, a, p in reversed(self.lbma.recent(10)):
            lines.append(f"{iso}: {_fmt_px(a)} | {_fmt_px(p)}")

        monthly = self.lbma.monthly_summary(self.cfg.lbma.history_months)
        if monthly:
            lines.append("")
            lines.append("🗓️ Per bulan (avg | min-max | n):")
            for m in monthly:
                lines.append(f"{m['month']}: {m['avg']:.1f} | "
                             f"{m['min']:.1f}-{m['max']:.1f} | {m['n']}")
        return "\n".join(lines)

    def _lbma_day_report(self, d: date) -> str:
        am, pm = self.lbma.get(d)
        if am is None and pm is None:
            return f"Tidak ada data LBMA untuk {d.isoformat()}."
        ref = self.lbma.reference_for(d)
        analysis = self.lbma.analyze_for(d)
        lines = [f"🏷️ LBMA {d.isoformat()}", f"AM={_fmt_px(am)} | PM={_fmt_px(pm)}"]
        if ref:
            lines.append(f"Level acuan: {ref.level_name}={ref.level:.2f} (SL {ref.sl_pips:.0f}p)")
        lines.append("Status: " + (("⛔ " + analysis.reason) if analysis.blocked
                                    else ("✅ " + analysis.reason)))
        return "\n".join(lines)

    def _lbma_month_report(self, ym: str) -> str:
        try:
            y, m = int(ym[:4]), int(ym[5:7])
            start = date(y, m, 1)
            end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
            end = date.fromordinal(end.toordinal() - 1)
        except ValueError:
            return "Format bulan salah. Pakai YYYY-MM."
        rows = self.lbma.range(start, end)
        if not rows:
            return f"Tidak ada data LBMA untuk {ym}."
        vals = [v for _, a, p in rows for v in (a, p) if v is not None]
        lines = [f"🗓️ LBMA {ym} (AM | PM) — {len(rows)} hari"]
        if vals:
            lines.append(f"avg={sum(vals)/len(vals):.1f} | min={min(vals):.1f} | max={max(vals):.1f}")
        lines.append("")
        for iso, a, p in rows:
            lines.append(f"{iso}: {_fmt_px(a)} | {_fmt_px(p)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # /lbma_fund - analisis fundamental-teknikal LBMA (bias PM vs AM, fib AM/PM)
    # ------------------------------------------------------------------ #
    def _cmd_lbma_fund(self) -> str:
        if not self.cfg.lbma_fund.enabled:
            return "Analisis fundamental LBMA nonaktif (set lbma_fund.enabled=true)."
        try:
            self.lbma.ensure_fresh(self.cfg.lbma.history_months)
        except Exception as exc:  # noqa: BLE001
            log.warning("Refresh LBMA (/lbma_fund) gagal: %s", exc)
        fund = self._lbma_fundamental()
        if fund is None:
            return "Data LBMA belum tersedia (cek koneksi internet)."

        bias_icon = "🟢" if fund.bias > 0 else ("🔴" if fund.bias < 0 else "⚪")
        lines = [
            f"📊 FUNDAMENTAL LBMA (tgl {fund.ref_date})",
            "AM = buka sesi London | PM = tutup sesi London",
            f"{bias_icon} BIAS: {fund.bias_str()}",
            f"→ {fund.interpretation}",
            f"streak: PM>AM {fund.pm_gt_am_streak}h | PM<AM {fund.am_gt_pm_streak}h"
            f"{' | PM higher-high ✅' if fund.pm_higher_high else ''}",
        ]

        m = fund.latest
        if m is not None:
            lines.append("")
            lines.append(
                f"Terbaru {m.date}: AM={_fmt_px(m.am)} PM={_fmt_px(m.pm)} "
                f"| Δ={_fmt_px(m.delta)} ({m.pct:.2f}% | {m.status})"
                if m.pct is not None else
                f"Terbaru {m.date}: AM={_fmt_px(m.am)} PM={_fmt_px(m.pm)} | {m.status}"
            )
            if m.rasio is not None:
                lines.append(f"RASIO PM/AM: {m.rasio:.2f}%")
            if m.grid:
                offs = self.cfg.lbma_fund.grid_offsets
                grid_txt = " | ".join(f"-{int(o)}:{g:.2f}" for o, g in zip(offs, m.grid))
                lines.append(f"Grid akumulasi (AM-offset): {grid_txt}")

        win = self.cfg.lbma_fund.fib_window_days
        if fund.fib_am is not None:
            lines.append("")
            lines.append(f"📐 FIBBO AM (jendela {win}h, {fund.fib_am.low:.2f}-{fund.fib_am.high:.2f}):")
            for r in lbma_fund_mod.FIB_RATIOS:
                lines.append(f"  {r*100:.1f}%: {fund.fib_am.levels[r]:.2f}")
        if fund.fib_pm is not None:
            lines.append(f"📐 FIBBO PM (jendela {win}h, {fund.fib_pm.low:.2f}-{fund.fib_pm.high:.2f}):")
            for r in lbma_fund_mod.FIB_RATIOS:
                lines.append(f"  {r*100:.1f}%: {fund.fib_pm.levels[r]:.2f}")

        if fund.titik50_am is not None:
            lines.append("")
            lines.append(f"Titik50 (rata2) AM={fund.titik50_am:.2f} PM={_fmt_px(fund.titik50_pm)}")
        if fund.median_am is not None:
            lines.append(f"Median AM={fund.median_am:.2f} PM={_fmt_px(fund.median_pm)}")

        recent = fund.daily[-self.cfg.lbma_fund.recent_days:]
        if recent:
            lines.append("")
            lines.append("📅 Harian (AM | PM | Δ | status):")
            for d in reversed(recent):
                delta_txt = f"{d.delta:+.2f}" if d.delta is not None else "-"
                lines.append(f"{d.date}: {_fmt_px(d.am)} | {_fmt_px(d.pm)} | {delta_txt} | {d.status}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # /fib - level Fibonacci terkini (golden zone)
    # ------------------------------------------------------------------ #
    def _cmd_fib(self) -> str:
        if not self.cfg.fib.enabled:
            return "Fibonacci nonaktif (set fib.enabled=true)."
        if not self.spec:
            return "Simbol belum siap."
        _, fib, src = self._crt_and_fib()
        if fib is None or fib.rng <= 0:
            return "Fib: leg swing belum terdeteksi (data kurang)."

        gz_lo, gz_hi = fib.golden_zone(self.cfg.fib)
        lines = [
            f"📐 FIBONACCI ({src})",
            f"leg: {fib.low:.2f} → {fib.high:.2f} (dir {'UP' if fib.direction > 0 else 'DOWN'})",
            f"golden zone: {gz_lo:.2f} - {gz_hi:.2f}",
            "retracement:",
        ]
        for r in fibonacci.RETR_RATIOS:
            lines.append(f"  {r:.3f}: {fib.levels[r]:.2f}")
        lines.append("extension:")
        for r in fibonacci.EXT_RATIOS:
            lines.append(f"  {r:.3f}: {fib.ext[r]:.2f}")

        tick = self.client.get_tick(self.spec.name)
        if tick and tick.bid > 0:
            px = (tick.bid + tick.ask) / 2.0
            nr, npx, _ = fib.nearest(px)
            inz = "✅ DI golden zone" if fib.in_golden_zone(px, self.cfg.fib) else "di luar GZ"
            lines.append(f"harga {px:.2f}: {inz} | fib terdekat {nr:.3f} @ {npx:.2f}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # /sr - peta Support/Resistance M5/M15/H1
    # ------------------------------------------------------------------ #
    def _cmd_sr(self) -> str:
        if not self.cfg.sr.enabled:
            return "Support/Resistance nonaktif (set sr.enabled=true)."
        if not self.spec:
            return "Simbol belum siap."
        candles = self.cfg.loop.candles
        dfs = {
            "M5": self.data.get_rates("M5", candles),
            "M15": self.data.get_rates("M15", candles),
            "H1": self.data.get_rates("H1", candles),
        }
        sr = sr_mod.detect_levels(dfs, self.cfg.sr, self.cfg.lbma.pip_size)
        if not sr.supports and not sr.resistances:
            return "S/R: belum ada level terdeteksi (data kurang)."
        tick = self.client.get_tick(self.spec.name)
        px = (tick.bid + tick.ask) / 2.0 if tick and tick.bid > 0 else None
        lines = ["🧱 SUPPORT/RESISTANCE (M5/M15/H1)"]
        if px is not None:
            lines.append(f"harga: {px:.2f}")
        # Resistance di atas harga (terdekat dulu), lalu support di bawah.
        res = sorted(sr.resistances, key=lambda lv: lv.price, reverse=True)
        sup = sorted(sr.supports, key=lambda lv: lv.price, reverse=True)
        lines.append("— Resistance —")
        for lv in res[-6:] if len(res) > 6 else res:
            mark = " ⟵" if px is not None and abs(lv.price - px) <= self.cfg.sr.touch_pips * self.cfg.lbma.pip_size else ""
            lines.append(f"  {lv.price:.2f} [{','.join(sorted(set(lv.tfs)))}] str{lv.strength}{mark}")
        lines.append("— Support —")
        for lv in sup[:6]:
            mark = " ⟵" if px is not None and abs(lv.price - px) <= self.cfg.sr.touch_pips * self.cfg.lbma.pip_size else ""
            lines.append(f"  {lv.price:.2f} [{','.join(sorted(set(lv.tfs)))}] str{lv.strength}{mark}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def shutdown(self) -> None:
        self._running = False
        self._save_runtime()
        self.client.shutdown()
        self.journal.close()
        log.info("Bot berhenti.")


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Bot Scalping BTCUSD (MT5 + Telegram)")
    parser.add_argument("--config", default=None, help="path config.yaml")
    parser.add_argument("--env", default=None, help="path .env")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(config_path=args.config, env_path=args.env)
    log.info("Konfigurasi dimuat. Secrets: %s", cfg.secrets.redacted())

    bot = TradingBot(cfg)
    try:
        if not bot.setup():
            log.error("Setup gagal. Keluar.")
            return
        bot.run()
    except KeyboardInterrupt:
        log.info("Interrupt diterima.")
    except MT5Unavailable as exc:
        log.error("MT5 tidak tersedia: %s", exc)
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()
