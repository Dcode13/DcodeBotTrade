"""Unit test deteksi & entry Support/Resistance multi-TF (offline, pure)."""

from __future__ import annotations

import pandas as pd

from core.config import SRConfig
from core import support_resistance as srm


def _zigzag_df(mids: list[float]) -> pd.DataFrame:
    rows = [{"open": m, "high": m + 2, "low": m - 2, "close": m + 0.5} for m in mids]
    return pd.DataFrame(rows, index=pd.date_range("2026-01-01", periods=len(rows), freq="5min"))


def _cfg(**kw) -> SRConfig:
    base = dict(pivot_n=2, lookback=60, cluster_pips=40.0, touch_pips=30.0,
                sl_buffer_pips=20.0, rr_ratio=2.0, min_strength=1,
                require_m5_candle=True, min_body_ratio=0.2)
    base.update(kw)
    return SRConfig(**base)


def test_detect_levels_finds_support_and_resistance():
    # Zigzag: swing high ~2080, swing low ~2000 berulang.
    mids = [2000, 2040, 2080, 2040, 2000, 2040, 2080, 2040, 2000, 2040, 2080, 2040, 2010]
    df = _zigzag_df(mids)
    sr = srm.detect_levels({"M5": df}, _cfg(), pip_size=0.1)
    assert sr.supports, "harus ada support"
    assert sr.resistances, "harus ada resistance"
    # support ~2000, resistance ~2080
    assert any(abs(lv.price - 2000) < 5 for lv in sr.supports)
    assert any(abs(lv.price - 2080) < 5 for lv in sr.resistances)


def test_clustering_merges_close_levels_across_tfs():
    mids = [2000, 2040, 2080, 2040, 2000, 2040, 2080, 2040, 2000, 2040, 2080, 2040, 2010]
    df1 = _zigzag_df(mids)
    df2 = _zigzag_df([m + 1 for m in mids])  # nyaris sama -> harus menyatu
    sr = srm.detect_levels({"M5": df1, "H1": df2}, _cfg(cluster_pips=50.0), pip_size=0.1)
    # support gabungan punya kontribusi 2 TF.
    sup = sr.nearest("support", 2000.0)
    assert sup is not None
    assert "M5" in sup.tfs and "H1" in sup.tfs
    assert sup.strength >= 2


def test_buy_at_support_with_bullish_m5():
    sr = srm.SRMap(
        supports=[srm.SRLevel(2000.0, "support", ["M15", "H1"], 4)],
        resistances=[srm.SRLevel(2080.0, "resistance", ["H1"], 2)],
    )
    # 3 baris; candle terakhir CLOSE (iloc[-2]) bullish & harga ~ support.
    df = pd.DataFrame(
        [{"open": 2000, "high": 2003, "low": 1999, "close": 2001},
         {"open": 2000.5, "high": 2003, "low": 2000, "close": 2002},   # konfirmasi bullish
         {"open": 2002, "high": 2002.5, "low": 2001, "close": 2002.2}],  # bar berjalan
        index=pd.date_range("2026-01-01", periods=3, freq="5min"),
    )
    sig, reason = srm.evaluate_sr(sr, df, bid=2002.0, ask=2002.2, cfg=_cfg(), pip_size=0.1)
    assert sig is not None, reason
    assert sig.direction == "BUY"
    assert sig.sl < sig.entry < sig.tp


def test_sell_at_resistance_with_bearish_m5():
    sr = srm.SRMap(
        supports=[srm.SRLevel(2000.0, "support", ["H1"], 2)],
        resistances=[srm.SRLevel(2080.0, "resistance", ["M15", "H1"], 4)],
    )
    df = pd.DataFrame(
        [{"open": 2080, "high": 2081, "low": 2077, "close": 2079},
         {"open": 2080, "high": 2082, "low": 2078, "close": 2078},     # konfirmasi bearish
         {"open": 2078, "high": 2078.5, "low": 2077, "close": 2077.8}],  # bar berjalan
        index=pd.date_range("2026-01-01", periods=3, freq="5min"),
    )
    sig, reason = srm.evaluate_sr(sr, df, bid=2078.0, ask=2078.2, cfg=_cfg(), pip_size=0.1)
    assert sig is not None, reason
    assert sig.direction == "SELL"
    assert sig.tp < sig.entry < sig.sl


def test_no_entry_when_far_from_levels():
    sr = srm.SRMap(
        supports=[srm.SRLevel(2000.0, "support", ["H1"], 2)],
        resistances=[srm.SRLevel(2080.0, "resistance", ["H1"], 2)],
    )
    df = pd.DataFrame(
        [{"open": 2040, "high": 2042, "low": 2038, "close": 2041},
         {"open": 2040, "high": 2041, "low": 2039, "close": 2040},      # jauh dari S & R
         {"open": 2040, "high": 2041, "low": 2039, "close": 2040.5}],
        index=pd.date_range("2026-01-01", periods=3, freq="5min"),
    )
    sig, reason = srm.evaluate_sr(sr, df, bid=2040.0, ask=2040.2, cfg=_cfg(), pip_size=0.1)
    assert sig is None
    assert "belum di zona" in reason
