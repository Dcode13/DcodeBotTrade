"""Filter fundamental / berita (§9).

PENTING: ini *filter (gate)* untuk menghindari trading di momen berbahaya,
BUKAN generator sinyal. Tiga lapis (semua opsional & gagal-aman):

1. Kalender ekonomi (event high-impact USD: FOMC/CPI/NFP) -> blackout window.
2. Fear & Greed Index kripto (opsional) -> skip saat sentimen ekstrem.
3. Headline sentiment (hook bersih, default off).

Fail-safe: jika API gagal/timeout -> perilaku sesuai ``fail_mode``
(``skip`` = jangan entry saat ragu, ``continue`` = lanjut + log warning).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from core.config import FundamentalsConfig

log = logging.getLogger(__name__)


@dataclass
class FundamentalDecision:
    allowed: bool
    reason: str


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class FundamentalsFilter:
    """Gabungan filter berita/sentimen. Stateless kecuali cache ringan."""

    def __init__(self, cfg: FundamentalsConfig, news_api_key: str = "") -> None:
        self.cfg = cfg
        self.news_api_key = news_api_key
        # Cache kalender: (monotonic_expiry, events_json). Hindari fetch tiap loop.
        self._cal_cache: tuple[float, object] | None = None

    # ------------------------------------------------------------------ #
    def _fail(self, what: str) -> FundamentalDecision:
        """Perilaku saat API gagal/ragu."""
        if self.cfg.fail_mode == "continue":
            log.warning("Fundamental %s gagal -> fail_mode=continue (lanjut).", what)
            return FundamentalDecision(True, f"{what} gagal, fail_mode=continue")
        log.warning("Fundamental %s gagal -> fail_mode=skip (tidak entry).", what)
        return FundamentalDecision(False, f"{what} gagal, fail_mode=skip")

    # ------------------------------------------------------------------ #
    # 1. Kalender ekonomi
    # ------------------------------------------------------------------ #
    def check_calendar(self, now: datetime | None = None) -> FundamentalDecision:
        """Blokir entry dalam window menit sebelum/sesudah event high-impact.

        Mengharapkan endpoint JSON berisi list event dengan field minimal:
        ``date`` (ISO8601), ``impact`` (mis. "High"), ``country``/``currency``.
        Bentuk persis berbeda antar penyedia -> sesuaikan ``_parse_event_time``.
        """
        if not self.cfg.calendar_url:
            return FundamentalDecision(True, "kalender nonaktif")

        now = now or _now_utc()
        events = self._cached_events()
        if events is None:
            try:
                params = {}
                if self.news_api_key:
                    params["apikey"] = self.news_api_key
                resp = requests.get(
                    self.cfg.calendar_url,
                    params=params or None,
                    timeout=self.cfg.http_timeout_sec,
                )
                resp.raise_for_status()
                events = resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("Kalender error: %s", exc)
                return self._fail("kalender")
            ttl = max(1, self.cfg.calendar_cache_minutes) * 60.0
            self._cal_cache = (time.monotonic() + ttl, events)

        window = self.cfg.no_trade_window_minutes
        try:
            for ev in self._iter_high_impact_usd(events):
                ev_time = ev["time"]
                delta_min = abs((ev_time - now).total_seconds()) / 60.0
                if delta_min <= window:
                    return FundamentalDecision(
                        False,
                        f"Blackout berita: '{ev.get('title', 'event')}' "
                        f"dalam {delta_min:.0f} mnt (window {window})",
                    )
        except Exception as exc:  # noqa: BLE001 - parsing tak terduga -> fail-safe
            log.warning("Parse kalender gagal: %s", exc)
            return self._fail("kalender(parse)")

        return FundamentalDecision(True, "tidak ada event high-impact dekat")

    def _cached_events(self) -> object | None:
        """Kembalikan events dari cache bila masih segar, else None (perlu fetch)."""
        if self._cal_cache is not None and time.monotonic() < self._cal_cache[0]:
            return self._cal_cache[1]
        return None

    @staticmethod
    def _iter_high_impact_usd(events: object):
        """Normalisasi berbagai bentuk payload -> dict {time, title, impact}.

        Toleran terhadap beberapa skema umum penyedia kalender.
        """
        if not isinstance(events, list):
            return
        for ev in events:
            if not isinstance(ev, dict):
                continue
            impact = str(ev.get("impact", ev.get("importance", ""))).lower()
            currency = str(
                ev.get("currency", ev.get("country", ev.get("ccy", "")))
            ).upper()
            if "high" not in impact and impact not in {"3", "high impact"}:
                continue
            if currency and "USD" not in currency and "US" not in currency:
                continue
            raw_time = ev.get("date") or ev.get("time") or ev.get("datetime")
            if not raw_time:
                continue
            try:
                ev_time = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                if ev_time.tzinfo is None:
                    ev_time = ev_time.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            yield {
                "time": ev_time,
                "title": ev.get("title") or ev.get("event") or "event",
                "impact": impact,
            }

    # ------------------------------------------------------------------ #
    # 2. Fear & Greed Index
    # ------------------------------------------------------------------ #
    def check_fear_greed(self) -> FundamentalDecision:
        if not self.cfg.fear_greed_filter:
            return FundamentalDecision(True, "fear&greed nonaktif")
        try:
            resp = requests.get(
                self.cfg.fear_greed_url, timeout=self.cfg.http_timeout_sec
            )
            resp.raise_for_status()
            data = resp.json()
            value = int(data["data"][0]["value"])
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            log.warning("Fear&Greed error: %s", exc)
            return self._fail("fear&greed")

        if value < self.cfg.fear_greed_min:
            return FundamentalDecision(
                False, f"Fear&Greed {value} < {self.cfg.fear_greed_min} (extreme fear)"
            )
        if value > self.cfg.fear_greed_max:
            return FundamentalDecision(
                False, f"Fear&Greed {value} > {self.cfg.fear_greed_max} (extreme greed)"
            )
        return FundamentalDecision(True, f"Fear&Greed {value} normal")

    # ------------------------------------------------------------------ #
    # Gabungan
    # ------------------------------------------------------------------ #
    def is_trading_allowed(self, now: datetime | None = None) -> FundamentalDecision:
        """Evaluasi semua filter aktif. Block pertama yang menolak menang."""
        if not self.cfg.enabled:
            return FundamentalDecision(True, "filter fundamental nonaktif")

        cal = self.check_calendar(now)
        if not cal.allowed:
            return cal

        fg = self.check_fear_greed()
        if not fg.allowed:
            return fg

        return FundamentalDecision(True, "fundamental OK")
