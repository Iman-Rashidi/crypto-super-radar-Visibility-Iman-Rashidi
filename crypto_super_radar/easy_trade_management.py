"""Easy-mode breakout/retest and paper-position lifecycle manager.

The module never submits exchange orders. All updates require completed candles.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
import numpy as np
from typing import Any, Optional

from .database import connect_database


def _num(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def evaluate_breakout_retest(
    candles: Any,
    level: Any,
    side: str,
    *,
    last_candle_closed: bool = True,
    buffer_pct: float = 0.15,
    retest_tolerance_pct: float = 0.50,
    minimum_volume_ratio: float = 1.20,
    max_retest_bars: int = 8,
) -> dict[str, Any]:
    """Classify the latest horizontal level break and its retest without look-ahead."""
    import pandas as pd

    direction = str(side).upper()
    required = {"open", "high", "low", "close", "volume"}
    if direction not in {"LONG", "SHORT"}:
        return {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "Direction must be LONG or SHORT."}
    if not isinstance(candles, pd.DataFrame) or not required.issubset(candles.columns):
        return {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "Completed OHLCV history is required."}
    px = _num(level)
    if px is None or px <= 0:
        return {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "A positive support/resistance level is required."}
    if len(candles) < 24:
        return {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "At least 24 candles are required for volume context."}
    frame = candles.copy()
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        return {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "Unique chronological candle timestamps are required."}
    if not last_candle_closed:
        frame = frame.iloc[:-1].copy()
    if len(frame) < 24:
        return {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "Too few completed candles remain."}
    for key in required:
        frame[key] = pd.to_numeric(frame[key], errors="coerce")
    vals = frame[list(required)].to_numpy(dtype=float)
    if not np.isfinite(vals).all() or (frame[["open", "high", "low", "close"]] <= 0).any().any() or (frame["volume"] < 0).any():
        return {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "OHLCV values are malformed."}
    if ((frame.high < frame[["open", "low", "close"]].max(axis=1)) |
        (frame.low > frame[["open", "high", "close"]].min(axis=1))).any():
        return {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "OHLC boundaries are inconsistent."}
    if not (0 <= buffer_pct <= 5 and 0 <= retest_tolerance_pct <= 10 and
            0 < minimum_volume_ratio <= 20 and 1 <= max_retest_bars <= 50):
        return {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "Breakout parameters are outside valid bounds."}

    close = frame.close.to_numpy(float)
    high = frame.high.to_numpy(float)
    low = frame.low.to_numpy(float)
    volume = frame.volume.to_numpy(float)
    threshold = px * buffer_pct / 100.0
    break_i = None
    for i in range(20, len(frame)):
        prior = close[i - 1]
        crossed = (prior <= px + threshold and close[i] > px + threshold) if direction == "LONG" else (
            prior >= px - threshold and close[i] < px - threshold
        )
        if crossed:
            break_i = i

    if break_i is None:
        return {"status": "KNOWN", "state": "NO_SETUP", "side": direction,
                "reference_level": px, "used_as_hard_gate": False,
                "reason": "No fresh close-through breakout of the supplied level was found."}

    baseline = float(np.mean(volume[max(0, break_i - 20):break_i]))
    volume_ratio = float(volume[break_i] / baseline) if baseline > 0 else None
    volume_confirmed = volume_ratio is not None and volume_ratio >= minimum_volume_ratio
    breakout_state = "BREAKOUT_CONFIRMED" if volume_confirmed else "BREAKOUT_CANDIDATE"
    retest_index = None
    false_index = None
    confirm_index = None
    end = min(len(frame), break_i + max_retest_bars + 1)
    tolerance = px * retest_tolerance_pct / 100.0
    for i in range(break_i + 1, end):
        if direction == "LONG":
            if close[i] < px - threshold:
                false_index = i
                break
            touched = low[i] <= px + tolerance and close[i] >= px - threshold
            confirmed = retest_index is not None and close[i] > high[retest_index] and close[i] > px + threshold
        else:
            if close[i] > px + threshold:
                false_index = i
                break
            touched = high[i] >= px - tolerance and close[i] <= px + threshold
            confirmed = retest_index is not None and close[i] < low[retest_index] and close[i] < px - threshold
        if retest_index is None and touched:
            retest_index = i
        elif confirmed:
            confirm_index = i
            break

    if false_index is not None:
        state = "FALSE_BREAKOUT"
        status = "WARNING"
        reason = "Price closed back through the broken level before a valid retest confirmation."
    elif confirm_index is not None:
        state = "RETEST_CONFIRMED"
        status = "KNOWN"
        reason = "A completed retest candle held the level and a later close confirmed continuation."
    elif retest_index is not None:
        state = "RETEST_TOUCHED"
        status = "KNOWN"
        reason = "The broken level was retested; a later completed close is still required."
    elif len(frame) - 1 - break_i >= max_retest_bars:
        state = "RETEST_EXPIRED"
        status = "KNOWN"
        reason = "No valid retest occurred within the configured observation window."
    else:
        state = "RETEST_PENDING" if volume_confirmed else "BREAKOUT_CANDIDATE"
        status = "KNOWN"
        reason = "Breakout remains provisional until volume/price and retest context improve."

    idx = lambda i: frame.index[i].isoformat() if i is not None else None
    return {
        "status": status, "state": state, "side": direction, "reference_level": px,
        "breakout_time": idx(break_i), "breakout_close": float(close[break_i]),
        "breakout_volume_ratio": volume_ratio, "volume_confirmed": volume_confirmed,
        "retest_time": idx(retest_index), "confirmation_time": idx(confirm_index),
        "false_breakout_time": idx(false_index), "max_retest_bars": max_retest_bars,
        "used_as_hard_gate": False, "reason": reason,
    }


def stop_execution_guidance(stop_mode: str) -> dict[str, Any]:
    mode = str(stop_mode).upper()
    if mode not in {"HARD", "MENTAL"}:
        return {"status": "UNKNOWN", "reason": "Stop mode must be HARD or MENTAL."}
    if mode == "HARD":
        return {"status": "KNOWN", "mode": mode, "automatic_trigger_expected": True,
                "guidance": "A hard stop is a broker/exchange instruction; gaps and slippage can still exceed the planned loss."}
    return {"status": "WARNING", "mode": mode, "automatic_trigger_expected": False,
            "guidance": "A mental stop requires manual action and has execution-delay and emotional-discipline risk; it is not equivalent to a hard stop."}


class EasyPositionManager:
    """SQLite-backed paper position journal and completed-candle stop manager."""

    def __init__(
        self, db: Any = None, *, paper_equity: float = 10_000.0,
        risk_per_trade_pct: float = 0.50, max_position_notional_pct: float = 10.0,
        fee_bps_per_side: float = 8.0, slippage_bps_per_side: float = 10.0,
    ):
        self.db = db if db is not None else connect_database()
        self.paper_equity = _num(paper_equity)
        self.risk_per_trade_pct = _num(risk_per_trade_pct)
        self.max_position_notional_pct = _num(max_position_notional_pct)
        self.fee_bps_per_side = _num(fee_bps_per_side)
        self.slippage_bps_per_side = _num(slippage_bps_per_side)
        if (self.paper_equity is None or self.paper_equity <= 0
            or self.risk_per_trade_pct is None or not 0 < self.risk_per_trade_pct <= 2
            or self.max_position_notional_pct is None or not 0 < self.max_position_notional_pct <= 25
            or self.fee_bps_per_side is None or self.fee_bps_per_side < 0
            or self.slippage_bps_per_side is None or self.slippage_bps_per_side < 0):
            raise ValueError("Easy paper account and risk limits are invalid.")
        self.lock = threading.RLock()
        with self.lock, self.db:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS easy_positions (
                    position_id TEXT PRIMARY KEY, asset TEXT NOT NULL, side TEXT NOT NULL,
                    quantity REAL NOT NULL, entry_price REAL NOT NULL, initial_stop REAL NOT NULL,
                    current_stop REAL NOT NULL, take_profit REAL NOT NULL, stop_mode TEXT NOT NULL,
                    opened_at REAL NOT NULL, bars_held INTEGER NOT NULL DEFAULT 0,
                    peak_price REAL NOT NULL, trough_price REAL NOT NULL,
                    max_holding_bars INTEGER NOT NULL DEFAULT 100, status TEXT NOT NULL DEFAULT 'OPEN'
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS easy_trade_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT NOT NULL,
                    event_time REAL NOT NULL, event_type TEXT NOT NULL, reference_price REAL,
                    details_json TEXT NOT NULL
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS easy_trade_journal (
                    journal_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT NOT NULL,
                    asset TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL,
                    entry_price REAL NOT NULL, exit_price REAL NOT NULL, initial_stop REAL NOT NULL,
                    final_stop REAL NOT NULL, take_profit REAL NOT NULL, gross_pnl REAL NOT NULL,
                    opened_at REAL NOT NULL, closed_at REAL NOT NULL, exit_reason TEXT NOT NULL,
                    stop_mode TEXT NOT NULL, bars_held INTEGER NOT NULL
                )
            """)

    def _event(self, position_id: str, event_type: str, price: Optional[float], details: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO easy_trade_events(position_id,event_time,event_type,reference_price,details_json) VALUES(?,?,?,?,?)",
            (position_id, time.time(), event_type, price, json.dumps(details, ensure_ascii=False, default=str)),
        )

    def open_position(
        self, asset: str, side: str, quantity: float, entry_price: float, stop_loss: float,
        take_profit: float, *, stop_mode: str = "HARD", max_holding_bars: int = 100,
    ) -> dict[str, Any]:
        direction, mode = str(side).upper(), str(stop_mode).upper()
        q, entry, stop, target = map(_num, (quantity, entry_price, stop_loss, take_profit))
        if not isinstance(asset, str) or not asset.strip() or direction not in {"LONG", "SHORT"} or mode not in {"HARD", "MENTAL"}:
            return {"status": "UNKNOWN", "reason": "Asset, side, or stop mode is invalid."}
        if any(x is None or x <= 0 for x in (q, entry, stop, target)) or max_holding_bars < 1:
            return {"status": "UNKNOWN", "reason": "Quantity, entry, stop, target and holding bars must be positive."}
        if direction == "LONG" and not stop < entry < target:
            return {"status": "UNKNOWN", "reason": "LONG requires stop < entry < target."}
        if direction == "SHORT" and not target < entry < stop:
            return {"status": "UNKNOWN", "reason": "SHORT requires target < entry < stop."}
        loss_per_unit = abs(entry - stop) + entry * (
            2.0 * (self.fee_bps_per_side + self.slippage_bps_per_side) / 10000.0
        )
        modeled_loss = q * loss_per_unit
        notional = q * entry
        risk_budget = self.paper_equity * self.risk_per_trade_pct / 100.0
        notional_cap = self.paper_equity * self.max_position_notional_pct / 100.0
        if modeled_loss > risk_budget + 1e-9 or notional > notional_cap + 1e-9:
            max_qty_risk = risk_budget / loss_per_unit
            max_qty_notional = notional_cap / entry
            return {
                "status": "BLOCKED",
                "reason": "Paper position exceeds the configured stop-risk budget or notional cap.",
                "modeled_loss": modeled_loss, "risk_budget": risk_budget,
                "notional": notional, "notional_cap": notional_cap,
                "maximum_allowed_quantity": min(max_qty_risk, max_qty_notional),
                "orders_submitted": False,
            }
        position_id = uuid.uuid4().hex
        now = time.time()
        with self.lock, self.db:
            existing = self.db.execute("SELECT 1 FROM easy_positions WHERE status='OPEN' LIMIT 1").fetchone()
            if existing:
                return {"status": "BLOCKED", "reason": "An Easy paper position is already open."}
            self.db.execute("""
                INSERT INTO easy_positions(position_id,asset,side,quantity,entry_price,initial_stop,current_stop,
                    take_profit,stop_mode,opened_at,bars_held,peak_price,trough_price,max_holding_bars,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,'OPEN')
            """, (position_id, asset.strip(), direction, q, entry, stop, stop, target,
                  mode, now, entry, entry, int(max_holding_bars)))
            self._event(position_id, "OPENED", entry, {
                "stop_mode": mode, "paper_only": True, "risk_budget": risk_budget,
                "modeled_loss_including_estimated_costs": modeled_loss,
                "position_notional": notional,
            })
        return {
            "status": "OPENED", "position_id": position_id,
            "stop_guidance": stop_execution_guidance(mode),
            "risk_budget": risk_budget,
            "modeled_loss_including_estimated_costs": modeled_loss,
            "position_notional": notional,
            "orders_submitted": False,
        }

    def get_position(self, position_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM easy_positions WHERE position_id=?", (position_id,)).fetchone()
        return dict(row) if row else None

    def open_positions(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM easy_positions WHERE status='OPEN' ORDER BY opened_at").fetchall()
        return [dict(row) for row in rows]

    def update_position(
        self, position_id: str, candle: dict[str, Any], *, atr_value: Optional[float] = None,
        trail_atr_multiple: float = 2.0, candle_closed: bool = True,
    ) -> dict[str, Any]:
        if not candle_closed:
            return {"status": "PROVISIONAL", "reason": "Open candle ignored; no position state changed."}
        if not isinstance(candle, dict):
            return {"status": "UNKNOWN", "reason": "A completed OHLC candle is required."}
        o, h, l, c = (_num(candle.get(k)) for k in ("open", "high", "low", "close"))
        if any(x is None or x <= 0 for x in (o, h, l, c)) or h < max(o, l, c) or l > min(o, h, c):
            return {"status": "UNKNOWN", "reason": "Completed candle OHLC is malformed."}
        atr = _num(atr_value) if atr_value is not None else None
        mult = _num(trail_atr_multiple)
        if mult is None or mult <= 0 or (atr_value is not None and (atr is None or atr <= 0)):
            return {"status": "UNKNOWN", "reason": "Trailing ATR settings are invalid."}
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM easy_positions WHERE position_id=? AND status='OPEN'", (position_id,)).fetchone()
            if not row:
                return {"status": "NOT_FOUND", "reason": "Open position was not found."}
            pos = dict(row)
            side = pos["side"]
            qty, entry, old_stop, target = (float(pos[k]) for k in ("quantity", "entry_price", "current_stop", "take_profit"))
            stop_hit = l <= old_stop if side == "LONG" else h >= old_stop
            target_hit = h >= target if side == "LONG" else l <= target
            bars = int(pos["bars_held"]) + 1
            reason = None
            exit_price = None
            if stop_hit and pos["stop_mode"] == "HARD":
                reason, exit_price = "STOP_LOSS", old_stop
            elif stop_hit and pos["stop_mode"] == "MENTAL":
                self._event(position_id, "MENTAL_STOP_ALERT", old_stop, {"bar_high": h, "bar_low": l, "manual_action_required": True})
            elif target_hit:
                reason, exit_price = "TAKE_PROFIT", target
            elif bars >= int(pos["max_holding_bars"]):
                reason, exit_price = "TIME_EXIT", c

            if reason:
                pnl = (exit_price - entry) * qty * (1.0 if side == "LONG" else -1.0)
                self.db.execute("""
                    INSERT INTO easy_trade_journal(position_id,asset,side,quantity,entry_price,exit_price,
                        initial_stop,final_stop,take_profit,gross_pnl,opened_at,closed_at,exit_reason,stop_mode,bars_held)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (position_id, pos["asset"], side, qty, entry, exit_price,
                      pos["initial_stop"], old_stop, target, pnl, pos["opened_at"],
                      time.time(), reason, pos["stop_mode"], bars))
                self._event(position_id, "CLOSED", exit_price, {"reason": reason, "gross_pnl": pnl})
                self.db.execute("UPDATE easy_positions SET status='CLOSED',bars_held=? WHERE position_id=?", (bars, position_id))
                return {"status": "CLOSED", "reason": reason, "exit_price": exit_price, "gross_pnl": pnl,
                        "stop_was_hit": stop_hit, "target_was_hit": target_hit,
                        "conservative_intrabar_rule": "STOP_FIRST" if stop_hit and target_hit else "NOT_AMBIGUOUS"}

            peak = max(float(pos["peak_price"]), h)
            trough = min(float(pos["trough_price"]), l)
            new_stop = old_stop
            if atr is not None:
                candidate = c - atr * mult if side == "LONG" else c + atr * mult
                if side == "LONG" and 0 < candidate < c:
                    new_stop = max(old_stop, candidate)
                elif side == "SHORT" and candidate > c:
                    new_stop = min(old_stop, candidate)
            self.db.execute("""
                UPDATE easy_positions SET bars_held=?,peak_price=?,trough_price=?,current_stop=?
                WHERE position_id=?
            """, (bars, peak, trough, new_stop, position_id))
            if new_stop != old_stop:
                self._event(position_id, "TRAILING_STOP_RATCHETED", new_stop, {"previous_stop": old_stop, "atr": atr, "multiple": mult})
            return {"status": "OPEN", "position_id": position_id, "bars_held": bars,
                    "current_stop": new_stop, "previous_stop": old_stop,
                    "peak_price": peak, "trough_price": trough,
                    "mental_stop_alert": stop_hit and pos["stop_mode"] == "MENTAL",
                    "stop_can_only_tighten": True}

    def manual_close(self, position_id: str, exit_price: float, reason: str = "MANUAL_CLOSE") -> dict[str, Any]:
        price = _num(exit_price)
        if price is None or price <= 0:
            return {"status": "UNKNOWN", "reason": "A positive manual exit price is required."}
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM easy_positions WHERE position_id=? AND status='OPEN'", (position_id,)).fetchone()
            if not row:
                return {"status": "NOT_FOUND", "reason": "Open position was not found."}
            pos = dict(row)
            side = pos["side"]
            pnl = (price - float(pos["entry_price"])) * float(pos["quantity"]) * (1.0 if side == "LONG" else -1.0)
            self.db.execute("""
                INSERT INTO easy_trade_journal(position_id,asset,side,quantity,entry_price,exit_price,
                    initial_stop,final_stop,take_profit,gross_pnl,opened_at,closed_at,exit_reason,stop_mode,bars_held)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (position_id, pos["asset"], side, pos["quantity"], pos["entry_price"], price,
                  pos["initial_stop"], pos["current_stop"], pos["take_profit"], pnl,
                  pos["opened_at"], time.time(), str(reason)[:80], pos["stop_mode"], pos["bars_held"]))
            self._event(position_id, "CLOSED", price, {"reason": reason, "gross_pnl": pnl, "manual": True})
            self.db.execute("UPDATE easy_positions SET status='CLOSED' WHERE position_id=?", (position_id,))
        return {"status": "CLOSED", "reason": reason, "exit_price": price, "gross_pnl": pnl}

    def journal(self, limit: int = 100) -> list[dict[str, Any]]:
        n = max(1, min(int(limit), 500))
        with self.lock:
            rows = self.db.execute("SELECT * FROM easy_trade_journal ORDER BY closed_at DESC LIMIT ?", (n,)).fetchall()
        return [dict(row) for row in rows]

    def events(self, position_id: str, limit: int = 100) -> list[dict[str, Any]]:
        n = max(1, min(int(limit), 500))
        with self.lock:
            rows = self.db.execute("SELECT * FROM easy_trade_events WHERE position_id=? ORDER BY event_id DESC LIMIT ?", (position_id, n)).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json"))
            except (TypeError, ValueError):
                item["details"] = {}
            results.append(item)
        return results
