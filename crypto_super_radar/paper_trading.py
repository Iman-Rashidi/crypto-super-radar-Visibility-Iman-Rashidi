"""Free, SQLite-backed paper trading bot for validated spot LONG signals.

This module never calls an exchange trading endpoint and has no live-order mode.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .models import AssetSpec


PAPER_ASSETS = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _status(value: Any) -> str:
    return str(getattr(value, "value", value)).upper()


class PaperLedger:
    """Minimal paper account and immutable closed-trade journal in SQLite."""

    def __init__(self, path: Optional[str] = None, initial_equity_usd: float = 10_000.0):
        if not math.isfinite(initial_equity_usd) or initial_equity_usd <= 0:
            raise ValueError("Initial paper equity must be a positive finite number.")
        self.path = path or os.getenv("CSR_DB_PATH", "data/crypto_super_radar.sqlite3")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_equity REAL NOT NULL,
                    cash_balance REAL NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS paper_positions (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    asset TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    entry_fee REAL NOT NULL,
                    opened_at INTEGER NOT NULL,
                    signal_bar TEXT NOT NULL,
                    confirmation_score REAL NOT NULL,
                    risk_budget REAL NOT NULL,
                    mark_price REAL NOT NULL,
                    initial_stop_loss REAL NOT NULL DEFAULT 0,
                    max_holding_days INTEGER NOT NULL DEFAULT 14,
                    peak_price REAL NOT NULL DEFAULT 0
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    entry_fee REAL NOT NULL,
                    exit_fee REAL NOT NULL,
                    gross_pnl REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    opened_at INTEGER NOT NULL,
                    closed_at INTEGER NOT NULL,
                    signal_bar TEXT NOT NULL,
                    confirmation_score REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    UNIQUE(asset, signal_bar)
                )
            """)
            position_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(paper_positions)")
            }
            for name, declaration in (
                ("initial_stop_loss", "REAL NOT NULL DEFAULT 0"),
                ("max_holding_days", "INTEGER NOT NULL DEFAULT 14"),
                ("peak_price", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in position_columns:
                    self._db.execute(
                        f"ALTER TABLE paper_positions ADD COLUMN {name} {declaration}"
                    )
            self._db.execute(
                "UPDATE paper_positions SET initial_stop_loss=stop_loss WHERE initial_stop_loss<=0"
            )
            self._db.execute(
                "UPDATE paper_positions SET peak_price=entry_price WHERE peak_price<=0"
            )
            self._db.execute(
                "INSERT OR IGNORE INTO paper_account(id, initial_equity, cash_balance, updated_at) VALUES (1, ?, ?, ?)",
                (float(initial_equity_usd), float(initial_equity_usd), int(time.time())),
            )

    def position(self) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM paper_positions WHERE id=1").fetchone()
        return dict(row) if row else None

    def has_traded_signal_bar(self, asset: str, signal_bar: str) -> bool:
        with self._lock:
            closed = self._db.execute(
                "SELECT 1 FROM paper_trades WHERE asset=? AND signal_bar=? LIMIT 1",
                (asset, signal_bar),
            ).fetchone()
            opened = self._db.execute(
                "SELECT 1 FROM paper_positions WHERE asset=? AND signal_bar=? LIMIT 1",
                (asset, signal_bar),
            ).fetchone()
        return bool(closed or opened)

    def open_position(self, position: dict[str, Any], debit: float) -> bool:
        with self._lock, self._db:
            account = self._db.execute(
                "SELECT cash_balance FROM paper_account WHERE id=1"
            ).fetchone()
            active = self._db.execute("SELECT 1 FROM paper_positions LIMIT 1").fetchone()
            duplicate = self._db.execute(
                "SELECT 1 FROM paper_trades WHERE asset=? AND signal_bar=? LIMIT 1",
                (position["asset"], position["signal_bar"]),
            ).fetchone()
            if active or duplicate or account is None or debit > float(account["cash_balance"]):
                return False
            self._db.execute("""
                INSERT INTO paper_positions
                (id, asset, quantity, entry_price, stop_loss, take_profit, entry_fee,
                 opened_at, signal_bar, confirmation_score, risk_budget, mark_price,
                 initial_stop_loss, max_holding_days, peak_price)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position["asset"], position["quantity"], position["entry_price"],
                position["stop_loss"], position["take_profit"], position["entry_fee"],
                position["opened_at"], position["signal_bar"],
                position["confirmation_score"], position["risk_budget"],
                position["entry_price"], position["initial_stop_loss"],
                position["max_holding_days"], position["entry_price"],
            ))
            self._db.execute(
                "UPDATE paper_account SET cash_balance=cash_balance-?, updated_at=? WHERE id=1",
                (debit, int(time.time())),
            )
        return True

    def update_management(self, price: float, stop_loss: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE paper_positions
                SET mark_price=?, peak_price=MAX(peak_price, ?),
                    stop_loss=MAX(stop_loss, ?)
                WHERE id=1""",
                (float(price), float(price), float(stop_loss)),
            )

    def risk_controls(
        self, *, asset: str, daily_loss_limit_pct: float, max_drawdown_pct: float,
        max_consecutive_losses: int, cooldown_hours_after_loss: float,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        now = int(time.time()) if now is None else int(now)
        day_start = now // 86_400 * 86_400
        state = self.state()
        initial = float(state["account"]["initial_equity_usd"])
        equity = float(state["account"]["equity_usd"])
        with self._lock:
            daily_net = float(self._db.execute(
                "SELECT COALESCE(SUM(net_pnl), 0.0) FROM paper_trades WHERE closed_at>=?",
                (day_start,),
            ).fetchone()[0])
            recent = self._db.execute(
                "SELECT net_pnl FROM paper_trades ORDER BY closed_at DESC, id DESC LIMIT ?",
                (int(max_consecutive_losses),),
            ).fetchall()
            history = self._db.execute(
                "SELECT net_pnl FROM paper_trades ORDER BY closed_at ASC, id ASC"
            ).fetchall()
            last_asset = self._db.execute(
                "SELECT net_pnl, closed_at FROM paper_trades WHERE asset=? "
                "ORDER BY closed_at DESC, id DESC LIMIT 1",
                (asset,),
            ).fetchone()
        consecutive = 0
        for row in recent:
            if float(row["net_pnl"]) >= 0:
                break
            consecutive += 1
        daily_loss_pct = max(0.0, -daily_net / initial * 100.0) if initial > 0 else 0.0
        running_equity = initial
        peak_equity = initial
        max_historical_drawdown = 0.0
        for row in history:
            running_equity += float(row["net_pnl"])
            peak_equity = max(peak_equity, running_equity)
            if peak_equity > 0:
                max_historical_drawdown = max(
                    max_historical_drawdown,
                    (peak_equity - running_equity) / peak_equity * 100.0,
                )
        peak_equity = max(peak_equity, equity)
        current_drawdown = (
            (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 100.0
        )
        drawdown_pct = max(max_historical_drawdown, current_drawdown)
        cooldown_remaining = 0.0
        if last_asset and float(last_asset["net_pnl"]) < 0:
            cooldown_remaining = max(
                0.0, cooldown_hours_after_loss * 3600.0 - (now - int(last_asset["closed_at"]))
            ) / 3600.0
        reasons = []
        if daily_loss_pct >= daily_loss_limit_pct * 100.0:
            reasons.append("DAILY_LOSS_LIMIT")
        if drawdown_pct >= max_drawdown_pct * 100.0:
            reasons.append("MAX_EQUITY_DRAWDOWN")
        if consecutive >= max_consecutive_losses:
            reasons.append("CONSECUTIVE_LOSSES")
        if cooldown_remaining > 0:
            reasons.append("POST_LOSS_COOLDOWN")
        return {
            "entry_allowed": not reasons,
            "halt_reasons": reasons,
            "daily_realized_loss_pct": round(daily_loss_pct, 4),
            "max_equity_drawdown_pct": round(drawdown_pct, 4),
            "consecutive_losses": consecutive,
            "cooldown_hours_remaining": round(cooldown_remaining, 2),
            "limits": {
                "daily_loss_pct": daily_loss_limit_pct * 100.0,
                "max_drawdown_pct": max_drawdown_pct * 100.0,
                "max_consecutive_losses": max_consecutive_losses,
                "cooldown_hours_after_loss": cooldown_hours_after_loss,
            },
        }

    def close_position(self, exit_price: float, exit_fee_rate: float, reason: str) -> Optional[dict[str, Any]]:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM paper_positions WHERE id=1").fetchone()
            if row is None:
                return None
            p = dict(row)
            exit_fee = p["quantity"] * exit_price * exit_fee_rate
            gross = p["quantity"] * (exit_price - p["entry_price"])
            net = gross - p["entry_fee"] - exit_fee
            self._db.execute(
                "UPDATE paper_account SET cash_balance=cash_balance+?, updated_at=? WHERE id=1",
                (p["quantity"] * exit_price - exit_fee, int(time.time())),
            )
            self._db.execute("""
                INSERT INTO paper_trades
                (asset, side, quantity, entry_price, exit_price, stop_loss, take_profit,
                 entry_fee, exit_fee, gross_pnl, net_pnl, opened_at, closed_at,
                 signal_bar, confirmation_score, exit_reason)
                VALUES (?, 'LONG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["asset"], p["quantity"], p["entry_price"], exit_price,
                p["stop_loss"], p["take_profit"], p["entry_fee"], exit_fee,
                gross, net, p["opened_at"], int(time.time()), p["signal_bar"],
                p["confirmation_score"], reason,
            ))
            self._db.execute("DELETE FROM paper_positions WHERE id=1")
        return {"asset": p["asset"], "gross_pnl_usd": gross, "net_pnl_usd": net,
                "exit_price": exit_price, "exit_reason": reason}

    def state(self) -> dict[str, Any]:
        with self._lock:
            account = self._db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            position = self._db.execute("SELECT * FROM paper_positions WHERE id=1").fetchone()
            trades = self._db.execute(
                "SELECT * FROM paper_trades ORDER BY closed_at DESC, id DESC LIMIT 20"
            ).fetchall()
            realized_total = self._db.execute(
                "SELECT COALESCE(SUM(net_pnl), 0.0) AS total FROM paper_trades"
            ).fetchone()["total"]
        cash = float(account["cash_balance"])
        initial = float(account["initial_equity"])
        open_position = dict(position) if position else None
        unrealized = 0.0
        equity = cash
        if open_position:
            mark = float(open_position["mark_price"])
            qty = float(open_position["quantity"])
            equity += qty * mark
            unrealized = (qty * (mark - float(open_position["entry_price"]))
                      - float(open_position["entry_fee"]) - qty * mark * 0.0008)
        realized = float(realized_total)
        return {
            "mode": "PAPER_ONLY",
            "live_orders_enabled": False,
            "orders_submitted": False,
            "account": {
                "initial_equity_usd": initial,
                "cash_balance_usd": cash,
                "equity_usd": equity,
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": unrealized,
                "total_return_pct": (equity / initial - 1.0) * 100.0,
            },
            "open_position": open_position,
            "recent_trades": [dict(row) for row in trades],
            "storage": "LOCAL_SQLITE",
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()


class PaperTradingBot:
    """One-cycle spot paper trader; requires every existing signal gate to PASS."""

    def __init__(
        self,
        engine: Any,
        *,
        ledger: Optional[PaperLedger] = None,
        risk_per_trade_pct: float = 0.005,
        max_position_notional_pct: float = 0.10,
        max_risk_score: float = 45.0,
        min_confirmation_score: float = 75.0,
        fee_bps_per_side: float = 8.0,
        slippage_bps_per_side: float = 10.0,
        daily_loss_limit_pct: float = 0.015,
        max_drawdown_pct: float = 0.10,
        max_consecutive_losses: int = 3,
        cooldown_hours_after_loss: float = 24.0,
        min_net_reward_risk: float = 1.8,
    ):
        self.engine = engine
        self.ledger = ledger or PaperLedger()
        values = (risk_per_trade_pct, max_position_notional_pct, max_risk_score,
                  min_confirmation_score, fee_bps_per_side, slippage_bps_per_side,
                  daily_loss_limit_pct, max_drawdown_pct, cooldown_hours_after_loss,
                  min_net_reward_risk)
        if not all(math.isfinite(float(x)) for x in values):
            raise ValueError("Paper trading configuration must be finite.")
        if not 0 < risk_per_trade_pct <= 0.02:
            raise ValueError("Per-trade risk must be greater than zero and at most 2%.")
        if not 0 < max_position_notional_pct <= 0.25:
            raise ValueError("Position notional cap must be greater than zero and at most 25%.")
        if fee_bps_per_side < 0 or slippage_bps_per_side < 0:
            raise ValueError("Fee and slippage assumptions cannot be negative.")
        if not 0 < daily_loss_limit_pct <= 0.10 or not 0 < max_drawdown_pct <= 0.50:
            raise ValueError("Daily loss and max drawdown limits must be conservative positive fractions.")
        if not isinstance(max_consecutive_losses, int) or max_consecutive_losses <= 0:
            raise ValueError("Maximum consecutive losses must be a positive integer.")
        if cooldown_hours_after_loss < 0 or min_net_reward_risk < 1.0:
            raise ValueError("Cooldown cannot be negative and net reward/risk must be at least 1.");
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_notional_pct = max_position_notional_pct
        self.max_risk_score = max_risk_score
        self.min_confirmation_score = min_confirmation_score
        self.fee_rate = fee_bps_per_side / 10_000.0
        self.slippage_rate = slippage_bps_per_side / 10_000.0
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.cooldown_hours_after_loss = cooldown_hours_after_loss
        self.min_net_reward_risk = min_net_reward_risk

    def state(self) -> dict[str, Any]:
        state = self.ledger.state()
        position = state.get("open_position")
        recent_trades = state.get("recent_trades") or []
        control_asset = (position or {}).get("asset") or (
            recent_trades[0].get("asset") if recent_trades else ""
        )
        state["risk_controls"] = self.ledger.risk_controls(
            asset=control_asset,
            daily_loss_limit_pct=self.daily_loss_limit_pct,
            max_drawdown_pct=self.max_drawdown_pct,
            max_consecutive_losses=self.max_consecutive_losses,
            cooldown_hours_after_loss=self.cooldown_hours_after_loss,
        )
        return state

    def run_once(self, asset_input: str | AssetSpec) -> dict[str, Any]:
        asset = asset_input if isinstance(asset_input, AssetSpec) else AssetSpec.parse(asset_input)
        if asset.market_type != "cex" or asset.symbol not in PAPER_ASSETS:
            return {"status": "SKIPPED", "asset": asset.raw,
                    "reason": "Paper bot currently accepts only BTC, ETH and SOL spot symbols."}
        position = self.ledger.position()
        if position and position["asset"] != asset.raw:
            return {"status": "BLOCKED", "asset": asset.raw,
                    "reason": "One paper position is already open; manage it before scanning another asset."}
        data = getattr(self.engine, "data", None)
        if position:
            try:
                snapshot_getter = getattr(data, "snapshot", None) or getattr(self.engine, "snapshot", None)
                if not callable(snapshot_getter):
                    raise RuntimeError("snapshot reader unavailable")
                snapshot = snapshot_getter(asset)
            except Exception:
                return {"status": "UNKNOWN", "asset": asset.raw,
                        "reason": "Fresh price unavailable; open paper position was left unchanged."}
            price = _number(getattr(snapshot, "price_usd", None))
            if price is None or price <= 0:
                return {"status": "UNKNOWN", "asset": asset.raw,
                        "reason": "Fresh cross-validated price is invalid; position was left unchanged."}
            entry = float(position["entry_price"])
            initial_stop = float(position.get("initial_stop_loss") or position["stop_loss"])
            initial_risk = max(0.0, entry - initial_stop)
            peak = max(float(position.get("peak_price") or entry), price)
            managed_stop = float(position["stop_loss"])
            if initial_risk > 0 and peak >= entry + initial_risk:
                break_even = entry * (1.0 + 2.0 * self.fee_rate + 2.0 * self.slippage_rate)
                managed_stop = max(managed_stop, break_even)
            if initial_risk > 0 and peak >= entry + 1.5 * initial_risk:
                managed_stop = max(managed_stop, peak - initial_risk)
            self.ledger.update_management(price, managed_stop)
            managed_position = self.ledger.position() or position
            active_stop = float(managed_position["stop_loss"])
            if price <= active_stop:
                fill = min(price, active_stop) * (1.0 - self.slippage_rate)
                initial_stop = float(managed_position.get("initial_stop_loss") or active_stop)
                exit_reason = "TRAILING_STOP" if active_stop > initial_stop else "STOP_LOSS"
                closed = self.ledger.close_position(fill, self.fee_rate, exit_reason)
                return {"status": "CLOSED", "asset": asset.raw, "trade": closed}
            if price >= float(position["take_profit"]):
                fill = float(position["take_profit"]) * (1.0 - self.slippage_rate)
                closed = self.ledger.close_position(fill, self.fee_rate, "TAKE_PROFIT")
                return {"status": "CLOSED", "asset": asset.raw, "trade": closed}
            max_holding = max(1, int(position.get("max_holding_days") or 14))
            if int(time.time()) - int(position["opened_at"]) >= max_holding * 86_400:
                fill = price * (1.0 - self.slippage_rate)
                closed = self.ledger.close_position(fill, self.fee_rate, "TIME_EXIT")
                return {"status": "CLOSED", "asset": asset.raw, "trade": closed}
            return {"status": "HOLDING", "asset": asset.raw, "mark_price": price,
                    "active_stop_loss": active_stop,
                    "unrealized_pnl_usd": self.ledger.state()["account"]["unrealized_pnl_usd"]}

        try:
            result = self.engine.analyze(asset)
        except Exception as exc:
            return {"status": "UNKNOWN", "asset": asset.raw,
                    "reason": f"Market scan failed; paper state was not changed ({type(exc).__name__})."}
        snapshots = getattr(data, "snapshots", {})
        snapshot = snapshots.get(asset.raw) if isinstance(snapshots, dict) else None
        price = _number(getattr(snapshot, "price_usd", None))
        if price is None or price <= 0:
            return {"status": "UNKNOWN", "asset": asset.raw,
                    "reason": "A fresh cross-validated public spot price is unavailable."}

        gates = getattr(result, "gates", [])
        if not gates or any(_status(gate.status) != "PASS" for gate in gates):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Every mandatory signal gate must PASS before paper entry."}
        signal = _status(getattr(result, "signal", "NO TRADE"))
        if signal != "LONG":
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Paper spot bot opens LONG signals only; short and neutral outputs are skipped."}
        score = _number(getattr(result, "confirmation_score", None))
        risk = _number(getattr(result, "estimated_risk_score", None))
        stop = _number(getattr(result, "stop_loss", None))
        target = _number(getattr(result, "take_profit", None))
        if (score is None or score < self.min_confirmation_score
                or risk is None or risk >= self.max_risk_score
                or stop is None or target is None):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Score, risk or stop/target plan did not pass paper safety limits."}
        entry = price * (1.0 + self.slippage_rate)
        exit_at_stop = stop * (1.0 - self.slippage_rate)
        if not (0 < exit_at_stop < entry < target):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Spot entry, stop-loss and take-profit levels are not ordered safely."}
        frame = getattr(data, "candle_frames", {}).get(asset.raw)
        if frame is None or frame.empty or not hasattr(frame.index[-1], "isoformat"):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "A timestamped completed candle is required to prevent duplicate entries."}
        signal_bar = frame.index[-1].isoformat()
        if self.ledger.has_traded_signal_bar(asset.raw, signal_bar):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "This completed-candle signal was already paper-traded."}

        controls = self.ledger.risk_controls(
            asset=asset.raw,
            daily_loss_limit_pct=self.daily_loss_limit_pct,
            max_drawdown_pct=self.max_drawdown_pct,
            max_consecutive_losses=self.max_consecutive_losses,
            cooldown_hours_after_loss=self.cooldown_hours_after_loss,
        )
        if not controls["entry_allowed"]:
            return {"status": "RISK_HALT", "asset": asset.raw,
                    "reason": "Paper entries paused by risk circuit breaker.",
                    "risk_controls": controls}

        state = self.ledger.state()
        equity = _number(state["account"]["equity_usd"])
        cash = _number(state["account"]["cash_balance_usd"])
        if equity is None or cash is None or equity <= 0:
            return {"status": "UNKNOWN", "asset": asset.raw, "reason": "Paper account balance is invalid."}
        risk_budget = equity * self.risk_per_trade_pct
        unit_risk = entry - exit_at_stop + entry * self.fee_rate + exit_at_stop * self.fee_rate
        target_fill = target * (1.0 - self.slippage_rate)
        unit_reward = target_fill - entry - entry * self.fee_rate - target_fill * self.fee_rate
        net_reward_risk = unit_reward / unit_risk if unit_risk > 0 else None
        if unit_risk <= 0 or unit_reward <= 0 or net_reward_risk is None:
            return {"status": "NO_TRADE", "asset": asset.raw, "reason": "Net reward/risk is invalid after costs."}
        if net_reward_risk < self.min_net_reward_risk:
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Net reward/risk after fees and slippage is below the minimum.",
                    "net_reward_risk": round(net_reward_risk, 3)}
        holding_raw = _number(getattr(result, "expected_holding_days", None))
        if holding_raw is None or holding_raw < 1:
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "A valid maximum holding period is required for paper entry."}
        max_holding_days = min(90, max(1, int(holding_raw)))
        quantity = min(
            risk_budget / unit_risk,
            equity * self.max_position_notional_pct / entry,
            cash / (entry * (1.0 + self.fee_rate)),
        )
        notional = quantity * entry
        entry_fee = notional * self.fee_rate
        debit = notional + entry_fee
        if not math.isfinite(quantity) or quantity <= 0 or debit > cash:
            return {"status": "NO_TRADE", "asset": asset.raw, "reason": "Position sizing returned no safe quantity."}
        record = {
            "asset": asset.raw, "quantity": quantity, "entry_price": entry,
            "stop_loss": stop, "initial_stop_loss": stop,
            "take_profit": target, "entry_fee": entry_fee,
            "opened_at": int(time.time()), "signal_bar": signal_bar,
            "confirmation_score": score, "risk_budget": risk_budget,
            "max_holding_days": max_holding_days,
        }
        if not self.ledger.open_position(record, debit):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Paper ledger rejected duplicate, concurrent or unaffordable entry."}
        return {"status": "OPENED", "asset": asset.raw, "side": "LONG",
                "quantity": quantity, "entry_price": entry, "stop_loss": stop,
                "take_profit": target, "risk_budget_usd": risk_budget,
                "confirmation_score": score, "net_reward_risk": round(net_reward_risk, 3),
                "max_holding_days": max_holding_days, "fee_usd": entry_fee,
                "risk_controls": controls, "mode": "PAPER_ONLY", "orders_submitted": False}
