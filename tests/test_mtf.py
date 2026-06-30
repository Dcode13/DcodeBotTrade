"""Unit test entry alignment multi-timeframe (evaluate_mtf), offline & pure."""

from __future__ import annotations

import pandas as pd

from core.config import MTFConfig, StrategyConfig
from core import strategy as strat


def _trend_df(direction: str, n: int = 230, step: float = 2.0) -> pd.DataFrame:
    """DataFrame tren jelas: close naik (up) / turun (down), body candle besar."""
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


def _cfg() -> StrategyConfig:
    # Matikan filter RSI agar tren sintetik (RSI ekstrem) tetap lolos trigger.
    return StrategyConfig(rsi_filter=False, ema_fast=50, ema_slow=200)


def test_mtf_all_up_triggers_buy():
    cfg = _cfg()
    up = _trend_df("up")
    sig, reason = strat.evaluate_mtf(up, up, up, up, cfg, MTFConfig(mode="all"),
                                     bid=2459.0, ask=2459.2)
    assert sig is not None, reason
    assert sig.direction == "BUY"
    assert sig.sl < sig.entry < sig.tp
    assert sig.sl_distance > 0


def test_mtf_all_down_triggers_sell():
    cfg = _cfg()
    down = _trend_df("down")
    sig, reason = strat.evaluate_mtf(down, down, down, down, cfg, MTFConfig(mode="all"),
                                     bid=1541.0, ask=1541.2)
    assert sig is not None, reason
    assert sig.direction == "SELL"
    assert sig.tp < sig.entry < sig.sl


def test_mtf_all_mode_not_aligned_returns_none():
    cfg = _cfg()
    up = _trend_df("up")
    down = _trend_df("down")
    # H1 up, M15 up, M5 DOWN -> mode "all" gagal.
    sig, reason = strat.evaluate_mtf(up, up, down, up, cfg, MTFConfig(mode="all"),
                                     bid=2000.0, ask=2000.2)
    assert sig is None
    assert "belum align" in reason


def test_mtf_majority_mode_allows_two_of_three():
    cfg = _cfg()
    up = _trend_df("up")
    down = _trend_df("down")
    # H1 up, M15 up, M5 down -> mayoritas UP & H1 bukan DOWN -> BUY.
    sig, reason = strat.evaluate_mtf(up, up, down, up, cfg, MTFConfig(mode="majority"),
                                     bid=2459.0, ask=2459.2)
    assert sig is not None, reason
    assert sig.direction == "BUY"


def test_mtf_h1_only_follows_h1_bias():
    cfg = _cfg()
    up = _trend_df("up")
    down = _trend_df("down")
    # mode "h1": ikut H1 saja. H1 down -> SELL walau M15/M5 up.
    sig, reason = strat.evaluate_mtf(down, up, up, down, cfg, MTFConfig(mode="h1"),
                                     bid=1541.0, ask=1541.2)
    assert sig is not None, reason
    assert sig.direction == "SELL"
