"""Referensi fundamental LBMA Gold (AM & PM) untuk strategi emas (XAUUSD).

Modul ini punya dua bagian:

1. ``LBMAStore`` - mengunduh feed JSON publik LBMA (AM auction 10:30 London &
   PM auction 15:00 London), menyimpan riwayat **6 bulan** ke cache lokal
   (``data/lbma_history.json``), dan menyediakan lookup per hari/bulan/tahun
   untuk perintah ``/LBMA``.

2. Logika entry berbasis LBMA (PURE, tanpa MT5) sesuai aturan user:

   Aturan 1   - Bandingkan harga XAUUSD terkini terhadap *level acuan* LBMA:
                * harga terkini DI BAWAH level -> tunggu harga NAIK menyentuh
                  level -> SELL (fade di resistance).
                * harga terkini DI ATAS level  -> tunggu harga TURUN menyentuh
                  level -> BUY  (fade di support).
   Aturan 1a  - Bila LBMA AM > PM  -> level acuan = PM.
   Aturan 1b  - Bila LBMA PM > AM  -> level acuan = AM (SL 50 pips).
   Aturan 2   - Bila harga-harga LBMA pada 2 hari sebelumnya BERDEKATAN
                (rentang <= ~300 pips) -> pasar konsolidasi -> JANGAN entry.

Catatan satuan "pip" emas: 1 pip = 0.1 (10 point pada quote 2 desimal). Jadi
50 pips = $5.0 dan 300 pips = $30.0. Nilai ``pip_size`` dapat dikonfigurasi di
``config.yaml`` (lbma.pip_size) bila brokermu memakai konvensi berbeda.

Feed bebas diakses untuk keperluan pribadi/edukasi; redistribusi komersial butuh
lisensi dari ICE Benchmark Administration (IBA). Lihat ketentuan di situs LBMA/IBA.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import requests

from core.config import LBMAConfig

log = logging.getLogger(__name__)

AM_URL = "https://prices.lbma.org.uk/json/gold_am.json"
PM_URL = "https://prices.lbma.org.uk/json/gold_pm.json"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; dcode-gold-bot/1.0; +https://www.lbma.org.uk/)",
    "Accept": "application/json, text/plain, */*",
}


# --------------------------------------------------------------------------- #
# Util tanggal
# --------------------------------------------------------------------------- #
def months_ago(d: dt.date, months: int) -> dt.date:
    """Tanggal ``months`` bulan sebelum ``d`` (clamp ke hari terakhir bila perlu)."""
    month_index = d.year * 12 + (d.month - 1) - months
    y, m = divmod(month_index, 12)
    m += 1
    if m == 12:
        first_next = dt.date(y + 1, 1, 1)
    else:
        first_next = dt.date(y, m + 1, 1)
    last_day = (first_next - dt.timedelta(days=1)).day
    return dt.date(y, m, min(d.day, last_day))


def _usd_from_row(row: dict) -> float | None:
    """Ambil nilai USD (v[0]) dari satu baris feed LBMA."""
    v = row.get("v") or []
    if not v:
        return None
    try:
        val = v[0]
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _index_usd(rows: list) -> dict[str, float]:
    """Map {"YYYY-MM-DD": USD} dari list feed (lewati baris tanpa USD)."""
    out: dict[str, float] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ds = row.get("d")
        if not ds:
            continue
        try:
            dt.date.fromisoformat(str(ds))  # validasi format
        except ValueError:
            continue
        usd = _usd_from_row(row)
        if usd is not None:
            out[str(ds)] = usd
    return out


# --------------------------------------------------------------------------- #
# Data acuan & analisis
# --------------------------------------------------------------------------- #
@dataclass
class LBMAReference:
    """Acuan LBMA untuk satu tanggal + level yang dipilih (aturan 1a/1b)."""

    ref_date: dt.date
    am: float | None
    pm: float | None
    level_name: str            # "AM" | "PM"
    level: float
    sl_pips: float             # SL yang ditetapkan untuk level ini


@dataclass
class LBMAAnalysis:
    """Hasil analisis fundamental LBMA sebelum cek touch harga."""

    reference: LBMAReference | None
    blocked: bool
    reason: str
    proximity_range: float = 0.0       # rentang (max-min) nilai LBMA jendela (harga)
    window_values: list[float] = field(default_factory=list)


@dataclass
class LBMATouch:
    """Sinyal entry ketika harga menyentuh level acuan (aturan 1)."""

    direction: str             # "BUY" | "SELL"
    entry: float
    sl: float
    tp: float
    sl_distance: float
    side: str                  # "below" | "above" (posisi harga vs level saat scan)
    reason: str


# --------------------------------------------------------------------------- #
def choose_reference(
    ref_date: dt.date, am: float | None, pm: float | None, cfg: LBMAConfig
) -> LBMAReference | None:
    """Pilih level acuan LBMA sesuai aturan 1a/1b.

    - AM > PM -> level = PM.
    - PM > AM -> level = AM (SL 50 pips).
    - AM == PM (atau salah satu hilang) -> pakai yang tersedia; default AM.
    """
    if am is None and pm is None:
        return None
    if am is None:
        return LBMAReference(ref_date, am, pm, "PM", float(pm), cfg.sl_pips)
    if pm is None:
        return LBMAReference(ref_date, am, pm, "AM", float(am), cfg.sl_pips)

    if am > pm:
        # Aturan 1a: AM lebih tinggi -> acuan PM.
        return LBMAReference(ref_date, am, pm, "PM", float(pm), cfg.sl_pips)
    if pm > am:
        # Aturan 1b: PM lebih tinggi -> acuan AM, SL 50 pips.
        return LBMAReference(ref_date, am, pm, "AM", float(am), cfg.sl_pips)
    # AM == PM
    return LBMAReference(ref_date, am, pm, "AM", float(am), cfg.sl_pips)


def collect_prev_day_values(
    am_map: dict[str, float],
    pm_map: dict[str, float],
    ref_date: dt.date,
    days: int,
) -> list[float]:
    """Ambil semua nilai AM & PM untuk ``days`` hari kalender-LBMA tepat sebelum ref_date."""
    all_dates = sorted(set(am_map) | set(pm_map))
    prev = [d for d in all_dates if dt.date.fromisoformat(d) < ref_date]
    chosen = prev[-days:] if days > 0 else []
    values: list[float] = []
    for d in chosen:
        if d in am_map:
            values.append(am_map[d])
        if d in pm_map:
            values.append(pm_map[d])
    return values


def analyze(
    am_map: dict[str, float],
    pm_map: dict[str, float],
    ref_date: dt.date,
    cfg: LBMAConfig,
) -> LBMAAnalysis:
    """Bangun acuan LBMA untuk ``ref_date`` + terapkan filter konsolidasi (aturan 2)."""
    iso = ref_date.isoformat()
    am = am_map.get(iso)
    pm = pm_map.get(iso)
    ref = choose_reference(ref_date, am, pm, cfg)
    if ref is None:
        return LBMAAnalysis(None, True, f"Tidak ada data LBMA untuk {iso}")

    # Aturan 2: konsolidasi 2 hari sebelumnya (rentang <= proximity_pips).
    threshold = cfg.proximity_pips * cfg.pip_size
    window = collect_prev_day_values(am_map, pm_map, ref_date, cfg.proximity_days)
    prox_range = (max(window) - min(window)) if len(window) >= 2 else 0.0

    if len(window) >= 2 and prox_range <= threshold:
        return LBMAAnalysis(
            reference=ref,
            blocked=True,
            reason=(
                f"Konsolidasi: rentang LBMA {cfg.proximity_days} hari terakhir "
                f"{prox_range:.2f} <= {threshold:.2f} (~{cfg.proximity_pips:.0f} pips) "
                f"-> tidak entry"
            ),
            proximity_range=prox_range,
            window_values=window,
        )

    return LBMAAnalysis(
        reference=ref,
        blocked=False,
        reason="OK (LBMA tidak konsolidasi)",
        proximity_range=prox_range,
        window_values=window,
    )


def touch_signal(
    ref: LBMAReference,
    bid: float,
    ask: float,
    last_close: float,
    cfg: LBMAConfig,
) -> tuple[LBMATouch | None, str]:
    """Cek apakah harga sudah MENYENTUH level acuan -> bangun sinyal (aturan 1).

    Arah ditentukan dari posisi ``last_close`` (candle terakhir close) vs level:
      - last_close < level -> sisi "below" -> tunggu harga NAIK ke level -> SELL.
      - last_close > level -> sisi "above" -> tunggu harga TURUN ke level -> BUY.

    "Menyentuh" = harga live berada dalam ``entry_tolerance_pips`` dari level.
    """
    level = ref.level
    pip = cfg.pip_size
    tol = cfg.entry_tolerance_pips * pip
    sl_dist = ref.sl_pips * pip

    if sl_dist <= 0:
        return None, "sl_distance <= 0 (cek lbma.sl_pips/pip_size)"

    if last_close < level:
        # Harga di bawah level -> SELL saat harga naik mencapai level.
        reached = ask >= (level - tol)
        if not reached:
            return None, (
                f"SELL pending: harga {ask:.2f} belum mencapai level {ref.level_name} "
                f"{level:.2f} (butuh >= {level - tol:.2f})"
            )
        entry = ask
        sl = entry + sl_dist
        tp = entry - cfg.rr_ratio * sl_dist
        return (
            LBMATouch(
                direction="SELL",
                entry=entry,
                sl=sl,
                tp=tp,
                sl_distance=sl_dist,
                side="below",
                reason=(
                    f"LBMA {ref.level_name}={level:.2f}: harga dari bawah menyentuh "
                    f"level -> SELL (SL {ref.sl_pips:.0f}p={sl_dist:.2f})"
                ),
            ),
            "touch SELL",
        )

    if last_close > level:
        # Harga di atas level -> BUY saat harga turun mencapai level.
        reached = bid <= (level + tol)
        if not reached:
            return None, (
                f"BUY pending: harga {bid:.2f} belum turun ke level {ref.level_name} "
                f"{level:.2f} (butuh <= {level + tol:.2f})"
            )
        entry = ask
        sl = entry - sl_dist
        tp = entry + cfg.rr_ratio * sl_dist
        return (
            LBMATouch(
                direction="BUY",
                entry=entry,
                sl=sl,
                tp=tp,
                sl_distance=sl_dist,
                side="above",
                reason=(
                    f"LBMA {ref.level_name}={level:.2f}: harga dari atas menyentuh "
                    f"level -> BUY (SL {ref.sl_pips:.0f}p={sl_dist:.2f})"
                ),
            ),
            "touch BUY",
        )

    return None, f"Harga tepat di level {ref.level_name} {level:.2f} (arah ambigu) - tunggu"


# --------------------------------------------------------------------------- #
# Store: unduh + cache riwayat 6 bulan
# --------------------------------------------------------------------------- #
class LBMAStore:
    """Pengelola unduh & cache feed LBMA Gold (USD)."""

    def __init__(self, cfg: LBMAConfig, cache_path: str | Path = "data/lbma_history.json") -> None:
        self.cfg = cfg
        self.cache_path = Path(cache_path)
        self.am_map: dict[str, float] = {}
        self.pm_map: dict[str, float] = {}
        self.updated_utc: str | None = None
        self.load_cache()

    # ------------------------------------------------------------------ #
    def load_cache(self) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Gagal baca cache LBMA: %s", exc)
            return False
        self.am_map = {str(k): float(v) for k, v in (data.get("am") or {}).items()}
        self.pm_map = {str(k): float(v) for k, v in (data.get("pm") or {}).items()}
        self.updated_utc = data.get("updated_utc")
        log.info("Cache LBMA dimuat: %d AM, %d PM (updated=%s)",
                 len(self.am_map), len(self.pm_map), self.updated_utc)
        return bool(self.am_map or self.pm_map)

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_utc": self.updated_utc,
            "am": self.am_map,
            "pm": self.pm_map,
        }
        try:
            self.cache_path.write_text(
                json.dumps(payload, indent=0, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("Gagal tulis cache LBMA: %s", exc)

    # ------------------------------------------------------------------ #
    def _today(self) -> dt.date:
        return dt.datetime.now(dt.timezone.utc).date()

    def is_fresh(self) -> bool:
        """Cache dianggap segar bila di-update pada tanggal UTC hari ini."""
        if not self.updated_utc:
            return False
        try:
            upd = dt.datetime.fromisoformat(self.updated_utc).date()
        except ValueError:
            return False
        return upd == self._today()

    def refresh(self, months: int | None = None, timeout: int = 60) -> bool:
        """Unduh ulang feed AM & PM, simpan riwayat ``months`` bulan ke cache.

        Return True bila berhasil mengambil data baru. Bila gagal jaringan,
        cache lama dipertahankan (fail-safe) dan return False.
        """
        months = months or self.cfg.history_months
        end = self._today()
        start = months_ago(end, months)
        try:
            am_rows = requests.get(AM_URL, headers=_HEADERS, timeout=timeout).json()
            pm_rows = requests.get(PM_URL, headers=_HEADERS, timeout=timeout).json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Refresh LBMA gagal (pakai cache lama): %s", exc)
            return False

        am_all = _index_usd(am_rows)
        pm_all = _index_usd(pm_rows)
        start_iso = start.isoformat()
        self.am_map = {d: v for d, v in am_all.items() if d >= start_iso}
        self.pm_map = {d: v for d, v in pm_all.items() if d >= start_iso}
        self.updated_utc = dt.datetime.now(dt.timezone.utc).isoformat()
        self.save_cache()
        log.info("LBMA refresh OK: %d AM, %d PM (>= %s)",
                 len(self.am_map), len(self.pm_map), start_iso)
        return True

    def ensure_fresh(self, months: int | None = None) -> bool:
        """Refresh hanya bila cache belum ada / tidak segar hari ini."""
        if self.am_map and self.is_fresh():
            return True
        return self.refresh(months)

    # ------------------------------------------------------------------ #
    # Query
    # ------------------------------------------------------------------ #
    def has_data(self) -> bool:
        return bool(self.am_map or self.pm_map)

    def sorted_dates(self) -> list[str]:
        return sorted(set(self.am_map) | set(self.pm_map))

    def get(self, d: dt.date | str) -> tuple[float | None, float | None]:
        iso = d if isinstance(d, str) else d.isoformat()
        return self.am_map.get(iso), self.pm_map.get(iso)

    def latest_date(self) -> dt.date | None:
        dates = self.sorted_dates()
        return dt.date.fromisoformat(dates[-1]) if dates else None

    def reference_for(self, d: dt.date) -> LBMAReference | None:
        am, pm = self.get(d)
        return choose_reference(d, am, pm, self.cfg)

    def analyze_for(self, d: dt.date) -> LBMAAnalysis:
        return analyze(self.am_map, self.pm_map, d, self.cfg)

    def recent(self, n: int) -> list[tuple[str, float | None, float | None]]:
        """``n`` tanggal terbaru: (date_iso, am, pm)."""
        out = []
        for iso in self.sorted_dates()[-n:]:
            out.append((iso, self.am_map.get(iso), self.pm_map.get(iso)))
        return out

    def range(
        self, start: dt.date, end: dt.date
    ) -> list[tuple[str, float | None, float | None]]:
        s, e = start.isoformat(), end.isoformat()
        out = []
        for iso in self.sorted_dates():
            if s <= iso <= e:
                out.append((iso, self.am_map.get(iso), self.pm_map.get(iso)))
        return out

    def monthly_summary(self, months: int) -> list[dict]:
        """Ringkasan per bulan (rata-rata & min/max AM+PM) untuk ``months`` bulan terakhir."""
        buckets: dict[str, list[float]] = {}
        for iso in self.sorted_dates():
            ym = iso[:7]  # YYYY-MM
            vals = buckets.setdefault(ym, [])
            am = self.am_map.get(iso)
            pm = self.pm_map.get(iso)
            if am is not None:
                vals.append(am)
            if pm is not None:
                vals.append(pm)
        rows = []
        for ym in sorted(buckets)[-months:]:
            vals = buckets[ym]
            if not vals:
                continue
            rows.append({
                "month": ym,
                "avg": sum(vals) / len(vals),
                "min": min(vals),
                "max": max(vals),
                "n": len(vals),
            })
        return rows
