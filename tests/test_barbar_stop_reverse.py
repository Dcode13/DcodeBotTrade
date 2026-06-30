"""Unit test BARBAR stop-and-reverse: stop order lawan trailing di level SL candle.

Skenario yang dijaga:
- Posisi SELL + candle bearish, belum ada pending -> pasang BUY STOP di high+buffer
  (level SL candle SELL), siap exit + balik arah saat harga naik menembusnya.
- Posisi BUY + candle bullish -> pasang SELL STOP di low-buffer.
- Sudah ada BUY STOP lebih tinggi -> di-trailing turun mengikuti candle (modify).
- Candle searah lawan posisi -> tidak ada aksi.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from core.barbar import BarbarGrid
from core.config import BarbarConfig
from core.risk_manager import SymbolSpec


class _FakeMT5:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_MODIFY = 7
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008

    def __init__(self):
        self.sent = []

    def order_send(self, request):
        self.sent.append(request)
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_PLACED, comment="PLACED", order=555, deal=0, price=0.0
        )

    def last_error(self):  # pragma: no cover
        return (0, "ok")


def _spec() -> SymbolSpec:
    return SymbolSpec(
        name="XAUUSD", digits=2, point=0.01, trade_contract_size=100.0,
        trade_tick_size=0.01, trade_tick_value=1.0, volume_min=0.01,
        volume_max=10.0, volume_step=0.01, trade_stops_level=0,
    )


def _candle(o, h, l, c) -> pd.DataFrame:
    rows = [
        {"open": o, "high": h, "low": l, "close": c},
        {"open": c, "high": h, "low": l, "close": c},
    ]
    return pd.DataFrame(rows)


def _grid(monkeypatch, mt5, *, bid, ask):
    monkeypatch.setattr("core.barbar._require_mt5", lambda: mt5)
    monkeypatch.setattr("core.executor._require_mt5", lambda: mt5)
    cfg = BarbarConfig(
        stop_and_reverse=True, candle_follow_sl_buffer=0.10,
        base_lot=0.01, trailing_step=0.10, magic_number=99,
    )
    client = SimpleNamespace(get_tick=lambda name: SimpleNamespace(bid=bid, ask=ask))
    return BarbarGrid(client, cfg)


def test_sell_places_buy_stop_at_candle_high(monkeypatch):
    mt5 = _FakeMT5()
    grid = _grid(monkeypatch, mt5, bid=1990.0, ask=1990.2)
    pos = SimpleNamespace(ticket=1, type=1, price_open=1995.0, sl=0.0, tp=0.0, volume=0.01, time=1)
    df = _candle(1993.0, 1993.5, 1990.5, 1991.0)  # bearish, high 1993.5

    res = grid.manage_stop_reverse(_spec(), [pos], [], df)

    assert res.opened == 1
    req = mt5.sent[-1]
    assert req["action"] == _FakeMT5.TRADE_ACTION_PENDING
    assert req["type"] == _FakeMT5.ORDER_TYPE_BUY_STOP
    # BUY STOP di high + buffer = 1993.5 + 0.10 = 1993.60 (= level SL candle SELL).
    assert req["price"] == pytest.approx(1993.60)


def test_buy_places_sell_stop_at_candle_low(monkeypatch):
    mt5 = _FakeMT5()
    grid = _grid(monkeypatch, mt5, bid=2010.0, ask=2010.2)
    pos = SimpleNamespace(ticket=2, type=0, price_open=2005.0, sl=0.0, tp=0.0, volume=0.01, time=1)
    df = _candle(2007.0, 2009.5, 2006.5, 2009.0)  # bullish, low 2006.5

    res = grid.manage_stop_reverse(_spec(), [pos], [], df)

    assert res.opened == 1
    req = mt5.sent[-1]
    assert req["type"] == _FakeMT5.ORDER_TYPE_SELL_STOP
    # SELL STOP di low - buffer = 2006.5 - 0.10 = 2006.40.
    assert req["price"] == pytest.approx(2006.40)


def test_existing_buy_stop_trails_down(monkeypatch):
    mt5 = _FakeMT5()
    grid = _grid(monkeypatch, mt5, bid=1990.0, ask=1990.2)
    pos = SimpleNamespace(ticket=3, type=1, price_open=1995.0, sl=0.0, tp=0.0, volume=0.01, time=1)
    # BUY STOP lama jauh di atas (1996.0) -> harus turun ke 1993.60.
    existing = SimpleNamespace(ticket=900, type=4, price_open=1996.0, sl=0.0, tp=0.0)
    df = _candle(1993.0, 1993.5, 1990.5, 1991.0)

    res = grid.manage_stop_reverse(_spec(), [pos], [existing], df)

    assert res.modified == 1
    req = mt5.sent[-1]
    assert req["action"] == _FakeMT5.TRADE_ACTION_MODIFY
    assert req["order"] == 900
    assert req["price"] == pytest.approx(1993.60)


def test_reverse_lot_match_uses_position_volume(monkeypatch):
    mt5 = _FakeMT5()
    grid = _grid(monkeypatch, mt5, bid=1990.0, ask=1990.2)
    grid.cfg.stop_reverse_lot_mode = "MATCH"
    # Posisi SELL volume 0.08 -> order reverse harus 0.08 (bukan base_lot 0.01).
    pos = SimpleNamespace(ticket=5, type=1, price_open=1995.0, sl=0.0, tp=0.0, volume=0.08, time=1)
    df = _candle(1993.0, 1993.5, 1990.5, 1991.0)

    res = grid.manage_stop_reverse(_spec(), [pos], [], df)

    assert res.opened == 1
    assert mt5.sent[-1]["volume"] == pytest.approx(0.08)


def test_reverse_lot_fixed_uses_configured_value(monkeypatch):
    mt5 = _FakeMT5()
    grid = _grid(monkeypatch, mt5, bid=1990.0, ask=1990.2)
    grid.cfg.stop_reverse_lot_mode = "FIXED"
    grid.cfg.stop_reverse_lot = 0.05
    pos = SimpleNamespace(ticket=6, type=1, price_open=1995.0, sl=0.0, tp=0.0, volume=0.08, time=1)
    df = _candle(1993.0, 1993.5, 1990.5, 1991.0)

    res = grid.manage_stop_reverse(_spec(), [pos], [], df)

    assert res.opened == 1
    assert mt5.sent[-1]["volume"] == pytest.approx(0.05)


def test_counter_trend_candle_no_action(monkeypatch):
    mt5 = _FakeMT5()
    grid = _grid(monkeypatch, mt5, bid=1990.0, ask=1990.2)
    pos = SimpleNamespace(ticket=4, type=1, price_open=1995.0, sl=0.0, tp=0.0, volume=0.01, time=1)
    df = _candle(1990.0, 1993.5, 1989.5, 1992.0)  # bullish -> lawan posisi SELL

    res = grid.manage_stop_reverse(_spec(), [pos], [], df)

    assert res.opened == 0 and res.modified == 0
    assert mt5.sent == []
