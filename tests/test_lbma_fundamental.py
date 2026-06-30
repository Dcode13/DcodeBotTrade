"""Unit test analisis fundamental-teknikal LBMA (offline, tanpa jaringan/MT5).

Angka acuan diambil PERSIS dari spreadsheet user 'HARGA LBMA HARIAN'
(tab Juni 2026) agar implementasi setia pada sumber:

  Tgl 1: AM=4490.4 PM=4449.3 -> DELTA=-41.1 %=-92.37 STATUS=TURUN RASIO=99.08
         grid (AM-150/300/400) = 4340.4 / 4190.4 / 4090.4
  FIBBO AM (HIGH=4523.8, LOW=4079.85): 23.6%=4419.0278 38.2%=4354.2111
         50%=4301.825 61.8%=4249.4389 78.6%=4174.8553
"""

from __future__ import annotations

import datetime as dt

from core.config import LBMAFundamentalConfig
from core import lbma_fundamental as lf


def cfg(**kw) -> LBMAFundamentalConfig:
    base = dict(
        enabled=True, require_confirmation=False, fib_window_days=22,
        recent_days=10, bullish_streak_days=3, bearish_streak_days=3,
        grid_offsets=[150.0, 300.0, 400.0],
    )
    base.update(kw)
    return LBMAFundamentalConfig(**base)


# --------------------------------------------------------------------------- #
# Metrik harian (cocokkan baris tgl 1 spreadsheet)
# --------------------------------------------------------------------------- #
def test_daily_metric_matches_spreadsheet_row():
    m = lf.daily_metric("2026-06-01", am=4490.4, pm=4449.3, cfg=cfg())
    assert abs(m.delta - (-41.1)) < 1e-6
    assert m.status == "TURUN"
    # % = DELTA / PM * 10000 (skala spreadsheet) -> -92.37
    assert abs(m.pct - (-92.37)) < 0.01
    # RASIO = PM / AM * 100 -> 99.08
    assert abs(m.rasio - 99.08) < 0.01
    # Grid akumulasi AM - 150/300/400
    assert [round(g, 2) for g in m.grid] == [4340.40, 4190.40, 4090.40]


def test_daily_metric_status_naik_when_pm_above_am():
    m = lf.daily_metric("2026-06-03", am=4441.25, pm=4444.6, cfg=cfg())
    assert m.status == "NAIK"
    assert m.delta > 0
    assert m.pm_gt_am is True


def test_daily_metric_missing_side_is_safe():
    m = lf.daily_metric("2026-06-01", am=4490.4, pm=None, cfg=cfg())
    assert m.delta is None and m.pct is None and m.rasio is None
    assert m.status == "-"
    assert m.pm_gt_am is None
    # Grid tetap dihitung dari AM yang ada.
    assert m.grid and abs(m.grid[0] - 4340.4) < 1e-9


# --------------------------------------------------------------------------- #
# Fibonacci AM (cocokkan blok FIBBO AM spreadsheet)
# --------------------------------------------------------------------------- #
def test_compute_fib_matches_fibbo_am():
    # Nilai AM Juni (16 hari) -> HIGH=4523.8, LOW=4079.85.
    am_vals = [4490.4, 4523.8, 4441.25, 4464.95, 4463.1, 4280.6, 4326.75,
               4166.4, 4079.85, 4233.05, 4337.55, 4343.3, 4331.85, 4264.9,
               4164.55, 4207.75]
    fib = lf.compute_fib(am_vals)
    assert fib is not None
    assert abs(fib.high - 4523.8) < 1e-9
    assert abs(fib.low - 4079.85) < 1e-9
    assert abs(fib.levels[0.0] - 4523.8) < 1e-9
    assert abs(fib.levels[0.236] - 4419.0278) < 1e-3
    assert abs(fib.levels[0.382] - 4354.2111) < 1e-3
    assert abs(fib.levels[0.5] - 4301.825) < 1e-3
    assert abs(fib.levels[0.618] - 4249.4389) < 1e-3
    assert abs(fib.levels[0.786] - 4174.8553) < 1e-3
    assert abs(fib.levels[1.0] - 4079.85) < 1e-9


def test_compute_fib_needs_range():
    assert lf.compute_fib([4500.0]) is None       # < 2 nilai
    assert lf.compute_fib([4500.0, 4500.0]) is None  # rentang nol


# --------------------------------------------------------------------------- #
# Bias multi-hari (tabel interpretasi)
# --------------------------------------------------------------------------- #
def _maps(rows: dict[str, tuple[float, float]]):
    am = {d: v[0] for d, v in rows.items()}
    pm = {d: v[1] for d, v in rows.items()}
    return am, pm


def test_bias_bullish_when_pm_gt_am_three_days_higher_high():
    # PM > AM 3 hari & PM terus higher-high -> akumulasi/buying (bias +1).
    am, pm = _maps({
        "2026-06-15": (4337.55, 4355.20),
        "2026-06-16": (4343.30, 4360.00),
        "2026-06-17": (4331.85, 4370.00),
    })
    fund = lf.analyze(am, pm, dt.date(2026, 6, 17), cfg())
    assert fund.bias == 1
    assert fund.pm_gt_am_streak == 3
    assert fund.pm_higher_high is True
    assert "kumulasi" in fund.interpretation.lower()


def test_bias_bullish_without_higher_high_flags_distribution():
    # PM > AM 3 hari TAPI PM tidak higher-high -> waspada distribusi terselubung.
    am, pm = _maps({
        "2026-06-15": (4337.55, 4400.00),
        "2026-06-16": (4343.30, 4360.00),  # PM turun
        "2026-06-17": (4331.85, 4380.00),  # naik lagi tapi < hari pertama
    })
    fund = lf.analyze(am, pm, dt.date(2026, 6, 17), cfg())
    assert fund.bias == 1
    assert fund.pm_higher_high is False
    assert "distribusi" in fund.interpretation.lower()


def test_bias_bearish_when_pm_lt_am_three_days():
    # Tgl 17-19 Juni: PM < AM beruntun -> tekanan jual sesi London (bias -1).
    am, pm = _maps({
        "2026-06-17": (4331.85, 4341.85),  # PM>AM (putus streak bearish sebelum)
        "2026-06-18": (4264.90, 4236.15),  # PM<AM
        "2026-06-19": (4164.55, 4150.90),  # PM<AM
    })
    fund = lf.analyze(am, pm, dt.date(2026, 6, 19), cfg(bearish_streak_days=2))
    assert fund.bias == -1
    assert fund.am_gt_pm_streak == 2
    assert "jual" in fund.interpretation.lower()


def test_bias_neutral_when_streak_too_short():
    am, pm = _maps({
        "2026-06-16": (4343.30, 4335.80),  # PM<AM
        "2026-06-17": (4331.85, 4341.85),  # PM>AM (streak bullish hanya 1)
    })
    fund = lf.analyze(am, pm, dt.date(2026, 6, 17), cfg())
    assert fund.bias == 0


def test_analyze_window_limits_and_stats():
    am, pm = _maps({
        "2026-06-15": (4337.55, 4355.20),
        "2026-06-16": (4343.30, 4335.80),
        "2026-06-17": (4331.85, 4341.85),
    })
    fund = lf.analyze(am, pm, dt.date(2026, 6, 17), cfg(fib_window_days=2))
    # Hanya 2 hari terakhir yang masuk jendela.
    assert len(fund.daily) == 2
    assert fund.daily[0].date == "2026-06-16"
    assert fund.latest.date == "2026-06-17"
    assert fund.median_am is not None and fund.titik50_am is not None


def test_analyze_excludes_dates_after_ref():
    am, pm = _maps({
        "2026-06-17": (4331.85, 4341.85),
        "2026-06-18": (4264.90, 4236.15),
        "2026-06-19": (4164.55, 4150.90),  # setelah ref -> diabaikan
    })
    fund = lf.analyze(am, pm, dt.date(2026, 6, 18), cfg())
    assert fund.latest.date == "2026-06-18"
    assert all(d.date <= "2026-06-18" for d in fund.daily)


# --------------------------------------------------------------------------- #
# confirms() - konfirmasi lunak
# --------------------------------------------------------------------------- #
def test_confirms_neutral_fails_open():
    fund = lf.LBMAFundamental(ref_date="2026-06-19", bias=0, interpretation="x",
                              pm_gt_am_streak=0, am_gt_pm_streak=0, pm_higher_high=False)
    ok, _ = lf.confirms("BUY", fund, cfg())
    assert ok is True


def test_confirms_blocks_only_opposite_bias():
    bull = lf.LBMAFundamental(ref_date="x", bias=1, interpretation="bullish",
                              pm_gt_am_streak=3, am_gt_pm_streak=0, pm_higher_high=True)
    assert lf.confirms("BUY", bull, cfg())[0] is True
    assert lf.confirms("SELL", bull, cfg())[0] is False

    bear = lf.LBMAFundamental(ref_date="x", bias=-1, interpretation="bearish",
                              pm_gt_am_streak=0, am_gt_pm_streak=3, pm_higher_high=False)
    assert lf.confirms("SELL", bear, cfg())[0] is True
    assert lf.confirms("BUY", bear, cfg())[0] is False
