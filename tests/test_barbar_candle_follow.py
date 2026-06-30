"""Unit test BARBAR candle-follow: SL & TP ikut arah candle selama trend searah.

Skenario yang dijaga:
- BUY + candle M1 terakhir bullish -> SL naik di bawah low candle, TP didorong
  ke atas harga (profit tidak dipotong selama trend naik).
- SELL + candle bearish -> cermin: SL turun di atas high, TP didorong ke bawah.
- Candle berlawanan arah posisi -> tidak ada modifikasi sama sekali.
- SL & TP dikirim bersama (TRADE_ACTION_SLTP) supaya tidak saling menghapus.
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
    TRADE_ACTION_SLTP = 6
    TRADE_RETCODE_DONE = 10009

    def __init__(self):
        self.sent = []

    def order_send(self, request):
        self.sent.append(request)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="DONE", order=1, deal=1, price=0.0)

    def last_error(self):  # pragma: no cover - dipakai hanya bila order_send None
        return (0, "ok")


def _spec() -> SymbolSpec:
    return SymbolSpec(
        name="XAUUSD", digits=2, point=0.01, trade_contract_size=100.0,
        trade_tick_size=0.01, trade_tick_value=1.0, volume_min=0.01,
        volume_max=10.0, volume_step=0.01, trade_stops_level=0,
    )


def _candle(o, h, l, c) -> pd.DataFrame:
    # iloc[-2] adalah candle tertutup terakhir; tambahkan satu bar berjalan di akhir.
    rows = [
        {"open": o, "high": h, "low": l, "close": c},
        {"open": c, "high": h, "low": l, "close": c},
    ]
    return pd.DataFrame(rows)


def _grid(monkeypatch, mt5, *, bid, ask):
    monkeypatch.setattr("core.barbar._require_mt5", lambda: mt5)
    cfg = BarbarConfig(
        candle_follow=True, candle_follow_sl_buffer=0.10,
        candle_follow_tp_distance=2.0, trailing_step=0.10, magic_number=99,
    )
    client = SimpleNamespace(
        get_tick=lambda name: SimpleNamespace(bid=bid, ask=ask),
    )
    return BarbarGrid(client, cfg)


def test_buy_uptrend_trails_sl_up_and_pushes_tp_up(monkeypatch):
    mt5 = _FakeMT5()
    grid = _grid(monkeypatch, mt5, bid=2010.0, ask=2010.2)
    # Posisi BUY, SL lama jauh di bawah, TP lama dekat (akan didorong naik).
    pos = SimpleNamespace(ticket=1, type=0, price_open=2005.0, sl=2000.0, tp=2008.0, volume=0.01)
    grid.positions = lambda symbol: [pos]  # _fresh_position memakai ini

    df = _candle(2007.0, 2009.5, 2006.5, 2009.0)  # bullish, low 2006.5
    res = grid.manage_candle_follow(_spec(), [pos], df)

    assert res.modified == 1
    req = mt5.sent[-1]
    assert req["action"] == _FakeMT5.TRADE_ACTION_SLTP
    # SL = low - buffer = 2006.5 - 0.10 = 2006.40 (naik dari 2000).
    assert req["sl"] == pytest.approx(2006.40)
    # TP didorong ke depan harga: bid 2010 + 2.0 = 2012.0 (naik dari 2008).
    assert req["tp"] == pytest.approx(2012.0)


def test_sell_downtrend_trails_sl_down_and_pushes_tp_down(monkeypatch):
    mt5 = _FakeMT5()
    grid = _grid(monkeypatch, mt5, bid=1990.0, ask=1990.2)
    pos = SimpleNamespace(ticket=2, type=1, price_open=1995.0, sl=2000.0, tp=1992.0, volume=0.01)
    grid.positions = lambda symbol: [pos]

    df = _candle(1993.0, 1993.5, 1990.5, 1991.0)  # bearish, high 1993.5
    res = grid.manage_candle_follow(_spec(), [pos], df)

    assert res.modified == 1
    req = mt5.sent[-1]
    # SL = high + buffer = 1993.5 + 0.10 = 1993.60 (turun dari 2000).
    assert req["sl"] == pytest.approx(1993.60)
    # TP didorong ke bawah harga: ask 1990.2 - 2.0 = 1988.2 (turun dari 1992).
    assert req["tp"] == pytest.approx(1988.2)


def test_counter_trend_candle_does_not_modify(monkeypatch):
    mt5 = _FakeMT5()
    grid = _grid(monkeypatch, mt5, bid=2010.0, ask=2010.2)
    pos = SimpleNamespace(ticket=3, type=0, price_open=2005.0, sl=2000.0, tp=2008.0, volume=0.01)
    grid.positions = lambda symbol: [pos]

    df = _candle(2009.0, 2009.5, 2006.5, 2007.0)  # bearish -> lawan posisi BUY
    res = grid.manage_candle_follow(_spec(), [pos], df)

    assert res.modified == 0
    assert mt5.sent == []
