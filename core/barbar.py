"""BARBAR strategy: XAUUSD M1 hedged martingale grid.

This module keeps the aggressive grid logic isolated from the normal signal
engine. It only touches positions and pending orders that use
``BarbarConfig.magic_number``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Any

import pandas as pd

from core.config import BarbarConfig, StraddleM1Config, TIMEFRAME_MINUTES
from core.executor import deduce_filling
from core.mt5_client import MT5Client, _require_mt5
from core.risk_manager import SymbolSpec, _round_to_step

log = logging.getLogger(__name__)


@dataclass
class BarbarCycleResult:
    """Result of one BARBAR management cycle."""

    events: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocked: str = ""
    opened: int = 0
    closed: int = 0
    canceled: int = 0
    modified: int = 0
    exit_profit: float | None = None
    exit_reason: str = ""

    @property
    def touched_account(self) -> bool:
        return bool(self.opened or self.closed or self.canceled or self.modified)


class BarbarGrid:
    """Runtime engine for the BARBAR hedged martingale grid."""

    def __init__(self, client: MT5Client, cfg: BarbarConfig) -> None:
        self.client = client
        self.cfg = cfg
        self.last_bar_time: str | None = None
        self.cooldown_until: float = 0.0

    # ------------------------------------------------------------------ #
    def cycle(
        self,
        spec: SymbolSpec,
        df_m1: pd.DataFrame | None,
        execution_enabled: bool,
        autotrading_enabled: bool,
        allow_new_entries: bool = True,
        block_reason: str = "",
        bar_time: str | None = None,
    ) -> BarbarCycleResult:
        """Run one management cycle.

        Exits are checked before entry filters so risk stops can still close a
        basket while the system is cooling down or the spread is wide.
        """
        res = BarbarCycleResult()
        positions = self.positions(spec.name)
        pending = self.pending_orders(spec.name)

        exit_reason = self._exit_reason(positions)
        if exit_reason:
            if not execution_enabled:
                res.blocked = f"{exit_reason}, but execution is not live"
                return res
            if not autotrading_enabled:
                res.blocked = f"{exit_reason}, but MT5 AutoTrading is OFF"
                return res
            profit_before = self.basket_profit(positions)
            close_res = self.close_all(spec, reason=exit_reason)
            res.events.extend(close_res.events)
            res.errors.extend(close_res.errors)
            res.closed += close_res.closed
            res.canceled += close_res.canceled
            res.exit_profit = profit_before
            res.exit_reason = exit_reason
            if exit_reason != "take_profit":
                self.cooldown_until = time.time() + max(0, int(self.cfg.cooldown_after_stop))
            return res

        if positions and self.cfg.trailing_stop and execution_enabled and autotrading_enabled:
            trail_res = self.manage_trailing_stops(spec, positions)
            res.events.extend(trail_res.events)
            res.errors.extend(trail_res.errors)
            res.modified += trail_res.modified

        if positions and self.cfg.auto_take_profit and execution_enabled and autotrading_enabled:
            tp_res = self.manage_take_profits(spec, positions)
            res.events.extend(tp_res.events)
            res.errors.extend(tp_res.errors)
            res.modified += tp_res.modified

        if positions and self.cfg.candle_follow and execution_enabled and autotrading_enabled:
            cf_res = self.manage_candle_follow(spec, positions, df_m1)
            res.events.extend(cf_res.events)
            res.errors.extend(cf_res.errors)
            res.modified += cf_res.modified

        if not allow_new_entries:
            res.blocked = block_reason or "new BARBAR entries blocked"
            return res

        filter_msg = self.check_filters(spec)
        if filter_msg:
            res.blocked = filter_msg
            return res

        if self.cooldown_until > time.time():
            left = int(self.cooldown_until - time.time())
            res.blocked = f"cooldown {left}s after stop"
            return res

        if not positions and not pending:
            if self._one_position_per_bar_blocked(bar_time):
                res.blocked = "one_position_per_bar: this M1 bar already used"
                return res
            if not execution_enabled:
                res.blocked = "BARBAR alert-only: would start new basket"
                return res
            if not autotrading_enabled:
                res.blocked = "MT5 AutoTrading is OFF"
                return res
            if self.cfg.entry_mode.upper() == "MARKET":
                if self.cfg.base_lot > self.cfg.max_total_lots:
                    res.blocked = (
                        f"base_lot {self.cfg.base_lot} > max_total_lots "
                        f"{self.cfg.max_total_lots}"
                    )
                    return res
                direction, why = self._market_bias(df_m1)
                if not direction:
                    res.blocked = why
                    return res
                open_res = self.open_market(spec, direction, self.cfg.base_lot, "barbar_initial")
                res.events.extend(open_res.events)
                res.errors.extend(open_res.errors)
                res.opened += open_res.opened
            else:
                open_res = self.place_straddle(spec)
                res.events.extend(open_res.events)
                res.errors.extend(open_res.errors)
                res.opened += open_res.opened
            if res.touched_account:
                self.last_bar_time = bar_time or self.last_bar_time
            return res

        if (
            positions
            and pending
            and self.cfg.cancel_opposite_pending
            and not self.cfg.stop_and_reverse
        ):
            cancel_res = self.cancel_opposite_pending(spec, positions, pending)
            res.events.extend(cancel_res.events)
            res.errors.extend(cancel_res.errors)
            res.canceled += cancel_res.canceled

        if positions and self.cfg.stop_and_reverse and execution_enabled and autotrading_enabled:
            sar_res = self.manage_stop_reverse(spec, positions, pending, df_m1)
            res.events.extend(sar_res.events)
            res.errors.extend(sar_res.errors)
            res.opened += sar_res.opened
            res.modified += sar_res.modified

        rec_res = self.manage_recovery(
            spec, positions, bar_time, execution_enabled, autotrading_enabled
        )
        res.events.extend(rec_res.events)
        res.errors.extend(rec_res.errors)
        res.blocked = rec_res.blocked
        res.opened += rec_res.opened
        if rec_res.opened:
            self.last_bar_time = bar_time or self.last_bar_time
        return res

    # ------------------------------------------------------------------ #
    def check_filters(self, spec: SymbolSpec) -> str:
        if not self.client.is_market_open(spec.name):
            return "market closed or symbol disabled"
        tick = self.client.get_tick(spec.name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            return "tick unavailable"
        spread_price = float(tick.ask - tick.bid)
        if spread_price > float(self.cfg.max_spread):
            return f"spread {spread_price:.{spec.digits}f} > max {self.cfg.max_spread}"
        hours_msg = self._trade_hours_block()
        if hours_msg:
            return hours_msg
        return ""

    def _trade_hours_block(self) -> str:
        raw = (self.cfg.trade_hours_utc or "").strip()
        if not raw:
            return ""
        now = datetime.now(timezone.utc).time()
        windows = [part.strip() for part in raw.split(",") if part.strip()]
        for window in windows:
            try:
                start_s, end_s = window.split("-", 1)
                start = dtime.fromisoformat(start_s.strip())
                end = dtime.fromisoformat(end_s.strip())
            except ValueError:
                log.warning("Invalid barbar.trade_hours_utc window: %s", window)
                continue
            if start <= end:
                if start <= now <= end:
                    return ""
            elif now >= start or now <= end:
                return ""
        return f"outside trade_hours_utc ({raw})"

    # ------------------------------------------------------------------ #
    def positions(self, symbol: str) -> list[Any]:
        m = _require_mt5()
        positions = m.positions_get(symbol=symbol)
        if positions is None:
            return []
        return [p for p in positions if int(getattr(p, "magic", 0)) == self.cfg.magic_number]

    def pending_orders(self, symbol: str) -> list[Any]:
        m = _require_mt5()
        orders = m.orders_get(symbol=symbol)
        if orders is None:
            return []
        return [o for o in orders if int(getattr(o, "magic", 0)) == self.cfg.magic_number]

    def basket_profit(self, positions: list[Any]) -> float:
        total = 0.0
        for p in positions:
            total += float(getattr(p, "profit", 0.0))
            total += float(getattr(p, "swap", 0.0))
        return total

    def total_lots(self, positions: list[Any]) -> float:
        return sum(float(getattr(p, "volume", 0.0)) for p in positions)

    # ------------------------------------------------------------------ #
    def place_straddle(self, spec: SymbolSpec) -> BarbarCycleResult:
        res = BarbarCycleResult()
        tick = self.client.get_tick(spec.name)
        if tick is None:
            res.blocked = "tick unavailable"
            return res
        base_lot = self._fit_lot(self.cfg.base_lot, spec)
        if base_lot <= 0:
            res.blocked = "base_lot <= 0 after broker limits"
            return res
        if base_lot * 2 > self.cfg.max_total_lots:
            res.blocked = (
                f"straddle potential lot {base_lot * 2:.2f} > "
                f"max_total_lots {self.cfg.max_total_lots}"
            )
            return res
        min_dist = spec.trade_stops_level * spec.point
        if min_dist > 0 and self.cfg.straddle_distance < min_dist:
            res.blocked = (
                f"straddle_distance {self.cfg.straddle_distance:.{spec.digits}f} "
                f"< broker min {min_dist:.{spec.digits}f}"
            )
            return res

        buy_price = round(float(tick.ask) + self.cfg.straddle_distance, spec.digits)
        sell_price = round(float(tick.bid) - self.cfg.straddle_distance, spec.digits)
        buy_sl = self._sl_for("BUY", buy_price, spec)
        sell_sl = self._sl_for("SELL", sell_price, spec)
        buy_tp = self._tp_for("BUY", buy_price, base_lot, spec)
        sell_tp = self._tp_for("SELL", sell_price, base_lot, spec)
        buy = self._send_pending(spec, "BUY_STOP", base_lot, buy_price, buy_sl, buy_tp)
        sell = self._send_pending(spec, "SELL_STOP", base_lot, sell_price, sell_sl, sell_tp)
        for label, item in (("BUY STOP", buy), ("SELL STOP", sell)):
            if item["ok"]:
                res.opened += 1
                res.events.append(
                    f"{label} {base_lot} lot placed @ {item['price']:.{spec.digits}f}"
                )
            else:
                res.errors.append(f"{label} failed: {item['comment']}")
        return res

    def open_market(
        self, spec: SymbolSpec, direction: str, lots: float, comment: str
    ) -> BarbarCycleResult:
        res = BarbarCycleResult()
        lots = self._fit_lot(lots, spec)
        if lots <= 0:
            res.blocked = "lot <= 0 after broker limits"
            return res
        item = self._send_market(spec, direction, lots, comment)
        if item["ok"]:
            res.opened = 1
            res.events.append(
                f"{direction} {lots} lot opened @ {item['price']:.{spec.digits}f}"
            )
        else:
            res.errors.append(f"{direction} open failed: {item['comment']}")
        return res

    def manage_recovery(
        self,
        spec: SymbolSpec,
        positions: list[Any],
        bar_time: str | None,
        execution_enabled: bool,
        autotrading_enabled: bool,
    ) -> BarbarCycleResult:
        res = BarbarCycleResult()
        if not positions:
            return res
        if len(positions) >= int(self.cfg.max_grid_levels):
            res.blocked = f"max_grid_levels reached ({len(positions)})"
            return res
        total_lots = self.total_lots(positions)
        if total_lots >= float(self.cfg.max_total_lots):
            res.blocked = f"max_total_lots reached ({total_lots:.2f})"
            return res
        if self._one_position_per_bar_blocked(bar_time):
            res.blocked = "one_position_per_bar: this M1 bar already used"
            return res

        last = self._last_position(positions)
        direction = self._position_direction(last)
        if not direction:
            res.blocked = "unknown last position direction"
            return res
        if not self._is_against_last(spec, last, direction):
            return res

        if not execution_enabled:
            res.blocked = "BARBAR alert-only: would add recovery level"
            return res
        if not autotrading_enabled:
            res.blocked = "MT5 AutoTrading is OFF"
            return res

        next_direction = self._next_recovery_direction(direction)
        requested = float(getattr(last, "volume", 0.0)) * float(self.cfg.lot_multiplier)
        remaining = max(0.0, float(self.cfg.max_total_lots) - total_lots)
        if remaining < spec.volume_min:
            res.blocked = (
                f"remaining lot room {remaining:.2f} < broker min {spec.volume_min}"
            )
            return res
        next_lot = self._fit_lot(min(requested, remaining), spec)
        if next_lot > remaining + 1e-9:
            next_lot = _round_to_step(remaining, spec.volume_step)
        if next_lot < spec.volume_min:
            res.blocked = (
                f"remaining lot room {remaining:.2f} < broker min {spec.volume_min}"
            )
            return res

        open_res = self.open_market(spec, next_direction, next_lot, "barbar_recovery")
        res.events.extend(open_res.events)
        res.errors.extend(open_res.errors)
        res.blocked = open_res.blocked
        res.opened += open_res.opened
        return res

    # ------------------------------------------------------------------ #
    def cancel_opposite_pending(
        self, spec: SymbolSpec, positions: list[Any], pending: list[Any]
    ) -> BarbarCycleResult:
        m = _require_mt5()
        active_dirs = {self._position_direction(p) for p in positions}
        active_dirs.discard("")
        res = BarbarCycleResult()
        for order in pending:
            order_type = int(getattr(order, "type", -1))
            is_buy_stop = order_type == m.ORDER_TYPE_BUY_STOP
            is_sell_stop = order_type == m.ORDER_TYPE_SELL_STOP
            should_cancel = ("BUY" in active_dirs and is_sell_stop) or (
                "SELL" in active_dirs and is_buy_stop
            )
            if not should_cancel:
                continue
            item = self._cancel_order(spec, order)
            if item["ok"]:
                res.canceled += 1
                res.events.append(f"opposite pending canceled ticket={item['ticket']}")
            else:
                res.errors.append(f"cancel pending failed: {item['comment']}")
        return res

    def close_all(self, spec: SymbolSpec, reason: str = "manual") -> BarbarCycleResult:
        res = BarbarCycleResult()
        positions = self.positions(spec.name)
        pending = self.pending_orders(spec.name)
        for p in positions:
            item = self._close_position(spec, p, reason)
            if item["ok"]:
                res.closed += 1
                res.events.append(f"closed ticket={item['ticket']} ({reason})")
            else:
                res.errors.append(f"close ticket={getattr(p, 'ticket', '?')} failed: {item['comment']}")
        for o in pending:
            item = self._cancel_order(spec, o)
            if item["ok"]:
                res.canceled += 1
                res.events.append(f"pending canceled ticket={item['ticket']}")
            else:
                res.errors.append(f"cancel ticket={getattr(o, 'ticket', '?')} failed: {item['comment']}")
        return res

    def manage_trailing_stops(self, spec: SymbolSpec, positions: list[Any]) -> BarbarCycleResult:
        """Move BARBAR SL with the trend once a position is already profitable."""
        res = BarbarCycleResult()
        if not positions:
            return res
        tick = self.client.get_tick(spec.name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            res.blocked = "tick unavailable for trailing"
            return res
        basket_lock = self._basket_profit_lock_hit(positions)
        quick_lock = max(0.0, float(self.cfg.quick_profit_lock))
        normal_start = max(0.0, float(self.cfg.trailing_start))
        allow_loss_trailing = bool(self.cfg.trailing_when_loss)

        for p in positions:
            direction = self._position_direction(p)
            if not direction:
                continue
            current = float(tick.bid if direction == "BUY" else tick.ask)
            entry = float(getattr(p, "price_open", 0.0))
            move = (current - entry) if direction == "BUY" else (entry - current)
            is_profit = move > 0
            should_lock = (
                (is_profit and (basket_lock or move >= quick_lock or move >= normal_start))
                or (allow_loss_trailing and not is_profit)
            )
            if not should_lock:
                continue

            new_sl = self._trailing_sl_candidate(
                spec, p, direction, current, entry,
                allow_loss_side=allow_loss_trailing and not is_profit,
            )
            if new_sl is None:
                continue
            old_sl = float(getattr(p, "sl", 0.0) or 0.0)
            if not self._sl_improves(direction, old_sl, new_sl, spec):
                continue

            item = self._modify_sl(spec, p, new_sl)
            if item["ok"]:
                res.modified += 1
                reason = "loss/flat trailing"
                if basket_lock and is_profit:
                    reason = "basket 1% lock"
                elif is_profit:
                    reason = "quick profit lock"
                if is_profit and move >= normal_start:
                    reason = "trailing"
                res.events.append(
                    f"trailing SL ticket={getattr(p, 'ticket', '?')} "
                    f"{direction} -> {new_sl:.{spec.digits}f} ({reason})"
                )
            else:
                res.errors.append(
                    f"trailing SL ticket={getattr(p, 'ticket', '?')} failed: {item['comment']}"
                )
        return res

    def manage_take_profits(self, spec: SymbolSpec, positions: list[Any]) -> BarbarCycleResult:
        """Ensure every BARBAR position has an automatic TP."""
        res = BarbarCycleResult()
        for p in positions:
            direction = self._position_direction(p)
            if not direction:
                continue
            current_tp = float(getattr(p, "tp", 0.0) or 0.0)
            if current_tp > 0:
                continue
            entry = float(getattr(p, "price_open", 0.0))
            lots = float(getattr(p, "volume", 0.0) or 0.0)
            tp = self._tp_for_open_position(direction, entry, lots, spec)
            if tp <= 0:
                continue
            item = self._modify_tp(spec, p, tp)
            if item["ok"]:
                res.modified += 1
                res.events.append(
                    f"auto TP ticket={getattr(p, 'ticket', '?')} "
                    f"{direction} -> {tp:.{spec.digits}f}"
                )
            else:
                res.errors.append(
                    f"auto TP ticket={getattr(p, 'ticket', '?')} failed: {item['comment']}"
                )
        return res

    def manage_candle_follow(
        self, spec: SymbolSpec, positions: list[Any], df_m1: pd.DataFrame | None
    ) -> BarbarCycleResult:
        """Trail SL and TP along the M1 candle structure while the trend holds.

        For a BUY whose last closed M1 candle is still bullish the SL is pulled
        up just under that candle's low and the TP is pushed further above the
        market, so profit keeps running while price climbs. SELL positions
        mirror this under a bearish candle. A counter-trend candle leaves SL/TP
        untouched, so the distance-based trailing can still lock in the move.
        """
        res = BarbarCycleResult()
        if not positions:
            return res
        if df_m1 is None or df_m1.empty or len(df_m1) < 2:
            return res
        tick = self.client.get_tick(spec.name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            res.blocked = "tick unavailable for candle follow"
            return res

        bar = df_m1.iloc[-2]  # last fully closed candle
        bar_open = float(bar["open"])
        bar_close = float(bar["close"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bullish = bar_close > bar_open
        bearish = bar_close < bar_open
        buffer = max(0.0, float(self.cfg.candle_follow_sl_buffer))
        min_dist = spec.trade_stops_level * spec.point
        broker_gap = min_dist if min_dist > 0 else spec.point

        for p in positions:
            direction = self._position_direction(p)
            if not direction:
                continue
            # Only ride a candle that confirms the position's direction.
            if (direction == "BUY" and not bullish) or (direction == "SELL" and not bearish):
                continue

            ticket = int(getattr(p, "ticket", 0))
            fresh = self._fresh_position(spec.name, ticket) or p
            current = float(tick.bid if direction == "BUY" else tick.ask)
            old_sl = float(getattr(fresh, "sl", 0.0) or 0.0)
            old_tp = float(getattr(fresh, "tp", 0.0) or 0.0)
            lots = float(getattr(fresh, "volume", 0.0) or 0.0)

            new_sl = self._candle_follow_sl(
                spec, direction, bar_low, bar_high, buffer, current, broker_gap
            )
            sl_changed = new_sl is not None and self._sl_improves(direction, old_sl, new_sl, spec)

            new_tp = self._candle_follow_tp(spec, direction, current, broker_gap, lots, old_tp)
            tp_changed = new_tp is not None and self._tp_extends(direction, old_tp, new_tp, spec)

            if not sl_changed and not tp_changed:
                continue
            final_sl = new_sl if sl_changed else old_sl
            final_tp = new_tp if tp_changed else old_tp
            item = self._modify_sltp(spec, fresh, final_sl, final_tp)
            if item["ok"]:
                res.modified += 1
                parts = []
                if sl_changed:
                    parts.append(f"SL->{final_sl:.{spec.digits}f}")
                if tp_changed:
                    parts.append(f"TP->{final_tp:.{spec.digits}f}")
                res.events.append(
                    f"candle-follow ticket={ticket} {direction} {' '.join(parts)}"
                )
            else:
                res.errors.append(
                    f"candle-follow ticket={ticket} failed: {item['comment']}"
                )
        return res

    def _candle_follow_sl(
        self,
        spec: SymbolSpec,
        direction: str,
        bar_low: float,
        bar_high: float,
        buffer: float,
        current: float,
        broker_gap: float,
    ) -> float | None:
        """SL just beyond the last candle's extreme, clamped to broker distance."""
        if direction == "BUY":
            new_sl = min(bar_low - buffer, current - broker_gap)
            if new_sl <= 0 or new_sl >= current:
                return None
            return round(new_sl, spec.digits)
        new_sl = max(bar_high + buffer, current + broker_gap)
        if new_sl <= current:
            return None
        return round(new_sl, spec.digits)

    def _candle_follow_tp(
        self,
        spec: SymbolSpec,
        direction: str,
        current: float,
        broker_gap: float,
        lots: float,
        old_tp: float,
    ) -> float | None:
        """TP kept ahead of the market so the trend is not capped early."""
        tp_dist = float(self.cfg.candle_follow_tp_distance)
        if tp_dist <= 0:
            tp_dist = self._tp_distance(lots, spec)
        if tp_dist <= 0:
            return None
        tp_dist = max(tp_dist, broker_gap)
        if direction == "BUY":
            candidate = current + tp_dist
            new_tp = max(candidate, old_tp) if old_tp > 0 else candidate
            if new_tp <= current:
                return None
            return round(new_tp, spec.digits)
        candidate = current - tp_dist
        new_tp = min(candidate, old_tp) if old_tp > 0 else candidate
        if new_tp <= 0 or new_tp >= current:
            return None
        return round(new_tp, spec.digits)

    def _tp_extends(self, direction: str, old_tp: float, new_tp: float, spec: SymbolSpec) -> bool:
        """True only when TP moves further into profit by at least one step."""
        if old_tp <= 0:
            return True
        step = max(spec.point, float(self.cfg.trailing_step))
        if direction == "BUY":
            return (new_tp - old_tp) >= step
        return (old_tp - new_tp) >= step

    def manage_stop_reverse(
        self,
        spec: SymbolSpec,
        positions: list[Any],
        pending: list[Any],
        df_m1: pd.DataFrame | None,
    ) -> BarbarCycleResult:
        """Keep a trailing opposite-side STOP order at the candle-follow SL level.

        The pending stop sits at the same price as the open position's candle
        SL, so a single reversal both stops the trade out and flips into a fresh
        position that rides the new trend (stop-and-reverse). A SELL is guarded
        by a BUY STOP above, a BUY by a SELL STOP below; both trail down/up with
        the M1 candle structure while the candle confirms the open trend.
        """
        res = BarbarCycleResult()
        if not positions:
            return res
        if df_m1 is None or df_m1.empty or len(df_m1) < 2:
            return res
        tick = self.client.get_tick(spec.name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            res.blocked = "tick unavailable for stop-and-reverse"
            return res

        bar = df_m1.iloc[-2]
        bar_open = float(bar["open"])
        bar_close = float(bar["close"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bullish = bar_close > bar_open
        bearish = bar_close < bar_open

        direction = self._position_direction(self._last_position(positions))
        if not direction:
            return res
        # Only trail/place the reverse stop while the candle confirms the trend.
        if (direction == "BUY" and not bullish) or (direction == "SELL" and not bearish):
            return res

        lot = self._stop_reverse_lot(spec, positions)
        if lot <= 0:
            res.blocked = "reverse lot <= 0 after broker limits for stop-and-reverse"
            return res

        buffer = max(0.0, float(self.cfg.candle_follow_sl_buffer))
        min_dist = spec.trade_stops_level * spec.point
        broker_gap = min_dist if min_dist > 0 else spec.point
        current = float(tick.bid if direction == "BUY" else tick.ask)
        level = self._candle_follow_sl(
            spec, direction, bar_low, bar_high, buffer, current, broker_gap
        )
        if level is None:
            return res

        m = _require_mt5()
        if direction == "SELL":
            order_kind, want_type = "BUY_STOP", m.ORDER_TYPE_BUY_STOP
        else:
            order_kind, want_type = "SELL_STOP", m.ORDER_TYPE_SELL_STOP
        existing = [o for o in pending if int(getattr(o, "type", -1)) == want_type]

        if not existing:
            item = self._send_pending(spec, order_kind, lot, level, 0.0, 0.0)
            if item["ok"]:
                res.opened += 1
                res.events.append(
                    f"reverse {order_kind} {lot} @ {level:.{spec.digits}f} (SAR vs {direction})"
                )
            else:
                res.errors.append(f"reverse {order_kind} failed: {item['comment']}")
            return res

        order = existing[0]
        old_price = float(getattr(order, "price_open", 0.0) or 0.0)
        # Trail only toward price (lower BUY STOP / higher SELL STOP), never away.
        if not self._sl_improves(direction, old_price, level, spec):
            return res
        item = self._modify_pending(spec, order, level)
        if item["ok"]:
            res.modified += 1
            res.events.append(
                f"reverse {order_kind} trail -> {level:.{spec.digits}f} ticket={item['ticket']}"
            )
        else:
            res.errors.append(f"reverse {order_kind} trail failed: {item['comment']}")
        return res

    def _stop_reverse_lot(self, spec: SymbolSpec, positions: list[Any]) -> float:
        """Volume for the reverse stop order per ``stop_reverse_lot_mode``."""
        mode = (self.cfg.stop_reverse_lot_mode or "BASE").upper()
        if mode == "MATCH" and positions:
            base = float(getattr(self._last_position(positions), "volume", 0.0) or 0.0)
        elif mode == "FIXED" and float(self.cfg.stop_reverse_lot) > 0:
            base = float(self.cfg.stop_reverse_lot)
        else:
            base = float(self.cfg.base_lot)
        return self._fit_lot(base, spec)

    def _basket_profit_lock_hit(self, positions: list[Any]) -> bool:
        pct = float(self.cfg.profit_lock_percent)
        if pct <= 0:
            return False
        info = self.client.account_info()
        balance = float(getattr(info, "balance", 0.0)) if info else 0.0
        if balance <= 0:
            return False
        target = balance * pct / 100.0
        return self.basket_profit(positions) >= target

    # ------------------------------------------------------------------ #
    def status_text(self, spec: SymbolSpec) -> str:
        positions = self.positions(spec.name)
        pending = self.pending_orders(spec.name)
        profit = self.basket_profit(positions)
        total_lots = self.total_lots(positions)
        cooldown = max(0, int(self.cooldown_until - time.time()))
        lines = [
            "BARBAR STATUS",
            f"symbol: {spec.name}",
            f"magic: {self.cfg.magic_number}",
            f"entry_mode: {self.cfg.entry_mode.upper()} | recovery: {self.cfg.recovery_mode.upper()}",
            f"positions: {len(positions)} | pending: {len(pending)}",
            f"basket P/L: {profit:.2f} | total lots: {total_lots:.2f}/{self.cfg.max_total_lots}",
            f"TP basket: {self.cfg.take_profit_usd:.2f} | max loss: {self.cfg.max_basket_loss_usd:.2f}",
            (
                f"auto TP: {'ON' if self.cfg.auto_take_profit else 'OFF'} | "
                f"distance {'auto' if self.cfg.per_position_tp <= 0 else self.cfg.per_position_tp}"
            ),
            f"grid: step {self.cfg.grid_step} | levels {len(positions)}/{self.cfg.max_grid_levels}",
            (
                f"trailing: {'ON' if self.cfg.trailing_stop else 'OFF'} | "
                f"start {self.cfg.trailing_start} | dist {self.cfg.trailing_distance} | "
                f"lock +{self.cfg.breakeven_plus} | basket {self.cfg.profit_lock_percent}% | "
                f"quick {self.cfg.quick_profit_lock} | loss {'ON' if self.cfg.trailing_when_loss else 'OFF'}"
            ),
            (
                f"candle follow: {'ON' if self.cfg.candle_follow else 'OFF'} | "
                f"SL buffer {self.cfg.candle_follow_sl_buffer} | "
                f"TP dist {'auto' if self.cfg.candle_follow_tp_distance <= 0 else self.cfg.candle_follow_tp_distance}"
            ),
            (
                f"stop-and-reverse: {'ON' if self.cfg.stop_and_reverse else 'OFF'} | "
                f"lot {self.cfg.stop_reverse_lot_mode.upper()}"
                + (f" {self.cfg.stop_reverse_lot}"
                   if self.cfg.stop_reverse_lot_mode.upper() == "FIXED" else "")
            ),
            f"spread max: {self.cfg.max_spread} price units | cooldown: {cooldown}s",
        ]
        for p in positions:
            side = self._position_direction(p)
            lines.append(
                f"#{getattr(p, 'ticket', '?')} {side} {float(p.volume):.2f} "
                f"@ {float(p.price_open):.{spec.digits}f} "
                f"SL={float(getattr(p, 'sl', 0.0) or 0.0):.{spec.digits}f} "
                f"TP={float(getattr(p, 'tp', 0.0) or 0.0):.{spec.digits}f} "
                f"P/L={float(p.profit):.2f}"
            )
        for o in pending:
            vol = float(getattr(o, "volume_initial", getattr(o, "volume_current", 0.0)))
            price = float(getattr(o, "price_open", getattr(o, "price_current", 0.0)))
            sl = float(getattr(o, "sl", 0.0) or 0.0)
            tp = float(getattr(o, "tp", 0.0) or 0.0)
            lines.append(
                f"pending #{getattr(o, 'ticket', '?')} type={getattr(o, 'type', '?')} "
                f"{vol:.2f} @ {price:.{spec.digits}f} "
                f"SL={sl:.{spec.digits}f} TP={tp:.{spec.digits}f}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def _exit_reason(self, positions: list[Any]) -> str:
        if not positions:
            return ""
        profit = self.basket_profit(positions)
        if profit >= float(self.cfg.take_profit_usd):
            return "take_profit"
        if -profit >= float(self.cfg.max_basket_loss_usd):
            return "max_basket_loss"
        info = self.client.account_info()
        if info:
            balance = float(getattr(info, "balance", 0.0))
            equity = float(getattr(info, "equity", 0.0))
            if balance > 0:
                dd_pct = (balance - equity) / balance * 100.0
                if dd_pct >= float(self.cfg.equity_stop_percent):
                    return "equity_stop"
        return ""

    def _market_bias(self, df_m1: pd.DataFrame | None) -> tuple[str | None, str]:
        if df_m1 is None or df_m1.empty or len(df_m1) < 2:
            return None, "M1 data unavailable for MARKET bias"
        bar = df_m1.iloc[-2]
        o = float(bar["open"])
        c = float(bar["close"])
        if c > o:
            return "BUY", "last closed M1 bullish"
        if c < o:
            return "SELL", "last closed M1 bearish"
        return None, "last closed M1 doji"

    def _one_position_per_bar_blocked(self, bar_time: str | None) -> bool:
        return bool(
            self.cfg.one_position_per_bar
            and bar_time
            and self.last_bar_time == bar_time
        )

    def _last_position(self, positions: list[Any]) -> Any:
        return max(positions, key=lambda p: (int(getattr(p, "time", 0)), int(getattr(p, "ticket", 0))))

    def _position_direction(self, position: Any) -> str:
        m = _require_mt5()
        ptype = int(getattr(position, "type", -1))
        if ptype == m.POSITION_TYPE_BUY:
            return "BUY"
        if ptype == m.POSITION_TYPE_SELL:
            return "SELL"
        return ""

    def _is_against_last(self, spec: SymbolSpec, position: Any, direction: str) -> bool:
        tick = self.client.get_tick(spec.name)
        if tick is None:
            return False
        open_price = float(getattr(position, "price_open", 0.0))
        if direction == "BUY":
            return float(tick.bid) <= open_price - float(self.cfg.grid_step)
        if direction == "SELL":
            return float(tick.ask) >= open_price + float(self.cfg.grid_step)
        return False

    def _next_recovery_direction(self, last_direction: str) -> str:
        if self.cfg.recovery_mode.upper() == "AVERAGE":
            return last_direction
        return "SELL" if last_direction == "BUY" else "BUY"

    def _fit_lot(self, lots: float, spec: SymbolSpec) -> float:
        lots = _round_to_step(float(lots), spec.volume_step)
        if lots <= 0:
            return 0.0
        lots = max(spec.volume_min, min(lots, spec.volume_max))
        return _round_to_step(lots, spec.volume_step)

    def _sl_for(self, direction: str, entry_price: float, spec: SymbolSpec) -> float:
        if self.cfg.per_position_sl <= 0:
            return 0.0
        if direction == "BUY":
            return round(entry_price - self.cfg.per_position_sl, spec.digits)
        return round(entry_price + self.cfg.per_position_sl, spec.digits)

    def _tp_distance(self, lots: float, spec: SymbolSpec) -> float:
        if not self.cfg.auto_take_profit:
            return 0.0
        if self.cfg.per_position_tp > 0:
            dist = float(self.cfg.per_position_tp)
        else:
            money_per_unit = spec.money_per_unit
            if lots <= 0 or money_per_unit <= 0 or self.cfg.take_profit_usd <= 0:
                return 0.0
            dist = float(self.cfg.take_profit_usd) / (float(lots) * money_per_unit)
        min_dist = spec.trade_stops_level * spec.point
        if min_dist > 0:
            dist = max(dist, min_dist)
        return dist

    def _tp_for(self, direction: str, entry_price: float, lots: float, spec: SymbolSpec) -> float:
        dist = self._tp_distance(lots, spec)
        if dist <= 0:
            return 0.0
        if direction == "BUY":
            return round(entry_price + dist, spec.digits)
        return round(entry_price - dist, spec.digits)

    def _tp_for_open_position(
        self, direction: str, entry_price: float, lots: float, spec: SymbolSpec
    ) -> float:
        tp = self._tp_for(direction, entry_price, lots, spec)
        if tp <= 0:
            return 0.0
        tick = self.client.get_tick(spec.name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            return tp
        min_dist = spec.trade_stops_level * spec.point
        gap = min_dist if min_dist > 0 else spec.point
        if direction == "BUY":
            min_tp = float(tick.ask) + gap
            if tp <= min_tp:
                tp = min_tp
        else:
            max_tp = float(tick.bid) - gap
            if tp >= max_tp:
                tp = max_tp
        if tp <= 0:
            return 0.0
        return round(tp, spec.digits)

    def _trailing_sl_candidate(
        self,
        spec: SymbolSpec,
        position: Any,
        direction: str,
        current: float,
        entry: float,
        allow_loss_side: bool = False,
    ) -> float | None:
        """Return a broker-valid trailing SL that locks profit, or None."""
        min_dist = spec.trade_stops_level * spec.point
        # Keep at least one point of air even on symbols with stops_level=0.
        broker_gap = min_dist if min_dist > 0 else spec.point
        distance = max(float(self.cfg.trailing_distance), broker_gap)
        plus = max(0.0, float(self.cfg.breakeven_plus))

        if direction == "BUY":
            lock_sl = entry + plus
            trail_sl = current - distance
            candidate = max(lock_sl, trail_sl)
            max_allowed = current - broker_gap
            new_sl = min(candidate, max_allowed)
            if self.cfg.trailing_lock_profit_only and not allow_loss_side and new_sl <= entry:
                return None
            if new_sl <= 0 or new_sl >= current:
                return None
            return round(new_sl, spec.digits)

        lock_sl = entry - plus
        trail_sl = current + distance
        candidate = min(lock_sl, trail_sl)
        min_allowed = current + broker_gap
        new_sl = max(candidate, min_allowed)
        if self.cfg.trailing_lock_profit_only and not allow_loss_side and new_sl >= entry:
            return None
        if new_sl <= current:
            return None
        return round(new_sl, spec.digits)

    def _sl_improves(self, direction: str, old_sl: float, new_sl: float, spec: SymbolSpec) -> bool:
        step = max(spec.point, float(self.cfg.trailing_step))
        if direction == "BUY":
            return old_sl <= 0 or (new_sl - old_sl) >= step
        return old_sl <= 0 or (old_sl - new_sl) >= step

    def _modify_sl(self, spec: SymbolSpec, position: Any, sl: float) -> dict[str, Any]:
        m = _require_mt5()
        ticket = int(getattr(position, "ticket", 0))
        fresh = self._fresh_position(spec.name, ticket) or position
        tp = float(getattr(fresh, "tp", 0.0) or 0.0)
        request = {
            "action": m.TRADE_ACTION_SLTP,
            "symbol": spec.name,
            "position": ticket,
            "sl": round(float(sl), spec.digits),
            "tp": round(tp, spec.digits) if tp else 0.0,
            "magic": int(self.cfg.magic_number),
        }
        return self._order_send(request, m, ticket=ticket)

    def _modify_tp(self, spec: SymbolSpec, position: Any, tp: float) -> dict[str, Any]:
        m = _require_mt5()
        ticket = int(getattr(position, "ticket", 0))
        fresh = self._fresh_position(spec.name, ticket) or position
        sl = float(getattr(fresh, "sl", 0.0) or 0.0)
        request = {
            "action": m.TRADE_ACTION_SLTP,
            "symbol": spec.name,
            "position": ticket,
            "sl": round(sl, spec.digits) if sl else 0.0,
            "tp": round(float(tp), spec.digits),
            "magic": int(self.cfg.magic_number),
        }
        return self._order_send(request, m, ticket=ticket)

    def _modify_sltp(
        self, spec: SymbolSpec, position: Any, sl: float, tp: float
    ) -> dict[str, Any]:
        """Set SL and TP together so neither is wiped by a one-sided modify."""
        m = _require_mt5()
        ticket = int(getattr(position, "ticket", 0))
        request = {
            "action": m.TRADE_ACTION_SLTP,
            "symbol": spec.name,
            "position": ticket,
            "sl": round(float(sl), spec.digits) if sl else 0.0,
            "tp": round(float(tp), spec.digits) if tp else 0.0,
            "magic": int(self.cfg.magic_number),
        }
        return self._order_send(request, m, ticket=ticket)

    def _modify_pending(
        self, spec: SymbolSpec, order: Any, price: float, sl: float = 0.0, tp: float = 0.0
    ) -> dict[str, Any]:
        """Re-price a pending stop order so it can trail with the candle."""
        m = _require_mt5()
        ticket = int(getattr(order, "ticket", getattr(order, "order", 0)))
        request = {
            "action": m.TRADE_ACTION_MODIFY,
            "order": ticket,
            "symbol": spec.name,
            "price": round(float(price), spec.digits),
            "sl": round(float(sl), spec.digits) if sl else 0.0,
            "tp": round(float(tp), spec.digits) if tp else 0.0,
            "type_time": m.ORDER_TIME_GTC,
            "magic": int(self.cfg.magic_number),
        }
        return self._order_send(request, m, ticket=ticket, pending=True)

    def _fresh_position(self, symbol: str, ticket: int) -> Any | None:
        for p in self.positions(symbol):
            if int(getattr(p, "ticket", 0)) == int(ticket):
                return p
        return None

    # ------------------------------------------------------------------ #
    def _send_market(self, spec: SymbolSpec, direction: str, lots: float, comment: str) -> dict[str, Any]:
        m = _require_mt5()
        is_buy = direction == "BUY"
        last = self._result(False, -1, "order not attempted")
        for _ in range(3):
            tick = self.client.get_tick(spec.name)
            if tick is None:
                return self._result(False, -1, "tick unavailable")
            price = float(tick.ask if is_buy else tick.bid)
            request = {
                "action": m.TRADE_ACTION_DEAL,
                "symbol": spec.name,
                "volume": float(lots),
                "type": m.ORDER_TYPE_BUY if is_buy else m.ORDER_TYPE_SELL,
                "price": price,
                "sl": self._sl_for(direction, price, spec),
                "tp": self._tp_for(direction, price, lots, spec),
                "deviation": int(self.cfg.deviation),
                "magic": int(self.cfg.magic_number),
                "comment": comment,
                "type_time": m.ORDER_TIME_GTC,
                "type_filling": deduce_filling(spec),
            }
            last = self._order_send(request, m, price=price)
            if last["ok"] or last["retcode"] != getattr(m, "TRADE_RETCODE_REQUOTE", -999):
                return last
        return last

    def _send_pending(
        self, spec: SymbolSpec, order_kind: str, lots: float, price: float, sl: float, tp: float
    ) -> dict[str, Any]:
        m = _require_mt5()
        order_type = m.ORDER_TYPE_BUY_STOP if order_kind == "BUY_STOP" else m.ORDER_TYPE_SELL_STOP
        request = {
            "action": m.TRADE_ACTION_PENDING,
            "symbol": spec.name,
            "volume": float(self._fit_lot(lots, spec)),
            "type": order_type,
            "price": float(price),
            "sl": float(sl),
            "tp": float(tp),
            "deviation": int(self.cfg.deviation),
            "magic": int(self.cfg.magic_number),
            "comment": "barbar_straddle",
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": getattr(m, "ORDER_FILLING_RETURN", deduce_filling(spec)),
        }
        return self._order_send(request, m, price=price, pending=True)

    def _close_position(self, spec: SymbolSpec, position: Any, reason: str) -> dict[str, Any]:
        m = _require_mt5()
        is_buy = int(position.type) == m.POSITION_TYPE_BUY
        last = self._result(False, -1, "order not attempted", ticket=int(position.ticket))
        for _ in range(3):
            tick = self.client.get_tick(spec.name)
            if tick is None:
                return self._result(False, -1, "tick unavailable")
            price = float(tick.bid if is_buy else tick.ask)
            request = {
                "action": m.TRADE_ACTION_DEAL,
                "symbol": spec.name,
                "volume": float(position.volume),
                "type": m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY,
                "position": int(position.ticket),
                "price": price,
                "deviation": int(self.cfg.deviation),
                "magic": int(self.cfg.magic_number),
                "comment": f"barbar_close_{reason}"[:31],
                "type_time": m.ORDER_TIME_GTC,
                "type_filling": deduce_filling(spec),
            }
            last = self._order_send(request, m, ticket=int(position.ticket), price=price)
            if last["ok"] or last["retcode"] != getattr(m, "TRADE_RETCODE_REQUOTE", -999):
                return last
        return last

    def _cancel_order(self, spec: SymbolSpec, order: Any) -> dict[str, Any]:
        m = _require_mt5()
        ticket = int(getattr(order, "ticket", getattr(order, "order", 0)))
        request = {
            "action": m.TRADE_ACTION_REMOVE,
            "order": ticket,
            "symbol": spec.name,
            "magic": int(self.cfg.magic_number),
            "comment": "barbar_cancel",
        }
        return self._order_send(request, m, ticket=ticket)

    def _order_send(
        self,
        request: dict[str, Any],
        m: Any,
        ticket: int | None = None,
        price: float | None = None,
        pending: bool = False,
    ) -> dict[str, Any]:
        result = m.order_send(request)
        item = self._interpret(result, m, ticket=ticket, price=price, pending=pending)
        if item["ok"]:
            return item
        if item["retcode"] == getattr(m, "TRADE_RETCODE_INVALID_FILL", -999):
            req2 = dict(request)
            req2["type_filling"] = getattr(m, "ORDER_FILLING_RETURN", req2.get("type_filling"))
            result = m.order_send(req2)
            return self._interpret(result, m, ticket=ticket, price=price, pending=pending)
        return item

    def _interpret(
        self,
        result: Any,
        m: Any,
        ticket: int | None = None,
        price: float | None = None,
        pending: bool = False,
    ) -> dict[str, Any]:
        if result is None:
            return self._result(False, -1, f"order_send None: {m.last_error()}", ticket, price)
        retcode = int(result.retcode)
        ok_codes = {getattr(m, "TRADE_RETCODE_DONE", 10009)}
        if pending:
            ok_codes.add(getattr(m, "TRADE_RETCODE_PLACED", 10008))
        ok = retcode in ok_codes
        comment = self._retcode_label(m, retcode)
        raw_comment = getattr(result, "comment", "")
        if raw_comment:
            comment = f"{comment}: {raw_comment}"
        return self._result(
            ok=ok,
            retcode=retcode,
            comment=comment,
            ticket=ticket or getattr(result, "order", None) or getattr(result, "deal", None),
            price=getattr(result, "price", None) or price,
        )

    def _retcode_label(self, m: Any, retcode: int) -> str:
        mapping = {
            getattr(m, "TRADE_RETCODE_PLACED", 10008): "PLACED",
            getattr(m, "TRADE_RETCODE_DONE", 10009): "DONE",
            getattr(m, "TRADE_RETCODE_REQUOTE", 10004): "REQUOTE",
            getattr(m, "TRADE_RETCODE_INVALID_STOPS", 10016): "INVALID_STOPS",
            getattr(m, "TRADE_RETCODE_NO_MONEY", 10019): "NO_MONEY",
            getattr(m, "TRADE_RETCODE_MARKET_CLOSED", 10018): "MARKET_CLOSED",
            getattr(m, "TRADE_RETCODE_INVALID_FILL", 10030): "INVALID_FILL",
            getattr(m, "TRADE_RETCODE_TRADE_DISABLED", 10017): "TRADE_DISABLED",
            getattr(m, "TRADE_RETCODE_CLIENT_DISABLES_AT", 10027): "AUTOTRADING_DISABLED",
        }
        return mapping.get(retcode, f"retcode={retcode}")

    def _result(
        self,
        ok: bool,
        retcode: int,
        comment: str,
        ticket: int | None = None,
        price: float | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "retcode": retcode,
            "comment": comment,
            "ticket": ticket,
            "price": price or 0.0,
        }


# --------------------------------------------------------------------------- #
# Shared order-send plumbing (used by the StraddleM1 engine below).
# --------------------------------------------------------------------------- #
def _order_retcode_label(m: Any, retcode: int) -> str:
    mapping = {
        getattr(m, "TRADE_RETCODE_PLACED", 10008): "PLACED",
        getattr(m, "TRADE_RETCODE_DONE", 10009): "DONE",
        getattr(m, "TRADE_RETCODE_REQUOTE", 10004): "REQUOTE",
        getattr(m, "TRADE_RETCODE_INVALID_STOPS", 10016): "INVALID_STOPS",
        getattr(m, "TRADE_RETCODE_NO_MONEY", 10019): "NO_MONEY",
        getattr(m, "TRADE_RETCODE_MARKET_CLOSED", 10018): "MARKET_CLOSED",
        getattr(m, "TRADE_RETCODE_INVALID_FILL", 10030): "INVALID_FILL",
        getattr(m, "TRADE_RETCODE_TRADE_DISABLED", 10017): "TRADE_DISABLED",
        getattr(m, "TRADE_RETCODE_CLIENT_DISABLES_AT", 10027): "AUTOTRADING_DISABLED",
    }
    return mapping.get(retcode, f"retcode={retcode}")


def _make_order_result(
    ok: bool, retcode: int, comment: str, ticket: int | None = None, price: float | None = None
) -> dict[str, Any]:
    return {"ok": ok, "retcode": retcode, "comment": comment, "ticket": ticket, "price": price or 0.0}


def _interpret_order_result(
    result: Any, m: Any, ticket: int | None = None, price: float | None = None, pending: bool = False
) -> dict[str, Any]:
    if result is None:
        return _make_order_result(False, -1, f"order_send None: {m.last_error()}", ticket, price)
    retcode = int(result.retcode)
    ok_codes = {getattr(m, "TRADE_RETCODE_DONE", 10009)}
    if pending:
        ok_codes.add(getattr(m, "TRADE_RETCODE_PLACED", 10008))
    ok = retcode in ok_codes
    comment = _order_retcode_label(m, retcode)
    raw_comment = getattr(result, "comment", "")
    if raw_comment:
        comment = f"{comment}: {raw_comment}"
    return _make_order_result(
        ok=ok,
        retcode=retcode,
        comment=comment,
        ticket=ticket or getattr(result, "order", None) or getattr(result, "deal", None),
        price=getattr(result, "price", None) or price,
    )


def _send_order_request(
    request: dict[str, Any], m: Any, ticket: int | None = None,
    price: float | None = None, pending: bool = False,
) -> dict[str, Any]:
    result = m.order_send(request)
    item = _interpret_order_result(result, m, ticket=ticket, price=price, pending=pending)
    if item["ok"]:
        return item
    if item["retcode"] == getattr(m, "TRADE_RETCODE_INVALID_FILL", -999):
        req2 = dict(request)
        req2["type_filling"] = getattr(m, "ORDER_FILLING_RETURN", req2.get("type_filling"))
        result = m.order_send(req2)
        return _interpret_order_result(result, m, ticket=ticket, price=price, pending=pending)
    return item


class StraddleM1:
    """StraddleM1 EA engine: single-position straddle + trailing + stop-and-reverse.

    Faithful to the StraddleM1 spec for XAUUSD M1: fixed lot, NO take-profit, NO
    averaging, NO martingale, one position at a time. A 4-state machine guards a
    resting reverse order so a stop-out flips direction (stop-and-reverse) instead
    of restarting a fresh straddle.

    States (derived from the account each cycle):
      FLAT_IDLE        no position, no pending -> place straddle on a new M1 bar
      STRADDLE_PENDING two stop legs waiting   -> refresh straddle on a new bar
      IN_POSITION      one position open       -> trail SL, drag reverse order with it
      REVERSE_PENDING  one lone stop waiting   -> protect it; never re-straddle until fill

    All distances are POINTS (XAUUSD 2-digit: 1 point = ``spec.point`` = 0.01).
    """

    FLAT_IDLE = "FLAT_IDLE"
    STRADDLE_PENDING = "STRADDLE_PENDING"
    IN_POSITION = "IN_POSITION"
    REVERSE_PENDING = "REVERSE_PENDING"

    STRADDLE_COMMENT = "stradm1"
    REVERSE_COMMENT = "revm1"

    def __init__(self, client: MT5Client, cfg: StraddleM1Config) -> None:
        self.client = client
        self.cfg = cfg
        self.last_bar_time: str | None = None
        self.reverse_started_at: float = 0.0
        self.trades_today: int = 0
        self.trade_day: str = ""

    # ------------------------------------------------------------------ #
    def cycle(
        self,
        spec: SymbolSpec,
        df_m1: pd.DataFrame | None,
        execution_enabled: bool,
        autotrading_enabled: bool,
        allow_new_entries: bool = True,
        block_reason: str = "",
        bar_time: str | None = None,
    ) -> BarbarCycleResult:
        """Run one StraddleM1 management cycle (state machine)."""
        res = BarbarCycleResult()
        self._roll_day(spec)
        positions = self.positions(spec.name)
        pending = self.pending_orders(spec.name)

        # Robustness: enforce a single position (close any accidental extras).
        if len(positions) > 1:
            if execution_enabled and autotrading_enabled:
                self._enforce_single(spec, res, positions)
                positions = self.positions(spec.name)
            else:
                res.blocked = "multiple positions but execution not live"
                return res

        state = self._state(positions, pending)

        if state == self.IN_POSITION:
            self.reverse_started_at = 0.0
            if not (execution_enabled and autotrading_enabled):
                res.blocked = "IN_POSITION but execution not live"
                return res
            self._manage_position(spec, res, positions[0], pending)
            return res

        if state == self.REVERSE_PENDING:
            if self.reverse_started_at <= 0:
                self.reverse_started_at = time.time()
            timeout = self._reverse_timeout_seconds()
            elapsed = time.time() - self.reverse_started_at
            if timeout > 0 and elapsed >= timeout and execution_enabled and autotrading_enabled:
                self._cancel_all(spec, pending, res)
                self.reverse_started_at = 0.0
                res.events.append("reverse canceled (timeout) -> back to flat")
            else:
                res.blocked = "REVERSE_PENDING: menunggu reverse fill"
            return res

        # FLAT_IDLE / STRADDLE_PENDING: only act at the open of a fresh M1 bar.
        self.reverse_started_at = 0.0
        new_bar = bar_time is not None and bar_time != self.last_bar_time
        if not new_bar:
            res.blocked = "straddle aktif, menunggu fill" if pending else "menunggu candle M1 baru"
            return res
        if not allow_new_entries:
            res.blocked = block_reason or "new StraddleM1 entries blocked"
            return res
        block = self._entry_filters(spec, execution_enabled, autotrading_enabled)
        if block:
            res.blocked = block
            return res

        self.last_bar_time = bar_time
        self._cancel_all(spec, pending, res)  # clear stale straddle legs first
        if self._place_straddle(spec, df_m1, res):
            self.trades_today += 1
        return res

    # ------------------------------------------------------------------ #
    def positions(self, symbol: str) -> list[Any]:
        m = _require_mt5()
        positions = m.positions_get(symbol=symbol)
        if positions is None:
            return []
        return [p for p in positions if int(getattr(p, "magic", 0)) == self.cfg.magic_number]

    def pending_orders(self, symbol: str) -> list[Any]:
        m = _require_mt5()
        orders = m.orders_get(symbol=symbol)
        if orders is None:
            return []
        return [o for o in orders if int(getattr(o, "magic", 0)) == self.cfg.magic_number]

    def _state(self, positions: list[Any], pending: list[Any]) -> str:
        if positions:
            return self.IN_POSITION
        if not pending:
            return self.FLAT_IDLE
        # A lone resting stop with no position is the reverse order; a full
        # straddle rests two legs. Half-done straddles are never left behind.
        return self.STRADDLE_PENDING if len(pending) >= 2 else self.REVERSE_PENDING

    # ------------------------------------------------------------------ #
    def _manage_position(
        self, spec: SymbolSpec, res: BarbarCycleResult, pos: Any, pending: list[Any]
    ) -> None:
        m = _require_mt5()
        direction = "BUY" if int(pos.type) == m.POSITION_TYPE_BUY else "SELL"
        tick = self.client.get_tick(spec.name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            res.blocked = "tick unavailable for position management"
            return
        sl_dist = float(self.cfg.sl_pts) * spec.point
        entry = float(pos.price_open)
        old_sl = float(getattr(pos, "sl", 0.0) or 0.0)

        # Ensure the entry SL exists (each leg = SL_pts from entry).
        if old_sl <= 0:
            baseline = round(entry - sl_dist if direction == "BUY" else entry + sl_dist, spec.digits)
            item = self._modify_position_sl(spec, pos, baseline)
            if item["ok"]:
                old_sl = baseline
                res.modified += 1
                res.events.append(f"baseline SL {direction} -> {baseline:.{spec.digits}f}")
            else:
                res.errors.append(f"baseline SL failed: {item['comment']}")

        # Trailing (after breakeven, only toward profit, min step throttle).
        effective_sl = old_sl
        new_sl = self._trail_sl(spec, direction, entry, old_sl, tick)
        if new_sl is not None:
            item = self._modify_position_sl(spec, pos, new_sl)
            if item["ok"]:
                effective_sl = new_sl
                res.modified += 1
                res.events.append(f"trail SL {direction} -> {new_sl:.{spec.digits}f}")
            else:
                res.errors.append(f"trail SL failed: {item['comment']}")

        # Reverse stop order follows the SL (stop-and-reverse).
        self._sync_reverse(spec, res, direction, effective_sl, pending, tick)

    def _trail_sl(
        self, spec: SymbolSpec, direction: str, entry: float, old_sl: float, tick: Any
    ) -> float | None:
        point = spec.point
        trail_dist = float(self.cfg.trail_dist_pts) * point
        trail_step = max(point, float(self.cfg.trail_step_pts) * point)
        broker_gap = max(spec.trade_stops_level * point, point)
        if direction == "BUY":
            bid = float(tick.bid)
            if bid < entry:  # breakeven not reached
                return None
            cand = min(bid - trail_dist, bid - broker_gap)
            if cand <= 0 or cand >= bid:
                return None
            if old_sl > 0 and (cand - old_sl) < trail_step:
                return None
            return round(cand, spec.digits)
        ask = float(tick.ask)
        if ask > entry:  # breakeven not reached
            return None
        cand = max(ask + trail_dist, ask + broker_gap)
        if cand <= ask:
            return None
        if old_sl > 0 and (old_sl - cand) < trail_step:
            return None
        return round(cand, spec.digits)

    def _sync_reverse(
        self, spec: SymbolSpec, res: BarbarCycleResult, direction: str,
        sl: float, pending: list[Any], tick: Any,
    ) -> None:
        if sl <= 0:
            return
        m = _require_mt5()
        point = spec.point
        rev_gap = float(self.cfg.rev_gap_pts) * point
        sl_dist = float(self.cfg.sl_pts) * point
        trail_step = max(point, float(self.cfg.trail_step_pts) * point)
        broker_gap = max(spec.trade_stops_level * point, point)

        if direction == "BUY":  # reverse is a SELL STOP below the SL
            kind, want_type = "SELL_STOP", m.ORDER_TYPE_SELL_STOP
            price = min(sl - rev_gap, float(tick.bid) - broker_gap)
            rev_sl = price + sl_dist
        else:  # reverse is a BUY STOP above the SL
            kind, want_type = "BUY_STOP", m.ORDER_TYPE_BUY_STOP
            price = max(sl + rev_gap, float(tick.ask) + broker_gap)
            rev_sl = price - sl_dist
        price = round(price, spec.digits)
        rev_sl = round(rev_sl, spec.digits)
        if price <= 0:
            return

        # Drop any wrong-side leftover, then place or trail the reverse stop.
        for o in [x for x in pending if int(getattr(x, "type", -1)) != want_type]:
            item = self._cancel(spec, o)
            if item["ok"]:
                res.canceled += 1
            else:
                res.errors.append(f"cancel stray pending failed: {item['comment']}")
        existing = [x for x in pending if int(getattr(x, "type", -1)) == want_type]
        if not existing:
            item = self._place_pending(spec, kind, price, rev_sl, self.REVERSE_COMMENT)
            if item["ok"]:
                res.opened += 1
                res.events.append(f"reverse {kind} @ {price:.{spec.digits}f} (SAR vs {direction})")
            else:
                res.errors.append(f"reverse {kind} failed: {item['comment']}")
            return
        order = existing[0]
        cur = float(getattr(order, "price_open", 0.0) or 0.0)
        if abs(cur - price) < trail_step:  # throttle
            return
        item = self._modify_pending(spec, order, price, rev_sl)
        if item["ok"]:
            res.modified += 1
            res.events.append(f"reverse {kind} trail -> {price:.{spec.digits}f}")
        else:
            res.errors.append(f"reverse {kind} trail failed: {item['comment']}")

    # ------------------------------------------------------------------ #
    def _place_straddle(
        self, spec: SymbolSpec, df_m1: pd.DataFrame | None, res: BarbarCycleResult
    ) -> bool:
        if df_m1 is None or df_m1.empty or len(df_m1) < 2:
            res.blocked = "M1 data unavailable for straddle"
            return False
        lot = self._fit_lot(spec)
        if lot <= 0:
            res.blocked = "lot <= 0 after broker limits"
            return False
        tick = self.client.get_tick(spec.name)
        if tick is None:
            res.blocked = "tick unavailable"
            return False
        point = spec.point
        offset = float(self.cfg.offset_pts) * point
        sl_dist = float(self.cfg.sl_pts) * point
        broker_gap = max(spec.trade_stops_level * point, point)
        prev = df_m1.iloc[-2]  # last closed candle (High[1]/Low[1])
        high = float(prev["high"])
        low = float(prev["low"])
        buy_price = round(max(high + offset, float(tick.ask) + broker_gap), spec.digits)
        sell_price = round(min(low - offset, float(tick.bid) - broker_gap), spec.digits)
        buy_sl = round(buy_price - sl_dist, spec.digits)
        sell_sl = round(sell_price + sl_dist, spec.digits)

        buy = self._place_pending(spec, "BUY_STOP", buy_price, buy_sl, self.STRADDLE_COMMENT)
        sell = self._place_pending(spec, "SELL_STOP", sell_price, sell_sl, self.STRADDLE_COMMENT)
        if buy["ok"]:
            res.events.append(f"BUY STOP {lot} @ {buy_price:.{spec.digits}f} SL {buy_sl:.{spec.digits}f}")
        else:
            res.errors.append(f"BUY STOP failed: {buy['comment']}")
        if sell["ok"]:
            res.events.append(f"SELL STOP {lot} @ {sell_price:.{spec.digits}f} SL {sell_sl:.{spec.digits}f}")
        else:
            res.errors.append(f"SELL STOP failed: {sell['comment']}")

        # Robustness: never leave a half-done straddle resting.
        if buy["ok"] != sell["ok"]:
            lone = buy if buy["ok"] else sell
            if lone.get("ticket"):
                self._cancel_ticket(spec, int(lone["ticket"]))
                res.canceled += 1
            res.errors.append("partial straddle -> canceled lone leg")
            return False
        if buy["ok"] and sell["ok"]:
            res.opened += 2
            return True
        return False

    def _enforce_single(self, spec: SymbolSpec, res: BarbarCycleResult, positions: list[Any]) -> None:
        keep = self._newest(positions)
        for p in positions:
            if int(getattr(p, "ticket", 0)) == int(getattr(keep, "ticket", 0)):
                continue
            item = self._close_position(spec, p)
            if item["ok"]:
                res.closed += 1
                res.events.append(f"closed duplicate position ticket={getattr(p, 'ticket', '?')}")
            else:
                res.errors.append(
                    f"close duplicate ticket={getattr(p, 'ticket', '?')} failed: {item['comment']}"
                )

    # ------------------------------------------------------------------ #
    def _entry_filters(
        self, spec: SymbolSpec, execution_enabled: bool, autotrading_enabled: bool
    ) -> str:
        if not execution_enabled:
            return "StraddleM1 alert-only: would place straddle"
        if not autotrading_enabled:
            return "MT5 AutoTrading is OFF"
        if not self.client.is_market_open(spec.name):
            return "market closed or symbol disabled"
        tick = self.client.get_tick(spec.name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            return "tick unavailable"
        if float(self.cfg.max_spread_pts) > 0:
            spread_pts = (float(tick.ask) - float(tick.bid)) / spec.point
            if spread_pts > float(self.cfg.max_spread_pts):
                return f"spread {spread_pts:.0f}pts > max {self.cfg.max_spread_pts:.0f}pts"
        if self.cfg.use_time_filter and self._in_block_window(tick):
            return (
                f"time filter block {self.cfg.block_start_hour}-{self.cfg.block_end_hour} (server)"
            )
        if int(self.cfg.max_trades_per_day) > 0 and self.trades_today >= int(self.cfg.max_trades_per_day):
            return f"max_trades_per_day reached ({self.trades_today})"
        if float(self.cfg.max_daily_loss) > 0:
            loss = -self._realized_pnl_today()
            if loss >= float(self.cfg.max_daily_loss):
                return f"max_daily_loss reached (-{loss:.2f})"
        return ""

    def _server_hour(self, tick: Any) -> int:
        ts = int(getattr(tick, "time", 0) or 0)
        if ts <= 0:
            return datetime.now(timezone.utc).hour
        return datetime.fromtimestamp(ts, tz=timezone.utc).hour

    def _in_block_window(self, tick: Any) -> bool:
        h = self._server_hour(tick)
        start = int(self.cfg.block_start_hour) % 24
        end = int(self.cfg.block_end_hour) % 24
        if start == end:
            return False
        if start < end:
            return start <= h < end
        return h >= start or h < end  # wrap-around midnight

    def _roll_day(self, spec: SymbolSpec) -> None:
        tick = self.client.get_tick(spec.name)
        ts = int(getattr(tick, "time", 0) or 0) if tick else 0
        if ts > 0:
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self.trade_day:
            self.trade_day = day
            self.trades_today = 0

    def _realized_pnl_today(self) -> float:
        m = _require_mt5()
        try:
            now = datetime.now(timezone.utc)
            start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            deals = m.history_deals_get(start, now)
        except Exception:  # noqa: BLE001 - broker/history quirks must not block trading
            return 0.0
        if not deals:
            return 0.0
        total = 0.0
        for d in deals:
            if int(getattr(d, "magic", 0)) != self.cfg.magic_number:
                continue
            total += (
                float(getattr(d, "profit", 0.0))
                + float(getattr(d, "swap", 0.0))
                + float(getattr(d, "commission", 0.0))
            )
        return total

    def _reverse_timeout_seconds(self) -> float:
        bars = int(self.cfg.reverse_timeout_bars)
        if bars <= 0:
            return 0.0
        minutes = TIMEFRAME_MINUTES.get((self.cfg.timeframe or "M1").upper(), 1)
        return float(bars) * float(minutes) * 60.0

    # ------------------------------------------------------------------ #
    def _fit_lot(self, spec: SymbolSpec) -> float:
        lots = _round_to_step(float(self.cfg.lot), spec.volume_step)
        if lots <= 0:
            return 0.0
        lots = max(spec.volume_min, min(lots, spec.volume_max))
        return _round_to_step(lots, spec.volume_step)

    def _newest(self, positions: list[Any]) -> Any:
        return max(
            positions, key=lambda p: (int(getattr(p, "time", 0)), int(getattr(p, "ticket", 0)))
        )

    def _place_pending(
        self, spec: SymbolSpec, kind: str, price: float, sl: float, comment: str
    ) -> dict[str, Any]:
        m = _require_mt5()
        order_type = m.ORDER_TYPE_BUY_STOP if kind == "BUY_STOP" else m.ORDER_TYPE_SELL_STOP
        request = {
            "action": m.TRADE_ACTION_PENDING,
            "symbol": spec.name,
            "volume": float(self._fit_lot(spec)),
            "type": order_type,
            "price": round(float(price), spec.digits),
            "sl": round(float(sl), spec.digits) if sl else 0.0,
            "tp": 0.0,
            "deviation": int(self.cfg.deviation),
            "magic": int(self.cfg.magic_number),
            "comment": comment,
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": getattr(m, "ORDER_FILLING_RETURN", deduce_filling(spec)),
        }
        return _send_order_request(request, m, price=price, pending=True)

    def _modify_pending(
        self, spec: SymbolSpec, order: Any, price: float, sl: float
    ) -> dict[str, Any]:
        m = _require_mt5()
        ticket = int(getattr(order, "ticket", getattr(order, "order", 0)))
        request = {
            "action": m.TRADE_ACTION_MODIFY,
            "order": ticket,
            "symbol": spec.name,
            "price": round(float(price), spec.digits),
            "sl": round(float(sl), spec.digits) if sl else 0.0,
            "tp": 0.0,
            "type_time": m.ORDER_TIME_GTC,
            "magic": int(self.cfg.magic_number),
        }
        return _send_order_request(request, m, ticket=ticket, pending=True)

    def _modify_position_sl(self, spec: SymbolSpec, pos: Any, sl: float) -> dict[str, Any]:
        m = _require_mt5()
        ticket = int(getattr(pos, "ticket", 0))
        request = {
            "action": m.TRADE_ACTION_SLTP,
            "symbol": spec.name,
            "position": ticket,
            "sl": round(float(sl), spec.digits),
            "tp": 0.0,  # StraddleM1 never uses a take-profit
            "magic": int(self.cfg.magic_number),
        }
        return _send_order_request(request, m, ticket=ticket)

    def _cancel(self, spec: SymbolSpec, order: Any) -> dict[str, Any]:
        return self._cancel_ticket(spec, int(getattr(order, "ticket", getattr(order, "order", 0))))

    def _cancel_ticket(self, spec: SymbolSpec, ticket: int) -> dict[str, Any]:
        m = _require_mt5()
        request = {
            "action": m.TRADE_ACTION_REMOVE,
            "order": int(ticket),
            "symbol": spec.name,
            "magic": int(self.cfg.magic_number),
            "comment": "stradm1_cancel",
        }
        return _send_order_request(request, m, ticket=int(ticket))

    def _cancel_all(self, spec: SymbolSpec, orders: list[Any], res: BarbarCycleResult) -> None:
        for o in orders:
            item = self._cancel(spec, o)
            if item["ok"]:
                res.canceled += 1
            else:
                res.errors.append(f"cancel pending failed: {item['comment']}")

    def _close_position(self, spec: SymbolSpec, pos: Any) -> dict[str, Any]:
        m = _require_mt5()
        is_buy = int(pos.type) == m.POSITION_TYPE_BUY
        last = _make_order_result(False, -1, "order not attempted", ticket=int(pos.ticket))
        for _ in range(3):
            tick = self.client.get_tick(spec.name)
            if tick is None:
                return _make_order_result(False, -1, "tick unavailable")
            price = float(tick.bid if is_buy else tick.ask)
            request = {
                "action": m.TRADE_ACTION_DEAL,
                "symbol": spec.name,
                "volume": float(pos.volume),
                "type": m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY,
                "position": int(pos.ticket),
                "price": price,
                "deviation": int(self.cfg.deviation),
                "magic": int(self.cfg.magic_number),
                "comment": "stradm1_close",
                "type_time": m.ORDER_TIME_GTC,
                "type_filling": deduce_filling(spec),
            }
            last = _send_order_request(request, m, ticket=int(pos.ticket), price=price)
            if last["ok"] or last["retcode"] != getattr(m, "TRADE_RETCODE_REQUOTE", -999):
                return last
        return last

    def close_all(self, spec: SymbolSpec) -> BarbarCycleResult:
        res = BarbarCycleResult()
        for p in self.positions(spec.name):
            item = self._close_position(spec, p)
            if item["ok"]:
                res.closed += 1
                res.events.append(f"closed ticket={item['ticket']}")
            else:
                res.errors.append(f"close failed: {item['comment']}")
        self._cancel_all(spec, self.pending_orders(spec.name), res)
        self.reverse_started_at = 0.0
        return res

    # ------------------------------------------------------------------ #
    def status_text(self, spec: SymbolSpec) -> str:
        m = _require_mt5()
        positions = self.positions(spec.name)
        pending = self.pending_orders(spec.name)
        state = self._state(positions, pending)
        max_tr = f"/{self.cfg.max_trades_per_day}" if int(self.cfg.max_trades_per_day) > 0 else ""
        lines = [
            "STRADDLE-M1 STATUS",
            f"symbol: {spec.name} | magic: {self.cfg.magic_number}",
            f"state: {state}",
            f"lot: {self.cfg.lot} | offset {self.cfg.offset_pts}p | SL {self.cfg.sl_pts}p (NO TP)",
            (
                f"trail dist {self.cfg.trail_dist_pts}p / step {self.cfg.trail_step_pts}p | "
                f"reverse gap {self.cfg.rev_gap_pts}p | timeout {self.cfg.reverse_timeout_bars} bar"
            ),
            (
                f"spread max: {self.cfg.max_spread_pts}p | time filter: "
                f"{'ON' if self.cfg.use_time_filter else 'OFF'} "
                f"({self.cfg.block_start_hour}-{self.cfg.block_end_hour} server)"
            ),
            f"trades today: {self.trades_today}{max_tr}",
            f"positions: {len(positions)} | pending: {len(pending)}",
        ]
        for p in positions:
            side = "BUY" if int(p.type) == m.POSITION_TYPE_BUY else "SELL"
            lines.append(
                f"#{getattr(p, 'ticket', '?')} {side} {float(p.volume):.2f} "
                f"@ {float(p.price_open):.{spec.digits}f} "
                f"SL={float(getattr(p, 'sl', 0.0) or 0.0):.{spec.digits}f} "
                f"P/L={float(p.profit):.2f}"
            )
        for o in pending:
            price = float(getattr(o, "price_open", getattr(o, "price_current", 0.0)))
            lines.append(
                f"pending #{getattr(o, 'ticket', '?')} type={getattr(o, 'type', '?')} "
                f"@ {price:.{spec.digits}f} SL={float(getattr(o, 'sl', 0.0) or 0.0):.{spec.digits}f}"
            )
        return "\n".join(lines)
