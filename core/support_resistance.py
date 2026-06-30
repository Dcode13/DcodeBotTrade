"""Analisis Support/Resistance multi-timeframe (M5, M15, H1) + entry di M5.

Alur:
  1. Deteksi swing high (resistance) & swing low (support) di tiap TF (fractal).
  2. Gabungkan level berdekatan lintas-TF jadi ZONA (confluence). Zona yang
     didukung TF lebih tinggi / lebih banyak sentuhan -> lebih kuat.
  3. Entry di M5:
       - harga di SUPPORT + candle M5 bullish  -> BUY  (bounce)
       - harga di RESISTANCE + candle M5 bearish -> SELL (rejection)
     SL ditaruh di luar zona; TP berbasis RR (info: zona lawan terdekat).

Pure: hanya pandas. Tanpa MT5. Mudah diuji.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.config import SRConfig
from core.strategy import Signal, find_swings

# Bobot kepentingan per timeframe (TF lebih tinggi = level lebih kuat).
_TF_WEIGHT = {"M1": 1, "M5": 1, "M15": 2, "M30": 3, "H1": 4, "H4": 6, "D1": 8}


@dataclass
class SRLevel:
    price: float
    kind: str                       # "support" | "resistance"
    tfs: list[str] = field(default_factory=list)
    touches: int = 0

    @property
    def strength(self) -> int:
        return self.touches + sum(_TF_WEIGHT.get(tf, 1) for tf in set(self.tfs))


@dataclass
class SRMap:
    supports: list[SRLevel] = field(default_factory=list)
    resistances: list[SRLevel] = field(default_factory=list)

    def nearest(self, kind: str, price: float) -> SRLevel | None:
        levels = self.supports if kind == "support" else self.resistances
        if not levels:
            return None
        return min(levels, key=lambda lv: abs(lv.price - price))

    def next_resistance_above(self, price: float) -> SRLevel | None:
        cand = [lv for lv in self.resistances if lv.price > price]
        return min(cand, key=lambda lv: lv.price - price) if cand else None

    def next_support_below(self, price: float) -> SRLevel | None:
        cand = [lv for lv in self.supports if lv.price < price]
        return min(cand, key=lambda lv: price - lv.price) if cand else None

    def all_sorted(self) -> list[SRLevel]:
        return sorted(self.supports + self.resistances, key=lambda lv: lv.price)


# --------------------------------------------------------------------------- #
def _cluster(points: list[tuple[float, str]], kind: str, gap: float) -> list[SRLevel]:
    """Gabungkan titik (price, tf) yang berdekatan (<= gap) jadi satu SRLevel."""
    if not points:
        return []
    points = sorted(points, key=lambda p: p[0])
    levels: list[SRLevel] = []
    cur_prices = [points[0][0]]
    cur_tfs = [points[0][1]]
    for price, tf in points[1:]:
        if price - cur_prices[-1] <= gap:
            cur_prices.append(price)
            cur_tfs.append(tf)
        else:
            levels.append(SRLevel(
                price=sum(cur_prices) / len(cur_prices), kind=kind,
                tfs=list(cur_tfs), touches=len(cur_prices)))
            cur_prices = [price]
            cur_tfs = [tf]
    levels.append(SRLevel(
        price=sum(cur_prices) / len(cur_prices), kind=kind,
        tfs=list(cur_tfs), touches=len(cur_prices)))
    return levels


def detect_levels(dfs: dict[str, pd.DataFrame], cfg: SRConfig, pip_size: float) -> SRMap:
    """Bangun SRMap dari swing high/low di tiap TF pada ``dfs`` ({tf: df})."""
    sup_points: list[tuple[float, str]] = []
    res_points: list[tuple[float, str]] = []
    for tf, df in dfs.items():
        if df is None or df.empty:
            continue
        highs, lows = find_swings(df, cfg.pivot_n, cfg.lookback)
        for s in highs:
            res_points.append((s.price, tf))
        for s in lows:
            sup_points.append((s.price, tf))

    gap = cfg.cluster_pips * pip_size
    supports = _cluster(sup_points, "support", gap)
    resistances = _cluster(res_points, "resistance", gap)
    # Filter kekuatan minimum.
    supports = [lv for lv in supports if lv.strength >= cfg.min_strength]
    resistances = [lv for lv in resistances if lv.strength >= cfg.min_strength]
    return SRMap(supports=supports, resistances=resistances)


# --------------------------------------------------------------------------- #
def _body_ratio_last_closed(df_m5: pd.DataFrame) -> tuple[float, float, float]:
    """(open, close, body_ratio) candle M5 terakhir yang sudah close."""
    bar = df_m5.iloc[-2]
    o, c = float(bar["open"]), float(bar["close"])
    h, l = float(bar["high"]), float(bar["low"])
    rng = h - l
    ratio = (abs(c - o) / rng) if rng > 0 else 0.0
    return o, c, ratio


def evaluate_sr(
    sr: SRMap,
    df_m5: pd.DataFrame,
    bid: float,
    ask: float,
    cfg: SRConfig,
    pip_size: float,
) -> tuple[Signal | None, str]:
    """Entry di M5 berdasar S/R. Return (Signal|None, alasan)."""
    if df_m5 is None or len(df_m5) < 3:
        return None, "S/R: data M5 kurang"
    price = float(df_m5["close"].iloc[-2])
    tol = cfg.touch_pips * pip_size
    buf = cfg.sl_buffer_pips * pip_size
    o, c, body = _body_ratio_last_closed(df_m5)
    bullish = c > o
    bearish = c < o
    sig_time = df_m5.index[-2]

    sup = sr.nearest("support", price)
    res = sr.nearest("resistance", price)

    # --- Di SUPPORT -> BUY ---
    if sup is not None and abs(price - sup.price) <= tol:
        if cfg.require_m5_candle and not (bullish and body >= cfg.min_body_ratio):
            return None, (f"S/R: di support {sup.price:.2f} (str {sup.strength}) "
                          f"tunggu candle M5 bullish (body {body:.2f})")
        entry = ask
        sl = sup.price - buf
        sl_dist = entry - sl
        if sl_dist <= 0:
            return None, "S/R: sl_dist<=0 di support"
        tp = entry + cfg.rr_ratio * sl_dist
        nxt = sr.next_resistance_above(price)
        nxt_txt = f" -> target R {nxt.price:.2f}" if nxt else ""
        signal = Signal(
            direction="BUY", entry=entry, sl=sl, tp=tp, sl_distance=sl_dist,
            zone=sup.price, atr_m1=sl_dist, bias="UP", signal_bar_time=sig_time,
            body_ratio=body, rsi_m1=0.0,
            reason=(f"S/R BUY di support {sup.price:.2f} "
                    f"[{','.join(sorted(set(sup.tfs)))}] str {sup.strength}{nxt_txt}"),
        )
        return signal, signal.reason

    # --- Di RESISTANCE -> SELL ---
    if res is not None and abs(price - res.price) <= tol:
        if cfg.require_m5_candle and not (bearish and body >= cfg.min_body_ratio):
            return None, (f"S/R: di resistance {res.price:.2f} (str {res.strength}) "
                          f"tunggu candle M5 bearish (body {body:.2f})")
        entry = bid
        sl = res.price + buf
        sl_dist = sl - entry
        if sl_dist <= 0:
            return None, "S/R: sl_dist<=0 di resistance"
        tp = entry - cfg.rr_ratio * sl_dist
        nxt = sr.next_support_below(price)
        nxt_txt = f" -> target S {nxt.price:.2f}" if nxt else ""
        signal = Signal(
            direction="SELL", entry=entry, sl=sl, tp=tp, sl_distance=sl_dist,
            zone=res.price, atr_m1=sl_dist, bias="DOWN", signal_bar_time=sig_time,
            body_ratio=body, rsi_m1=0.0,
            reason=(f"S/R SELL di resistance {res.price:.2f} "
                    f"[{','.join(sorted(set(res.tfs)))}] str {res.strength}{nxt_txt}"),
        )
        return signal, signal.reason

    # Tidak di zona manapun.
    ds = abs(price - sup.price) / pip_size if sup else None
    dr = abs(price - res.price) / pip_size if res else None
    parts = []
    if sup:
        parts.append(f"support {sup.price:.2f} ({ds:.0f}p)")
    if res:
        parts.append(f"resistance {res.price:.2f} ({dr:.0f}p)")
    return None, "S/R: harga belum di zona | " + " | ".join(parts) if parts else "S/R: tak ada level"
