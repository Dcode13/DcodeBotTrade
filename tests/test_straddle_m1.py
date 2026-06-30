"""Unit test StraddleM1 EA: state machine straddle + trailing + stop-and-reverse.

Skenario yang dijaga:
- FLAT_IDLE + candle M1 baru -> pasang BUY STOP High[1]+offset & SELL STOP Low[1]-offset,
  SL tiap leg = SL_pts dari entry, TANPA TP.
- IN_POSITION -> SL trailing setelah breakeven, order reverse menempel RevGap dari SL.
- REVERSE_PENDING -> tidak pasang straddle baru (lindungi order reverse).
- STRADDLE_PENDING + bukan bar baru -> tunggu fill (tidak ada order baru).
- Lebih dari satu posisi -> tutup kelebihannya (enforce single position).
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from core.barbar import StraddleM1
from core.config import StraddleM1Config
from core.risk_manager import SymbolSpec


def _done(order=1, deal=1, price=0.0):
    return SimpleNamespace(retcode=10009, comment="DONE", order=order, deal=deal, price=price)


def _placed(order=500, price=0.0):
    return SimpleNamespace(retcode=10008, comment="PLACED", order=order, deal=0, price=price)


class _FakeMT5:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_MODIFY = 7
    TRADE_ACTION_REMOVE = 2
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_REQUOTE = 10004

    def __init__(self, positions=None, orders=None):
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self.sent = []
        self._next = 600

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return list(self._orders)

    def history_deals_get(self, start, end):
        return []

    def last_error(self):  # pragma: no cover
        return (0, "ok")

    def order_send(self, req):
        self.sent.append(req)
        action = req["action"]
        if action == self.TRADE_ACTION_DEAL and "position" in req:
            self._positions = [
                p for p in self._positions if int(p.ticket) != int(req["position"])
            ]
            return _done(deal=1)
        if action == self.TRADE_ACTION_PENDING:
            self._next += 1
            return _placed(order=self._next)
        if action == self.TRADE_ACTION_REMOVE:
            self._orders = [
                o for o in self._orders if int(getattr(o, "ticket", 0)) != int(req["order"])
            ]
            return _done(order=req["order"])
        return _done(order=req.get("order", 1))


def _spec() -> SymbolSpec:
    return SymbolSpec(
        name="XAUUSD", digits=2, point=0.01, trade_contract_size=100.0,
        trade_tick_size=0.01, trade_tick_value=1.0, volume_min=0.01,
        volume_max=10.0, volume_step=0.01, trade_stops_level=0,
    )


def _df(prev_high, prev_low) -> pd.DataFrame:
    rows = [
        {"open": prev_low, "high": prev_high, "low": prev_low, "close": prev_high},
        {"open": prev_high, "high": prev_high, "low": prev_low, "close": prev_high},
    ]
    return pd.DataFrame(rows)


def _grid(monkeypatch, mt5, *, bid, ask):
    monkeypatch.setattr("core.barbar._require_mt5", lambda: mt5)
    monkeypatch.setattr("core.executor._require_mt5", lambda: mt5)
    cfg = StraddleM1Config(
        lot=0.01, magic_number=770017, offset_pts=30, sl_pts=50,
        trail_dist_pts=20, trail_step_pts=20, rev_gap_pts=20, max_spread_pts=30,
    )
    tick = SimpleNamespace(bid=bid, ask=ask, time=1_700_000_000)
    client = SimpleNamespace(
        get_tick=lambda name: tick,
        is_market_open=lambda name: True,
    )
    return StraddleM1(client, cfg)


def _by_action(sent, action):
    return [r for r in sent if r["action"] == action]


def _cycle(grid, spec, df, **kw):
    base = dict(execution_enabled=True, autotrading_enabled=True, allow_new_entries=True, bar_time="t2")
    base.update(kw)
    return grid.cycle(spec, df, **base)


def test_flat_idle_new_bar_places_straddle(monkeypatch):
    mt5 = _FakeMT5(positions=[], orders=[])
    grid = _grid(monkeypatch, mt5, bid=1999.0, ask=1999.2)
    res = _cycle(grid, _spec(), _df(prev_high=2000.0, prev_low=1998.0))

    pend = _by_action(mt5.sent, _FakeMT5.TRADE_ACTION_PENDING)
    assert len(pend) == 2
    buy = next(r for r in pend if r["type"] == _FakeMT5.ORDER_TYPE_BUY_STOP)
    sell = next(r for r in pend if r["type"] == _FakeMT5.ORDER_TYPE_SELL_STOP)
    assert buy["price"] == pytest.approx(2000.30)   # High[1] + 0.30
    assert buy["sl"] == pytest.approx(1999.80)      # entry - 0.50, no TP
    assert buy["tp"] == 0.0
    assert sell["price"] == pytest.approx(1997.70)  # Low[1] - 0.30
    assert sell["sl"] == pytest.approx(1998.20)
    assert res.opened == 2
    assert grid.last_bar_time == "t2" and grid.trades_today == 1


def test_in_position_trails_sl_and_places_reverse(monkeypatch):
    mt5 = _FakeMT5(
        positions=[SimpleNamespace(
            ticket=10, type=0, price_open=2000.0, sl=1999.50, tp=0.0,
            volume=0.01, profit=1.0, time=1, magic=770017,
        )],
        orders=[],
    )
    grid = _grid(monkeypatch, mt5, bid=2001.0, ask=2001.2)  # bid >= entry -> breakeven
    _cycle(grid, _spec(), None)

    sltp = _by_action(mt5.sent, _FakeMT5.TRADE_ACTION_SLTP)
    assert sltp and sltp[-1]["sl"] == pytest.approx(2000.80)  # bid - trail_dist 0.20
    assert sltp[-1]["tp"] == 0.0
    pend = _by_action(mt5.sent, _FakeMT5.TRADE_ACTION_PENDING)
    rev = next(r for r in pend if r["type"] == _FakeMT5.ORDER_TYPE_SELL_STOP)
    assert rev["price"] == pytest.approx(2000.60)  # SL 2000.80 - rev_gap 0.20


def test_reverse_pending_does_not_place_straddle(monkeypatch):
    rev = SimpleNamespace(ticket=900, type=5, price_open=1999.0, sl=1999.5, tp=0.0, magic=770017)
    mt5 = _FakeMT5(positions=[], orders=[rev])  # one lone stop = REVERSE_PENDING
    grid = _grid(monkeypatch, mt5, bid=1999.0, ask=1999.2)
    res = _cycle(grid, _spec(), _df(2000.0, 1998.0))

    assert "REVERSE_PENDING" in res.blocked
    assert _by_action(mt5.sent, _FakeMT5.TRADE_ACTION_PENDING) == []
    assert grid.reverse_started_at > 0


def test_straddle_pending_same_bar_waits(monkeypatch):
    orders = [
        SimpleNamespace(ticket=801, type=4, price_open=2000.3, sl=1999.8, tp=0.0, magic=770017),
        SimpleNamespace(ticket=802, type=5, price_open=1997.7, sl=1998.2, tp=0.0, magic=770017),
    ]
    mt5 = _FakeMT5(positions=[], orders=orders)
    grid = _grid(monkeypatch, mt5, bid=1999.0, ask=1999.2)
    grid.last_bar_time = "t2"  # same bar -> not new
    res = _cycle(grid, _spec(), _df(2000.0, 1998.0), bar_time="t2")

    assert "menunggu fill" in res.blocked
    assert mt5.sent == []


def test_enforce_single_position_closes_extra(monkeypatch):
    older = SimpleNamespace(ticket=1, type=0, price_open=2000.0, sl=1999.5, tp=0.0,
                            volume=0.01, profit=0.0, time=1, magic=770017)
    newer = SimpleNamespace(ticket=2, type=0, price_open=2000.0, sl=1999.5, tp=0.0,
                            volume=0.01, profit=0.0, time=2, magic=770017)
    mt5 = _FakeMT5(positions=[older, newer], orders=[])
    grid = _grid(monkeypatch, mt5, bid=2001.0, ask=2001.2)
    res = _cycle(grid, _spec(), None)

    closes = [r for r in mt5.sent
              if r["action"] == _FakeMT5.TRADE_ACTION_DEAL and r.get("position") == 1]
    assert closes and res.closed == 1  # older ticket closed, newest kept
