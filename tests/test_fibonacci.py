"""Unit test analitik Fibonacci (pure, tanpa MT5)."""

from __future__ import annotations

import pandas as pd

from core.config import FibConfig
from core import fibonacci as fib


def test_up_leg_retracement_and_golden_zone():
    f = fib.compute(low=2000.0, high=2100.0, direction=+1)  # range 100
    assert f.levels[0.0] == 2100.0          # awal retr = high
    assert f.levels[1.0] == 2000.0          # 100% retr = low
    assert abs(f.levels[0.5] - 2050.0) < 1e-9
    assert abs(f.levels[0.618] - (2100.0 - 0.618 * 100)) < 1e-9
    gz_lo, gz_hi = f.golden_zone(FibConfig())
    # GZ 0.5..0.786 -> 2050.0 .. 2021.4
    assert abs(gz_hi - 2050.0) < 1e-9
    assert abs(gz_lo - (2100.0 - 0.786 * 100)) < 1e-9
    assert f.in_golden_zone(2035.0, FibConfig()) is True
    assert f.in_golden_zone(2090.0, FibConfig()) is False


def test_down_leg_retracement():
    f = fib.compute(low=2000.0, high=2100.0, direction=-1)
    assert f.levels[0.0] == 2000.0          # awal retr (down) = low
    assert f.levels[1.0] == 2100.0
    assert abs(f.levels[0.5] - 2050.0) < 1e-9
    gz_lo, gz_hi = f.golden_zone(FibConfig())
    assert abs(gz_lo - 2050.0) < 1e-9
    assert abs(gz_hi - (2000.0 + 0.786 * 100)) < 1e-9


def test_extensions():
    up = fib.compute(2000.0, 2100.0, +1)
    assert abs(up.ext[1.272] - (2000.0 + 1.272 * 100)) < 1e-9   # di atas high
    down = fib.compute(2000.0, 2100.0, -1)
    assert abs(down.ext[1.618] - (2100.0 - 1.618 * 100)) < 1e-9  # di bawah low


def test_nearest_level():
    f = fib.compute(2000.0, 2100.0, +1)
    ratio, price, dist = f.nearest(2051.0)
    assert ratio == 0.5
    assert price == 2050.0
    assert abs(dist - 1.0) < 1e-9


def test_recent_leg_detection():
    # Zigzag jelas -> swing high di idx 2,7,12 & swing low di idx 4,9 (pivot_n=2).
    mid = [2000, 2008, 2016, 2008, 2000, 2008, 2016, 2024, 2016, 2008,
           2016, 2024, 2032, 2024, 2016]
    rows = [{"open": m, "high": m + 2, "low": m - 2, "close": m + 1} for m in mid]
    df = pd.DataFrame(rows, index=pd.date_range("2026-01-01", periods=len(rows), freq="15min"))
    leg = fib.recent_leg(df, pivot_n=2, lookback=60)
    assert leg is not None
    low, high, direction = leg
    assert high > low
    assert direction in (+1, -1)


def test_from_df_returns_none_on_short_data():
    df = pd.DataFrame(
        [{"open": 1, "high": 2, "low": 0, "close": 1}],
        index=pd.date_range("2026-01-01", periods=1, freq="15min"),
    )
    assert fib.from_df(df, FibConfig()) is None
