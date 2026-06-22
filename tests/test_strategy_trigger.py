"""Unit test trigger candle M1 & bias M15 (§7.1, §7.3, §19)."""

from __future__ import annotations

import pandas as pd

from core.config import StrategyConfig
from core.strategy import compute_bias, evaluate_trigger


def make_ohlc(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def _flat_then(signal_bar: tuple, n: int = 20) -> pd.DataFrame:
    """n bar flat, lalu signal_bar di posisi -2, lalu satu bar 'forming' -1."""
    rows = [(100.0, 100.5, 99.5, 100.0)] * n
    rows[-2] = signal_bar
    return make_ohlc(rows)


def test_bullish_confirmation_signal():
    cfg = StrategyConfig(rsi_filter=False, min_body_ratio=0.5)
    # signal bar: bullish, body 1.0 / range 1.2 = 0.83 >= 0.5
    df = _flat_then((100.0, 101.1, 99.9, 101.0))
    trig = evaluate_trigger(df, "UP", cfg)
    assert trig.is_signal
    assert trig.body_ratio >= 0.5


def test_doji_rejected_by_body_ratio():
    cfg = StrategyConfig(rsi_filter=False, min_body_ratio=0.5)
    # body 0.1 / range 1.0 = 0.1 < 0.5
    df = _flat_then((100.0, 100.6, 99.6, 100.1))
    trig = evaluate_trigger(df, "UP", cfg)
    assert not trig.is_signal
    assert "body_ratio" in trig.reason


def test_wrong_direction_candle_rejected():
    cfg = StrategyConfig(rsi_filter=False, min_body_ratio=0.5)
    # bias UP tapi candle bearish (close < open)
    df = _flat_then((101.0, 101.1, 99.9, 100.0))
    trig = evaluate_trigger(df, "UP", cfg)
    assert not trig.is_signal
    assert "bullish" in trig.reason


def test_rsi_overbought_blocks_buy():
    cfg = StrategyConfig(rsi_filter=True, rsi_overbought=50.0, min_body_ratio=0.5)
    # uptrend kuat -> RSI ~100 -> blokir BUY walau candle bullish kuat.
    rows = [(100.0 + i, 100.6 + i, 99.6 + i, 100.5 + i) for i in range(20)]
    # pastikan bar -2 bullish kuat
    rows[-2] = (118.0, 119.1, 117.9, 119.0)
    df = make_ohlc(rows)
    trig = evaluate_trigger(df, "UP", cfg)
    assert not trig.is_signal
    assert "RSI" in trig.reason


def test_bias_up_and_down():
    cfg = StrategyConfig(ema_fast=3, ema_slow=5)
    up = make_ohlc([(100 + i, 100 + i, 100 + i, 100 + i) for i in range(10)])
    assert compute_bias(up, cfg) == "UP"

    down = make_ohlc([(100 - i, 100 - i, 100 - i, 100 - i) for i in range(10)])
    assert compute_bias(down, cfg) == "DOWN"


def test_bias_none_when_insufficient_data():
    cfg = StrategyConfig()  # butuh ema_slow(200)+2 bar
    df = make_ohlc([(100, 100, 100, 100)] * 10)
    assert compute_bias(df, cfg) == "NONE"
