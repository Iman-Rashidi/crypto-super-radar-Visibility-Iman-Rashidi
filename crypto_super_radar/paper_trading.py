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
from .database import connect_database, storage_backend
from .signal_performance import summarize_closed_trades
from .execution_costs import calculate_fill_costs
from .paper_execution import PaperExecutionEventLedger, make_paper_client_order_id
from .paper_execution_adapter import PaperExecutionAdapter
from .paper_fill_simulator import PaperFillSimulator


PAPER_ASSETS = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _status(value: Any) -> str:
    return str(getattr(value, "value", value)).upper()


def _extract_observed_quote(snapshot: Any, *, max_age_ms: int = 60_000) -> Optional[dict[str, Any]]:
    """Read a fresh, source-attributed CEX bid/ask pair from a market snapshot."""
    raw = getattr(snapshot, "raw", None)
    if not isinstance(raw, dict):
        return None
    now_ms = int(time.time() * 1000)
    for key in ("provider_a", "provider_b"):
        provider = raw.get(key)
        if not isinstance(provider, dict):
            continue
        bid = _number(provider.get("bid_price"))
        ask = _number(provider.get("ask_price"))
        timestamp = _number(provider.get("quote_timestamp_ms"))
        if (
            bid is None or ask is None or timestamp is None
            or bid <= 0 or ask < bid or timestamp <= 0
        ):
            continue
        age_ms = now_ms - int(timestamp)
        if age_ms < -5_000 or age_ms > max_age_ms:
            continue
        source = provider.get("provider")
        if not isinstance(source, str) or not source.strip():
            source = str(getattr(snapshot, "provider_a" if key == "provider_a" else "provider_b", key))
        return {
            "bid": bid, "ask": ask, "timestamp_ms": int(timestamp),
            "age_ms": age_ms, "source": source,
        }
    return None


def _extract_order_book(
    data: Any, asset: AssetSpec, *, max_age_ms: int = 5_000,
) -> Optional[dict[str, Any]]:
    """Return a fresh, validated public Binance Spot top-of-book snapshot."""
    getter = getattr(data, "order_book_snapshot", None)
    if not callable(getter):
        return None
    try:
        raw = getter(asset)
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("symbol") != asset.symbol:
        return None
    if raw.get("timestamp_kind") != "LOCAL_RECEIVED_AT" or raw.get("orders_submitted") is not False:
        return None
    bid = _number(raw.get("best_bid"))
    ask = _number(raw.get("best_ask"))
    bid_qty = _number(raw.get("available_liquidity_sell_qty"))
    ask_qty = _number(raw.get("available_liquidity_buy_qty"))
    observed_at = _number(raw.get("observed_at_ms"))
    source = raw.get("source")
    if (
        bid is None or ask is None or bid_qty is None or ask_qty is None
        or observed_at is None or bid <= 0 or ask < bid
        or bid_qty < 0 or ask_qty < 0 or observed_at <= 0
        or not isinstance(source, str) or not source.strip()
    ):
        return None
    age_ms = int(time.time() * 1000) - int(observed_at)
    if age_ms < -2_000 or age_ms > max_age_ms:
        return None
    return {
        "symbol": asset.symbol, "bid": bid, "ask": ask,
        "available_sell_quantity": bid_qty, "available_buy_quantity": ask_qty,
        "observed_at_ms": int(observed_at), "age_ms": age_ms,
        "source": source.strip(), "timestamp_kind": "LOCAL_RECEIVED_AT",
        "orders_submitted": False,
    }


class PaperLedger:
    """Minimal paper account and immutable closed-trade journal in SQLite."""

    def __init__(self, path: Optional[str] = None, initial_equity_usd: float = 10_000.0):
        if not math.isfinite(initial_equity_usd) or initial_equity_usd <= 0:
            raise ValueError("Initial paper equity must be a positive finite number.")
        self.path = path or os.getenv("CSR_DB_PATH", "data/crypto_super_radar.sqlite3")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = connect_database(self.path)
        with self._db:
            self.execution_ledger = PaperExecutionEventLedger(self._db, lock=self._lock)
            self.execution_adapter = PaperExecutionAdapter(self.execution_ledger)
            self.fill_simulator = PaperFillSimulator(self.execution_ledger)
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
                    peak_price REAL NOT NULL DEFAULT 0,
                    entry_reference_price REAL,
                    entry_slippage_usd REAL,
                    entry_spread_cost_usd REAL,
                    entry_quote_source TEXT
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
                    initial_stop_loss REAL NOT NULL DEFAULT 0,
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
                    entry_reference_price REAL,
                    exit_reference_price REAL,
                    entry_slippage_usd REAL,
                    exit_slippage_usd REAL,
                    spread_cost_usd REAL,
                    entry_spread_cost_usd REAL,
                    exit_spread_cost_usd REAL,
                    entry_quote_source TEXT,
                    exit_quote_source TEXT,
                    funding_cost_usd REAL NOT NULL DEFAULT 0,
                    funding_status TEXT NOT NULL DEFAULT 'NOT_APPLICABLE',
                    UNIQUE(asset, signal_bar)
                )
            """)
            trade_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(paper_trades)")
            }
            if "initial_stop_loss" not in trade_columns:
                self._db.execute(
                    "ALTER TABLE paper_trades ADD COLUMN initial_stop_loss REAL NOT NULL DEFAULT 0"
                )
            for name, declaration in (
                ("entry_reference_price", "REAL"),
                ("exit_reference_price", "REAL"),
                ("entry_slippage_usd", "REAL"),
                ("exit_slippage_usd", "REAL"),
                ("spread_cost_usd", "REAL"),
                ("entry_spread_cost_usd", "REAL"),
                ("exit_spread_cost_usd", "REAL"),
                ("entry_quote_source", "TEXT"),
                ("exit_quote_source", "TEXT"),
                ("funding_cost_usd", "REAL NOT NULL DEFAULT 0"),
                ("funding_status", "TEXT NOT NULL DEFAULT 'NOT_APPLICABLE'"),
            ):
                if name not in trade_columns:
                    self._db.execute(
                        f"ALTER TABLE paper_trades ADD COLUMN {name} {declaration}"
                    )
            position_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(paper_positions)")
            }
            for name, declaration in (
                ("initial_stop_loss", "REAL NOT NULL DEFAULT 0"),
                ("max_holding_days", "INTEGER NOT NULL DEFAULT 14"),
                ("peak_price", "REAL NOT NULL DEFAULT 0"),
                ("entry_reference_price", "REAL"),
                ("entry_slippage_usd", "REAL"),
                ("entry_spread_cost_usd", "REAL"),
                ("entry_quote_source", "TEXT"),
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
        entry_order_id = make_paper_client_order_id(asset, signal_bar, "ENTRY")
        execution_order = self.execution_ledger.order_state(entry_order_id)
        return bool(closed or opened or execution_order)

    def _insert_open_position_projection(
        self, position: dict[str, Any], debit: float,
    ) -> bool:
        """Apply the filled entry to cash/position; caller owns the DB transaction."""
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
             initial_stop_loss, max_holding_days, peak_price,
             entry_reference_price, entry_slippage_usd, entry_spread_cost_usd,
             entry_quote_source)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            position["asset"], position["quantity"], position["entry_price"],
            position["stop_loss"], position["take_profit"], position["entry_fee"],
            position["opened_at"], position["signal_bar"],
            position["confirmation_score"], position["risk_budget"],
            position["entry_price"], position["initial_stop_loss"],
            position["max_holding_days"], position["entry_price"],
            position.get("entry_reference_price"),
            position.get("entry_slippage_usd"),
            position.get("entry_spread_cost_usd"),
            position.get("entry_quote_source"),
        ))
        self._db.execute(
            "UPDATE paper_account SET cash_balance=cash_balance-?, updated_at=? WHERE id=1",
            (debit, int(time.time())),
        )
        return True

    def open_position(self, position: dict[str, Any], debit: float) -> bool:
        """Legacy atomic immediate-fill entry, retained for existing callers/tests."""
        with self._lock, self._db:
            if not self._insert_open_position_projection(position, debit):
                return False
            self.execution_ledger.record_simulated_fill(
                client_order_id=make_paper_client_order_id(
                    position["asset"], position["signal_bar"], "ENTRY"
                ),
                asset=position["asset"], side="BUY", quantity=position["quantity"],
                fill_price=position["entry_price"], fee_paid_usd=position["entry_fee"],
                source=(
                    "OBSERVED_QUOTE_PAPER_FILL"
                    if position.get("entry_quote_source") else "MODELLED_PAPER_FILL"
                ),
                reason="PAPER_ENTRY", occurred_at=position["opened_at"],
            )
        return True

    def execute_entry_order(
        self, *,
        position: dict[str, Any],
        client_order_id: str,
        requested_quantity: float,
        order_book: dict[str, Any],
        fee_bps: float,
        adverse_impact_bps: float,
        max_participation_rate: float,
        fill_fractions: tuple[float, ...],
        max_quote_age_ms: int,
        simulated_latency_ms: int = 200,
    ) -> dict[str, Any]:
        """Simulate adapter-accepted entry fills and atomically project actual fill."""
        with self._lock, self._db:
            self.execution_adapter.submit_order(
                client_order_id=client_order_id, asset=position["asset"],
                side="BUY", quantity=requested_quantity,
                occurred_at=position["opened_at"],
            )
            simulation = self.fill_simulator.simulate_order(
                client_order_id=client_order_id,
                asset=position["asset"],
                side="BUY",
                requested_quantity=requested_quantity,
                bid=order_book["bid"],
                ask=order_book["ask"],
                available_liquidity_quantity=order_book["available_buy_quantity"],
                max_participation_rate=max_participation_rate,
                fee_bps=fee_bps,
                adverse_impact_bps=adverse_impact_bps,
                simulated_latency_ms=simulated_latency_ms,
                fill_fractions=fill_fractions,
                quote_quality="OBSERVED",
                quote_source=order_book["source"],
                quote_timestamp_ms=order_book["observed_at_ms"],
                max_quote_age_ms=max_quote_age_ms,
                cancel_unfilled_remainder=False,
                reason="PAPER_BOT_ENTRY",
            )
            requested = float(requested_quantity)
            filled = float(simulation["filled_quantity"])
            remaining = float(simulation["remaining_quantity"])
            tolerance = max(1e-12, requested * 1e-12)
            if remaining > tolerance:
                self.execution_adapter.cancel_order(
                    client_order_id=client_order_id,
                    reason="PAPER_ENTRY_UNFILLED_REMAINDER_CANCELED",
                    occurred_at=int(time.time()),
                )
            if filled <= tolerance:
                return {
                    "status": "CANCELED", "client_order_id": client_order_id,
                    "requested_quantity": requested, "filled_quantity": 0.0,
                    "remaining_quantity": requested, "average_fill_price": None,
                    "fee_paid_usd": 0.0, "simulation": simulation,
                    "mode": "PAPER_ONLY", "orders_submitted": False,
                }

            average_price = _number(simulation.get("average_fill_price"))
            cumulative_fee = _number(simulation.get("cumulative_fee_paid_usd"))
            if average_price is None or average_price <= 0 or cumulative_fee is None or cumulative_fee < 0:
                raise RuntimeError("Paper fill simulator returned incomplete cumulative fill economics")
            applied = dict(position)
            applied["quantity"] = filled
            applied["entry_price"] = average_price
            applied["entry_fee"] = cumulative_fee
            costs = calculate_fill_costs(
                side="BUY", quantity=filled,
                bid=order_book["bid"], ask=order_book["ask"],
                fill_price=average_price, fee_paid_usd=cumulative_fee,
                funding_applicable=False, quote_quality="OBSERVED",
            )
            applied["entry_reference_price"] = order_book["ask"]
            applied["entry_slippage_usd"] = costs["slippage_cost_usd"]
            applied["entry_spread_cost_usd"] = costs["spread_cost_usd"]
            applied["entry_quote_source"] = order_book["source"]
            debit = filled * average_price + cumulative_fee
            if not self._insert_open_position_projection(applied, debit):
                raise RuntimeError("Paper ledger rejected filled entry projection; execution transaction rolled back")
            return {
                "status": simulation["status"], "client_order_id": client_order_id,
                "requested_quantity": requested, "filled_quantity": filled,
                "remaining_quantity": remaining, "average_fill_price": average_price,
                "fee_paid_usd": cumulative_fee, "debit_usd": debit,
                "position": applied, "simulation": simulation,
                "mode": "PAPER_ONLY", "orders_submitted": False,
            }

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

    def close_position(
        self,
        exit_price: float,
        exit_fee_rate: float,
        reason: str,
        *,
        exit_reference_price: Optional[float] = None,
        exit_bid: Optional[float] = None,
        exit_ask: Optional[float] = None,
        exit_quote_source: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM paper_positions WHERE id=1").fetchone()
            if row is None:
                return None
            p = dict(row)
            closed_at = int(time.time())
            exit_fee = p["quantity"] * exit_price * exit_fee_rate
            gross = p["quantity"] * (exit_price - p["entry_price"])
            net = gross - p["entry_fee"] - exit_fee
            reference_exit = _number(exit_reference_price)
            normalized_bid = _number(exit_bid)
            normalized_ask = _number(exit_ask)
            if normalized_bid is not None and normalized_ask is not None:
                exit_costs = calculate_fill_costs(
                    side="SELL", quantity=p["quantity"], bid=normalized_bid,
                    ask=normalized_ask, fill_price=exit_price,
                    fee_paid_usd=exit_fee, funding_applicable=False,
                    quote_quality="OBSERVED",
                )
                exit_slippage = exit_costs["slippage_cost_usd"]
                exit_spread = exit_costs["spread_cost_usd"]
                normalized_quote_source = (
                    exit_quote_source.strip()
                    if isinstance(exit_quote_source, str) and exit_quote_source.strip()
                    else None
                )
            else:
                exit_slippage = (
                    max(0.0, (reference_exit - exit_price) * p["quantity"])
                    if reference_exit is not None and reference_exit > 0
                    else None
                )
                exit_spread = None
                normalized_quote_source = None
            entry_spread = _number(p.get("entry_spread_cost_usd"))
            total_spread = (
                entry_spread + exit_spread
                if entry_spread is not None and exit_spread is not None
                else None
            )
            self._db.execute(
                "UPDATE paper_account SET cash_balance=cash_balance+?, updated_at=? WHERE id=1",
                (p["quantity"] * exit_price - exit_fee, int(time.time())),
            )
            self._db.execute("""
                INSERT INTO paper_trades
                (asset, side, quantity, entry_price, exit_price, stop_loss, initial_stop_loss,
                 take_profit, entry_fee, exit_fee, gross_pnl, net_pnl, opened_at, closed_at,
                 signal_bar, confirmation_score, exit_reason, entry_reference_price,
                 exit_reference_price, entry_slippage_usd, exit_slippage_usd,
                 spread_cost_usd, entry_spread_cost_usd, exit_spread_cost_usd,
                 entry_quote_source, exit_quote_source, funding_cost_usd, funding_status)
                VALUES (?, 'LONG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["asset"], p["quantity"], p["entry_price"], exit_price,
                p["stop_loss"], p.get("initial_stop_loss") or p["stop_loss"],
                p["take_profit"], p["entry_fee"], exit_fee,
                gross, net, p["opened_at"], closed_at, p["signal_bar"],
                p["confirmation_score"], reason,
                p.get("entry_reference_price"), reference_exit, p.get("entry_slippage_usd"),
                exit_slippage, total_spread, entry_spread, exit_spread,
                p.get("entry_quote_source"), normalized_quote_source,
                0.0, "NOT_APPLICABLE",
            ))
            self._db.execute("DELETE FROM paper_positions WHERE id=1")
            self.execution_ledger.record_simulated_fill(
                client_order_id=make_paper_client_order_id(
                    p["asset"], p["signal_bar"], "EXIT"
                ),
                asset=p["asset"], side="SELL", quantity=p["quantity"],
                fill_price=exit_price, fee_paid_usd=exit_fee,
                source=(
                    "OBSERVED_QUOTE_PAPER_FILL"
                    if normalized_quote_source else "MODELLED_PAPER_FILL"
                ),
                reason=f"PAPER_EXIT:{reason}", occurred_at=closed_at,
            )
        return {
            "asset": p["asset"], "gross_pnl_usd": gross, "net_pnl_usd": net,
            "exit_price": exit_price, "exit_reason": reason,
            "entry_slippage_usd": p.get("entry_slippage_usd"),
            "exit_slippage_usd": exit_slippage,
            "entry_spread_cost_usd": entry_spread,
            "exit_spread_cost_usd": exit_spread,
            "spread_cost_usd": total_spread,
            "entry_quote_source": p.get("entry_quote_source"),
            "exit_quote_source": normalized_quote_source,
            "spread_attribution": "COMPLETE" if total_spread is not None else "INCOMPLETE",
        }

    def reconciliation(self) -> dict[str, Any]:
        """Compare account projections with immutable execution events.

        Missing events on legacy rows are INCOMPLETE, never reconstructed.
        Cash consistency uses initial equity + realized PnL - open-position debit.
        """
        with self._lock:
            account = self._db.execute(
                "SELECT initial_equity, cash_balance FROM paper_account WHERE id=1"
            ).fetchone()
            position_row = self._db.execute(
                "SELECT * FROM paper_positions WHERE id=1"
            ).fetchone()
            trade_rows = self._db.execute(
                "SELECT * FROM paper_trades ORDER BY closed_at ASC, id ASC"
            ).fetchall()

        replay = self.execution_ledger.replay_order_states()
        errors: list[str] = []
        missing_events: list[str] = []
        missing_projections: list[str] = []
        matched_fills = 0
        expected_order_ids: set[str] = set()

        if account is None:
            return {
                "status": "FAIL", "errors": ["paper_account row is missing"],
                "missing_events": [], "missing_projections": [],
                "event_replay_status": replay["status"],
            }
        if replay["status"] == "FAIL":
            errors.extend(replay["errors"])

        initial = float(account["initial_equity"])
        cash = float(account["cash_balance"])
        realized = sum(float(row["net_pnl"]) for row in trade_rows)
        position = dict(position_row) if position_row else None
        open_debit = (
            float(position["quantity"]) * float(position["entry_price"])
            + float(position["entry_fee"])
            if position else 0.0
        )
        expected_cash = initial + realized - open_debit
        cash_delta = cash - expected_cash
        cash_tolerance = max(0.01, abs(expected_cash) * 1e-8)
        if abs(cash_delta) > cash_tolerance:
            errors.append(
                f"cash projection mismatch: actual={cash:.8f}, expected={expected_cash:.8f}"
            )

        def verify_fill(
            *, order_id: str, asset: str, side: str, quantity: float,
            price: float, fee: float, label: str,
        ) -> None:
            nonlocal matched_fills
            expected_order_ids.add(order_id)
            order = self.execution_ledger.order_state(order_id)
            if order is None:
                missing_events.append(f"{label}:{order_id}")
                return
            if order["status"] not in {"FILLED", "CANCELED"}:
                errors.append(
                    f"{label}:{order_id} has terminal status {order['status']}, expected FILLED or CANCELED"
                )
                return
            fill_event = next(
                (
                    event for event in self.execution_ledger.recent_events(
                        1000, client_order_id=order_id
                    )
                    if event["status"] in {"PARTIALLY_FILLED", "FILLED"}
                ),
                None,
            )
            if fill_event is None:
                errors.append(f"{label}:{order_id} has no recorded fill event")
                return
            for field, actual, expected in (
                ("asset", str(order["asset"]), asset),
                ("side", str(order["side"]), side),
                ("fill_asset", str(fill_event["asset"]), asset),
                ("fill_side", str(fill_event["side"]), side),
            ):
                if actual != expected:
                    errors.append(f"{label}:{order_id} {field} mismatch")
            requested = float(order["quantity"])
            if quantity > requested + max(1e-12, requested * 1e-9):
                errors.append(f"{label}:{order_id} projected fill exceeds accepted order quantity")
            for field, actual, expected in (
                ("filled_quantity", float(order["filled_quantity"]), quantity),
                ("fill_event_quantity", float(fill_event["filled_quantity"]), quantity),
                ("fill_price", float(fill_event["fill_price"]), price),
                ("fee_paid_usd", float(fill_event["fee_paid_usd"]), fee),
            ):
                if not math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-8):
                    errors.append(f"{label}:{order_id} {field} mismatch")
            if not math.isclose(
                float(order["filled_quantity"]),
                float(fill_event["filled_quantity"]),
                rel_tol=1e-9, abs_tol=1e-12,
            ):
                errors.append(f"{label}:{order_id} final order state differs from last fill event")
            if not any(message.startswith(f"{label}:{order_id} ") for message in errors):
                matched_fills += 1

        for row in trade_rows:
            trade = dict(row)
            asset = str(trade["asset"])
            signal_bar = str(trade["signal_bar"])
            quantity = float(trade["quantity"])
            verify_fill(
                order_id=make_paper_client_order_id(asset, signal_bar, "ENTRY"),
                asset=asset, side="BUY", quantity=quantity,
                price=float(trade["entry_price"]), fee=float(trade["entry_fee"]),
                label=f"trade-{trade['id']}-entry",
            )
            verify_fill(
                order_id=make_paper_client_order_id(asset, signal_bar, "EXIT"),
                asset=asset, side="SELL", quantity=quantity,
                price=float(trade["exit_price"]), fee=float(trade["exit_fee"]),
                label=f"trade-{trade['id']}-exit",
            )

        if position:
            asset = str(position["asset"])
            signal_bar = str(position["signal_bar"])
            quantity = float(position["quantity"])
            entry_id = make_paper_client_order_id(asset, signal_bar, "ENTRY")
            exit_id = make_paper_client_order_id(asset, signal_bar, "EXIT")
            verify_fill(
                order_id=entry_id, asset=asset, side="BUY", quantity=quantity,
                price=float(position["entry_price"]), fee=float(position["entry_fee"]),
                label="open-position-entry",
            )
            exit_event = self.execution_ledger.order_state(exit_id)
            if exit_event is not None and exit_event["status"] == "FILLED":
                errors.append(f"open position still exists after filled exit order {exit_id}")

        for order in replay["orders"].values():
            order_id = str(order["client_order_id"])
            source = str(order.get("source") or "")
            is_paper_projection_event = (
                order_id.startswith(("paper-entry-", "paper-exit-"))
                or source.startswith((
                    "PAPER_SIMULATOR", "PAPER_FILL_SIMULATOR",
                    "PAPER_EXECUTION_ADAPTER", "PAPER_ORDER_BOOK_SNAPSHOT",
                ))
            )
            if is_paper_projection_event and order_id not in expected_order_ids:
                zero_fill_terminal = (
                    order.get("status") in {"CANCELED", "REJECTED"}
                    and float(order.get("filled_quantity") or 0.0) <= 1e-12
                )
                if not zero_fill_terminal:
                    missing_projections.append(order_id)

        if errors:
            result_status = "FAIL"
        elif missing_events or missing_projections:
            result_status = "INCOMPLETE"
        elif not trade_rows and not position and not expected_order_ids:
            result_status = "NO_DATA"
        else:
            result_status = "PASS"

        return {
            "status": result_status,
            "positions_checked": 1 if position else 0,
            "closed_trades_checked": len(trade_rows),
            "matched_fills": matched_fills,
            "event_count": replay["event_count"],
            "event_replay_status": replay["status"],
            "cash_balance_usd": round(cash, 8),
            "expected_cash_balance_usd": round(expected_cash, 8),
            "cash_delta_usd": round(cash_delta, 8),
            "missing_events": missing_events,
            "missing_projections": missing_projections,
            "errors": errors,
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            account = self._db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            position = self._db.execute("SELECT * FROM paper_positions WHERE id=1").fetchone()
            trades = self._db.execute(
                "SELECT * FROM paper_trades ORDER BY closed_at DESC, id DESC LIMIT 20"
            ).fetchall()
            all_trades = self._db.execute(
                "SELECT * FROM paper_trades ORDER BY closed_at ASC, id ASC LIMIT 10000"
            ).fetchall()
            realized_total = self._db.execute(
                "SELECT COALESCE(SUM(net_pnl), 0.0) AS total FROM paper_trades"
            ).fetchone()["total"]
        cash = float(account["cash_balance"])
        initial = float(account["initial_equity"])
        performance = summarize_closed_trades(
            [dict(row) for row in all_trades],
            initial_equity_usd=initial,
            min_sample_size=30,
        )
        open_position = dict(position) if position else None
        unrealized = 0.0
        equity = cash
        if open_position:
            mark = float(open_position["mark_price"])
            qty = float(open_position["quantity"])
            equity += qty * mark
            unrealized = (
                qty * (mark - float(open_position["entry_price"]))
                - float(open_position["entry_fee"])
            )
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
            "execution_ledger": {
                "event_count": self.execution_ledger.event_count(),
                "recent_orders": self.execution_ledger.latest_orders(20),
                "append_only": True,
                "mode": "PAPER_ONLY",
            },
            "reconciliation": self.reconciliation(),
            "performance": performance,
            "execution_cost_notes": {
                "entry_exit_slippage": "Observed against fresh best ask/bid when available; otherwise modelled versus the cross-validated reference price. Already embedded in fills and not deducted twice.",
                "spread": "Recorded per fill when a fresh timestamped bid/ask quote is available; missing quote coverage remains INCOMPLETE.",
                "funding": "NOT_APPLICABLE for this Spot-only paper ledger. Perpetual backtests require complete historical funding.",
            },
            "storage": storage_backend(),
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
        max_order_participation_rate: float = 0.10,
        fill_fractions: tuple[float, ...] = (0.40, 0.35, 0.25),
        max_order_book_age_ms: int = 5_000,
        simulated_execution_latency_ms: int = 200,
    ):
        self.engine = engine
        self.ledger = ledger or PaperLedger()
        values = (risk_per_trade_pct, max_position_notional_pct, max_risk_score,
                  min_confirmation_score, fee_bps_per_side, slippage_bps_per_side,
                  daily_loss_limit_pct, max_drawdown_pct, cooldown_hours_after_loss,
                  min_net_reward_risk, max_order_participation_rate)
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
        if not 0 < max_order_participation_rate <= 1:
            raise ValueError("Order-book participation rate must be in (0, 1].")
        if (
            not isinstance(max_order_book_age_ms, int)
            or isinstance(max_order_book_age_ms, bool)
            or max_order_book_age_ms <= 0
        ):
            raise ValueError("Maximum order-book age must be a positive integer.")
        if (
            not isinstance(simulated_execution_latency_ms, int)
            or isinstance(simulated_execution_latency_ms, bool)
            or simulated_execution_latency_ms < 0
        ):
            raise ValueError("Simulated execution latency must be a non-negative integer.")
        fractions = tuple(float(value) for value in fill_fractions)
        if (
            not fractions or len(fractions) > 20
            or any(not math.isfinite(value) or value <= 0 for value in fractions)
            or not math.isclose(sum(fractions), 1.0, rel_tol=1e-9, abs_tol=1e-9)
        ):
            raise ValueError("Fill fractions must be positive and sum to 1.0.")
        self.max_order_participation_rate = max_order_participation_rate
        self.fill_fractions = fractions
        self.max_order_book_age_ms = max_order_book_age_ms
        self.simulated_execution_latency_ms = simulated_execution_latency_ms

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
            exit_quote = _extract_observed_quote(snapshot)
            liquidation_mark = (
                min(price, float(exit_quote["bid"])) if exit_quote is not None else price
            )
            self.ledger.update_management(liquidation_mark, managed_stop)
            managed_position = self.ledger.position() or position
            active_stop = float(managed_position["stop_loss"])
            if price <= active_stop:
                reference_exit = min(price, active_stop)
                sell_reference = (
                    min(reference_exit, float(exit_quote["bid"]))
                    if exit_quote is not None else reference_exit
                )
                fill = sell_reference * (1.0 - self.slippage_rate)
                initial_stop = float(managed_position.get("initial_stop_loss") or active_stop)
                exit_reason = "TRAILING_STOP" if active_stop > initial_stop else "STOP_LOSS"
                closed = self.ledger.close_position(
                    fill, self.fee_rate, exit_reason,
                    exit_reference_price=reference_exit,
                    exit_bid=exit_quote["bid"] if exit_quote else None,
                    exit_ask=exit_quote["ask"] if exit_quote else None,
                    exit_quote_source=exit_quote["source"] if exit_quote else None,
                )
                return {"status": "CLOSED", "asset": asset.raw, "trade": closed}
            if price >= float(position["take_profit"]):
                reference_exit = float(position["take_profit"])
                sell_reference = (
                    min(reference_exit, float(exit_quote["bid"]))
                    if exit_quote is not None else reference_exit
                )
                fill = sell_reference * (1.0 - self.slippage_rate)
                closed = self.ledger.close_position(
                    fill, self.fee_rate, "TAKE_PROFIT",
                    exit_reference_price=reference_exit,
                    exit_bid=exit_quote["bid"] if exit_quote else None,
                    exit_ask=exit_quote["ask"] if exit_quote else None,
                    exit_quote_source=exit_quote["source"] if exit_quote else None,
                )
                return {"status": "CLOSED", "asset": asset.raw, "trade": closed}
            max_holding = max(1, int(position.get("max_holding_days") or 14))
            if int(time.time()) - int(position["opened_at"]) >= max_holding * 86_400:
                reference_exit = price
                sell_reference = (
                    min(reference_exit, float(exit_quote["bid"]))
                    if exit_quote is not None else reference_exit
                )
                fill = sell_reference * (1.0 - self.slippage_rate)
                closed = self.ledger.close_position(
                    fill, self.fee_rate, "TIME_EXIT",
                    exit_reference_price=reference_exit,
                    exit_bid=exit_quote["bid"] if exit_quote else None,
                    exit_ask=exit_quote["ask"] if exit_quote else None,
                    exit_quote_source=exit_quote["source"] if exit_quote else None,
                )
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
        entry_quote = _extract_order_book(
            data, asset, max_age_ms=self.max_order_book_age_ms,
        )
        if entry_quote is None:
            return {
                "status": "UNKNOWN", "asset": asset.raw,
                "reason": "A complete, fresh public Spot order-book snapshot is required; no paper order was created.",
            }
        entry_reference = float(entry_quote["ask"])
        entry = entry_reference * (1.0 + self.slippage_rate)
        half_spread_per_unit = (
            (float(entry_quote["ask"]) - float(entry_quote["bid"])) / 2.0
        )
        exit_at_stop = (
            (stop - half_spread_per_unit) * (1.0 - self.slippage_rate)
        )
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
        target_fill = (target - half_spread_per_unit) * (1.0 - self.slippage_rate)
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
        entry_costs = calculate_fill_costs(
            side="BUY", quantity=quantity, bid=entry_quote["bid"],
            ask=entry_quote["ask"], fill_price=entry,
            fee_paid_usd=entry_fee, funding_applicable=False,
            quote_quality="OBSERVED",
        )
        entry_slippage = entry_costs["slippage_cost_usd"]
        entry_spread_cost = entry_costs["spread_cost_usd"]
        entry_quote_source = entry_quote["source"]
        record = {
            "asset": asset.raw, "quantity": quantity, "entry_price": entry,
            "stop_loss": stop, "initial_stop_loss": stop,
            "take_profit": target, "entry_fee": entry_fee,
            "opened_at": int(time.time()), "signal_bar": signal_bar,
            "confirmation_score": score, "risk_budget": risk_budget,
            "max_holding_days": max_holding_days,
            "entry_reference_price": entry_reference,
            "entry_slippage_usd": entry_slippage,
            "entry_spread_cost_usd": entry_spread_cost,
            "entry_quote_source": entry_quote_source,
        }
        client_order_id = make_paper_client_order_id(asset.raw, signal_bar, "ENTRY")
        try:
            execution = self.ledger.execute_entry_order(
                position=record,
                client_order_id=client_order_id,
                requested_quantity=quantity,
                order_book=entry_quote,
                fee_bps=self.fee_rate * 10_000.0,
                adverse_impact_bps=self.slippage_rate * 10_000.0,
                max_participation_rate=self.max_order_participation_rate,
                fill_fractions=self.fill_fractions,
                max_quote_age_ms=self.max_order_book_age_ms,
                simulated_latency_ms=self.simulated_execution_latency_ms,
            )
        except Exception as exc:
            return {
                "status": "UNKNOWN", "asset": asset.raw,
                "reason": f"Paper entry execution/projection failed atomically ({type(exc).__name__}).",
            }
        if float(execution["filled_quantity"]) <= 0:
            return {
                "status": "NO_TRADE", "asset": asset.raw,
                "reason": "The public order book supplied no executable liquidity; the paper order remainder was canceled.",
                "client_order_id": client_order_id,
                "execution_status": execution["status"],
                "remaining_quantity": execution["remaining_quantity"],
                "mode": "PAPER_ONLY", "orders_submitted": False,
            }
        return {
            "status": "OPENED" if execution["status"] == "FILLED" else "PARTIALLY_OPENED",
            "asset": asset.raw, "side": "LONG",
            "quantity": execution["filled_quantity"],
            "requested_quantity": quantity,
            "remaining_quantity": execution["remaining_quantity"],
            "entry_price": execution["average_fill_price"],
            "stop_loss": stop, "take_profit": target,
            "risk_budget_usd": risk_budget,
            "confirmation_score": score, "net_reward_risk": round(net_reward_risk, 3),
            "max_holding_days": max_holding_days,
            "fee_usd": execution["fee_paid_usd"],
            "client_order_id": client_order_id,
            "execution_status": execution["status"],
            "execution_fill_count": execution["simulation"]["fill_count"],
            "risk_controls": controls, "mode": "PAPER_ONLY", "orders_submitted": False,
        }
