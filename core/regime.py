"""Filter rezim pasar (anti-sideways) berbasis ADX + alignment multi-timeframe.

Tujuan: berhenti "asal entry". Sebelum jalur entry mana pun dijalankan, bot
menilai apakah pasar sedang TREN JELAS atau SIDEWAYS dengan menganalisis
beberapa timeframe sekaligus (default H1/M15/M5/M3):

- Kekuatan tren  -> ADX setiap TF harus >= ``adx_min`` (ADX rendah = ranging).
- Arah tren      -> bias EMA (M15-style) tiap TF sepakat, dan (opsional) +DI/-DI
                    mengonfirmasi arah yang sama.

Bila pasar sideways atau arah tak sepakat -> ``direction`` = None dan SEMUA entry
diblok di pemanggil. Bila tren jelas -> ``direction`` (BUY/SELL) dipakai untuk
HANYA mengizinkan sinyal searah tren (sinyal lawan arah dibuang).

Pure: hanya tergantung pandas/indikator. Memakai candle terakhir yang SUDAH
close (``iloc[-2]``) agar konsisten dengan modul strategi (tanpa look-ahead).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from core import indicators
from core.config import RegimeConfig, StrategyConfig
from core.strategy import Bias, Direction, compute_bias


@dataclass
class TFRegime:
    """Hasil penilaian satu timeframe."""

    tf: str
    bias: Bias = "NONE"            # arah EMA (UP/DOWN/NONE)
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    trending: bool = False        # adx >= adx_min
    di_dir: Bias = "NONE"         # arah dari +DI/-DI
    vote: Bias = "NONE"           # suara akhir TF ini (UP/DOWN/NONE)
    ok: bool = False              # data cukup untuk dinilai


@dataclass
class RegimeResult:
    direction: Direction | None    # BUY / SELL / None (None = jangan entry)
    trending: bool
    reason: str
    per_tf: list[TFRegime] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [
            f"{t.tf}={t.bias}/ADX{t.adx:.0f}" + ("" if t.trending else "⚠")
            for t in self.per_tf
        ]
        return " ".join(parts)


_BIAS_TO_DIR: dict[Bias, Direction] = {"UP": "BUY", "DOWN": "SELL"}


def _assess_tf(df: pd.DataFrame, tf: str, strat: StrategyConfig, reg: RegimeConfig) -> TFRegime:
    """Nilai bias + ADX + arah +DI/-DI satu timeframe (candle terakhir close)."""
    res = TFRegime(tf=tf)
    if df is None or df.empty or len(df) < reg.adx_period + 2:
        return res  # ok tetap False -> data kurang

    res.bias = compute_bias(df, strat)
    adx_df = indicators.adx(df, reg.adx_period)
    adx_val = float(adx_df["adx"].iloc[-2])
    plus_di = float(adx_df["plus_di"].iloc[-2])
    minus_di = float(adx_df["minus_di"].iloc[-2])
    if pd.isna(adx_val):
        return res

    res.ok = True
    res.adx = adx_val
    res.plus_di = plus_di
    res.minus_di = minus_di
    res.trending = adx_val >= reg.adx_min
    res.di_dir = "UP" if plus_di > minus_di else ("DOWN" if minus_di > plus_di else "NONE")

    # Suara TF: butuh bias != NONE; bila di_confirms_direction, +DI/-DI wajib searah.
    vote = res.bias
    if reg.di_confirms_direction and vote != "NONE" and res.di_dir != vote:
        vote = "NONE"
    res.vote = vote
    return res


def assess(
    df_by_tf: dict[str, pd.DataFrame],
    strat: StrategyConfig,
    reg: RegimeConfig,
) -> RegimeResult:
    """Tentukan arah tren multi-TF atau SIDEWAYS.

    ``df_by_tf`` harus berisi semua timeframe di ``reg.timeframes`` (mis. H1,
    M15, M5, M3). Mengembalikan ``RegimeResult`` dengan ``direction`` None bila
    pasar tidak layak ditradingkan.
    """
    if not reg.enabled:
        return RegimeResult(direction=None, trending=False, reason="regime filter off (lolos)")

    per_tf = [_assess_tf(df_by_tf.get(tf), tf, strat, reg) for tf in reg.timeframes]

    missing = [t.tf for t in per_tf if not t.ok]
    if missing:
        return RegimeResult(
            direction=None, trending=False,
            reason=f"Regime: data belum cukup ({', '.join(missing)})", per_tf=per_tf,
        )

    # 1. Kekuatan tren: tidak boleh ada TF yang ADX-nya di bawah ambang.
    weak = [t for t in per_tf if not t.trending]
    if weak:
        detail = " ".join(f"{t.tf} ADX {t.adx:.1f}" for t in weak)
        return RegimeResult(
            direction=None, trending=False,
            reason=(f"Market SIDEWAYS: ADX < {reg.adx_min:.0f} di {detail} "
                    f"-> tunggu tren jelas"),
            per_tf=per_tf,
        )

    # 2. Arah: hitung suara tiap TF.
    ups = sum(1 for t in per_tf if t.vote == "UP")
    downs = sum(1 for t in per_tf if t.vote == "DOWN")
    n = len(per_tf)
    tag = " ".join(f"{t.tf}={t.vote}" for t in per_tf)

    direction: Direction | None = None
    if reg.require_all_aligned:
        if ups == n:
            direction = "BUY"
        elif downs == n:
            direction = "SELL"
    else:  # mayoritas, tanpa ada TF yang berlawanan
        if ups > downs and downs == 0 and ups >= (n // 2 + 1):
            direction = "BUY"
        elif downs > ups and ups == 0 and downs >= (n // 2 + 1):
            direction = "SELL"

    if direction is None:
        return RegimeResult(
            direction=None, trending=True,
            reason=f"Market SIDEWAYS: arah TF tak sepakat ({tag})",
            per_tf=per_tf,
        )

    adx_tag = " ".join(f"{t.tf} {t.adx:.0f}" for t in per_tf)
    return RegimeResult(
        direction=direction, trending=True,
        reason=f"Tren {direction} kuat & selaras (ADX: {adx_tag})",
        per_tf=per_tf,
    )
