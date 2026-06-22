"""Persistence: SQLite trade journal + state bot (§13).

Dua tabel:
- ``trades``  : satu baris per order/posisi (entry, sl, tp, exit, P/L, R, ...).
- ``bot_state``: key-value untuk state yang harus bertahan saat restart
  (consecutive_losses, equity awal hari, mode, last_bar_time, paused, dll).

Tidak meng-import MT5. Aman dipakai test & live.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.risk_manager import DayState


@dataclass
class TradeRecord:
    ticket: int
    symbol: str
    direction: str
    lots: float
    entry: float
    sl: float
    tp: float
    open_time: str
    sl_distance: float = 0.0   # jarak SL awal (untuk hitung R, tak diubah BE)
    reason: str = ""
    retcode: int = 0
    magic: int = 0
    exit_price: float | None = None
    close_time: str | None = None
    profit: float | None = None
    r_multiple: float | None = None
    status: str = "OPEN"  # OPEN | CLOSED


class Journal:
    """Wrapper SQLite sederhana (thread sederhana, koneksi check_same_thread off)."""

    def __init__(self, db_path: str | Path = "data/journal.sqlite") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ #
    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                ticket      INTEGER PRIMARY KEY,
                symbol      TEXT NOT NULL,
                direction   TEXT NOT NULL,
                lots        REAL NOT NULL,
                entry       REAL NOT NULL,
                sl          REAL,
                tp          REAL,
                open_time   TEXT NOT NULL,
                sl_distance REAL,
                reason      TEXT,
                retcode     INTEGER,
                magic       INTEGER,
                exit_price  REAL,
                close_time  TEXT,
                profit      REAL,
                r_multiple  REAL,
                status      TEXT NOT NULL DEFAULT 'OPEN'
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Trades
    # ------------------------------------------------------------------ #
    def record_open(self, t: TradeRecord) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO trades
            (ticket, symbol, direction, lots, entry, sl, tp, open_time,
             sl_distance, reason, retcode, magic, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (t.ticket, t.symbol, t.direction, t.lots, t.entry, t.sl, t.tp,
             t.open_time, t.sl_distance, t.reason, t.retcode, t.magic, "OPEN"),
        )
        self.conn.commit()

    def get_trade(self, ticket: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM trades WHERE ticket=?", (ticket,)
        ).fetchone()
        return dict(row) if row else None

    def record_close(
        self,
        ticket: int,
        exit_price: float,
        profit: float,
        r_multiple: float,
        close_time: str | None = None,
    ) -> None:
        close_time = close_time or datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE trades
            SET exit_price=?, profit=?, r_multiple=?, close_time=?, status='CLOSED'
            WHERE ticket=?
            """,
            (exit_price, profit, r_multiple, close_time, ticket),
        )
        self.conn.commit()

    def update_sl(self, ticket: int, sl: float) -> None:
        self.conn.execute("UPDATE trades SET sl=? WHERE ticket=?", (sl, ticket))
        self.conn.commit()

    def get_open_trades(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status='OPEN'"
        ).fetchall()
        return [dict(r) for r in rows]

    def has_ticket(self, ticket: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM trades WHERE ticket=?", (ticket,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ #
    # Laporan performa (§12 /report)
    # ------------------------------------------------------------------ #
    def performance_summary(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT profit, r_multiple FROM trades WHERE status='CLOSED'"
        ).fetchall()
        n = len(rows)
        if n == 0:
            return {"trades": 0}

        profits = [r["profit"] or 0.0 for r in rows]
        rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
        wins = [p for p in profits if p > 0]
        losses = [p for p in profits if p < 0]

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        return {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / n * 100.0) if n else 0.0,
            "net_profit": sum(profits),
            "avg_r": (sum(rs) / len(rs)) if rs else 0.0,
            "profit_factor": profit_factor,
        }

    # ------------------------------------------------------------------ #
    # State key-value
    # ------------------------------------------------------------------ #
    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM bot_state WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    # ------------------------------------------------------------------ #
    # DayState helpers (§8.2 + §13)
    # ------------------------------------------------------------------ #
    def save_day_state(self, state: DayState) -> None:
        self.set_state(
            "day_state",
            {
                "day": state.day,
                "start_equity": state.start_equity,
                "trades_today": state.trades_today,
                "consecutive_losses": state.consecutive_losses,
                "paused": state.paused,
            },
        )

    def load_day_state(self) -> DayState | None:
        data = self.get_state("day_state")
        if not data:
            return None
        return DayState(
            day=data["day"],
            start_equity=data["start_equity"],
            trades_today=data.get("trades_today", 0),
            consecutive_losses=data.get("consecutive_losses", 0),
            paused=data.get("paused", False),
        )

    def close(self) -> None:
        self.conn.close()
