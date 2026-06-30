"""Analisis fundamental-teknikal LBMA (port spreadsheet 'HARGA LBMA HARIAN').

Modul ini membaca riwayat fixing **AM** (pembukaan sesi London, auction 10:30)
dan **PM** (penutupan sesi London, auction 15:00) dari ``LBMAStore`` lalu
menurunkan analisis ala spreadsheet user:

1. Metrik harian per tanggal:
   * ``DELTA``  = PM - AM (selisih penutupan vs pembukaan London).
   * ``%``      = DELTA relatif terhadap PM (skala spreadsheet, lihat ``daily_metric``).
   * ``STATUS`` = NAIK bila PM > AM (London ditutup naik), TURUN bila PM < AM.
   * ``RASIO``  = PM / AM x 100 (>100% berarti ditutup di atas pembukaan).
   * ``grid``   = level akumulasi/average-down: AM dikurangi tiap offset
     (default 150/300/400) -> zona DCA bila harga turun.

2. Level **Fibonacci AM & PM** dari jendela ``fib_window_days`` hari terakhir
   (high = max fixing, low = min fixing, retracement high->low). Sama seperti
   blok FIBBO AM / FIBBO PM di spreadsheet.

3. **BIAS multi-hari** sesuai tabel interpretasi user:

   ===============================================  ============================
   Kondisi                                          Interpretasi
   ===============================================  ============================
   PM > AM selama >= ``bullish_streak_days`` hari   Bias bullish jangka pendek
   ...dan PM terus higher-high                       Akumulasi / buying lebih kuat
   ...tapi PM gagal higher-high                       Waspada distribusi terselubung
   PM < AM selama >= ``bearish_streak_days`` hari   Tekanan jual sesi London
   ===============================================  ============================

Murni pandas/stdlib: TIDAK meng-import MT5 sehingga dapat diuji offline & dipakai
backtester. Output dipakai sebagai **konteks/konfirmasi lunak** (mirip CRT),
bukan generator sinyal mandiri. Lihat ``confirms`` untuk gating opsional.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from statistics import median

from core.config import LBMAFundamentalConfig

log = logging.getLogger(__name__)

# Rasio retracement persis seperti blok FIBBO AM/PM pada spreadsheet.
FIB_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]


# --------------------------------------------------------------------------- #
# Struktur data
# --------------------------------------------------------------------------- #
@dataclass
class DailyMetric:
    """Satu baris tabel 'HARGA LBMA HARIAN' untuk satu tanggal."""

    date: str
    am: float | None
    pm: float | None
    delta: float | None        # PM - AM
    pct: float | None          # DELTA / PM x 10000 (skala % spreadsheet)
    status: str                # "NAIK" | "TURUN" | "DATAR" | "-"
    rasio: float | None        # PM / AM x 100
    grid: list[float] = field(default_factory=list)  # AM - tiap offset akumulasi

    @property
    def pm_gt_am(self) -> bool | None:
        """True bila PM > AM, False bila PM < AM, None bila salah satu hilang/sama."""
        if self.am is None or self.pm is None or self.am == self.pm:
            return None
        return self.pm > self.am


@dataclass
class FibSet:
    """Level Fibonacci dari jendela fixing (FIBBO AM / FIBBO PM)."""

    high: float
    low: float
    levels: dict[float, float] = field(default_factory=dict)  # ratio -> harga

    @property
    def rng(self) -> float:
        return self.high - self.low


@dataclass
class LBMAFundamental:
    """Hasil analisis fundamental-teknikal LBMA lengkap."""

    ref_date: str
    bias: int                  # +1 bullish, -1 bearish, 0 netral
    interpretation: str
    pm_gt_am_streak: int       # hari beruntun terbaru PM > AM
    am_gt_pm_streak: int       # hari beruntun terbaru PM < AM
    pm_higher_high: bool       # PM strictly naik sepanjang streak bullish
    latest: DailyMetric | None = None
    daily: list[DailyMetric] = field(default_factory=list)
    fib_am: FibSet | None = None
    fib_pm: FibSet | None = None
    titik50_am: float | None = None   # rata-rata AM jendela
    titik50_pm: float | None = None   # rata-rata PM jendela
    median_am: float | None = None
    median_pm: float | None = None
    summary: str = ""

    def bias_str(self) -> str:
        return "BULLISH" if self.bias > 0 else ("BEARISH" if self.bias < 0 else "NETRAL")


# --------------------------------------------------------------------------- #
# Metrik harian
# --------------------------------------------------------------------------- #
def daily_metric(
    date_iso: str, am: float | None, pm: float | None, cfg: LBMAFundamentalConfig
) -> DailyMetric:
    """Hitung satu baris metrik harian (DELTA/%/STATUS/RASIO/grid).

    Catatan kolom ``%``: spreadsheet menyimpan ``DELTA / PM`` lalu memformatnya
    dengan format persen Excel (x100), sehingga DELTA -41.1 pada PM 4449.3
    tampil sebagai ``-92.37%``. Untuk reproduksi persis nilai itu di sini:
    ``pct = DELTA / PM * 10000``.
    """
    delta = pct = rasio = None
    status = "-"
    grid: list[float] = []

    if am is not None and pm is not None:
        delta = pm - am
        if pm != 0:
            pct = delta / pm * 10000.0
        if am != 0:
            rasio = pm / am * 100.0
        if delta > 0:
            status = "NAIK"
        elif delta < 0:
            status = "TURUN"
        else:
            status = "DATAR"

    if am is not None:
        grid = [am - off for off in cfg.grid_offsets]

    return DailyMetric(
        date=date_iso, am=am, pm=pm, delta=delta, pct=pct,
        status=status, rasio=rasio, grid=grid,
    )


# --------------------------------------------------------------------------- #
# Fibonacci AM / PM
# --------------------------------------------------------------------------- #
def compute_fib(values: list[float]) -> FibSet | None:
    """Bangun level fib retracement high->low dari kumpulan fixing.

    ``ratio r`` -> ``high - r * (high - low)`` (0 = high, 1 = low).
    """
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    high, low = max(vals), min(vals)
    rng = high - low
    if rng <= 0:
        return None
    levels = {r: high - r * rng for r in FIB_RATIOS}
    return FibSet(high=high, low=low, levels=levels)


# --------------------------------------------------------------------------- #
# Pengumpulan jendela & streak
# --------------------------------------------------------------------------- #
def window_metrics(
    am_map: dict[str, float],
    pm_map: dict[str, float],
    ref_date: dt.date,
    cfg: LBMAFundamentalConfig,
) -> list[DailyMetric]:
    """Metrik harian untuk ``fib_window_days`` tanggal terakhir s/d ``ref_date``."""
    ref_iso = ref_date.isoformat()
    all_dates = sorted(d for d in (set(am_map) | set(pm_map)) if d <= ref_iso)
    days = max(1, cfg.fib_window_days)
    chosen = all_dates[-days:]
    return [daily_metric(d, am_map.get(d), pm_map.get(d), cfg) for d in chosen]


def _streaks(daily: list[DailyMetric]) -> tuple[int, int, bool]:
    """(streak PM>AM, streak PM<AM, PM higher-high sepanjang streak bullish).

    Streak dihitung dari tanggal TERBARU mundur sampai kondisi putus.
    """
    pm_gt = 0
    for m in reversed(daily):
        if m.pm_gt_am is True:
            pm_gt += 1
        else:
            break

    am_gt = 0
    for m in reversed(daily):
        if m.pm_gt_am is False:
            am_gt += 1
        else:
            break

    higher_high = False
    if pm_gt >= 2:
        streak = daily[-pm_gt:]
        pms = [m.pm for m in streak if m.pm is not None]
        higher_high = len(pms) == pm_gt and all(
            pms[i] > pms[i - 1] for i in range(1, len(pms))
        )
    return pm_gt, am_gt, higher_high


# --------------------------------------------------------------------------- #
# Orkestrasi
# --------------------------------------------------------------------------- #
def analyze(
    am_map: dict[str, float],
    pm_map: dict[str, float],
    ref_date: dt.date,
    cfg: LBMAFundamentalConfig,
) -> LBMAFundamental:
    """Analisis fundamental-teknikal LBMA penuh untuk ``ref_date``."""
    daily = window_metrics(am_map, pm_map, ref_date, cfg)
    latest = daily[-1] if daily else None
    pm_gt, am_gt, higher_high = _streaks(daily)

    am_vals = [m.am for m in daily if m.am is not None]
    pm_vals = [m.pm for m in daily if m.pm is not None]
    fib_am = compute_fib(am_vals)
    fib_pm = compute_fib(pm_vals)
    titik50_am = sum(am_vals) / len(am_vals) if am_vals else None
    titik50_pm = sum(pm_vals) / len(pm_vals) if pm_vals else None
    median_am = median(am_vals) if am_vals else None
    median_pm = median(pm_vals) if pm_vals else None

    bias, interp = _bias(pm_gt, am_gt, higher_high, cfg)

    arrow = "↑" if bias > 0 else ("↓" if bias < 0 else "→")
    summary = (
        f"FUND-LBMA {arrow} {('BULLISH' if bias > 0 else ('BEARISH' if bias < 0 else 'NETRAL'))} "
        f"| PM>AM {pm_gt}h / PM<AM {am_gt}h"
        f"{' / higher-high' if higher_high else ''} | {interp}"
    )

    return LBMAFundamental(
        ref_date=ref_date.isoformat(),
        bias=bias,
        interpretation=interp,
        pm_gt_am_streak=pm_gt,
        am_gt_pm_streak=am_gt,
        pm_higher_high=higher_high,
        latest=latest,
        daily=daily,
        fib_am=fib_am,
        fib_pm=fib_pm,
        titik50_am=titik50_am,
        titik50_pm=titik50_pm,
        median_am=median_am,
        median_pm=median_pm,
        summary=summary,
    )


def _bias(
    pm_gt: int, am_gt: int, higher_high: bool, cfg: LBMAFundamentalConfig
) -> tuple[int, str]:
    """Terjemahkan streak PM/AM ke (bias, interpretasi) sesuai tabel user."""
    bull_n = max(1, cfg.bullish_streak_days)
    bear_n = max(1, cfg.bearish_streak_days)

    if pm_gt >= bull_n:
        if higher_high:
            return +1, (f"Akumulasi/buying: PM>AM {pm_gt} hari & PM terus higher-high")
        return +1, (
            f"Bias bullish jangka pendek: PM>AM {pm_gt} hari, tetapi PM gagal "
            f"higher-high -> waspada distribusi terselubung"
        )
    if am_gt >= bear_n:
        return -1, f"Tekanan jual sesi London: PM<AM {am_gt} hari beruntun"

    if pm_gt >= 1:
        return 0, f"Netral (condong bullish): PM>AM {pm_gt} hari (< {bull_n})"
    if am_gt >= 1:
        return 0, f"Netral (condong bearish): PM<AM {am_gt} hari (< {bear_n})"
    return 0, "Netral: PM=AM atau data kurang"


def confirms(
    direction: str, fund: LBMAFundamental, cfg: LBMAFundamentalConfig
) -> tuple[bool, str]:
    """Apakah bias fundamental LBMA mengonfirmasi arah entry?

    Lunak (fail-open): netral selalu lolos; hanya BLOK bila bias fundamental
    BERLAWANAN arah. Dipakai hanya bila ``require_confirmation=True``.
    """
    want = +1 if direction == "BUY" else -1
    if fund.bias == 0:
        return True, "fundamental LBMA netral - lolos"
    if fund.bias == want:
        return True, f"fundamental searah ({fund.bias_str()})"
    return False, f"fundamental BERLAWANAN ({fund.bias_str()}): {fund.interpretation}"
