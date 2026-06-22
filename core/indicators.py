"""Indikator teknikal - implementasi mandiri (tanpa dependensi MT5).

Semua fungsi menerima ``pandas.Series``/``DataFrame`` dan mengembalikan
``Series`` sepanjang input (nilai awal bisa NaN). Pure & mudah diuji.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.astype(float).ewm(span=period, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range klasik: max(H-L, |H-prevC|, |L-prevC|)."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing via EMA setara)."""
    tr = true_range(df)
    # Wilder = ewm dengan alpha = 1/period
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder)."""
    close = series.astype(float)
    delta = close.diff()

    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi_val = 100.0 - (100.0 / (1.0 + rs))
    # Saat avg_loss == 0 -> RSI 100; saat avg_gain == 0 -> RSI 0.
    rsi_val = rsi_val.where(avg_loss != 0.0, 100.0)
    rsi_val = rsi_val.where(avg_gain != 0.0, 0.0)
    return rsi_val
