"""Penarikan data candle M15/M5/M1 -> DataFrame ber-index waktu (§6).

DataFrame standar yang dihasilkan punya kolom:
``open, high, low, close, tick_volume, spread, real_volume`` dengan index
``time`` (UTC, dari ``copy_rates_*``). Sisa kode (strategi/backtester) hanya
butuh open/high/low/close.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from core.mt5_client import MT5Client, _require_mt5

log = logging.getLogger(__name__)


def _timeframe_map() -> dict[str, int]:
    m = _require_mt5()
    return {
        "M1": m.TIMEFRAME_M1,
        "M5": m.TIMEFRAME_M5,
        "M15": m.TIMEFRAME_M15,
        "M30": m.TIMEFRAME_M30,
        "H1": m.TIMEFRAME_H1,
        "H4": m.TIMEFRAME_H4,
        "D1": m.TIMEFRAME_D1,
    }


def _rates_to_df(rates) -> pd.DataFrame:
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    return df


class MarketData:
    """Pembungkus penarikan candle MT5."""

    def __init__(self, client: MT5Client) -> None:
        self.client = client

    def get_rates(self, timeframe: str, count: int, symbol: str | None = None) -> pd.DataFrame:
        """Tarik ``count`` candle terakhir untuk ``timeframe``."""
        m = _require_mt5()
        sym = symbol or self.client.symbol
        tf = _timeframe_map()[timeframe]
        rates = m.copy_rates_from_pos(sym, tf, 0, count)
        if rates is None:
            log.warning("copy_rates_from_pos(%s,%s) None: %s", sym, timeframe, m.last_error())
            return pd.DataFrame()
        return _rates_to_df(rates)

    def get_rates_range(
        self, timeframe: str, start: datetime, end: datetime, symbol: str | None = None
    ) -> pd.DataFrame:
        """Tarik candle pada rentang waktu (untuk backtester, §14)."""
        m = _require_mt5()
        sym = symbol or self.client.symbol
        tf = _timeframe_map()[timeframe]
        rates = m.copy_rates_range(sym, tf, start, end)
        if rates is None:
            log.warning("copy_rates_range(%s,%s) None: %s", sym, timeframe, m.last_error())
            return pd.DataFrame()
        return _rates_to_df(rates)

    def get_multi(self, count: int, symbol: str | None = None) -> dict[str, pd.DataFrame]:
        """Tarik M15/M5/M1 sekaligus (kompatibilitas; pakai get_stack untuk TF dinamis)."""
        return {
            "M15": self.get_rates("M15", count, symbol),
            "M5": self.get_rates("M5", count, symbol),
            "M1": self.get_rates("M1", count, symbol),
        }

    def get_stack(self, tfs, count: int, symbol: str | None = None) -> dict[str, pd.DataFrame]:
        """Tarik 3 timeframe sesuai TimeframesConfig -> key 'trend'/'zone'/'entry'."""
        return {
            "trend": self.get_rates(tfs.trend, count, symbol),
            "zone": self.get_rates(tfs.zone, count, symbol),
            "entry": self.get_rates(tfs.entry, count, symbol),
        }
