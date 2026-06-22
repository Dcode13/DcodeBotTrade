"""Manajemen risiko: position sizing, circuit breaker, validasi pra-entry (§8).

Pure & testable. Spesifikasi kontrak (``SymbolSpec``) di-inject sebagai data
biasa, BUKAN dipanggil dari MT5 di sini -> bisa diuji tanpa terminal.

PRINSIP KERAS (§8, §20):
- Default risk_per_trade 1%. Tampilkan WARNING jika user set > 5%.
- Min-lot pada modal kecil -> default SKIP, bukan dipaksa entry.
- Circuit breaker tidak bisa di-bypass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.config import RiskConfig
from core.strategy import Signal


# --------------------------------------------------------------------------- #
# Spesifikasi simbol (diturunkan dari symbol_info -> §5.5)
# --------------------------------------------------------------------------- #
@dataclass
class SymbolSpec:
    name: str
    digits: int
    point: float
    trade_contract_size: float
    trade_tick_size: float
    trade_tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    trade_stops_level: int
    filling_mode: int = 0

    @property
    def money_per_unit(self) -> float:
        """Nilai uang per 1 unit pergerakan harga per 1.0 lot.

        money_per_unit = tick_value / tick_size  (§8.1)
        """
        if self.trade_tick_size <= 0:
            return 0.0
        return self.trade_tick_value / self.trade_tick_size


# --------------------------------------------------------------------------- #
# Hasil sizing
# --------------------------------------------------------------------------- #
@dataclass
class SizingResult:
    ok: bool
    lots: float
    risk_amount: float
    value_per_lot: float          # kerugian per 1.0 lot bila kena SL
    sl_distance: float
    min_lot_risk: float           # risiko aktual bila dipaksa volume_min
    reason: str
    warning: str = ""


def _round_to_step(value: float, step: float) -> float:
    """Floor ``value`` ke kelipatan ``step`` (toleransi float kecil)."""
    if step <= 0:
        return round(value, 8)
    steps = math.floor(value / step + 1e-9)
    return round(steps * step, 8)


def compute_position_size(
    equity: float,
    sl_distance: float,
    spec: SymbolSpec,
    risk: RiskConfig,
) -> SizingResult:
    """Hitung ukuran lot sesuai §8.1.

    Mengembalikan ``SizingResult``. ``ok=False`` berarti SKIP (mis. lot minimum
    melebihi target risiko dan override mati).
    """
    risk_amount = equity * risk.risk_per_trade
    money_per_unit = spec.money_per_unit
    value_per_lot = sl_distance * money_per_unit

    if value_per_lot <= 0 or money_per_unit <= 0:
        return SizingResult(False, 0.0, risk_amount, value_per_lot, sl_distance,
                            0.0, "value_per_lot <= 0 (spesifikasi/SL tidak valid)")

    min_lot_risk = spec.volume_min * value_per_lot

    lots_raw = risk_amount / value_per_lot
    lots = _round_to_step(lots_raw, spec.volume_step)

    # Lot hasil pembulatan lebih kecil dari volume_min -> lot minimum saja sudah
    # melebihi target risiko (§8.1 kasus modal kecil).
    if lots < spec.volume_min:
        if not risk.allow_min_lot_override:
            return SizingResult(
                ok=False,
                lots=0.0,
                risk_amount=risk_amount,
                value_per_lot=value_per_lot,
                sl_distance=sl_distance,
                min_lot_risk=min_lot_risk,
                reason=(
                    f"SKIP: lot minimum {spec.volume_min} berisiko "
                    f"{min_lot_risk:.2f} > target {risk_amount:.2f}. "
                    f"Set allow_min_lot_override=true untuk memaksa."
                ),
            )
        # Override: paksa volume_min, beri warning eksplisit.
        actual_risk_pct = (min_lot_risk / equity * 100.0) if equity > 0 else 0.0
        return SizingResult(
            ok=True,
            lots=spec.volume_min,
            risk_amount=risk_amount,
            value_per_lot=value_per_lot,
            sl_distance=sl_distance,
            min_lot_risk=min_lot_risk,
            reason="OK (override lot minimum)",
            warning=(
                f"OVERRIDE: memakai lot minimum {spec.volume_min}. Risiko aktual "
                f"~{min_lot_risk:.2f} ({actual_risk_pct:.1f}% equity), DI ATAS target "
                f"{risk.risk_per_trade * 100:.1f}%."
            ),
        )

    lots = min(lots, spec.volume_max)
    if lots < spec.volume_min:  # jaga-jaga setelah clamp atas
        lots = spec.volume_min

    return SizingResult(
        ok=True,
        lots=lots,
        risk_amount=risk_amount,
        value_per_lot=value_per_lot,
        sl_distance=sl_distance,
        min_lot_risk=min_lot_risk,
        reason="OK",
    )


def fixed_position_size(
    equity: float,
    sl_distance: float,
    spec: SymbolSpec,
    risk: RiskConfig,
) -> SizingResult:
    """Lot TETAP (``risk.fixed_lot``), bukan dihitung dari risk_per_trade.

    Lot tetap dipakai apa adanya (di-clamp ke volume_min/max/step). Risiko per
    trade jadi BERVARIASI mengikuti jarak SL. Beri WARNING bila risiko lot itu
    melewati ``risk_warn_threshold`` dari equity.
    """
    money_per_unit = spec.money_per_unit
    value_per_lot = sl_distance * money_per_unit
    risk_amount = equity * risk.risk_per_trade

    if value_per_lot <= 0 or money_per_unit <= 0:
        return SizingResult(False, 0.0, risk_amount, value_per_lot, sl_distance,
                            0.0, "value_per_lot <= 0 (spesifikasi/SL tidak valid)")

    lots = _round_to_step(risk.fixed_lot, spec.volume_step)
    lots = max(spec.volume_min, min(lots, spec.volume_max))

    risk_at_lot = lots * value_per_lot
    risk_pct = (risk_at_lot / equity * 100.0) if equity > 0 else 0.0

    warning = ""
    if risk_at_lot > equity * risk.risk_warn_threshold:
        warning = (
            f"⚠️ Lot tetap {lots} berisiko ~{risk_at_lot:.2f} "
            f"({risk_pct:.1f}% equity) > ambang {risk.risk_warn_threshold * 100:.0f}%. "
            f"Pertimbangkan lot lebih kecil."
        )

    return SizingResult(
        ok=True,
        lots=lots,
        risk_amount=risk_amount,
        value_per_lot=value_per_lot,
        sl_distance=sl_distance,
        min_lot_risk=spec.volume_min * value_per_lot,
        reason=f"OK (lot tetap {lots}, risiko ~{risk_at_lot:.2f} = {risk_pct:.1f}% equity)",
        warning=warning,
    )


def size_position(
    equity: float,
    sl_distance: float,
    spec: SymbolSpec,
    risk: RiskConfig,
) -> SizingResult:
    """Dispatcher sizing sesuai ``risk.position_sizing_mode`` ("risk" | "fixed")."""
    if risk.position_sizing_mode == "fixed":
        return fixed_position_size(equity, sl_distance, spec, risk)
    return compute_position_size(equity, sl_distance, spec, risk)


# --------------------------------------------------------------------------- #
# Circuit breakers (§8.2) - state harian
# --------------------------------------------------------------------------- #
@dataclass
class DayState:
    """State harian untuk circuit breaker (dipersist via journal)."""

    day: str                       # "YYYY-MM-DD"
    start_equity: float
    trades_today: int = 0
    consecutive_losses: int = 0
    paused: bool = False

    def drawdown_pct(self, equity: float) -> float:
        if self.start_equity <= 0:
            return 0.0
        return (self.start_equity - equity) / self.start_equity


@dataclass
class BreakerResult:
    allowed: bool
    reason: str
    breaker: str = ""  # nama breaker yang aktif (untuk alert)


def check_circuit_breakers(
    state: DayState, equity: float, risk: RiskConfig
) -> BreakerResult:
    """Cek semua circuit breaker. ``allowed=False`` -> JANGAN entry."""
    if state.paused:
        return BreakerResult(False, "Bot di-pause (butuh /resume)", "paused")

    dd = state.drawdown_pct(equity)
    if dd >= risk.max_daily_loss_pct:
        return BreakerResult(
            False,
            f"Max daily loss tercapai: DD {dd * 100:.2f}% >= "
            f"{risk.max_daily_loss_pct * 100:.2f}%",
            "max_daily_loss",
        )

    if state.consecutive_losses >= risk.max_consecutive_losses:
        return BreakerResult(
            False,
            f"Loss beruntun {state.consecutive_losses} >= "
            f"{risk.max_consecutive_losses} (butuh /resume)",
            "max_consecutive_losses",
        )

    if state.trades_today >= risk.max_trades_per_day:
        return BreakerResult(
            False,
            f"Batas trade harian tercapai: {state.trades_today} >= "
            f"{risk.max_trades_per_day}",
            "max_trades_per_day",
        )

    return BreakerResult(True, "OK")


# --------------------------------------------------------------------------- #
# Filter pra-entry (§8.3)
# --------------------------------------------------------------------------- #
def check_spread(spread_points: float, risk: RiskConfig) -> BreakerResult:
    if spread_points > risk.max_spread_points:
        return BreakerResult(
            False,
            f"Spread {spread_points:.0f} > maksimum {risk.max_spread_points:.0f} pts",
            "spread",
        )
    return BreakerResult(True, "OK")


def validate_stops(signal: Signal, spec: SymbolSpec) -> BreakerResult:
    """Hormati ``trade_stops_level`` (§7.4).

    Jarak SL & TP dari entry harus >= ``trade_stops_level * point``.
    """
    min_dist = spec.trade_stops_level * spec.point
    sl_dist = abs(signal.entry - signal.sl)
    tp_dist = abs(signal.entry - signal.tp)

    if min_dist > 0 and sl_dist < min_dist:
        return BreakerResult(
            False,
            f"Jarak SL {sl_dist:.{spec.digits}f} < minimum broker "
            f"{min_dist:.{spec.digits}f} (stops_level)",
            "stops_level",
        )
    if min_dist > 0 and tp_dist < min_dist:
        return BreakerResult(
            False,
            f"Jarak TP {tp_dist:.{spec.digits}f} < minimum broker "
            f"{min_dist:.{spec.digits}f} (stops_level)",
            "stops_level",
        )
    return BreakerResult(True, "OK")


def high_risk_warning(risk: RiskConfig) -> str | None:
    """Pesan WARNING jika risk_per_trade melewati ambang (§8, §20)."""
    if risk.risk_per_trade > risk.risk_warn_threshold:
        return (
            f"⚠️ WARNING: risk_per_trade = {risk.risk_per_trade * 100:.1f}% "
            f"> {risk.risk_warn_threshold * 100:.1f}%. Ini SANGAT berisiko "
            f"(beberapa loss beruntun bisa menghabiskan akun)."
        )
    return None
