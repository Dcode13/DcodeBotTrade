"""Cek koneksi MT5: login, discovery simbol BTCUSD, spesifikasi kontrak, spread.

Jalankan:  python tools\\check_mt5.py
Tidak mengirim order apa pun (read-only).
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.market_data import MarketData  # noqa: E402
from core.mt5_client import MT5Client  # noqa: E402


def main() -> None:
    cfg = load_config()
    print(f"Login={cfg.secrets.mt5_login} Server={cfg.secrets.mt5_server} "
          f"(password {'terisi' if cfg.secrets.mt5_password else 'KOSONG'})")

    client = MT5Client(cfg.secrets, cfg.symbol_pattern)
    if not client.connect():
        print("❌ Gagal connect. Pastikan terminal MT5 berjalan & login, "
              "lalu Allow Algo Trading aktif.")
        return

    info = client.account_info()
    print(f"\n✅ TERHUBUNG")
    print(f"   Akun   : {info.login}")
    print(f"   Server : {info.server}")
    print(f"   Mata uang: {info.currency}")
    print(f"   Balance: {info.balance:.2f} {info.currency}  (~{info.balance/100:.2f} unit, akun cent)")
    print(f"   Equity : {info.equity:.2f} {info.currency}")

    print("\n🔎 Mencari simbol BTCUSD...")
    symbol = client.discover_symbol()
    if not symbol:
        print("❌ Simbol BTCUSD tidak ditemukan dengan pola "
              f"'{cfg.symbol_pattern}'. Cek Market Watch broker.")
        client.shutdown()
        return
    print(f"✅ Simbol terpilih: {symbol}")

    spec = client.get_symbol_spec(symbol)
    print("\n📐 SPESIFIKASI KONTRAK (dari API broker):")
    print(f"   digits            : {spec.digits}")
    print(f"   point             : {spec.point}")
    print(f"   contract_size     : {spec.trade_contract_size}")
    print(f"   tick_size         : {spec.trade_tick_size}")
    print(f"   tick_value        : {spec.trade_tick_value}")
    print(f"   volume_min/step/max: {spec.volume_min} / {spec.volume_step} / {spec.volume_max}")
    print(f"   stops_level       : {spec.trade_stops_level} (min jarak SL/TP = {spec.trade_stops_level*spec.point})")
    print(f"   money_per_unit    : {spec.money_per_unit}")

    spread = client.get_spread_points(symbol)
    print(f"\n💹 Spread saat ini : {spread:.0f} points "
          f"(config max_spread_points={cfg.risk.max_spread_points})")
    if spread > cfg.risk.max_spread_points:
        print("   ⚠️  Spread > batas config -> bot akan SKIP entry. "
              "Pertimbangkan naikkan max_spread_points setelah verifikasi.")
    print(f"   Market open      : {client.is_market_open(symbol)}")

    # Cek data candle.
    data = MarketData(client)
    for tf in ("M15", "M5", "M1"):
        df = data.get_rates(tf, 250, symbol)
        print(f"   Candle {tf:<3}: {len(df)} bar terambil"
              + (f" (terakhir {df.index[-1]})" if not df.empty else ""))

    client.shutdown()
    print("\n✅ Semua cek selesai. Verifikasi angka di atas masuk akal sebelum live.")


if __name__ == "__main__":
    main()
