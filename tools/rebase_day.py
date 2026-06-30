"""Re-baseline equity awal hari ke equity sekarang (one-shot, bot HARUS berhenti).

Dipakai setelah deposit / tarik dana agar circuit breaker daily-loss tidak
salah-blokir. Jalankan saat bot tidak berjalan (hindari dua koneksi MT5):

    python -m tools.rebase_day
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.config import load_config
from core.journal import Journal
from core.mt5_client import MT5Client
from core.risk_manager import DayState


def main() -> None:
    cfg = load_config()
    client = MT5Client(cfg.secrets, cfg.symbol_pattern)
    if not client.connect():
        print("ERROR: gagal connect MT5 (pastikan terminal login & bot berhenti).")
        raise SystemExit(1)
    eq = client.equity()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    j = Journal()
    ds = j.load_day_state()
    old = ds.start_equity if ds else None
    if ds and ds.day == day:
        ds.start_equity = eq
        ds.consecutive_losses = 0
        ds.paused = False
    else:
        ds = DayState(day=day, start_equity=eq, trades_today=0,
                      consecutive_losses=0, paused=False)
    j.save_day_state(ds)
    j.set_state("paused", False)
    j.close()
    client.shutdown()
    print(f"OK: rebase start_equity {old} -> {eq:.2f} (day={day}, paused=False)")


if __name__ == "__main__":
    main()
