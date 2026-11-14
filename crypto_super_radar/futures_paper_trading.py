"""Direction-aware, fail-closed perpetual futures paper trading.

This module consumes the existing 12-gate perp analysis and public, cross-validated
perpetual-market data. It never loads exchange credentials or submits live orders.
Leverage is deliberately capped at 1x until exchange-specific margin tiers and
liquidation modelling are independently validated.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .ai_second_opinion import ai_connection_status, run_free_ai_second_opinion
from .database import connect_database
from .models import AssetSpec
from .paper_execution import PaperExecutionEventLedger


FUTURES_PAPER_ASSETS = frozenset({
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT",
    "TRXUSDT",
})


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _signal(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper().replace(" ", "_")


def _mandatory_gates_pass(gates: Any) -> bool:
    if not isinstance(gates, (list, tuple)) or len(gates) != 12:
        return False
    seen: set[int] = set()
    for gate in gates:
        gate_id = getattr(gate, "gate_id", None)
        if isinstance(gate_id, bool) or not isinstance(gate_id, int) or gate_id in seen:
            return False
        status = str(getattr(getattr(gate, "status", None), "value", getattr(gate, "status", ""))).upper()
        if status != "PASS":
            return False
        seen.add(gate_id)
    return seen == set(range(1, 13))


def _execution_id(asset: str, signal_bar: str, leg: str) -> str:
    digest = hashlib.sha256(f"{asset}|{signal_bar}|{leg}".encode("utf-8")).hexdigest()[:32]
    return f"futures-paper-{leg.lower()}-{digest}"


def _timestamp_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        try:
            return int(value.timestamp() * 1000)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    number = _number(value)
    if number is None or number <= 0:
        return None
    return int(number)


class FuturesPaperLedger:
    """SQLite paper account, directional positions, funding events and fill journal."""

    def __init__(self, path: Optional[str] = None, initial_equity_usd: float = 10_000.0):
        if not math.isfinite(initial_equity_usd) or initial_equity_usd <= 0:
            raise ValueError("Initial futures paper equity must be positive and finite.")
        selected_path = path or os.getenv("CSR_FUTURES_DB_PATH") or "data/crypto_super_radar_futures.sqlite3"
        self.path = selected_path
        if selected_path != ":memory:":
            Path(selected_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = connect_database(selected_path)
        with self._db:
            self.execution_ledger = PaperExecutionEventLedger(self._db, lock=self._lock)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS futures_paper_account (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    initial_equity REAL NOT NULL,
                    balance REAL NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS futures_paper_positions (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    asset TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    mark_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    initial_stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    leverage REAL NOT NULL,
                    initial_margin REAL NOT NULL,
                    entry_fee REAL NOT NULL,
                    funding_cost REAL NOT NULL DEFAULT 0,
                    opened_at INTEGER NOT NULL,
                    signal_bar TEXT NOT NULL,
                    confirmation_score REAL NOT NULL,
                    risk_budget REAL NOT NULL,
                    max_holding_hours INTEGER NOT NULL,
                    peak_price REAL NOT NULL,
                    trough_price REAL NOT NULL,
                    last_funding_time_ms INTEGER NOT NULL,
                    last_mark_at_ms INTEGER NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS futures_paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    leverage REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    initial_stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    entry_fee REAL NOT NULL,
                    exit_fee REAL NOT NULL,
                    gross_pnl REAL NOT NULL,
                    funding_cost REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    opened_at INTEGER NOT NULL,
                    closed_at INTEGER NOT NULL,
                    signal_bar TEXT NOT NULL,
                    confirmation_score REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    UNIQUE(asset, signal_bar)
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS futures_paper_funding_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    funding_time_ms INTEGER NOT NULL,
                    funding_rate REAL NOT NULL,
                    mark_price_at_observation REAL NOT NULL,
                    funding_cost_usd REAL NOT NULL,
                    attribution TEXT NOT NULL,
                    UNIQUE(asset, funding_time_ms)
                )
            """)
            self._db.execute(
                "INSERT OR IGNORE INTO futures_paper_account(id,initial_equity,balance,updated_at) VALUES(1,?,?,?)",
                (float(initial_equity_usd), float(initial_equity_usd), int(time.time())),
            )

    def position(self) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM futures_paper_positions WHERE id=1").fetchone()
        return dict(row) if row else None

    def open_position(self, position: dict[str, Any]) -> bool:
        with self._lock, self._db:
            account = self._db.execute(
                "SELECT balance FROM futures_paper_account WHERE id=1"
            ).fetchone()
            active = self._db.execute("SELECT 1 FROM futures_paper_positions WHERE id=1").fetchone()
            duplicate = self._db.execute(
                "SELECT 1 FROM futures_paper_trades WHERE asset=? AND signal_bar=?",
                (position["asset"], position["signal_bar"]),
            ).fetchone()
            if account is None or active or duplicate:
                return False
            balance = float(account["balance"])
            fee = float(position["entry_fee"])
            if fee < 0 or fee >= balance:
                return False
            self._db.execute("""
                INSERT INTO futures_paper_positions
                (id,asset,direction,quantity,entry_price,mark_price,stop_loss,
                 initial_stop_loss,take_profit,leverage,initial_margin,entry_fee,
                 funding_cost,opened_at,signal_bar,confirmation_score,risk_budget,
                 max_holding_hours,peak_price,trough_price,last_funding_time_ms,last_mark_at_ms)
                VALUES(1,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)
            """, (
                position["asset"], position["direction"], position["quantity"],
                position["entry_price"], position["entry_price"], position["stop_loss"],
                position["stop_loss"], position["take_profit"], position["leverage"],
                position["initial_margin"], fee, position["opened_at"],
                position["signal_bar"], position["confirmation_score"],
                position["risk_budget"], position["max_holding_hours"],
                position["entry_price"], position["entry_price"],
                position["last_funding_time_ms"], position["last_mark_at_ms"],
            ))
            self._db.execute(
                "UPDATE futures_paper_account SET balance=balance-?,updated_at=? WHERE id=1",
                (fee, int(time.time())),
            )
            side = "BUY" if position["direction"] == "LONG" else "SELL"
            self.execution_ledger.record_simulated_fill(
                client_order_id=_execution_id(position["asset"], position["signal_bar"], "ENTRY"),
                asset=position["asset"], side=side, quantity=position["quantity"],
                fill_price=position["entry_price"], fee_paid_usd=fee,
                source="FUTURES_PAPER_SIMULATOR", reason=f"PAPER_{position['direction']}_ENTRY",
                occurred_at=position["opened_at"],
            )
        return True

    def apply_funding(
        self, *, asset: str, funding_time_ms: int, funding_rate: float, mark_price: float
    ) -> Optional[float]:
        position = self.position()
        if position is None or position["asset"] != asset:
            return None
        direction_sign = 1.0 if position["direction"] == "LONG" else -1.0
        cost = float(position["quantity"]) * float(mark_price) * float(funding_rate) * direction_sign
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT 1 FROM futures_paper_funding_events WHERE asset=? AND funding_time_ms=?",
                (asset, int(funding_time_ms)),
            ).fetchone()
            if existing:
                return 0.0
            current = self._db.execute("SELECT * FROM futures_paper_positions WHERE id=1").fetchone()
            if current is None or current["asset"] != asset:
                return None
            self._db.execute("""
                INSERT INTO futures_paper_funding_events
                (asset,funding_time_ms,funding_rate,mark_price_at_observation,funding_cost_usd,attribution)
                VALUES(?,?,?,?,?,'CURRENT_MARK_AT_OBSERVATION')
            """, (asset, int(funding_time_ms), float(funding_rate), float(mark_price), cost))
            self._db.execute("""
                UPDATE futures_paper_positions
                SET funding_cost=funding_cost+?,last_funding_time_ms=MAX(last_funding_time_ms,?)
                WHERE id=1
            """, (cost, int(funding_time_ms)))
            self._db.execute(
                "UPDATE futures_paper_account SET balance=balance-?,updated_at=? WHERE id=1",
                (cost, int(time.time())),
            )
        return cost

    def update_management(self, *, mark_price: float, stop_loss: float, mark_at_ms: int) -> None:
        position = self.position()
        if position is None:
            return
        if position["direction"] == "LONG":
            peak = max(float(position["peak_price"]), mark_price)
            trough = min(float(position["trough_price"]), mark_price)
        else:
            peak = max(float(position["peak_price"]), mark_price)
            trough = min(float(position["trough_price"]), mark_price)
        with self._lock, self._db:
            self._db.execute("""
                UPDATE futures_paper_positions
                SET mark_price=?,stop_loss=?,peak_price=?,trough_price=?,last_mark_at_ms=?
                WHERE id=1
            """, (float(mark_price), float(stop_loss), peak, trough, int(mark_at_ms)))

    def close_position(
        self, *, exit_price: float, exit_fee_rate: float, reason: str
    ) -> Optional[dict[str, Any]]:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM futures_paper_positions WHERE id=1").fetchone()
            if row is None:
                return None
            p = dict(row)
            direction_sign = 1.0 if p["direction"] == "LONG" else -1.0
            exit_fee = float(p["quantity"]) * float(exit_price) * float(exit_fee_rate)
            gross = float(p["quantity"]) * (float(exit_price) - float(p["entry_price"])) * direction_sign
            funding = float(p["funding_cost"])
            net = gross - float(p["entry_fee"]) - exit_fee - funding
            self._db.execute("""
                INSERT INTO futures_paper_trades
                (asset,direction,quantity,leverage,entry_price,exit_price,stop_loss,
                 initial_stop_loss,take_profit,entry_fee,exit_fee,gross_pnl,funding_cost,
                 net_pnl,opened_at,closed_at,signal_bar,confirmation_score,exit_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                p["asset"], p["direction"], p["quantity"], p["leverage"],
                p["entry_price"], float(exit_price), p["stop_loss"], p["initial_stop_loss"],
                p["take_profit"], p["entry_fee"], exit_fee, gross, funding, net,
                p["opened_at"], int(time.time()), p["signal_bar"],
                p["confirmation_score"], reason,
            ))
            # Funding cash flows were already posted idempotently by apply_funding().
            # Subtracting the accumulated funding again here would double-charge the account.
            self._db.execute(
                "UPDATE futures_paper_account SET balance=balance+?-?,updated_at=? WHERE id=1",
                (gross, exit_fee, int(time.time())),
            )
            self._db.execute("DELETE FROM futures_paper_positions WHERE id=1")
            close_side = "SELL" if p["direction"] == "LONG" else "BUY"
            self.execution_ledger.record_simulated_fill(
                client_order_id=_execution_id(p["asset"], p["signal_bar"], "EXIT"),
                asset=p["asset"], side=close_side, quantity=p["quantity"],
                fill_price=float(exit_price), fee_paid_usd=exit_fee,
                source="FUTURES_PAPER_SIMULATOR", reason=f"PAPER_EXIT:{reason}",
                occurred_at=int(time.time()),
            )
        return {
            "asset": p["asset"], "direction": p["direction"],
            "gross_pnl_usd": gross, "funding_cost_usd": funding,
            "net_pnl_usd": net, "exit_price": float(exit_price),
            "exit_reason": reason, "leverage": p["leverage"],
        }

    def risk_controls(
        self, *, daily_loss_limit_pct: float, max_drawdown_pct: float,
        max_consecutive_losses: int, cooldown_hours_after_loss: float,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        now = int(time.time()) if now is None else int(now)
        day_start = now // 86400 * 86400
        with self._lock:
            account = self._db.execute("SELECT * FROM futures_paper_account WHERE id=1").fetchone()
            trades = self._db.execute(
                "SELECT net_pnl,closed_at FROM futures_paper_trades ORDER BY closed_at,id"
            ).fetchall()
            recent = list(reversed(trades[-max_consecutive_losses:]))
            daily = self._db.execute(
                "SELECT COALESCE(SUM(net_pnl),0) FROM futures_paper_trades WHERE closed_at>=?",
                (day_start,),
            ).fetchone()[0]
            position_row = self._db.execute("SELECT * FROM futures_paper_positions WHERE id=1").fetchone()
        initial = float(account["initial_equity"])
        balance = float(account["balance"])
        realized = [float(row["net_pnl"]) for row in trades]
        running = initial
        peak = initial
        historical_dd = 0.0
        for pnl in realized:
            running += pnl
            peak = max(peak, running)
            if peak > 0:
                historical_dd = max(historical_dd, (peak - running) / peak * 100.0)
        open_unrealized = 0.0
        used_margin = 0.0
        if position_row:
            p = dict(position_row)
            sign = 1.0 if p["direction"] == "LONG" else -1.0
            open_unrealized = p["quantity"] * (p["mark_price"] - p["entry_price"]) * sign
            used_margin = float(p["initial_margin"])
        equity = balance + open_unrealized
        peak = max(peak, equity)
        current_dd = (peak - equity) / peak * 100.0 if peak > 0 else 100.0
        drawdown = max(historical_dd, current_dd)
        consecutive = 0
        for row in recent:
            if float(row["net_pnl"]) >= 0:
                break
            consecutive += 1
        last_trade = self._db.execute(
            "SELECT net_pnl,closed_at FROM futures_paper_trades ORDER BY closed_at DESC,id DESC LIMIT 1"
        ).fetchone()
        cooldown = 0.0
        # Apply a loss cooldown only while the latest closed trade is still a loss.
        if last_trade and float(last_trade["net_pnl"]) < 0:
            cooldown = max(0.0, cooldown_hours_after_loss * 3600 - (now - int(last_trade["closed_at"]))) / 3600
        daily_loss = max(0.0, -float(daily) / initial * 100.0) if initial else 0.0
        reasons = []
        if daily_loss >= daily_loss_limit_pct * 100:
            reasons.append("DAILY_LOSS_LIMIT")
        if drawdown >= max_drawdown_pct * 100:
            reasons.append("MAX_EQUITY_DRAWDOWN")
        if consecutive >= max_consecutive_losses:
            reasons.append("CONSECUTIVE_LOSSES")
        if cooldown > 0:
            reasons.append("POST_LOSS_COOLDOWN")
        return {
            "entry_allowed": not reasons, "halt_reasons": reasons,
            "daily_realized_loss_pct": round(daily_loss, 4),
            "max_equity_drawdown_pct": round(drawdown, 4),
            "consecutive_losses": consecutive,
            "cooldown_hours_remaining": round(cooldown, 2),
            "used_margin_usd": round(used_margin, 8),
            "equity_usd": round(equity, 8),
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            account = self._db.execute("SELECT * FROM futures_paper_account WHERE id=1").fetchone()
            position = self._db.execute("SELECT * FROM futures_paper_positions WHERE id=1").fetchone()
            trades = self._db.execute(
                "SELECT * FROM futures_paper_trades ORDER BY closed_at DESC,id DESC LIMIT 30"
            ).fetchall()
            funding_count = int(self._db.execute(
                "SELECT COUNT(*) FROM futures_paper_funding_events"
            ).fetchone()[0])
            realized_pnl = float(self._db.execute(
                "SELECT COALESCE(SUM(net_pnl),0.0) FROM futures_paper_trades"
            ).fetchone()[0])
        balance = float(account["balance"])
        initial = float(account["initial_equity"])
        p = dict(position) if position else None
        unrealized = 0.0
        used_margin = 0.0
        if p:
            sign = 1.0 if p["direction"] == "LONG" else -1.0
            unrealized = float(p["quantity"]) * (float(p["mark_price"]) - float(p["entry_price"])) * sign
            used_margin = float(p["initial_margin"])
        equity = balance + unrealized
        return {
            "mode": "FUTURES_PAPER_ONLY",
            "market": "USDT-M_PERPETUAL",
            "leverage": 1.0,
            "leverage_policy": "HARDCAPPED_1X_UNTIL_EXCHANGE_LIQUIDATION_TIERS_ARE_VALIDATED",
            "live_orders_enabled": False,
            "orders_submitted": False,
            "private_exchange_credentials_used": False,
            "account": {
                "initial_equity_usd": initial,
                "balance_usd": balance,
                "equity_usd": equity,
                "available_margin_usd": max(0.0, equity - used_margin),
                "used_margin_usd": used_margin,
                "unrealized_pnl_usd": unrealized,
                "realized_pnl_usd": realized_pnl,
                "total_return_pct": (equity / initial - 1.0) * 100.0,
            },
            "open_position": p,
            "recent_trades": [dict(row) for row in trades],
            "funding_events_count": funding_count,
            "execution_ledger": {
                "event_count": self.execution_ledger.event_count(),
                "recent_orders": self.execution_ledger.latest_orders(20),
                "append_only": True,
                "journal_status": self.execution_ledger.replay_order_states()["status"],
            },
            "liquidation_model_status": "NOT_ESTIMATED_EXCHANGE_RISK_TIER_REQUIRED",
            "funding_attribution": "Funding rate is cross-checked; historical settlement is valued at the mark price observed when the paper process detects it.",
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()


class FuturesPaperTradingBot:
    """12-gate, LONG/SHORT USDT perpetual bot; paper fills only, no live execution."""

    def __init__(
        self, engine: Any, *, ledger: Optional[FuturesPaperLedger] = None,
        risk_per_trade_pct: float = 0.005, max_position_notional_pct: float = 0.10,
        max_risk_score: float = 45.0, min_confirmation_score: float = 75.0,
        fee_bps_per_side: float = 5.0, slippage_bps_per_side: float = 3.0,
        daily_loss_limit_pct: float = 0.015, max_drawdown_pct: float = 0.10,
        max_consecutive_losses: int = 3, cooldown_hours_after_loss: float = 24.0,
        min_net_reward_risk: float = 1.8, leverage: float = 1.0, ai_reviewer=None,
    ):
        self.engine = engine
        self.ledger = ledger or FuturesPaperLedger()
        self.ai_reviewer = ai_reviewer or run_free_ai_second_opinion
        values = (
            risk_per_trade_pct, max_position_notional_pct, max_risk_score,
            min_confirmation_score, fee_bps_per_side, slippage_bps_per_side,
            daily_loss_limit_pct, max_drawdown_pct, cooldown_hours_after_loss,
            min_net_reward_risk, leverage,
        )
        if not all(math.isfinite(float(x)) for x in values):
            raise ValueError("Futures paper configuration must be finite.")
        if not 0 < risk_per_trade_pct <= 0.02:
            raise ValueError("Per-trade risk must be >0 and <=2%.")
        if not 0 < max_position_notional_pct <= 0.25:
            raise ValueError("Max position notional must be >0 and <=25%.")
        if not 0 < leverage <= 1.0:
            raise ValueError("Futures paper leverage is hard-capped at 1x pending liquidation-tier validation.")
        if fee_bps_per_side < 0 or slippage_bps_per_side < 0:
            raise ValueError("Fee and slippage assumptions cannot be negative.")
        if not 0 < daily_loss_limit_pct <= 0.10 or not 0 < max_drawdown_pct <= 0.50:
            raise ValueError("Risk circuit-breaker limits are invalid.")
        if not isinstance(max_consecutive_losses, int) or max_consecutive_losses <= 0:
            raise ValueError("max_consecutive_losses must be positive.")
        if cooldown_hours_after_loss < 0 or min_net_reward_risk < 1:
            raise ValueError("Cooldown cannot be negative and reward/risk must be >=1.")
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
        self.leverage = leverage

    def state(self) -> dict[str, Any]:
        state = self.ledger.state()
        state["ai_connection"] = ai_connection_status()
        risk = self.ledger.risk_controls(
            daily_loss_limit_pct=self.daily_loss_limit_pct,
            max_drawdown_pct=self.max_drawdown_pct,
            max_consecutive_losses=self.max_consecutive_losses,
            cooldown_hours_after_loss=self.cooldown_hours_after_loss,
        )
        # Backward compatibility: entry_allowed has always represented only the
        # PnL/drawdown circuit breaker, not final trade eligibility.
        circuit_breaker_clear = risk.get("entry_allowed") is True
        risk.update({
            "risk_circuit_breaker_clear": circuit_breaker_clear,
            "risk_circuit_breaker_active": not circuit_breaker_clear,
            "entry_allowed_scope": "RISK_CIRCUIT_BREAKER_ONLY",
            "entry_allowed_scope_fa": (
                "این فیلد فقط وضعیت ترمز زیان و افت سرمایه را نشان می‌دهد؛ "
                "مجوز نهایی ورود نیست و جایگزین اعتبارسنجی بازار، ۱۲ فیلتر و AI نمی‌شود."
            ),
            "risk_circuit_breaker_status_fa": (
                "ترمز ریسک حساب فعال نیست؛ بااین‌حال ورود فقط پس از عبور همه کنترل‌های معامله ممکن است."
                if circuit_breaker_clear else
                "ترمز ریسک حساب فعال یا نامشخص است؛ ورود جدید باید متوقف بماند."
            ),
        })
        state["risk_controls"] = risk
        return state

    def _market(self, asset: AssetSpec) -> dict[str, Any]:
        provider = getattr(getattr(self.engine, "data", None), "perpetual", None)
        getter = getattr(provider, "execution_snapshot", None)
        if not callable(getter):
            raise RuntimeError("Cross-validated perpetual bid/ask and mark-price provider is unavailable.")
        market = getter(asset.symbol)
        if not isinstance(market, dict) or market.get("status") != "PASS":
            raise RuntimeError("Perpetual market microstructure did not pass independent validation.")
        for key in ("mark_price", "index_price", "best_bid", "best_ask", "funding_rate", "funding_interval_hours", "timestamp_ms"):
            if _number(market.get(key)) is None:
                raise RuntimeError(f"Perpetual {key} is unavailable or invalid.")
        if market["best_bid"] <= 0 or market["best_ask"] < market["best_bid"]:
            raise RuntimeError("Perpetual order book is invalid or crossed.")
        if abs(market["mark_price"] - market["index_price"]) / market["index_price"] > 0.02:
            raise RuntimeError("Mark/index basis exceeds the 2% paper safety ceiling.")
        age_ms = int(time.time() * 1000) - int(market["timestamp_ms"])
        if age_ms < -5_000 or age_ms > 120_000:
            raise RuntimeError("Perpetual bid/ask or mark price is stale.")
        return market

    def _risk(self) -> dict[str, Any]:
        return self.ledger.risk_controls(
            daily_loss_limit_pct=self.daily_loss_limit_pct,
            max_drawdown_pct=self.max_drawdown_pct,
            max_consecutive_losses=self.max_consecutive_losses,
            cooldown_hours_after_loss=self.cooldown_hours_after_loss,
        )

    def _funding(self, asset: AssetSpec, position: dict[str, Any], mark_price: float) -> str:
        data = getattr(self.engine, "data", None)
        reader = getattr(data, "funding_history", None)
        if not callable(reader):
            return "INCOMPLETE"
        try:
            series = reader(asset)
        except Exception:
            return "INCOMPLETE"
        if series is None or getattr(series, "empty", True):
            return "INCOMPLETE"
        now_ms = int(time.time() * 1000)
        opened_ms = int(position["opened_at"]) * 1000
        events = []
        try:
            for stamp, raw_rate in series.items():
                ts_ms = _timestamp_ms(stamp)
                rate = _number(raw_rate)
                if ts_ms is None or rate is None:
                    return "INCOMPLETE"
                if opened_ms < ts_ms <= now_ms and ts_ms > int(position["last_funding_time_ms"]):
                    events.append((ts_ms, rate))
        except Exception:
            return "INCOMPLETE"
        for ts_ms, rate in sorted(events):
            self.ledger.apply_funding(
                asset=asset.raw, funding_time_ms=ts_ms,
                funding_rate=rate, mark_price=mark_price,
            )
        return "COMPLETE"

    def _close_at_market(self, position: dict[str, Any], market: dict[str, Any], reason: str) -> dict[str, Any]:
        if position["direction"] == "LONG":
            reference = float(market["best_bid"])
            fill = reference * (1.0 - self.slippage_rate)
        else:
            reference = float(market["best_ask"])
            fill = reference * (1.0 + self.slippage_rate)
        trade = self.ledger.close_position(
            exit_price=fill, exit_fee_rate=self.fee_rate, reason=reason,
        )
        return {"status": "CLOSED", "asset": position["asset"], "trade": trade}

    def run_once(self, asset_input: str | AssetSpec) -> dict[str, Any]:
        asset = asset_input if isinstance(asset_input, AssetSpec) else AssetSpec.parse(asset_input)
        if asset.market_type != "perp" or asset.symbol not in FUTURES_PAPER_ASSETS:
            return {"status": "SKIPPED", "asset": asset.raw,
                    "reason": "Futures paper accepts only allowlisted USDT-M perpetual symbols using perp:SYMBOL."}
        position = self.ledger.position()
        if position and position["asset"] != asset.raw:
            return {"status": "BLOCKED", "asset": asset.raw,
                    "reason": "One futures paper position is already open; manage it before scanning another asset."}
        try:
            market = self._market(asset)
        except Exception as exc:
            # Keep the failure closed, but preserve a short sanitized diagnostic
            # so the dashboard can identify the provider/HTTP/data validation issue.
            detail = " ".join(str(exc).split())
            detail = re.sub(
                r"(?i)(authorization|api[_-]?key|secret|passphrase)(\\s*[:=]\\s*)[^,;\\s]+",
                r"\\1\\2[REDACTED]",
                detail,
            )[:240] or None
            return {
                "status": "UNKNOWN",
                "asset": asset.raw,
                "reason": f"Validated perpetual market data unavailable; no account mutation ({type(exc).__name__}).",
                "reason_code": "PERPETUAL_MARKET_DATA_UNAVAILABLE",
                "technical_error_type": type(exc).__name__,
                "technical_detail": detail,
                "entry_blocked": True,
                "entry_block_reason_code": "PERPETUAL_MARKET_DATA_UNAVAILABLE",
            }
        mark = float(market["mark_price"])
        now_ms = int(time.time() * 1000)

        if position:
            funding_status = self._funding(asset, position, mark)
            position = self.ledger.position() or position
            entry = float(position["entry_price"])
            initial_stop = float(position["initial_stop_loss"])
            initial_risk = abs(entry - initial_stop)
            active_stop = float(position["stop_loss"])
            if position["direction"] == "LONG":
                peak = max(float(position["peak_price"]), mark)
                if initial_risk > 0 and peak >= entry + initial_risk:
                    active_stop = max(active_stop, entry * (1.0 + 2.0 * self.fee_rate + 2.0 * self.slippage_rate))
                if initial_risk > 0 and peak >= entry + 1.5 * initial_risk:
                    active_stop = max(active_stop, peak - initial_risk)
            else:
                trough = min(float(position["trough_price"]), mark)
                if initial_risk > 0 and trough <= entry - initial_risk:
                    active_stop = min(active_stop, entry * (1.0 - 2.0 * self.fee_rate - 2.0 * self.slippage_rate))
                if initial_risk > 0 and trough <= entry - 1.5 * initial_risk:
                    active_stop = min(active_stop, trough + initial_risk)
            self.ledger.update_management(mark_price=mark, stop_loss=active_stop, mark_at_ms=now_ms)
            position = self.ledger.position() or position
            direction = position["direction"]
            stop_hit = mark <= active_stop if direction == "LONG" else mark >= active_stop
            target = float(position["take_profit"])
            target_hit = mark >= target if direction == "LONG" else mark <= target
            if stop_hit:
                reason = "TRAILING_STOP" if (
                    (direction == "LONG" and active_stop > initial_stop)
                    or (direction == "SHORT" and active_stop < initial_stop)
                ) else "STOP_LOSS"
                return self._close_at_market(position, market, reason)
            if target_hit:
                return self._close_at_market(position, market, "TAKE_PROFIT")
            max_hours = max(1, int(position["max_holding_hours"]))
            if int(time.time()) - int(position["opened_at"]) >= max_hours * 3600:
                return self._close_at_market(position, market, "TIME_EXIT")
            status = "HOLDING" if funding_status == "COMPLETE" else "HOLDING_FUNDING_INCOMPLETE"
            return {
                "status": status, "asset": asset.raw, "direction": direction,
                "mark_price": mark, "active_stop_loss": active_stop,
                "take_profit": target, "funding_status": funding_status,
                "unrealized_pnl_usd": self.ledger.state()["account"]["unrealized_pnl_usd"],
            }

        try:
            result = self.engine.analyze(asset)
        except Exception as exc:
            return {"status": "UNKNOWN", "asset": asset.raw,
                    "reason": f"Perpetual technical analysis failed ({type(exc).__name__})."}
        gates = getattr(result, "gates", [])
        if not _mandatory_gates_pass(gates):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "All 12 unique mandatory gates must be PASS for a futures entry."}
        direction = _signal(getattr(result, "signal", None))
        if direction not in {"LONG", "SHORT"}:
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Radar did not confirm LONG or SHORT; neutral/unknown conditions are not traded."}
        score = _number(getattr(result, "confirmation_score", None))
        risk = _number(getattr(result, "estimated_risk_score", None))
        stop = _number(getattr(result, "stop_loss", None))
        target = _number(getattr(result, "take_profit", None))
        if (
            score is None or score < self.min_confirmation_score
            or risk is None or risk >= self.max_risk_score
            or stop is None or target is None
        ):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Confirmation score, risk score or structural stop/target did not pass limits."}

        quote = float(market["best_ask"]) if direction == "LONG" else float(market["best_bid"])
        sign = 1.0 if direction == "LONG" else -1.0
        entry = quote * (1.0 + sign * self.slippage_rate)
        if direction == "LONG":
            if not (0 < stop < entry < target):
                return {"status": "NO_TRADE", "asset": asset.raw,
                        "reason": "LONG requires stop < entry < take-profit using the current futures quote."}
        elif not (0 < target < entry < stop):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "SHORT requires take-profit < entry < stop using the current futures quote."}

        data = getattr(self.engine, "data", None)
        frames = getattr(data, "candle_frames", {})
        frame = frames.get(asset.raw) if isinstance(frames, dict) else None
        if frame is None or getattr(frame, "empty", True) or not hasattr(frame.index[-1], "isoformat"):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "A timestamped completed perpetual candle is required for idempotency."}
        signal_bar = frame.index[-1].isoformat()
        if self.ledger._db.execute(
            "SELECT 1 FROM futures_paper_trades WHERE asset=? AND signal_bar=?",
            (asset.raw, signal_bar),
        ).fetchone():
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "This completed futures candle was already paper-traded."}

        controls = self._risk()
        if not controls["entry_allowed"]:
            return {"status": "RISK_HALT", "asset": asset.raw,
                    "reason": "Futures paper entries paused by the risk circuit breaker.",
                    "risk_controls": controls}
        state = self.ledger.state()
        equity = _number(state["account"]["equity_usd"])
        available = _number(state["account"]["available_margin_usd"])
        if equity is None or available is None or equity <= 0 or available <= 0:
            return {"status": "UNKNOWN", "asset": asset.raw,
                    "reason": "Futures paper equity or available margin is invalid."}

        holding_days = _number(getattr(result, "expected_holding_days", None))
        if holding_days is None or holding_days <= 0:
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Expected holding period is unavailable."}
        max_holding_hours = min(90 * 24, max(1, int(holding_days * 24)))
        funding_interval = float(market["funding_interval_hours"])
        if funding_interval <= 0:
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Funding interval is invalid; entry is blocked."}
        expected_funding_events = max(1, math.ceil(max_holding_hours / funding_interval))
        # Funding rates can change. Budget the absolute current rate as an adverse
        # per-settlement charge rather than assuming the bot will receive funding.
        estimated_adverse_funding_per_unit = (
            abs(float(market["funding_rate"])) * entry * expected_funding_events
        )
        stop_exit = float(stop) * (1.0 - sign * self.slippage_rate)
        target_exit = float(target) * (1.0 - sign * self.slippage_rate)
        unit_risk = (
            abs(entry - stop_exit) + entry * self.fee_rate + stop_exit * self.fee_rate
            + estimated_adverse_funding_per_unit
        )
        unit_reward = (
            sign * (target_exit - entry) - entry * self.fee_rate - target_exit * self.fee_rate
            - estimated_adverse_funding_per_unit
        )
        net_rr = unit_reward / unit_risk if unit_risk > 0 else None
        if unit_risk <= 0 or unit_reward <= 0 or net_rr is None or net_rr < self.min_net_reward_risk:
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Net reward/risk after simulated fees and slippage is below the minimum.",
                    "net_reward_risk": round(net_rr, 4) if net_rr is not None else None}

        risk_budget = equity * self.risk_per_trade_pct
        notional_cap = equity * self.max_position_notional_pct * self.leverage
        quantity = min(
            risk_budget / unit_risk,
            notional_cap / entry,
            available * self.leverage / entry,
        )
        notional = quantity * entry
        margin = notional / self.leverage
        entry_fee = notional * self.fee_rate
        if not math.isfinite(quantity) or quantity <= 0 or margin > available or entry_fee >= available:
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Position sizing or isolated margin is outside available paper equity."}

        funding_history = getattr(data, "funding_frames", {}).get(asset.raw)
        if funding_history is None or getattr(funding_history, "empty", True):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Cross-validated historical funding is mandatory; missing funding is never assumed to be zero."}

        try:
            ai_review = self.ai_reviewer(result, asset)
        except Exception as exc:
            ai_review = {"status": "UNKNOWN", "paper_entry_eligible": False,
                         "error_type": type(exc).__name__}
        if not isinstance(ai_review, dict) or str(ai_review.get("status", "")).upper() != "APPROVED" or ai_review.get("paper_entry_eligible") is not True:
            return {
                "status": "AI_BLOCKED", "asset": asset.raw,
                "reason": "Direction-aware AI second opinion did not approve this futures paper entry.",
                "ai_review": ai_review if isinstance(ai_review, dict) else {"status": "UNKNOWN"},
                "mode": "FUTURES_PAPER_ONLY", "orders_submitted": False,
            }

        # The AI round-trip can take several seconds. Re-read the independent
        # exchange quotes and refuse stale entry assumptions after that delay.
        try:
            refreshed_market = self._market(asset)
        except Exception as exc:
            return {
                "status": "NO_TRADE", "asset": asset.raw,
                "reason": f"Perpetual quote refresh failed after AI review ({type(exc).__name__}); entry canceled.",
                "ai_review": ai_review,
            }
        quote_drift = max(
            abs(float(refreshed_market[key]) - float(market[key])) / float(market[key])
            for key in ("mark_price", "best_bid", "best_ask")
        )
        funding_drift = abs(float(refreshed_market["funding_rate"]) - float(market["funding_rate"]))
        if quote_drift > 0.0005 or funding_drift > 0.0002:
            return {
                "status": "NO_TRADE", "asset": asset.raw,
                "reason": "Perpetual quote or funding changed materially during AI review; a new analysis cycle is required.",
                "quote_drift_bps": round(quote_drift * 10_000, 3),
                "funding_drift_bps": round(funding_drift * 10_000, 3),
                "ai_review": ai_review,
            }

        now = int(time.time())
        position_record = {
            "asset": asset.raw, "direction": direction, "quantity": quantity,
            "entry_price": entry, "stop_loss": stop, "take_profit": target,
            "leverage": self.leverage, "initial_margin": margin, "entry_fee": entry_fee,
            "opened_at": now, "signal_bar": signal_bar,
            "confirmation_score": score, "risk_budget": risk_budget,
            "max_holding_hours": max_holding_hours,
            "last_funding_time_ms": now * 1000,
            "last_mark_at_ms": int(market["timestamp_ms"]),
        }
        if not self.ledger.open_position(position_record):
            return {"status": "NO_TRADE", "asset": asset.raw,
                    "reason": "Futures paper ledger rejected duplicate or concurrent entry."}
        return {
            "status": "OPENED", "asset": asset.raw, "direction": direction,
            "quantity": quantity, "entry_price": entry, "stop_loss": stop,
            "take_profit": target, "leverage": self.leverage,
            "initial_margin_usd": margin, "risk_budget_usd": risk_budget,
            "net_reward_risk": round(net_rr, 4),
            "confirmation_score": score, "estimated_risk_score": risk,
            "funding_rate": market["funding_rate"],
            "funding_interval_hours": market["funding_interval_hours"],
            "expected_funding_events": expected_funding_events,
            "estimated_adverse_funding_per_unit": estimated_adverse_funding_per_unit,
            "quote_drift_bps_after_ai_review": round(quote_drift * 10_000, 3),
            "risk_controls": controls, "ai_review": ai_review,
            "mode": "FUTURES_PAPER_ONLY", "orders_submitted": False,
            "liquidation_model_status": "NOT_ESTIMATED_EXCHANGE_RISK_TIER_REQUIRED",
        }
