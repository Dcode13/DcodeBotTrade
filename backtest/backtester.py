"""Backtester strategi M15->M5->M1 (§14).

Terpisah dari engine live. Menerapkan logika §7 bar-per-bar dengan biaya
spread + estimasi slippage. Output: jumlah trade, win rate, average R,
profit factor, max drawdown, equity curve (dalam satuan R).

CATATAN JUJUR (§14): hasil backtest M1 sering JAUH lebih bagus daripada live
(spread/slippage/latency nyata lebih kejam). Perlakukan dengan skeptis.

Mode data:
- ``--days N``  : tarik historis langsung dari MT5 (butuh Windows + terminal).
- ``--csv-dir`` : muat M1.csv / M5.csv / M15.csv (kolom time,open,high,low,close).

``run_backtest()`` murni (tanpa MT5) sehingga dapat diuji/diberi data sintetis.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core import strategy as strat_mod
from core.config import StrategyConfig, load_config

log = logging.getLogger("backtester")


@dataclass
class BacktestResult:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_r: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_r: float = 0.0
    net_r: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    trade_log: list[dict] = field(default_factory=list)

    def pretty(self) -> str:
        pf = "∞" if self.profit_factor == float("inf") else f"{self.profit_factor:.2f}"
        return (
            "===== HASIL BACKTEST =====\n"
            f"trades        : {self.trades} (W:{self.wins} L:{self.losses})\n"
            f"win rate      : {self.win_rate:.1f}%\n"
            f"avg R         : {self.avg_r:.3f}\n"
            f"net R         : {self.net_r:.2f}\n"
            f"profit factor : {pf}\n"
            f"max drawdown  : {self.max_drawdown_r:.2f} R\n"
            "==========================\n"
            "Catatan: backtest M1 cenderung optimistis vs live. Skeptis."
        )


def _htf_view(htf: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    """Potong higher-TF agar iloc[-1]=bar berjalan, iloc[-2]=last closed pada ``t``."""
    pos = int((htf.index <= t).sum())  # jumlah bar dgn open_time <= t
    return htf.iloc[:pos]


def run_backtest(
    df_trend: pd.DataFrame,
    df_zone: pd.DataFrame,
    df_entry: pd.DataFrame,
    cfg: StrategyConfig,
    spread_points: float = 100.0,
    point: float = 0.01,
    slippage_points: float = 20.0,
    warmup: int = 250,
) -> BacktestResult:
    """Simulasi bar-per-bar pada data M1, sinkron dgn M5/M15 (no look-ahead).

    Model eksekusi:
    - Sinyal dievaluasi pada close M1 bar i (jadi ``iloc[-2]`` = bar i).
    - Entry di OPEN bar i+1 (hindari look-ahead).
    - Exit intrabar: jika low<=SL/high>=TP (BUY) -> isi di harga SL/TP.
      Bila SL & TP kena di bar sama -> asumsikan SL dulu (konservatif).
    - Biaya = (spread + slippage) dikurangkan dari R sekali (friction entry).
    """
    res = BacktestResult()
    n = len(df_entry)
    if n < warmup + 5 or df_zone.empty or df_trend.empty:
        log.warning("Data kurang untuk backtest.")
        return res

    friction_price = (spread_points + slippage_points) * point
    equity_r = 0.0
    peak_r = 0.0
    res.equity_curve.append(0.0)

    i = warmup
    while i < n - 2:
        bar_close_time = df_entry.index[i]
        # View data: M1 sampai i+1 (iloc[-2]=bar i, iloc[-1]=bar i+1 "forming").
        m1_view = df_entry.iloc[: i + 2]
        m5_view = _htf_view(df_zone, bar_close_time)
        m15_view = _htf_view(df_trend, bar_close_time)
        if len(m5_view) < 5 or len(m15_view) < cfg.ema_slow + 2:
            i += 1
            continue

        # Harga referensi = close bar i (proxy bid/ask sama; friction terpisah).
        ref = float(df_entry["close"].iloc[i])
        signal, _ = strat_mod.evaluate(m15_view, m5_view, m1_view, cfg, bid=ref, ask=ref)
        if signal is None:
            i += 1
            continue

        # Entry di open bar berikutnya (i+1).
        entry = float(df_entry["open"].iloc[i + 1])
        sl, tp = signal.sl, signal.tp
        sl_distance = signal.sl_distance
        if sl_distance <= 0:
            i += 1
            continue
        friction_r = friction_price / sl_distance

        # Walk forward cari exit.
        outcome_r = None
        exit_time = None
        for j in range(i + 1, n):
            hi = float(df_entry["high"].iloc[j])
            lo = float(df_entry["low"].iloc[j])
            if signal.direction == "BUY":
                hit_sl = lo <= sl
                hit_tp = hi >= tp
                if hit_sl and hit_tp:
                    outcome_r = -1.0
                elif hit_sl:
                    outcome_r = -1.0
                elif hit_tp:
                    outcome_r = cfg.rr_ratio
            else:  # SELL
                hit_sl = hi >= sl
                hit_tp = lo <= tp
                if hit_sl and hit_tp:
                    outcome_r = -1.0
                elif hit_sl:
                    outcome_r = -1.0
                elif hit_tp:
                    outcome_r = cfg.rr_ratio
            if outcome_r is not None:
                exit_time = df_entry.index[j]
                break

        if outcome_r is None:
            break  # data habis saat posisi masih terbuka

        net_r = outcome_r - friction_r
        equity_r += net_r
        peak_r = max(peak_r, equity_r)
        res.max_drawdown_r = max(res.max_drawdown_r, peak_r - equity_r)
        res.equity_curve.append(equity_r)

        res.trades += 1
        if net_r > 0:
            res.wins += 1
        else:
            res.losses += 1
        res.trade_log.append({
            "entry_time": str(df_entry.index[i + 1]),
            "exit_time": str(exit_time),
            "direction": signal.direction,
            "entry": entry, "sl": sl, "tp": tp,
            "gross_r": outcome_r, "net_r": net_r,
        })

        # Lanjut dari bar setelah exit (hindari overlap posisi).
        exit_idx = df_entry.index.get_loc(exit_time)
        i = int(exit_idx) + 1

    # Agregasi.
    if res.trades:
        rs = [t["net_r"] for t in res.trade_log]
        res.net_r = float(np.sum(rs))
        res.avg_r = float(np.mean(rs))
        res.win_rate = res.wins / res.trades * 100.0
        gross_win = sum(r for r in rs if r > 0)
        gross_loss = abs(sum(r for r in rs if r < 0))
        res.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return res


# --------------------------------------------------------------------------- #
def _load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()


def _load_from_mt5(cfg, days: int):
    """Tarik data historis via MT5 (hanya Windows) sesuai timeframe stack config."""
    from core.market_data import MarketData
    from core.mt5_client import MT5Client

    tfs = cfg.timeframes
    client = MT5Client(cfg.secrets, cfg.symbol_pattern)
    if not client.connect():
        raise RuntimeError("Gagal connect MT5.")
    symbol = client.discover_symbol()
    if not symbol:
        raise RuntimeError("Simbol tidak ditemukan.")
    data = MarketData(client)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    dfs = {
        "trend": data.get_rates_range(tfs.trend, start, end, symbol),
        "zone": data.get_rates_range(tfs.zone, start, end, symbol),
        "entry": data.get_rates_range(tfs.entry, start, end, symbol),
    }
    client.shutdown()
    return dfs


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Backtester strategi BTCUSD")
    parser.add_argument("--days", type=int, default=60, help="hari historis dari MT5")
    parser.add_argument("--csv-dir", default=None,
                        help="folder berisi <trend>.csv/<zone>.csv/<entry>.csv (sesuai TF config)")
    parser.add_argument("--spread", type=float, default=100.0, help="spread (points)")
    parser.add_argument("--slippage", type=float, default=20.0, help="slippage (points)")
    parser.add_argument("--out", default=None, help="simpan equity curve CSV ke path ini")
    args = parser.parse_args()

    cfg = load_config()
    point = 0.01
    tfs = cfg.timeframes
    tf_errors = tfs.validate()
    if tf_errors:
        for e in tf_errors:
            print(f"❌ {e}")
        return
    print(f"Timeframe: trend {tfs.trend} -> zone {tfs.zone} -> entry {tfs.entry}")

    if args.csv_dir:
        d = Path(args.csv_dir)
        dfs = {
            "trend": _load_csv(d / f"{tfs.trend}.csv"),
            "zone": _load_csv(d / f"{tfs.zone}.csv"),
            "entry": _load_csv(d / f"{tfs.entry}.csv"),
        }
    else:
        dfs = _load_from_mt5(cfg, args.days)

    res = run_backtest(
        dfs["trend"], dfs["zone"], dfs["entry"], cfg.strategy,
        spread_points=args.spread, point=point, slippage_points=args.slippage,
    )
    print(res.pretty())

    if args.out and res.equity_curve:
        pd.DataFrame({"equity_r": res.equity_curve}).to_csv(args.out, index=False)
        print(f"Equity curve disimpan: {args.out}")


if __name__ == "__main__":
    main()
