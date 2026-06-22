"""Unit test deteksi swing & pemilihan zona (§7.2, §19)."""

from __future__ import annotations

import pandas as pd

from core.config import StrategyConfig
from core.strategy import find_swings, select_zone


def make_df(highs: list[float], lows: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(highs), freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": lows,
            "high": highs,
            "low": lows,
            "close": highs,
        },
        index=idx,
    )


def test_find_swings_basic():
    highs = [10, 11, 12, 13, 15, 13, 12, 11, 14, 12, 10]
    lows = [10, 9, 8, 7, 5, 7, 8, 9, 6, 8, 10]
    df = make_df(highs, lows)
    sh, sl = find_swings(df, pivot_n=2, lookback=60)

    sh_idx = {s.index for s in sh}
    sl_idx = {s.index for s in sl}
    # swing high di i=4 (15) & i=8 (14); swing low di i=4 (5) & i=8 (6).
    assert 4 in sh_idx and 8 in sh_idx
    assert 4 in sl_idx and 8 in sl_idx
    # harga swing low benar.
    sl_prices = {round(s.price, 2) for s in sl}
    assert 5.0 in sl_prices and 6.0 in sl_prices


def test_select_zone_up_picks_highest_support_below_price():
    highs = [10, 11, 12, 13, 15, 13, 12, 11, 14, 12, 10]
    lows = [10, 9, 8, 7, 5, 7, 8, 9, 6, 8, 10]
    df = make_df(highs, lows)
    cfg = StrategyConfig(swing_pivot_n=2, swing_lookback=60)
    # Bias UP, harga 7 -> support di bawah: {5, 6} -> ambil tertinggi = 6.
    zone = select_zone(df, "UP", price=7.0, cfg=cfg)
    assert zone == 6.0


def test_select_zone_down_picks_lowest_resistance_above_price():
    highs = [10, 11, 12, 13, 15, 13, 12, 11, 14, 12, 10]
    lows = [10, 9, 8, 7, 5, 7, 8, 9, 6, 8, 10]
    df = make_df(highs, lows)
    cfg = StrategyConfig(swing_pivot_n=2, swing_lookback=60)
    # Bias DOWN, harga 13 -> resistance di atas: {15, 14} -> ambil terendah = 14.
    zone = select_zone(df, "DOWN", price=13.0, cfg=cfg)
    assert zone == 14.0


def test_select_zone_none_when_no_candidate():
    highs = [10, 11, 12, 13, 15, 13, 12, 11, 14, 12, 10]
    lows = [10, 9, 8, 7, 5, 7, 8, 9, 6, 8, 10]
    df = make_df(highs, lows)
    cfg = StrategyConfig(swing_pivot_n=2, swing_lookback=60)
    # Harga 1 -> tidak ada support di bawahnya.
    assert select_zone(df, "UP", price=1.0, cfg=cfg) is None
