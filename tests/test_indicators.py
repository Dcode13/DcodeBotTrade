"""Sanity test indikator EMA/ATR/RSI (§4, §19)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.indicators import atr, ema, rsi


def test_ema_constant_series_equals_constant():
    s = pd.Series([5.0] * 50)
    assert abs(ema(s, 10).iloc[-1] - 5.0) < 1e-9


def test_rsi_pure_uptrend_is_100():
    s = pd.Series([100.0 + i for i in range(30)])
    assert abs(rsi(s, 14).iloc[-1] - 100.0) < 1e-6


def test_rsi_pure_downtrend_is_0():
    s = pd.Series([100.0 - i for i in range(30)])
    assert abs(rsi(s, 14).iloc[-1] - 0.0) < 1e-6


def test_rsi_in_range():
    rng = np.random.default_rng(42)
    s = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    vals = rsi(s, 14).dropna()
    assert (vals >= 0).all() and (vals <= 100).all()


def test_atr_positive():
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, 100))
    df = pd.DataFrame({
        "open": close,
        "high": close + np.abs(rng.normal(0, 0.5, 100)),
        "low": close - np.abs(rng.normal(0, 0.5, 100)),
        "close": close,
    })
    a = atr(df, 14).dropna()
    assert (a > 0).all()
