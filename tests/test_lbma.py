"""Unit test logika acuan LBMA + konfirmasi CRT (offline, tanpa jaringan/MT5).

Mencakup aturan user:
  1  arah fade berdasar harga terkini vs level acuan LBMA
  1a AM > PM  -> level = PM
  1b PM > AM  -> level = AM (SL 50 pips)
  2  LBMA 2 hari sebelumnya berdekatan (<= ~300 pips) -> blok entry
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from core.config import CRTConfig, LBMAConfig
from core import crt_analysis
from core.lbma import analyze, choose_reference, collect_prev_day_values, touch_signal


def cfg(**kw) -> LBMAConfig:
    base = dict(pip_size=0.1, sl_pips=50.0, rr_ratio=2.0,
                proximity_pips=300.0, proximity_days=2, entry_tolerance_pips=20.0)
    base.update(kw)
    return LBMAConfig(**base)


# --------------------------------------------------------------------------- #
# Aturan 1a / 1b: pemilihan level acuan
# --------------------------------------------------------------------------- #
def test_rule_1a_am_higher_uses_pm():
    ref = choose_reference(dt.date(2026, 6, 22), am=2400.0, pm=2380.0, cfg=cfg())
    assert ref is not None
    assert ref.level_name == "PM"
    assert ref.level == 2380.0


def test_rule_1b_pm_higher_uses_am_with_sl():
    ref = choose_reference(dt.date(2026, 6, 22), am=2360.0, pm=2390.0, cfg=cfg(sl_pips=50))
    assert ref.level_name == "AM"
    assert ref.level == 2360.0
    assert ref.sl_pips == 50


def test_equal_am_pm_defaults_am():
    ref = choose_reference(dt.date(2026, 6, 22), am=2370.0, pm=2370.0, cfg=cfg())
    assert ref.level_name == "AM"


def test_missing_one_side():
    assert choose_reference(dt.date(2026, 6, 22), None, 2300.0, cfg()).level_name == "PM"
    assert choose_reference(dt.date(2026, 6, 22), 2300.0, None, cfg()).level_name == "AM"
    assert choose_reference(dt.date(2026, 6, 22), None, None, cfg()) is None


# --------------------------------------------------------------------------- #
# Aturan 1: arah fade (di bawah -> SELL, di atas -> BUY) + SL/TP
# --------------------------------------------------------------------------- #
def test_rule_1_below_level_sell_when_reached():
    # AM>PM -> level = PM = 2380. last_close di bawah -> SELL saat harga naik ke level.
    ref = choose_reference(dt.date(2026, 6, 22), am=2400.0, pm=2380.0, cfg=cfg())
    c = cfg()
    # harga belum sampai level (tol 20p=2.0 -> butuh ask >= 2378.0)
    sig, _ = touch_signal(ref, bid=2370.0, ask=2370.2, last_close=2370.0, cfg=c)
    assert sig is None
    # harga sudah menyentuh level
    sig, _ = touch_signal(ref, bid=2379.8, ask=2380.0, last_close=2370.0, cfg=c)
    assert sig is not None
    assert sig.direction == "SELL"
    assert abs(sig.sl_distance - 5.0) < 1e-9       # 50 pips * 0.1
    assert sig.sl > sig.entry                       # SL di atas utk SELL
    assert sig.tp < sig.entry
    assert abs((sig.entry - sig.tp) - 10.0) < 1e-9  # RR 2.0 * 5.0


def test_rule_1_above_level_buy_when_reached():
    # PM>AM -> level = AM = 2360. last_close di atas -> BUY saat harga turun ke level.
    ref = choose_reference(dt.date(2026, 6, 22), am=2360.0, pm=2390.0, cfg=cfg())
    c = cfg()
    sig, _ = touch_signal(ref, bid=2375.0, ask=2375.2, last_close=2380.0, cfg=c)
    assert sig is None                               # belum turun ke level
    sig, _ = touch_signal(ref, bid=2361.0, ask=2361.2, last_close=2380.0, cfg=c)
    assert sig is not None
    assert sig.direction == "BUY"
    assert sig.sl < sig.entry
    assert sig.tp > sig.entry


# --------------------------------------------------------------------------- #
# Aturan 2: konsolidasi 2 hari (<= 300 pips) memblok entry
# --------------------------------------------------------------------------- #
def _maps(values_by_date):
    am = {d: v[0] for d, v in values_by_date.items()}
    pm = {d: v[1] for d, v in values_by_date.items()}
    return am, pm


def test_rule_2_blocks_when_prev_2_days_clustered():
    # 2 hari sebelum 06-22 (yakni 20 & 21) rentang AM/PM kecil (<= $30).
    am, pm = _maps({
        "2026-06-20": (2360.0, 2362.0),
        "2026-06-21": (2365.0, 2368.0),   # range 2360..2368 = 8.0 <= 30
        "2026-06-22": (2400.0, 2380.0),
    })
    res = analyze(am, pm, dt.date(2026, 6, 22), cfg())
    assert res.reference is not None
    assert res.blocked is True
    assert res.proximity_range <= 30.0


def test_rule_2_allows_when_prev_2_days_spread_out():
    am, pm = _maps({
        "2026-06-20": (2300.0, 2310.0),
        "2026-06-21": (2360.0, 2370.0),   # range 2300..2370 = 70 > 30
        "2026-06-22": (2400.0, 2380.0),
    })
    res = analyze(am, pm, dt.date(2026, 6, 22), cfg())
    assert res.blocked is False
    assert res.proximity_range > 30.0


def test_collect_prev_day_values_excludes_ref_and_limits_window():
    am, pm = _maps({
        "2026-06-19": (1.0, 2.0),
        "2026-06-20": (3.0, 4.0),
        "2026-06-21": (5.0, 6.0),
        "2026-06-22": (7.0, 8.0),
    })
    vals = collect_prev_day_values(am, pm, dt.date(2026, 6, 22), days=2)
    assert sorted(vals) == [3.0, 4.0, 5.0, 6.0]   # hanya 20 & 21


# --------------------------------------------------------------------------- #
# CRT confirmation
# --------------------------------------------------------------------------- #
def _trend_df(direction: str, n: int = 60) -> pd.DataFrame:
    """DataFrame H1/M15 sintetis dengan tren jelas (untuk uji bias CRT)."""
    rows = []
    base = 2000.0
    for i in range(n):
        if direction == "up":
            o = base + i * 5
            c = o + 4
        else:
            o = base - i * 5
            c = o - 4
        h = max(o, c) + 1
        l = min(o, c) - 1
        rows.append({"open": o, "high": h, "low": l, "close": c})
    idx = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame(rows, index=idx)


def test_crt_confirms_failopen_on_insufficient_data():
    ctx = crt_analysis.analyze(None, None, CRTConfig())
    ok, _ = crt_analysis.confirms("BUY", ctx, CRTConfig())
    assert ok is True   # fail-open ketika data kurang


def test_crt_blocks_when_strictly_opposite():
    ctx = crt_analysis.CRTContext(bias=-1, choch_dir=0)
    ok, _ = crt_analysis.confirms("BUY", ctx, CRTConfig())
    assert ok is False
    ok2, _ = crt_analysis.confirms("SELL", ctx, CRTConfig())
    assert ok2 is True
