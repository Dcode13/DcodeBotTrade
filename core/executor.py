"""Eksekusi order: kirim/modify/close dengan penanganan retcode lengkap (§10).

Gerbang live (§16) TIDAK ditegakkan di sini melainkan di orchestrator
(main.py) sebelum memanggil ``open_position``. Modul ini murni "cara
mengirim order yang benar" + menangani semua retcode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.mt5_client import MT5Client, _require_mt5
from core.risk_manager import SymbolSpec
from core.strategy import Signal

log = logging.getLogger(__name__)

MAX_REQUOTE_RETRY = 3


@dataclass
class OrderResult:
    ok: bool
    retcode: int
    comment: str
    ticket: int | None = None
    price: float | None = None
    volume: float | None = None


def deduce_filling(spec: SymbolSpec) -> int:
    """Deduksi ``type_filling`` dari ``symbol_info.filling_mode`` (§10).

    filling_mode adalah bitmask: bit FOK / IOC. Jika tak jelas -> RETURN.
    """
    m = _require_mt5()
    mode = spec.filling_mode
    # SYMBOL_FILLING_FOK = 1, SYMBOL_FILLING_IOC = 2 (bitmask)
    if mode & 1:
        return m.ORDER_FILLING_FOK
    if mode & 2:
        return m.ORDER_FILLING_IOC
    return m.ORDER_FILLING_RETURN


class Executor:
    def __init__(self, client: MT5Client, magic: int, deviation: int = 50) -> None:
        self.client = client
        self.magic = magic
        self.deviation = deviation

    # ------------------------------------------------------------------ #
    def open_position(
        self, signal: Signal, lots: float, spec: SymbolSpec, comment: str = "btc_bot"
    ) -> OrderResult:
        """Kirim market order DEAL dengan SL/TP. Retry terbatas saat requote."""
        m = _require_mt5()
        symbol = spec.name
        order_type = m.ORDER_TYPE_BUY if signal.direction == "BUY" else m.ORDER_TYPE_SELL
        filling = deduce_filling(spec)

        last: OrderResult | None = None
        for attempt in range(1, MAX_REQUOTE_RETRY + 1):
            tick = self.client.get_tick(symbol)
            if tick is None:
                return OrderResult(False, -1, "tick None (market closed?)")
            price = tick.ask if signal.direction == "BUY" else tick.bid

            request = {
                "action": m.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(lots),
                "type": order_type,
                "price": float(price),
                "sl": round(float(signal.sl), spec.digits),
                "tp": round(float(signal.tp), spec.digits),
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": comment,
                "type_time": m.ORDER_TIME_GTC,
                "type_filling": filling,
            }

            result = m.order_send(request)
            last = self._interpret(result, m)
            if last.ok:
                log.info(
                    "Order OK: %s %.2f lot @ %.*f SL=%.*f TP=%.*f ticket=%s",
                    signal.direction, lots, spec.digits, last.price or price,
                    spec.digits, signal.sl, spec.digits, signal.tp, last.ticket,
                )
                return last

            # Requote -> ambil harga terbaru & coba lagi.
            if last.retcode == m.TRADE_RETCODE_REQUOTE:
                log.warning("Requote (attempt %d), retry dgn harga baru...", attempt)
                continue

            # Filling mode tak didukung -> coba mode lain sekali.
            if last.retcode == m.TRADE_RETCODE_INVALID_FILL and attempt == 1:
                log.warning("Filling mode ditolak, fallback ke RETURN.")
                request["type_filling"] = m.ORDER_FILLING_RETURN
                result = m.order_send(request)
                last = self._interpret(result, m)
                if last.ok:
                    return last
            # retcode lain (INVALID_STOPS / NO_MONEY / MARKET_CLOSED) -> berhenti.
            break

        return last or OrderResult(False, -1, "order_send mengembalikan None")

    # ------------------------------------------------------------------ #
    def modify_sl_tp(
        self, ticket: int, symbol: str, sl: float, tp: float | None, digits: int
    ) -> OrderResult:
        """Ubah SL (dan TP opsional) posisi berjalan (break-even/trailing)."""
        m = _require_mt5()
        request = {
            "action": m.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": ticket,
            "sl": round(float(sl), digits),
            "magic": self.magic,
        }
        if tp is not None:
            request["tp"] = round(float(tp), digits)
        result = m.order_send(request)
        return self._interpret(result, m)

    def close_position(self, position: Any, spec: SymbolSpec) -> OrderResult:
        """Tutup posisi via market order arah berlawanan."""
        m = _require_mt5()
        tick = self.client.get_tick(spec.name)
        if tick is None:
            return OrderResult(False, -1, "tick None saat close")

        is_buy = position.type == m.POSITION_TYPE_BUY
        close_type = m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY
        price = tick.bid if is_buy else tick.ask

        request = {
            "action": m.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": float(position.volume),
            "type": close_type,
            "position": position.ticket,
            "price": float(price),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "btc_bot_close",
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": deduce_filling(spec),
        }
        result = m.order_send(request)
        return self._interpret(result, m)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _interpret(result: Any, m: Any) -> OrderResult:
        """Terjemahkan hasil order_send -> OrderResult dengan retcode jelas."""
        if result is None:
            return OrderResult(False, -1, f"order_send None: {m.last_error()}")

        retcode = int(result.retcode)
        ok = retcode == m.TRADE_RETCODE_DONE  # 10009

        # Pemetaan retcode penting -> komentar manusiawi.
        names = {
            m.TRADE_RETCODE_REQUOTE: "REQUOTE",
            m.TRADE_RETCODE_INVALID_STOPS: "INVALID_STOPS",
            m.TRADE_RETCODE_NO_MONEY: "NO_MONEY",
            m.TRADE_RETCODE_MARKET_CLOSED: "MARKET_CLOSED",
            m.TRADE_RETCODE_INVALID_FILL: "INVALID_FILL",
            m.TRADE_RETCODE_TRADE_DISABLED: "TRADE_DISABLED",
            getattr(m, "TRADE_RETCODE_CLIENT_DISABLES_AT", 10027):
                "AUTOTRADING_DISABLED (aktifkan tombol Algo Trading di MT5)",
            m.TRADE_RETCODE_DONE: "DONE",
        }
        label = names.get(retcode, f"retcode={retcode}")
        comment = f"{label}: {getattr(result, 'comment', '')}".strip()

        if not ok:
            log.error("Order gagal -> %s", comment)

        return OrderResult(
            ok=ok,
            retcode=retcode,
            comment=comment,
            ticket=getattr(result, "order", None) or getattr(result, "deal", None),
            price=getattr(result, "price", None),
            volume=getattr(result, "volume", None),
        )
