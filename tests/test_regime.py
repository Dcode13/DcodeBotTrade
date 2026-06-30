"""Unit test filter rezim pasar (anti-sideways) + indikator ADX. Offline & pure."""

from __future__ import annotations

import numpy as np
import pandas as pd

from core import indicators
from core import regime as reg_mod
from core.config import RegimeConfig, StrategyConfig


def _trend_df(direction: str, n: int = 260, step: float = 2.0) -> pd.DataFrame:
    """Tren jelas & mulus -> ADX tinggi, bias EMA searah."""
    rows = []
    base = 2000.0
    for i in range(n):
        if direction == "up":
            o = base + i * step
            c = o + step
        else:
            o = base - i * step
            c = o - step
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        rows.append({"open": o, "high": h, "low": l, "close": c})
    idx = pd.date_range("2026-01-01", periods=n, freq="min")
    return pd.DataFrame(rows, index=idx)


def _range_df(n: int = 320, seed: int = 7) -> pd.DataFrame:
    """Pasar sideways: random-walk mean-reverting di sekitar base.

    Tanpa tren bersih -> ADX rendah & EMA fast/slow mendatar (bias NONE).
    """
    rng = np.random.default_rng(seed)
    base = 2000.0
    c_prev = base
    rows = []
    for _ in range(n):
        # tarik balik ke base (mean reversion) + noise kecil -> choppy, tak terarah.
        c = c_prev + 0.25 * (base - c_prev) + rng.normal(0.0, 1.2)
        o = c_prev
        h = max(o, c) + abs(rng.normal(0.0, 0.4))
        l = min(o, c) - abs(rng.normal(0.0, 0.4))
        rows.append({"open": o, "high": h, "low": l, "close": c})
        c_prev = c
    idx = pd.date_range("2026-01-01", periods=n, freq="min")
    return pd.DataFrame(rows, index=idx)


def _strat() -> StrategyConfig:
    return StrategyConfig(ema_fast=50, ema_slow=200)


# --------------------------------------------------------------------------- #
# ADX
# --------------------------------------------------------------------------- #
def test_adx_high_in_strong_trend():
    out = indicators.adx(_trend_df("up"), 14)
    assert float(out["adx"].iloc[-2]) > 40.0
    assert float(out["plus_di"].iloc[-2]) > float(out["minus_di"].iloc[-2])


def test_adx_low_in_range():
    out = indicators.adx(_range_df(), 14)
    assert float(out["adx"].iloc[-2]) < 25.0


# --------------------------------------------------------------------------- #
# Regime
# --------------------------------------------------------------------------- #
def _df_map(dfs: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return dfs


def test_regime_all_up_returns_buy():
    up = _trend_df("up")
    res = reg_mod.assess({"H1": up, "M15": up, "M5": up, "M3": up}, _strat(), RegimeConfig())
    assert res.direction == "BUY"
    assert res.trending is True


def test_regime_all_down_returns_sell():
    down = _trend_df("down")
    res = reg_mod.assess({"H1": down, "M15": down, "M5": down, "M3": down}, _strat(), RegimeConfig())
    assert res.direction == "SELL"


def test_regime_sideways_blocks():
    rng = _range_df()
    res = reg_mod.assess({"H1": rng, "M15": rng, "M5": rng, "M3": rng}, _strat(), RegimeConfig())
    assert res.direction is None
    assert "SIDEWAYS" in res.reason


def test_regime_conflicting_tf_blocks():
    up = _trend_df("up")
    down = _trend_df("down")
    # H1 up tapi M5 down -> arah tak sepakat -> blok.
    res = reg_mod.assess({"H1": up, "M15": up, "M5": down, "M3": up}, _strat(), RegimeConfig())
    assert res.direction is None


def test_regime_missing_data_blocks():
    up = _trend_df("up")
    res = reg_mod.assess({"H1": up, "M15": up, "M5": up, "M3": pd.DataFrame()}, _strat(), RegimeConfig())
    assert res.direction is None
    assert "belum cukup" in res.reason


def test_regime_disabled_passes_through():
    up = _trend_df("up")
    res = reg_mod.assess({"H1": up}, _strat(), RegimeConfig(enabled=False))
    assert res.direction is None
    assert "off" in res.reason
