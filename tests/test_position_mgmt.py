"""Unit test manajemen posisi: SL-plus/break-even WAJIB mempertahankan TP.

Bug yang dijaga: pada TRADE_ACTION_SLTP, TP yang tidak disertakan dianggap 0 oleh
MT5 -> TP terhapus. Saat memindah SL, PositionManager harus mengirim ulang TP.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.config import ManagementConfig, StrategyConfig
from core.executor import OrderResult
from core.position_manager import PositionManager


class _FakeMT5:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1


class _FakeExecutor:
    """Rekam argumen modify_sl_tp untuk verifikasi TP dipertahankan."""

    def __init__(self):
        self.calls = []

    def modify_sl_tp(self, ticket, symbol, sl, tp, digits):
        self.calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return OrderResult(ok=True, retcode=10009, comment="DONE")


class _FakeJournal:
    def __init__(self, trade):
        self._trade = trade
        self.updated = []

    def get_trade(self, ticket):
        return self._trade

    def update_sl(self, ticket, sl):
        self.updated.append((ticket, sl))


def _pm(monkeypatch, executor, journal, *, bid):
    monkeypatch.setattr("core.position_manager._require_mt5", lambda: _FakeMT5())
    client = SimpleNamespace(get_tick=lambda name: SimpleNamespace(bid=bid, ask=bid))
    mgmt = ManagementConfig(
        break_even=True, break_even_trigger_r=0.8, breakeven_plus_pips=10,
        trailing_stop=False, auto_tp=True,
    )
    return PositionManager(
        client=client, executor=executor, journal=journal,
        mgmt=mgmt, strat=StrategyConfig(), magic=770120, pip_size=0.1,
    )


def test_slplus_keeps_existing_tp(monkeypatch):
    # BUY entry 100, SL 99 (jarak 1.0), TP 103. Harga naik ke 102 -> r=2.0 -> SL-plus.
    position = SimpleNamespace(
        ticket=111, type=0, price_open=100.0, sl=99.0, tp=103.0, volume=0.01,
    )
    trade = {"entry": 100.0, "sl_distance": 1.0, "tp": 103.0, "direction": "BUY"}
    executor = _FakeExecutor()
    pm = _pm(monkeypatch, executor, _FakeJournal(trade), bid=102.0)

    spec = SimpleNamespace(name="XAUUSD", digits=2)
    msg = pm._apply_management(position, spec, atr_m1=None)

    assert msg is not None and "SL-plus" in msg
    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call["tp"] == 103.0, "TP harus dipertahankan saat SL-plus dipasang"
    assert call["sl"] > 100.0, "SL-plus harus mengunci profit di atas entry (BUY)"


def test_tp_falls_back_to_journal_when_position_tp_zero(monkeypatch):
    # Posisi melaporkan tp=0 (mis. broker), tapi journal punya TP -> tetap dikirim.
    position = SimpleNamespace(
        ticket=222, type=0, price_open=100.0, sl=99.0, tp=0.0, volume=0.01,
    )
    trade = {"entry": 100.0, "sl_distance": 1.0, "tp": 105.0, "direction": "BUY"}
    executor = _FakeExecutor()
    pm = _pm(monkeypatch, executor, _FakeJournal(trade), bid=102.0)

    spec = SimpleNamespace(name="XAUUSD", digits=2)
    pm._apply_management(position, spec, atr_m1=None)

    assert executor.calls[0]["tp"] == 105.0, "TP fallback dari journal harus dipakai"


def test_no_modify_before_trigger(monkeypatch):
    # Profit < trigger R -> tidak ada modifikasi SL sama sekali.
    position = SimpleNamespace(
        ticket=333, type=0, price_open=100.0, sl=99.0, tp=103.0, volume=0.01,
    )
    trade = {"entry": 100.0, "sl_distance": 1.0, "tp": 103.0, "direction": "BUY"}
    executor = _FakeExecutor()
    pm = _pm(monkeypatch, executor, _FakeJournal(trade), bid=100.2)  # r=0.2 < 0.8

    spec = SimpleNamespace(name="XAUUSD", digits=2)
    msg = pm._apply_management(position, spec, atr_m1=None)
    assert msg is None
    assert executor.calls == []
