"""Unit test position sizing & kasus modal kecil (§8.1, §19)."""

from __future__ import annotations

from core.config import RiskConfig
from core.risk_manager import (
    SymbolSpec,
    compute_position_size,
    fixed_position_size,
    size_position,
)


def make_spec(volume_min: float = 0.01, tick_value: float = 0.01,
              tick_size: float = 0.01) -> SymbolSpec:
    return SymbolSpec(
        name="BTCUSD",
        digits=2,
        point=0.01,
        trade_contract_size=1.0,
        trade_tick_size=tick_size,
        trade_tick_value=tick_value,
        volume_min=volume_min,
        volume_max=10.0,
        volume_step=0.01,
        trade_stops_level=0,
    )


def test_basic_sizing():
    spec = make_spec()  # money_per_unit = 0.01/0.01 = 1.0
    risk = RiskConfig(risk_per_trade=0.01)
    # equity 2000 (cent) -> risk_amount 20; sl_distance 100 -> value_per_lot 100.
    r = compute_position_size(equity=2000.0, sl_distance=100.0, spec=spec, risk=risk)
    assert r.ok
    assert r.value_per_lot == 100.0
    assert abs(r.lots - 0.2) < 1e-9  # 20 / 100 = 0.2


def test_lots_rounded_down_to_step():
    spec = make_spec()
    risk = RiskConfig(risk_per_trade=0.01)
    # risk_amount 20, value_per_lot 130 -> 0.1538 -> floor ke step 0.01 = 0.15
    r = compute_position_size(equity=2000.0, sl_distance=130.0, spec=spec, risk=risk)
    assert r.ok
    assert abs(r.lots - 0.15) < 1e-9


def test_min_lot_skip_when_override_off():
    spec = make_spec()
    risk = RiskConfig(risk_per_trade=0.01, allow_min_lot_override=False)
    # sl_distance 3000 -> value_per_lot 3000; risk_amount 20 -> lots_raw 0.0067 < min.
    r = compute_position_size(equity=2000.0, sl_distance=3000.0, spec=spec, risk=risk)
    assert not r.ok
    assert "lot minimum" in r.reason.lower()
    assert r.min_lot_risk == 30.0  # 0.01 * 3000


def test_min_lot_override_on_warns():
    spec = make_spec()
    risk = RiskConfig(risk_per_trade=0.01, allow_min_lot_override=True)
    r = compute_position_size(equity=2000.0, sl_distance=3000.0, spec=spec, risk=risk)
    assert r.ok
    assert abs(r.lots - spec.volume_min) < 1e-9
    assert r.warning != ""
    assert "OVERRIDE" in r.warning


def test_clamp_to_volume_max():
    spec = make_spec()
    risk = RiskConfig(risk_per_trade=0.5)  # sengaja besar
    # risk_amount 1000, value_per_lot 1 -> lots_raw 1000 -> clamp ke volume_max 10.
    r = compute_position_size(equity=2000.0, sl_distance=1.0, spec=spec, risk=risk)
    assert r.ok
    assert r.lots == spec.volume_max


def test_fixed_lot_used_as_is():
    spec = make_spec()
    risk = RiskConfig(position_sizing_mode="fixed", fixed_lot=0.1)
    r = fixed_position_size(equity=2000.0, sl_distance=50.0, spec=spec, risk=risk)
    assert r.ok
    assert abs(r.lots - 0.1) < 1e-9
    # risiko = lot * sl_distance * money_per_unit = 0.1 * 50 * 1 = 5.0
    assert abs(r.lots * r.value_per_lot - 5.0) < 1e-9


def test_fixed_lot_clamped_to_min():
    spec = make_spec(volume_min=0.5)
    risk = RiskConfig(position_sizing_mode="fixed", fixed_lot=0.1)
    r = fixed_position_size(equity=2000.0, sl_distance=50.0, spec=spec, risk=risk)
    assert r.lots == 0.5  # dipaksa ke volume_min


def test_fixed_lot_warns_when_risk_high():
    spec = make_spec()
    risk = RiskConfig(position_sizing_mode="fixed", fixed_lot=0.1, risk_warn_threshold=0.05)
    # sl_distance 2000 -> risiko 0.1*2000=200 = 10% equity 2000 -> warning
    r = fixed_position_size(equity=2000.0, sl_distance=2000.0, spec=spec, risk=risk)
    assert r.ok and r.warning != ""


def test_size_position_dispatches_by_mode():
    spec = make_spec()
    fixed = size_position(2000.0, 50.0, spec, RiskConfig(position_sizing_mode="fixed", fixed_lot=0.1))
    assert abs(fixed.lots - 0.1) < 1e-9
    risk = size_position(2000.0, 100.0, spec, RiskConfig(position_sizing_mode="risk", risk_per_trade=0.01))
    assert abs(risk.lots - 0.2) < 1e-9


def test_invalid_spec_returns_not_ok():
    spec = make_spec(tick_size=0.0)  # money_per_unit -> 0
    risk = RiskConfig()
    r = compute_position_size(equity=2000.0, sl_distance=100.0, spec=spec, risk=risk)
    assert not r.ok
