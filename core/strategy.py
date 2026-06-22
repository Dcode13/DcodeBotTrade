"""Logika sinyal multi-timeframe (M15 -> M5 -> M1).

Pure: hanya tergantung pandas/numpy + indikator. TIDAK meng-import MT5
sehingga dapat diuji & dipakai backtester. Mengembalikan objek ``Signal``
(atau ``None``) yang sudah berisi SL/TP/jarak SL siap dieksekusi.

Evaluasi memakai candle TERAKHIR YANG SUDAH CLOSE (``iloc[-2]``) agar tidak
mengintip candle berjalan (look-ahead bias).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

import pandas as pd

from core import indicators
from core.config import StrategyConfig

Direction = Literal["BUY", "SELL"]
Bias = Literal["UP", "DOWN", "NONE"]


@dataclass
class Signal:
    """Sinyal entry lengkap dengan rencana SL/TP."""

    direction: Direction
    entry: float          # harga referensi (ask utk BUY / bid utk SELL)
    sl: float
    tp: float
    sl_distance: float    # |entry - sl| dalam harga
    zone: float           # harga zona M5 yang dipakai
    atr_m1: float
    bias: Bias
    signal_bar_time: pd.Timestamp
    body_ratio: float
    rsi_m1: float
    reason: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["signal_bar_time"] = str(self.signal_bar_time)
        return d


@dataclass
class Swing:
    index: int
    price: float


# --------------------------------------------------------------------------- #
# 7.1 Bias tren - M15
# --------------------------------------------------------------------------- #
def compute_bias(df_m15: pd.DataFrame, cfg: StrategyConfig) -> Bias:
    """BIAS UP/DOWN/NONE dari EMA fast/slow pada candle M15 terakhir close."""
    if len(df_m15) < cfg.ema_slow + 2:
        return "NONE"

    close = df_m15["close"].astype(float)
    ema_fast = indicators.ema(close, cfg.ema_fast)
    ema_slow = indicators.ema(close, cfg.ema_slow)

    i = -2  # candle terakhir yang sudah close
    c = float(close.iloc[i])
    ef = float(ema_fast.iloc[i])
    es = float(ema_slow.iloc[i])

    if c > es and ef > es:
        return "UP"
    if c < es and ef < es:
        return "DOWN"
    return "NONE"


# --------------------------------------------------------------------------- #
# 7.2 Zona penting - M5 (swing/pivot)
# --------------------------------------------------------------------------- #
def find_swings(
    df: pd.DataFrame, pivot_n: int, lookback: int
) -> tuple[list[Swing], list[Swing]]:
    """Deteksi swing high & low memakai fractal lebar ``pivot_n``.

    Bar i = swing high jika ``high[i]`` adalah maksimum di jendela
    ``[i-n, i+n]`` (dan strictly > kedua tetangga langsung untuk hindari
    plateau). Swing low simetris. Hanya mempertimbangkan ``lookback`` candle
    terakhir. Bar dalam ``pivot_n`` terakhir tak bisa dikonfirmasi (butuh n
    bar sesudahnya), jadi diabaikan.
    """
    n = pivot_n
    highs: list[Swing] = []
    lows: list[Swing] = []
    if len(df) < 2 * n + 1:
        return highs, lows

    sub = df.iloc[-lookback:] if lookback and len(df) > lookback else df
    h = sub["high"].astype(float).to_numpy()
    l = sub["low"].astype(float).to_numpy()
    size = len(sub)

    for i in range(n, size - n):
        win_h = h[i - n : i + n + 1]
        win_l = l[i - n : i + n + 1]
        if h[i] == win_h.max() and h[i] > h[i - 1] and h[i] > h[i + 1]:
            highs.append(Swing(index=i, price=float(h[i])))
        if l[i] == win_l.min() and l[i] < l[i - 1] and l[i] < l[i + 1]:
            lows.append(Swing(index=i, price=float(l[i])))
    return highs, lows


def select_zone(
    df_m5: pd.DataFrame, bias: Bias, price: float, cfg: StrategyConfig
) -> float | None:
    """Pilih zona terdekat searah bias.

    BIAS UP   -> support (swing low) DI BAWAH harga -> ambil yang TERTINGGI.
    BIAS DOWN -> resistance (swing high) DI ATAS harga -> ambil yang TERENDAH.
    """
    highs, lows = find_swings(df_m5, cfg.swing_pivot_n, cfg.swing_lookback)

    if bias == "UP":
        candidates = [s.price for s in lows if s.price < price]
        return max(candidates) if candidates else None
    if bias == "DOWN":
        candidates = [s.price for s in highs if s.price > price]
        return min(candidates) if candidates else None
    return None


# --------------------------------------------------------------------------- #
# 7.3 Trigger eksekusi - M1
# --------------------------------------------------------------------------- #
@dataclass
class Trigger:
    is_signal: bool
    body_ratio: float
    rsi: float
    reason: str


def evaluate_trigger(
    df_m1: pd.DataFrame, bias: Bias, cfg: StrategyConfig
) -> Trigger:
    """Cek momentum candle M1 terakhir close (``iloc[-2]``)."""
    needed = max(cfg.rsi_period + 2, 3)
    if len(df_m1) < needed:
        return Trigger(False, 0.0, 0.0, "data M1 kurang")

    rsi_series = indicators.rsi(df_m1["close"], cfg.rsi_period)
    bar = df_m1.iloc[-2]
    o, h, l, c = (float(bar["open"]), float(bar["high"]),
                  float(bar["low"]), float(bar["close"]))
    rng = h - l
    if rng <= 0:
        return Trigger(False, 0.0, float(rsi_series.iloc[-2]), "range candle nol")

    body_ratio = abs(c - o) / rng
    rsi_val = float(rsi_series.iloc[-2])

    if body_ratio < cfg.min_body_ratio:
        return Trigger(False, body_ratio, rsi_val,
                       f"body_ratio {body_ratio:.2f} < {cfg.min_body_ratio}")

    if bias == "UP":
        if c <= o:
            return Trigger(False, body_ratio, rsi_val, "candle bukan bullish")
        if cfg.rsi_filter and rsi_val > cfg.rsi_overbought:
            return Trigger(False, body_ratio, rsi_val,
                           f"RSI {rsi_val:.1f} > overbought {cfg.rsi_overbought}")
        return Trigger(True, body_ratio, rsi_val, "momentum bullish M1")

    if bias == "DOWN":
        if o <= c:
            return Trigger(False, body_ratio, rsi_val, "candle bukan bearish")
        if cfg.rsi_filter and rsi_val < cfg.rsi_oversold:
            return Trigger(False, body_ratio, rsi_val,
                           f"RSI {rsi_val:.1f} < oversold {cfg.rsi_oversold}")
        return Trigger(True, body_ratio, rsi_val, "momentum bearish M1")

    return Trigger(False, body_ratio, rsi_val, "bias NONE")


# --------------------------------------------------------------------------- #
# 7.4 Penempatan SL & TP + perakitan Signal
# --------------------------------------------------------------------------- #
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_signal(
    df_m1: pd.DataFrame,
    bias: Bias,
    zone: float,
    ref_price: float,
    cfg: StrategyConfig,
    trigger: Trigger,
) -> Signal | None:
    """Hitung SL/TP final dari signal candle + zona + ATR(M1)."""
    atr_m1_series = indicators.atr(df_m1, cfg.atr_period)
    atr_m1 = float(atr_m1_series.iloc[-2])
    if atr_m1 <= 0 or pd.isna(atr_m1):
        return None

    bar = df_m1.iloc[-2]
    sig_low = float(bar["low"])
    sig_high = float(bar["high"])
    bar_time = df_m1.index[-2]
    buffer = cfg.sl_buffer_atr_m1 * atr_m1

    if bias == "UP":
        raw_sl = min(sig_low, zone) - buffer
        sl_distance = ref_price - raw_sl
        sl_distance = _clamp(sl_distance,
                             cfg.sl_min_atr_m1 * atr_m1,
                             cfg.sl_max_atr_m1 * atr_m1)
        sl = ref_price - sl_distance
        tp = ref_price + cfg.rr_ratio * sl_distance
        direction: Direction = "BUY"
    elif bias == "DOWN":
        raw_sl = max(sig_high, zone) + buffer
        sl_distance = raw_sl - ref_price
        sl_distance = _clamp(sl_distance,
                             cfg.sl_min_atr_m1 * atr_m1,
                             cfg.sl_max_atr_m1 * atr_m1)
        sl = ref_price + sl_distance
        tp = ref_price - cfg.rr_ratio * sl_distance
        direction = "SELL"
    else:
        return None

    if sl_distance <= 0:
        return None

    return Signal(
        direction=direction,
        entry=ref_price,
        sl=sl,
        tp=tp,
        sl_distance=sl_distance,
        zone=zone,
        atr_m1=atr_m1,
        bias=bias,
        signal_bar_time=bar_time,
        body_ratio=trigger.body_ratio,
        rsi_m1=trigger.rsi,
        reason=trigger.reason,
    )


# --------------------------------------------------------------------------- #
# Orkestrasi penuh
# --------------------------------------------------------------------------- #
def evaluate(
    df_m15: pd.DataFrame,
    df_m5: pd.DataFrame,
    df_m1: pd.DataFrame,
    cfg: StrategyConfig,
    bid: float | None = None,
    ask: float | None = None,
) -> tuple[Signal | None, str]:
    """Evaluasi penuh trend->zona->entry.

    Tiga dataframe ini berasal dari timeframe yang DAPAT DIKONFIGURASI
    (``config.timeframes``: trend/zone/entry, default M15/M5/M1). Fungsi ini
    TF-agnostik: ``df_m15`` = TF trend, ``df_m5`` = TF zona, ``df_m1`` = TF entry.

    Mengembalikan ``(Signal | None, alasan)``. ``alasan`` selalu diisi untuk
    keperluan log/alert walau tidak ada sinyal.

    ``bid``/``ask`` adalah harga tick terkini. Bila tidak tersedia, dipakai
    close M1 terakhir sebagai referensi (cocok untuk backtest).
    """
    # 1. Bias M15
    bias = compute_bias(df_m15, cfg)
    if bias == "NONE":
        return None, "Bias M15 = NONE (tidak trading)"

    # Harga referensi M5 untuk proximity zona.
    if len(df_m5) < 2:
        return None, "data M5 kurang"
    m5_price = float(df_m5["close"].iloc[-2])

    # 2. Zona M5
    zone = select_zone(df_m5, bias, m5_price, cfg)
    if zone is None:
        return None, f"Tidak ada zona {('support' if bias == 'UP' else 'resistance')} searah bias {bias}"

    atr_m5 = float(indicators.atr(df_m5, cfg.atr_period).iloc[-2])
    if atr_m5 <= 0 or pd.isna(atr_m5):
        return None, "ATR M5 tidak valid"

    proximity = cfg.zone_proximity_atr_m5 * atr_m5
    if abs(m5_price - zone) > proximity:
        return None, (f"Harga belum dekat zona ({abs(m5_price - zone):.2f} > "
                      f"{proximity:.2f}) - menunggu")

    # 3. Trigger M1
    trigger = evaluate_trigger(df_m1, bias, cfg)
    if not trigger.is_signal:
        return None, f"Trigger M1 belum valid: {trigger.reason}"

    # Harga referensi entry.
    if bias == "UP":
        ref_price = ask if ask is not None else float(df_m1["close"].iloc[-2])
    else:
        ref_price = bid if bid is not None else float(df_m1["close"].iloc[-2])

    # 4. SL/TP
    signal = build_signal(df_m1, bias, zone, ref_price, cfg, trigger)
    if signal is None:
        return None, "Gagal menghitung SL/TP (ATR/jarak tidak valid)"

    return signal, signal.reason
