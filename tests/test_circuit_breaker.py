"""Unit test circuit breaker & filter pra-entry (§8.2, §8.3, §19)."""

from __future__ import annotations

import pandas as pd

from core.config import RiskConfig, StrategyConfig
from core.risk_manager import (
    DayState,
    SymbolSpec,
    check_circuit_breakers,
    check_spread,
    high_risk_warning,
    validate_stops,
)
from core.strategy import Signal


def base_state(equity: float = 2000.0) -> DayState:
    return DayState(day="2024-01-01", start_equity=equity)


def test_allowed_when_clean():
    risk = RiskConfig()
    r = check_circuit_breakers(base_state(), equity=2000.0, risk=risk)
    assert r.allowed


def test_paused_blocks():
    risk = RiskConfig()
    state = base_state()
    state.paused = True
    r = check_circuit_breakers(state, equity=2000.0, risk=risk)
    assert not r.allowed
    assert r.breaker == "paused"


def test_max_consecutive_losses_blocks():
    risk = RiskConfig(max_consecutive_losses=3)
    state = base_state()
    state.consecutive_losses = 3
    r = check_circuit_breakers(state, equity=2000.0, risk=risk)
    assert not r.allowed
    assert r.breaker == "max_consecutive_losses"


def test_daily_loss_blocks():
    risk = RiskConfig(max_daily_loss_pct=0.05)
    state = base_state(equity=2000.0)
    # equity turun 10% -> melebihi 5%
    r = check_circuit_breakers(state, equity=1800.0, risk=risk)
    assert not r.allowed
    assert r.breaker == "max_daily_loss"


def test_max_trades_blocks():
    risk = RiskConfig(max_trades_per_day=8)
    state = base_state()
    state.trades_today = 8
    r = check_circuit_breakers(state, equity=2000.0, risk=risk)
    assert not r.allowed
    assert r.breaker == "max_trades_per_day"


def test_spread_filter():
    risk = RiskConfig(max_spread_points=250)
    assert check_spread(100, risk).allowed
    assert not check_spread(300, risk).allowed


def test_high_risk_warning():
    assert high_risk_warning(RiskConfig(risk_per_trade=0.01)) is None
    assert high_risk_warning(RiskConfig(risk_per_trade=0.10)) is not None


def _spec(stops_level: int) -> SymbolSpec:
    return SymbolSpec(
        name="BTCUSD", digits=2, point=0.01, trade_contract_size=1.0,
        trade_tick_size=0.01, trade_tick_value=0.01, volume_min=0.01,
        volume_max=10.0, volume_step=0.01, trade_stops_level=stops_level,
    )


def _signal(entry: float, sl: float, tp: float) -> Signal:
    return Signal(
        direction="BUY", entry=entry, sl=sl, tp=tp,
        sl_distance=abs(entry - sl), zone=sl, atr_m1=1.0, bias="UP",
        signal_bar_time=pd.Timestamp("2024-01-01", tz="UTC"),
        body_ratio=0.8, rsi_m1=50.0, reason="test",
    )


def test_validate_stops_blocks_too_close():
    spec = _spec(stops_level=500)  # min_dist = 500 * 0.01 = 5.0
    sig = _signal(entry=100.0, sl=99.0, tp=102.0)  # sl_dist 1.0 < 5.0
    r = validate_stops(sig, spec)
    assert not r.allowed
    assert r.breaker == "stops_level"


def test_validate_stops_allows_far_enough():
    spec = _spec(stops_level=10)  # min_dist = 0.10
    sig = _signal(entry=100.0, sl=99.0, tp=101.5)
    assert validate_stops(sig, spec).allowed
