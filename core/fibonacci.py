"""Analitik Fibonacci (retracement & extension) untuk XAUUSD.

Dipakai dua hal:
  1. Konteks/analitik (ditampilkan di /fib & alert): level fib dari leg swing.
  2. Entry "market bagus" (trend-continuation): bila harga retrace ke GOLDEN ZONE
     (0.5-0.786) searah bias CRT + ada konfirmasi CHoCH -> entry searah tren.

Konvensi leg (parameter t: 0=awal leg, 1=akhir leg):
  - UP leg (direction +1): awal=low, akhir=high. price(t) = low + t*range.
      retracement ratio r -> price = high - r*range (pullback turun dari high).
      extension   ratio r -> price = low  + r*range (target lanjut di atas high).
  - DOWN leg (direction -1): awal=high, akhir=low. price(t) = high - t*range.
      retracement ratio r -> price = low  + r*range.
      extension   ratio r -> price = high - r*range.

Pure: hanya pandas. Tanpa MT5. Mudah diuji.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.config import FibConfig
from core.strategy import find_swings

# Rasio retracement & extension standar.
RETR_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.705, 0.786, 1.0]
EXT_RATIOS = [1.272, 1.414, 1.618, 2.0]


@dataclass
class FibLevels:
    direction: int                 # +1 up-leg, -1 down-leg
    low: float
    high: float
    levels: dict[float, float] = field(default_factory=dict)  # retracement ratio->price
    ext: dict[float, float] = field(default_factory=dict)     # extension ratio->price

    @property
    def rng(self) -> float:
        return self.high - self.low

    def golden_zone(self, cfg: FibConfig) -> tuple[float, float]:
        """(low, high) batas golden zone retracement (0.5-0.786)."""
        a = self._retr(cfg.gz_start)
        b = self._retr(cfg.gz_end)
        return (min(a, b), max(a, b))

    def _retr(self, r: float) -> float:
        if self.direction > 0:
            return self.high - r * self.rng
        return self.low + r * self.rng

    def in_golden_zone(self, price: float, cfg: FibConfig) -> bool:
        lo, hi = self.golden_zone(cfg)
        return lo <= price <= hi

    def nearest(self, price: float) -> tuple[float, float, float]:
        """Level fib retracement terdekat: (ratio, price, |jarak|)."""
        best = min(self.levels.items(), key=lambda kv: abs(kv[1] - price))
        return best[0], best[1], abs(best[1] - price)


# --------------------------------------------------------------------------- #
def compute(low: float, high: float, direction: int) -> FibLevels:
    """Bangun seluruh level fib dari leg [low, high] berarah ``direction``."""
    if high <= low:
        return FibLevels(direction, low, high, {}, {})
    rng = high - low
    levels: dict[float, float] = {}
    ext: dict[float, float] = {}
    for r in RETR_RATIOS:
        levels[r] = (high - r * rng) if direction > 0 else (low + r * rng)
    for r in EXT_RATIOS:
        ext[r] = (low + r * rng) if direction > 0 else (high - r * rng)
    return FibLevels(direction, low, high, levels, ext)


def recent_leg(
    df: pd.DataFrame, pivot_n: int, lookback: int
) -> tuple[float, float, int] | None:
    """Deteksi leg swing terakhir -> (low, high, direction).

    direction +1 bila ekstrem terbaru adalah swing HIGH (leg naik low->high),
    -1 bila swing LOW (leg turun high->low).
    """
    highs, lows = find_swings(df, pivot_n, lookback)
    if not highs or not lows:
        return None
    last_high = highs[-1]
    last_low = lows[-1]
    if last_high.index > last_low.index:
        direction = +1
    else:
        direction = -1
    low = min(last_high.price, last_low.price)
    high = max(last_high.price, last_low.price)
    if high <= low:
        return None
    return low, high, direction


def from_df(df: pd.DataFrame, cfg: FibConfig) -> FibLevels | None:
    """Hitung FibLevels dari leg swing terakhir pada ``df`` (untuk /fib & status)."""
    leg = recent_leg(df, cfg.pivot_n, cfg.lookback)
    if leg is None:
        return None
    low, high, direction = leg
    return compute(low, high, direction)
