"""Unit test filter fundamental: caching kalender + blackout event high-impact."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.config import FundamentalsConfig
from core.fundamentals import FundamentalsFilter


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _cfg(**kw) -> FundamentalsConfig:
    base = dict(
        enabled=True,
        calendar_url="https://example.test/cal.json",
        no_trade_window_minutes=30,
        calendar_cache_minutes=15,
        fail_mode="continue",
    )
    base.update(kw)
    return FundamentalsConfig(**base)


def test_calendar_caches_between_calls(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return _FakeResp([])  # tak ada event

    monkeypatch.setattr("core.fundamentals.requests.get", fake_get)
    f = FundamentalsFilter(_cfg())
    f.check_calendar()
    f.check_calendar()
    f.check_calendar()
    assert calls["n"] == 1, "kalender harus di-cache, hanya 1 fetch"


def test_calendar_blocks_near_high_impact_usd(monkeypatch):
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    soon = now + timedelta(minutes=10)
    payload = [
        {"title": "Core CPI m/m", "country": "USD", "impact": "High",
         "date": soon.isoformat()},
    ]
    monkeypatch.setattr(
        "core.fundamentals.requests.get",
        lambda url, params=None, timeout=None: _FakeResp(payload),
    )
    f = FundamentalsFilter(_cfg())
    d = f.check_calendar(now=now)
    assert d.allowed is False
    assert "Blackout" in d.reason


def test_calendar_allows_when_event_far(monkeypatch):
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    far = now + timedelta(hours=5)
    payload = [
        {"title": "FOMC", "country": "USD", "impact": "High", "date": far.isoformat()},
    ]
    monkeypatch.setattr(
        "core.fundamentals.requests.get",
        lambda url, params=None, timeout=None: _FakeResp(payload),
    )
    f = FundamentalsFilter(_cfg())
    assert f.check_calendar(now=now).allowed is True


def test_calendar_ignores_non_usd(monkeypatch):
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    soon = now + timedelta(minutes=5)
    payload = [
        {"title": "ECB Rate", "country": "EUR", "impact": "High", "date": soon.isoformat()},
    ]
    monkeypatch.setattr(
        "core.fundamentals.requests.get",
        lambda url, params=None, timeout=None: _FakeResp(payload),
    )
    f = FundamentalsFilter(_cfg())
    assert f.check_calendar(now=now).allowed is True
