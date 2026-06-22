"""Smoke test backtester pada data sintetis (tanpa MT5) (§14)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.backtester import BacktestResult, run_backtest
from core.config import StrategyConfig


def _synthetic_m1(n: int = 4200, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 5, n)
    close = 30000 + np.cumsum(steps)
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    high = close + np.abs(rng.normal(0, 8, n))
    low = close - np.abs(rng.normal(0, 8, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close}, index=idx
    )


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    return df.resample(rule).agg(agg).dropna()


def test_backtest_runs_without_crashing():
    m1 = _synthetic_m1()
    m5 = _resample(m1, "5min")
    m15 = _resample(m1, "15min")
    cfg = StrategyConfig()

    res = run_backtest(m15, m5, m1, cfg, spread_points=100, point=0.01,
                       slippage_points=20, warmup=250)
    assert isinstance(res, BacktestResult)
    assert res.trades >= 0
    assert res.equity_curve[0] == 0.0
    # konsistensi agregasi
    assert res.wins + res.losses == res.trades


def test_backtest_insufficient_data_returns_empty():
    m1 = _synthetic_m1(n=100)
    m5 = _resample(m1, "5min")
    m15 = _resample(m1, "15min")
    res = run_backtest(m15, m5, m1, StrategyConfig(), warmup=250)
    assert res.trades == 0
