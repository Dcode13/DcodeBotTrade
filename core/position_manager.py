"""Manajemen posisi: break-even, trailing, deteksi tutup, reconcile (§11).

Hanya menyentuh posisi milik bot (filter ``magic``). Saat posisi tertutup,
P/L realisasi diambil dari ``history_deals_get`` untuk meng-update
``consecutive_losses`` & journal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from core import indicators
from core.config import ManagementConfig, StrategyConfig
from core.executor import Executor
from core.journal import Journal
from core.mt5_client import MT5Client, _require_mt5
from core.risk_manager import SymbolSpec

log = logging.getLogger(__name__)


@dataclass
class CloseEvent:
    ticket: int
    direction: str
    profit: float
    r_multiple: float
    exit_price: float
    is_win: bool


@dataclass
class ManageResult:
    closed: list[CloseEvent] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)  # pesan break-even/trailing


class PositionManager:
    def __init__(
        self,
        client: MT5Client,
        executor: Executor,
        journal: Journal,
        mgmt: ManagementConfig,
        strat: StrategyConfig,
        magic: int,
        pip_size: float = 0.1,
    ) -> None:
        self.client = client
        self.executor = executor
        self.journal = journal
        self.mgmt = mgmt
        self.strat = strat
        self.magic = magic
        self.pip_size = pip_size
        self._known_tickets: set[int] = set()

    # ------------------------------------------------------------------ #
    def get_open_positions(self, symbol: str) -> list[Any]:
        m = _require_mt5()
        positions = m.positions_get(symbol=symbol)
        if positions is None:
            return []
        return [p for p in positions if p.magic == self.magic]

    # ------------------------------------------------------------------ #
    def reconcile(self, symbol: str) -> None:
        """Sinkronkan state internal dengan posisi nyata saat restart (§11)."""
        live = self.get_open_positions(symbol)
        live_tickets = {p.ticket for p in live}
        self._known_tickets = set(live_tickets)

        # Posisi yang journal-nya OPEN tapi sudah tak ada -> finalisasi.
        for tr in self.journal.get_open_trades():
            if tr["ticket"] not in live_tickets:
                self._finalize_closed(tr["ticket"], tr)

        # Posisi nyata yang belum tercatat -> adopsi (sl_distance dari SL kini).
        recorded = {t["ticket"] for t in self.journal.get_open_trades()}
        for p in live:
            if p.ticket not in recorded:
                log.warning("Adopsi posisi tak tercatat ticket=%s", p.ticket)
                from core.journal import TradeRecord

                sl_dist = abs(p.price_open - p.sl) if p.sl else 0.0
                self.journal.record_open(TradeRecord(
                    ticket=p.ticket, symbol=symbol,
                    direction="BUY" if p.type == 0 else "SELL",
                    lots=p.volume, entry=p.price_open, sl=p.sl, tp=p.tp,
                    open_time=datetime.now(timezone.utc).isoformat(),
                    sl_distance=sl_dist, reason="adopted", magic=self.magic,
                ))
        log.info("Reconcile selesai. Posisi aktif: %s", sorted(live_tickets))

    # ------------------------------------------------------------------ #
    def manage(self, spec: SymbolSpec, df_m1: pd.DataFrame | None = None) -> ManageResult:
        """Loop pengelolaan: deteksi tutup + break-even + trailing."""
        result = ManageResult()
        live = self.get_open_positions(spec.name)
        live_tickets = {p.ticket for p in live}

        # 1. Deteksi posisi yang tertutup sejak loop lalu.
        for ticket in self._known_tickets - live_tickets:
            tr = self.journal.get_trade(ticket)
            ev = self._finalize_closed(ticket, tr)
            if ev:
                result.closed.append(ev)

        # 2. Break-even & trailing untuk posisi aktif.
        atr_m1 = None
        if df_m1 is not None and len(df_m1) > self.strat.atr_period + 2:
            atr_m1 = float(indicators.atr(df_m1, self.strat.atr_period).iloc[-2])

        for p in live:
            msg = self._apply_management(p, spec, atr_m1)
            if msg:
                result.modified.append(msg)

        self._known_tickets = set(live_tickets)
        return result

    # ------------------------------------------------------------------ #
    def _apply_management(
        self, position: Any, spec: SymbolSpec, atr_m1: float | None
    ) -> str | None:
        """Terapkan break-even lalu trailing pada satu posisi."""
        m = _require_mt5()
        tr = self.journal.get_trade(position.ticket)
        entry = float(tr["entry"]) if tr else float(position.price_open)
        sl_distance = float(tr["sl_distance"]) if tr and tr.get("sl_distance") else \
            abs(position.price_open - position.sl)
        if sl_distance <= 0:
            return None

        is_buy = position.type == m.POSITION_TYPE_BUY
        tick = self.client.get_tick(spec.name)
        if tick is None:
            return None
        current = tick.bid if is_buy else tick.ask

        # R saat ini.
        move = (current - entry) if is_buy else (entry - current)
        r_now = move / sl_distance

        new_sl: float | None = None
        action = ""

        # Break-even + SL PLUS: kunci profit beberapa pips saat profit >= trigger R.
        if self.mgmt.break_even and r_now >= self.mgmt.break_even_trigger_r:
            plus = self.mgmt.breakeven_plus_pips * self.pip_size
            be_price = (entry + plus) if is_buy else (entry - plus)
            # Jangan taruh SL di sisi salah dari harga sekarang (broker menolak) ->
            # fallback ke breakeven murni bila plus terlalu besar.
            if is_buy and be_price >= current:
                be_price = entry
            elif (not is_buy) and be_price <= current:
                be_price = entry
            label = "SL-plus" if self.mgmt.breakeven_plus_pips > 0 and be_price != entry else "break-even"
            if is_buy and position.sl < be_price:
                new_sl, action = be_price, label
            elif (not is_buy) and (position.sl == 0 or position.sl > be_price):
                new_sl, action = be_price, label

        # Trailing (menimpa BE bila lebih jauh ke arah profit).
        if self.mgmt.trailing_stop and atr_m1 and atr_m1 > 0 and r_now >= self.mgmt.break_even_trigger_r:
            trail = self.mgmt.trailing_atr_mult * atr_m1
            cand = (current - trail) if is_buy else (current + trail)
            if is_buy and cand > max(position.sl, new_sl or 0):
                new_sl, action = cand, "trailing"
            elif (not is_buy) and (position.sl == 0 or cand < min(position.sl, new_sl or 1e18)):
                new_sl, action = cand, "trailing"

        if new_sl is None:
            return None

        new_sl = round(new_sl, spec.digits)
        # PENTING: pertahankan TP yang sudah ada. Pada TRADE_ACTION_SLTP, TP yang
        # TIDAK disertakan dianggap 0 oleh MT5 -> TP terhapus. Jadi saat SL-plus/
        # break-even/trailing dipasang, kita kirim ulang TP posisi agar TP TETAP ADA.
        keep_tp = float(position.tp) if getattr(position, "tp", 0) else None
        if keep_tp is None and tr and tr.get("tp"):
            keep_tp = float(tr["tp"])
        res = self.executor.modify_sl_tp(
            position.ticket, spec.name, new_sl, keep_tp, spec.digits
        )
        if res.ok:
            self.journal.update_sl(position.ticket, new_sl)
            return f"{action}: ticket {position.ticket} SL -> {new_sl:.{spec.digits}f}"
        log.warning("Gagal %s ticket %s: %s", action, position.ticket, res.comment)
        return None

    # ------------------------------------------------------------------ #
    def _finalize_closed(self, ticket: int, tr: dict[str, Any] | None) -> CloseEvent | None:
        """Ambil P/L realisasi dari history & update journal."""
        m = _require_mt5()
        deals = m.history_deals_get(position=ticket)
        if deals is None or len(deals) == 0:
            log.warning("history_deals_get(position=%s) kosong.", ticket)
            return None

        profit = sum(
            float(d.profit) + float(d.swap) + float(d.commission)
            for d in deals if d.magic == self.magic or d.magic == 0
        )
        # Deal penutup (terakhir) untuk harga exit.
        exit_price = float(deals[-1].price)
        direction = tr["direction"] if tr else ("BUY" if deals[0].type == 0 else "SELL")

        sl_distance = float(tr["sl_distance"]) if tr and tr.get("sl_distance") else 0.0
        entry = float(tr["entry"]) if tr else float(deals[0].price)
        if sl_distance > 0:
            move = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
            r_multiple = move / sl_distance
        else:
            r_multiple = 0.0

        is_win = profit > 0
        if tr:
            self.journal.record_close(ticket, exit_price, profit, r_multiple)
        log.info(
            "Posisi tutup ticket=%s P/L=%.2f R=%.2f (%s)",
            ticket, profit, r_multiple, "WIN" if is_win else "LOSS",
        )
        return CloseEvent(ticket, direction, profit, r_multiple, exit_price, is_win)
