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
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pandas as pd

from core import strategy as strat_mod
from core.config import AppConfig, load_config
from core.executor import Executor
from core.fundamentals import FundamentalsFilter
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
        )
        self.fundamentals = FundamentalsFilter(cfg.fundamentals, cfg.secrets.news_api_key)
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
        self._last_heartbeat = 0.0
        self.day_state: DayState | None = self.journal.load_day_state()
        self._running = True

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

        if not self.client.connect():
            log.error("Gagal connect MT5.")
            return False
        symbol = self.client.discover_symbol()
        if not symbol:
            log.error("Simbol BTCUSD tidak ditemukan.")
            return False
        self.spec = self.client.get_symbol_spec(symbol)
        log.info("Spesifikasi simbol: %s", self.spec)

        self.position_mgr.reconcile(symbol)
        self.reset_day_if_needed()

        warn = high_risk_warning(self.cfg.risk)
        if warn:
            log.warning(warn)
            self.notifier.send(warn)

        if self.execution_enabled and not self.client.autotrading_enabled():
            self.notifier.send(
                "⚠️ Mode LIVE tapi AutoTrading di terminal MT5 OFF. Order tidak akan "
                "terkirim sampai kamu klik tombol 'Algo Trading' (hijau) di MT5."
            )

        self.notifier.send(
            f"🤖 Bot start | mode={self.mode_str} | simbol={symbol}\n"
            f"TF: trend {self.tfs.trend} → zona {self.tfs.zone} → entry {self.tfs.entry}\n"
            f"equity={self.client.equity():.2f} {self._currency()} "
            f"(akun cent: ~{self.client.equity()/100:.2f} unit mata uang)\n"
            f"EXECUTE={str(self.cfg.secrets.execute).lower()} | "
            f"live_confirmed={self.live_confirmed} | paused={self.paused}"
        )
        return True

    def _currency(self) -> str:
        info = self.client.account_info()
        return info.currency if info else "?"

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
        # 1. Perintah Telegram.
        self.tg.poll_and_process()

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

        self._evaluate_entry(df_stack)
        self._heartbeat()

    # ------------------------------------------------------------------ #
    def _evaluate_entry(self, df_stack: dict[str, pd.DataFrame]) -> str:
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
    def _execute(self, signal: strat_mod.Signal, lots: float) -> bool:
        assert self.spec is not None and self.day_state is not None
        result = self.executor.open_position(signal, lots, self.spec)
        if not result.ok:
            self.notifier.send(f"❌ Order GAGAL: {result.comment}")
            return False

        ticket = result.ticket or 0
        self.journal.record_open(TradeRecord(
            ticket=ticket, symbol=self.spec.name, direction=signal.direction,
            lots=lots, entry=result.price or signal.entry, sl=signal.sl, tp=signal.tp,
            open_time=datetime.now(timezone.utc).isoformat(),
            sl_distance=signal.sl_distance, reason=signal.reason,
            retcode=result.retcode, magic=self.cfg.magic,
        ))
        self.position_mgr._known_tickets.add(ticket)
        self.day_state.trades_today += 1
        self.journal.save_day_state(self.day_state)
        self.notifier.send(
            f"✅ ORDER TERKIRIM {signal.direction} {lots} lot @ "
            f"{result.price or signal.entry:.{self.spec.digits}f} ticket={ticket}"
        )
        return True

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
    # Telegram command handler (§12.2)
    # ------------------------------------------------------------------ #
    def handle_command(self, cmd: str, args: list[str]) -> str:
        handlers = {
            "start": self._cmd_help, "help": self._cmd_help,
            "status": self._cmd_status, "positions": self._cmd_positions,
            "balance": self._cmd_balance, "risk": self._cmd_risk,
            "set_risk": lambda: self._cmd_set_risk(args),
            "pause": self._cmd_pause, "resume": self._cmd_resume,
            "stop": self._cmd_stop, "confirm_live": self._cmd_confirm_live,
            "disable_exec": self._cmd_disable_exec, "report": self._cmd_report,
        }
        fn = handlers.get(cmd)
        if fn is None:
            return f"Perintah tidak dikenal: /{cmd}. Ketik /help."
        return fn()

    def _cmd_help(self) -> str:
        return (
            "🤖 Bot Scalping BTCUSD\n"
            "/status - mode, bias, equity, DD, loss beruntun\n"
            "/positions - posisi terbuka\n"
            "/balance - balance & equity (cent)\n"
            "/risk - parameter risiko\n"
            "/set_risk <pct> - ubah risk per trade (mis. /set_risk 1 = 1%)\n"
            "/pause /resume - hentikan/lanjut entry baru\n"
            "/stop - kill switch (matikan eksekusi + pause)\n"
            "/confirm_live - aktifkan eksekusi uang asli\n"
            "/disable_exec - kembali ke alert-only\n"
            "/report - ringkasan performa"
        )

    def _cmd_status(self) -> str:
        bias = "?"
        try:
            df_trend = self.data.get_rates(self.tfs.trend, self.cfg.loop.candles)
            if not df_trend.empty:
                bias = strat_mod.compute_bias(df_trend, self.cfg.strategy)
        except Exception:  # noqa: BLE001
            pass
        ds = self.day_state
        dd = ds.drawdown_pct(self.client.equity()) * 100 if ds else 0.0
        return (
            f"📊 STATUS\n"
            f"mode: {self.mode_str} (EXECUTE={str(self.cfg.secrets.execute).lower()}, "
            f"live_confirmed={self.live_confirmed})\n"
            f"simbol: {self.spec.name if self.spec else '?'}\n"
            f"TF: {self.tfs.trend}→{self.tfs.zone}→{self.tfs.entry}\n"
            f"bias {self.tfs.trend}: {bias}\n"
            f"equity: {self.client.equity():.2f} {self._currency()}\n"
            f"DD harian: {dd:.2f}%\n"
            f"loss beruntun: {ds.consecutive_losses if ds else 0}\n"
            f"trade hari ini: {ds.trades_today if ds else 0}/{self.cfg.risk.max_trades_per_day}\n"
            f"paused: {self.paused}"
        )

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
        return (
            f"💰 BALANCE\n"
            f"balance: {info.balance:.2f} {info.currency}\n"
            f"equity: {info.equity:.2f} {info.currency}\n"
            f"⚠️ Akun CENT: angka dalam sen. ~{info.equity/100:.2f} unit mata uang riil."
        )

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

        # Langsung cek entry sekarang (buka posisi jika ada sinyal valid).
        try:
            status = self._try_immediate_entry()
        except Exception as exc:  # noqa: BLE001 - jangan biarkan crash handler
            log.exception("Immediate entry error")
            status = f"⚠️ Gagal cek entry langsung: {exc}"
        return (f"🔴 LIVE AKTIF (mode={self.mode_str}). Order uang asli akan dikirim "
                f"saat sinyal valid.\nCek entry sekarang → {status}")

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
