"""Analisis teknikal CRT (port inti dari GridScalper_CRT EA, MQL5 -> Python).

Dipakai sebagai **lapisan konfirmasi** untuk strategi LBMA (gold). Yang di-port:

  Layer 1  - MSS H1 (Market Structure Shift) -> bias + leg displacement.
  Layer 2  - Golden Zone H1 (retracement 0.5-0.786) + cek OB / FVG overlap.
  Layer 3  - CHoCH / BOS M15 di dalam golden zone (break swing + sentuh zona).
  Layer 5  - Filter momentum body-ratio candle CHoCH.

Konvensi indeks ala MQL: ``i=0`` = bar terbaru (sedang berjalan), ``i=1`` =
bar terakhir yang sudah close. Helper ``_get`` memetakan ke ``df.iloc[-1-i]``.

CATATAN: deteksi MSS/CHoCH/OB/FVG bersifat HEURISTIK (sama seperti EA aslinya).
Ini konfirmasi/konteks, bukan jaminan. Wajib backtest sebelum live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from core.config import CRTConfig

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
@dataclass
class CRTContext:
    bias: int = 0              # +1 bullish, -1 bearish, 0 none
    leg_low: float = 0.0
    leg_high: float = 0.0
    gz_lower: float = 0.0
    gz_upper: float = 0.0
    has_ob: bool = False
    has_fvg: bool = False
    in_golden_zone: bool = False
    choch_dir: int = 0         # +1/-1/0 arah CHoCH M15 dalam zona
    choch_body_ratio: float = 0.0
    momentum_ok: bool = False
    summary: str = "CRT: data kurang"

    def bias_str(self) -> str:
        return "BULLISH" if self.bias > 0 else ("BEARISH" if self.bias < 0 else "NONE")


# --------------------------------------------------------------------------- #
# Helper akses ala MQL (i=0 terbaru)
# --------------------------------------------------------------------------- #
def _get(df: pd.DataFrame, col: str, i: int) -> float:
    return float(df[col].iloc[-1 - i])


def _valid_idx(n: int, i: int) -> bool:
    return 0 <= i < n


def body_ratio(df: pd.DataFrame, i: int) -> float:
    o, c = _get(df, "open", i), _get(df, "close", i)
    h, l = _get(df, "high", i), _get(df, "low", i)
    rng = h - l
    if rng <= 0:
        return 0.0
    return abs(c - o) / rng


def is_swing_high(df: pd.DataFrame, i: int, strength: int) -> bool:
    n = len(df)
    if not _valid_idx(n, i + strength) or not _valid_idx(n, i - strength):
        return False
    v = _get(df, "high", i)
    for k in range(1, strength + 1):
        if _get(df, "high", i + k) >= v:
            return False
        if _get(df, "high", i - k) >= v:
            return False
    return True


def is_swing_low(df: pd.DataFrame, i: int, strength: int) -> bool:
    n = len(df)
    if not _valid_idx(n, i + strength) or not _valid_idx(n, i - strength):
        return False
    v = _get(df, "low", i)
    for k in range(1, strength + 1):
        if _get(df, "low", i + k) <= v:
            return False
        if _get(df, "low", i - k) <= v:
            return False
    return True


# --------------------------------------------------------------------------- #
# Layer 1 - MSS H1
# --------------------------------------------------------------------------- #
def detect_mss_h1(df_h1: pd.DataFrame, cfg: CRTConfig) -> tuple[int, float, float]:
    """Return (bias, leg_low, leg_high). bias 0 = tidak ada MSS."""
    n = len(df_h1)
    s = cfg.swing_h1
    scan = min(cfg.h1_scan_bars, n)
    if scan < 2 * s + 3:
        return 0, 0.0, 0.0

    last_sh = -1
    last_sl = -1
    for i in range(s + 1, scan - s):
        if last_sh < 0 and is_swing_high(df_h1, i, s):
            last_sh = i
        if last_sl < 0 and is_swing_low(df_h1, i, s):
            last_sl = i
        if last_sh >= 0 and last_sl >= 0:
            break
    if last_sh < 0 or last_sl < 0:
        return 0, 0.0, 0.0

    swing_high = _get(df_h1, "high", last_sh)
    swing_low = _get(df_h1, "low", last_sl)

    # Break bullish: close menembus swing high dengan body kuat.
    for i in range(1, last_sh):
        if _get(df_h1, "close", i) > swing_high and body_ratio(df_h1, i) >= cfg.body_ratio_h1:
            return +1, swing_low, _get(df_h1, "high", i)
    # Break bearish.
    for i in range(1, last_sl):
        if _get(df_h1, "close", i) < swing_low and body_ratio(df_h1, i) >= cfg.body_ratio_h1:
            return -1, _get(df_h1, "low", i), swing_high
    return 0, 0.0, 0.0


# --------------------------------------------------------------------------- #
# Layer 2 - Golden Zone + OB/FVG
# --------------------------------------------------------------------------- #
def build_golden_zone(bias: int, leg_low: float, leg_high: float, cfg: CRTConfig) -> tuple[float, float]:
    leg = leg_high - leg_low
    if leg <= 0:
        return 0.0, 0.0
    if bias > 0:
        gz_upper = leg_high - leg * cfg.gz_start
        gz_lower = leg_high - leg * cfg.gz_end
    else:
        gz_lower = leg_low + leg * cfg.gz_start
        gz_upper = leg_low + leg * cfg.gz_end
    if gz_upper < gz_lower:
        gz_upper, gz_lower = gz_lower, gz_upper
    return gz_lower, gz_upper


def has_order_block(df: pd.DataFrame, bias: int, z_low: float, z_high: float, bars: int) -> bool:
    n = min(bars, len(df))
    for i in range(1, n - 1):
        o, c = _get(df, "open", i), _get(df, "close", i)
        is_ob = (bias > 0 and c < o) or (bias < 0 and c > o)
        if not is_ob:
            continue
        body_hi, body_lo = max(o, c), min(o, c)
        if body_hi >= z_low and body_lo <= z_high:
            return True
    return False


def has_fvg(df: pd.DataFrame, bias: int, z_low: float, z_high: float, bars: int, min_gap: float) -> bool:
    n = min(bars, len(df))
    for i in range(2, n - 1):
        if bias > 0:
            a = _get(df, "low", i - 1)
            b = _get(df, "high", i + 1)
            if a - b >= min_gap:
                g_lo, g_hi = b, a
                if g_hi >= z_low and g_lo <= z_high:
                    return True
        else:
            a = _get(df, "high", i - 1)
            b = _get(df, "low", i + 1)
            if b - a >= min_gap:
                g_lo, g_hi = a, b
                if g_hi >= z_low and g_lo <= z_high:
                    return True
    return False


# --------------------------------------------------------------------------- #
# Layer 3 - CHoCH / BOS M15 dalam golden zone
# --------------------------------------------------------------------------- #
def detect_choch_m15(
    df_m15: pd.DataFrame, bias: int, gz_lower: float, gz_upper: float, cfg: CRTConfig
) -> tuple[int, float]:
    """Return (choch_dir, body_ratio_candle). 0 = belum ada CHoCH valid dalam zona."""
    n = len(df_m15)
    s = cfg.swing_m15
    scan = min(cfg.m15_scan_bars, n)
    if scan < 2 * s + 3:
        return 0, 0.0

    low1 = _get(df_m15, "low", 1)
    high1 = _get(df_m15, "high", 1)
    inside = (low1 <= gz_upper and high1 >= gz_lower)

    if bias > 0:
        sh_idx = -1
        for i in range(s + 1, scan - s):
            if is_swing_high(df_m15, i, s):
                sh_idx = i
                break
        if sh_idx < 0:
            return 0, 0.0
        sh = _get(df_m15, "high", sh_idx)
        c1 = _get(df_m15, "close", 1)
        broke = (c1 > sh) if cfg.l3_strict else (high1 > sh)
        if broke and inside:
            return +1, body_ratio(df_m15, 1)
    elif bias < 0:
        sl_idx = -1
        for i in range(s + 1, scan - s):
            if is_swing_low(df_m15, i, s):
                sl_idx = i
                break
        if sl_idx < 0:
            return 0, 0.0
        sl = _get(df_m15, "low", sl_idx)
        c1 = _get(df_m15, "close", 1)
        broke = (c1 < sl) if cfg.l3_strict else (low1 < sl)
        if broke and inside:
            return -1, body_ratio(df_m15, 1)
    return 0, 0.0


# --------------------------------------------------------------------------- #
# Orkestrasi
# --------------------------------------------------------------------------- #
def analyze(
    df_h1: pd.DataFrame | None,
    df_m15: pd.DataFrame | None,
    cfg: CRTConfig,
    point: float = 0.01,
) -> CRTContext:
    """Hitung konteks CRT lengkap dari H1 (bias/zone) + M15 (CHoCH)."""
    ctx = CRTContext()
    if df_h1 is None or df_m15 is None or len(df_h1) < 10 or len(df_m15) < 10:
        return ctx

    bias, leg_low, leg_high = detect_mss_h1(df_h1, cfg)
    ctx.bias, ctx.leg_low, ctx.leg_high = bias, leg_low, leg_high
    if bias == 0:
        ctx.summary = "CRT: belum ada MSS H1 (bias NONE)"
        return ctx

    gz_lower, gz_upper = build_golden_zone(bias, leg_low, leg_high, cfg)
    ctx.gz_lower, ctx.gz_upper = gz_lower, gz_upper
    ctx.has_ob = has_order_block(df_h1, bias, gz_lower, gz_upper, cfg.h1_scan_bars)
    ctx.has_fvg = has_fvg(df_h1, bias, gz_lower, gz_upper, cfg.h1_scan_bars,
                          cfg.fvg_min_points * point)

    # Apakah harga M15 terakhir (close) berada di golden zone?
    last_close = _get(df_m15, "close", 1)
    ctx.in_golden_zone = (gz_lower <= last_close <= gz_upper) if gz_upper > gz_lower else False

    choch_dir, br = detect_choch_m15(df_m15, bias, gz_lower, gz_upper, cfg)
    ctx.choch_dir = choch_dir
    ctx.choch_body_ratio = br
    ctx.momentum_ok = bool(choch_dir != 0 and br >= cfg.body_ratio_m15)

    ctx.summary = (
        f"CRT: bias {ctx.bias_str()} | GZ {gz_lower:.2f}-{gz_upper:.2f} "
        f"OB={'y' if ctx.has_ob else '-'} FVG={'y' if ctx.has_fvg else '-'} | "
        f"inZone={'y' if ctx.in_golden_zone else '-'} | "
        f"CHoCH={'+' if choch_dir > 0 else ('-' if choch_dir < 0 else '0')} "
        f"(body {br:.2f}{', momOK' if ctx.momentum_ok else ''})"
    )
    return ctx


def confirms(direction: str, ctx: CRTContext, cfg: CRTConfig) -> tuple[bool, str]:
    """Apakah CRT mengonfirmasi arah entry LBMA?

    Aturan (lentur, tidak memblok berlebihan):
      - Data CRT kurang / netral -> dianggap konfirmasi (fail-open, beri catatan).
      - Konfirmasi bila: bias H1 searah ATAU ada CHoCH M15 searah dgn momentum OK.
      - BLOK hanya bila bias H1 BERLAWANAN arah DAN tak ada CHoCH searah.
    """
    want = +1 if direction == "BUY" else -1

    if ctx.bias == 0 and ctx.choch_dir == 0:
        return True, "CRT netral (data kurang) - lolos"

    aligned_bias = (ctx.bias == want)
    choch_align = (ctx.choch_dir == want and ctx.momentum_ok)
    opposite_bias = (ctx.bias == -want)

    if aligned_bias or choch_align:
        why = []
        if aligned_bias:
            why.append("bias searah")
        if choch_align:
            why.append("CHoCH searah")
        return True, "CRT konfirmasi (" + ", ".join(why) + ")"

    if opposite_bias:
        return False, f"CRT BERLAWANAN (bias {ctx.bias_str()}, tanpa CHoCH searah)"

    return True, "CRT tidak menentang (bias netral)"
