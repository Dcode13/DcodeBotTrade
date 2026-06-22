"""Unit test validasi & pemuatan timeframe stack (konfigurable)."""

from __future__ import annotations

from core.config import TimeframesConfig, load_config


def test_default_timeframes_valid():
    tfs = TimeframesConfig()
    assert tfs.validate() == []
    assert (tfs.trend, tfs.zone, tfs.entry) == ("M15", "M5", "M1")


def test_custom_slower_stack_valid():
    tfs = TimeframesConfig(trend="H1", zone="M15", entry="M5")
    assert tfs.validate() == []


def test_invalid_timeframe_name():
    tfs = TimeframesConfig(trend="M15", zone="M7", entry="M1")
    errors = tfs.validate()
    assert errors and "M7" in errors[0]


def test_wrong_order_rejected():
    # entry lebih besar dari zone -> salah urutan
    tfs = TimeframesConfig(trend="M15", zone="M1", entry="M5")
    errors = tfs.validate()
    assert errors and "urutan" in errors[0].lower()


def test_equal_tf_allowed():
    # zone == entry diperbolehkan (>=)
    tfs = TimeframesConfig(trend="M5", zone="M5", entry="M5")
    assert tfs.validate() == []


def test_load_config_has_timeframes():
    cfg = load_config(load_env=False)
    assert cfg.timeframes.validate() == []
