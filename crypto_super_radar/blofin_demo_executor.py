"""Guarded BloFin Demo Futures executor.

This module has no production exchange host, no transfer/withdrawal methods,
and no default order capability. A real Demo order requires both an explicit
per-call opt-in and BLOFIN_DEMO_TRADING_ENABLED=true, separate Demo trade
credentials, and durable Turso-backed event storage.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping

import requests

from .blofin_demo_readonly import (
    BloFinDemoError,
    BloFinDemoHTTPError,
    BloFinDemoReadOnly,
    BloFinDemoResponseError,
    DEMO_REST_URL,
    _check_response_codes,
    _signature,
)
from .database import connect_database, storage_backend


class BloFinDemoExecutionBlocked(RuntimeError):
    """A safety gate prevented Demo execution."""


class BloFinDemoExecutionUnknown(RuntimeError):
    """An order request may have reached BloFin; reconcile before any retry."""


_TRADE_WRITE_LOCK = threading.Lock()
_TRADE_WRITE_TIMES: deque[float] = deque()


def _wait_for_trade_write_slot() -> None:
    """Keep this process below BloFin's 30 requests/10s trading endpoint cap."""
    while True:
        with _TRADE_WRITE_LOCK:
            now = time.monotonic()
            while _TRADE_WRITE_TIMES and now - _TRADE_WRITE_TIMES[0] >= 10.0:
                _TRADE_WRITE_TIMES.popleft()
            if len(_TRADE_WRITE_TIMES) < 25:
                _TRADE_WRITE_TIMES.append(now)
                return
            delay = max(0.05, 10.0 - (now - _TRADE_WRITE_TIMES[0]) + 0.05)
        time.sleep(delay)


def _finite(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{field} is outside the permitted numeric range")
    return number


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} is not a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _analysis_record(signal: Any) -> dict[str, Any]:
    if hasattr(signal, "to_record") and callable(signal.to_record):
        record = signal.to_record()
    elif isinstance(signal, Mapping):
        record = dict(signal)
    else:
        raise ValueError("signal must be an AnalysisResult or mapping")
    if not isinstance(record, dict):
        raise ValueError("signal record is invalid")
    return record


@dataclass(frozen=True)
class BloFinDemoRiskPolicy:
    risk_per_trade_pct: float = 0.0025
    daily_loss_limit_pct: float = 0.01
    max_drawdown_pct: float = 0.05
    max_margin_usage_pct: float = 0.10
    max_leverage: int = 3
    max_concurrent_positions: int = 1
    min_confirmation_score: float = 75.0
    max_risk_score: float = 45.0
    min_reward_risk: float = 1.8
    max_entry_deviation_pct: float = 0.005

    def __post_init__(self) -> None:
        values = (
            self.risk_per_trade_pct, self.daily_loss_limit_pct, self.max_drawdown_pct,
            self.max_margin_usage_pct, self.min_confirmation_score, self.max_risk_score,
            self.min_reward_risk, self.max_entry_deviation_pct,
        )
        if not all(math.isfinite(float(v)) for v in values):
            raise ValueError("Risk policy values must be finite")
        if not 0 < self.risk_per_trade_pct <= 0.005:
            raise ValueError("Per-trade risk must be in (0, 0.5%]")
        if not 0 < self.daily_loss_limit_pct <= 0.03:
            raise ValueError("Daily loss limit must be in (0, 3%]")
        if not 0 < self.max_drawdown_pct <= 0.15:
            raise ValueError("Maximum drawdown must be in (0, 15%]")
        if not 0 < self.max_margin_usage_pct <= 0.25:
            raise ValueError("Margin usage must be in (0, 25%]")
        if isinstance(self.max_leverage, bool) or not 1 <= int(self.max_leverage) <= 3:
            raise ValueError("Demo test leverage is restricted to 1x through 3x")
        if not 1 <= int(self.max_concurrent_positions) <= 3:
            raise ValueError("Concurrent positions must be between 1 and 3")
        if self.min_reward_risk < 1.0 or self.max_entry_deviation_pct <= 0:
            raise ValueError("Reward/risk or entry-deviation policy is invalid")


def build_demo_order_plan(
    signal: Any,
    instrument: Mapping[str, Any],
    *,
    inst_id: str,
    equity_usdt: float,
    leverage: int = 1,
    policy: BloFinDemoRiskPolicy | None = None,
) -> dict[str, Any]:
    """Validate a 12-gate signal and size USDT-linear contracts from stop risk."""
    policy = policy or BloFinDemoRiskPolicy()
    record = _analysis_record(signal)
    side = str(record.get("signal", "")).upper()
    if side not in {"LONG", "SHORT"}:
        raise BloFinDemoExecutionBlocked("Only LONG or SHORT signals may enter Demo execution.")
    if str(record.get("inst_id") or record.get("instrument_id") or inst_id) != inst_id:
        raise BloFinDemoExecutionBlocked("Signal instrument does not match the requested contract.")
    signal_id = str(record.get("signal_id") or record.get("signal_bar") or "").strip()
    if not signal_id:
        raise BloFinDemoExecutionBlocked("A stable signal_id or signal_bar is required for idempotency.")

    score = _finite(record.get("confirmation_score"), "confirmation_score")
    risk_score = _finite(record.get("estimated_risk_score"), "estimated_risk_score")
    if score < policy.min_confirmation_score or risk_score > policy.max_risk_score:
        raise BloFinDemoExecutionBlocked("Signal score/risk score did not pass the execution policy.")

    gate_pass = record.get("gate_pass_count")
    gate_fail = record.get("gate_fail_count")
    gate_unknown = record.get("gate_unknown_count")
    gates = record.get("gates")
    if isinstance(gates, list):
        gate_pass = sum(1 for g in gates if isinstance(g, Mapping) and str(g.get("status", "")).upper() == "PASS")
        gate_fail = sum(1 for g in gates if isinstance(g, Mapping) and str(g.get("status", "")).upper() == "FAIL")
        gate_unknown = sum(1 for g in gates if isinstance(g, Mapping) and str(g.get("status", "")).upper() == "UNKNOWN")
    if (gate_pass, gate_fail, gate_unknown) != (12, 0, 0):
        raise BloFinDemoExecutionBlocked("All 12 confirmation gates must PASS; FAIL/UNKNOWN blocks entry.")

    if instrument.get("instId") != inst_id or instrument.get("apiPermission") != "full":
        raise BloFinDemoExecutionBlocked("Contract is not explicitly marked apiPermission=full.")
    if instrument.get("state") != "live" or instrument.get("contractType") != "linear":
        raise BloFinDemoExecutionBlocked("Only live linear contracts are supported.")
    if instrument.get("settleCurrency") != "USDT":
        raise BloFinDemoExecutionBlocked("Only USDT-settled linear contracts are supported in this phase.")

    entry = _decimal(record.get("entry_price_usd", record.get("entry_price")), "entry")
    stop = _decimal(record.get("stop_loss_usd", record.get("stop_loss")), "stop_loss")
    target = _decimal(record.get("take_profit_usd", record.get("take_profit")), "take_profit")
    if entry <= 0 or stop <= 0 or target <= 0:
        raise BloFinDemoExecutionBlocked("Entry, stop-loss and take-profit prices must be positive.")
    if side == "LONG":
        if not stop < entry < target:
            raise BloFinDemoExecutionBlocked("LONG requires stop < entry < take-profit.")
        risk_distance, reward_distance = entry - stop, target - entry
    else:
        if not target < entry < stop:
            raise BloFinDemoExecutionBlocked("SHORT requires take-profit < entry < stop.")
        risk_distance, reward_distance = stop - entry, entry - target
    reward_risk = reward_distance / risk_distance
    if reward_risk < Decimal(str(policy.min_reward_risk)):
        raise BloFinDemoExecutionBlocked("Net reward/risk is below the configured minimum.")

    if isinstance(leverage, bool) or int(leverage) != leverage or not 1 <= int(leverage) <= policy.max_leverage:
        raise BloFinDemoExecutionBlocked("Leverage must be an integer from 1x to 3x.")
    equity = _decimal(equity_usdt, "equity")
    if equity <= 0:
        raise BloFinDemoExecutionBlocked("Futures equity must be positive.")

    contract_value = _decimal(instrument.get("contractValue"), "contractValue")
    min_size = _decimal(instrument.get("minSize"), "minSize")
    lot_size = _decimal(instrument.get("lotSize"), "lotSize")
    tick_size = _decimal(instrument.get("tickSize"), "tickSize")
    if min(contract_value, min_size, lot_size, tick_size) <= 0:
        raise BloFinDemoExecutionBlocked("Contract size/tick metadata is invalid.")

    # Round protective trigger prices in the conservative direction.
    if side == "LONG":
        # Stop is rounded toward entry to avoid increasing planned loss.
        stop = (stop / tick_size).to_integral_value(rounding=ROUND_CEILING) * tick_size
        target = (target / tick_size).to_integral_value(rounding=ROUND_CEILING) * tick_size
        risk_distance, reward_distance = entry - stop, target - entry
    else:
        # For shorts, a lower stop and lower target are the conservative tick directions.
        stop = (stop / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size
        target = (target / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size
        risk_distance, reward_distance = stop - entry, entry - target
    if risk_distance <= 0 or reward_distance / risk_distance < Decimal(str(policy.min_reward_risk)):
        raise BloFinDemoExecutionBlocked("Tick rounding made the stop/target invalid or reward/risk too small.")

    risk_budget = equity * Decimal(str(policy.risk_per_trade_pct))
    risk_per_contract = risk_distance * contract_value
    risk_quantity = risk_budget / risk_per_contract
    margin_budget = equity * Decimal(str(policy.max_margin_usage_pct))
    notional_per_contract = entry * contract_value
    margin_quantity = margin_budget * Decimal(int(leverage)) / notional_per_contract
    quantity_before_lot = min(risk_quantity, margin_quantity)
    quantity = (quantity_before_lot / lot_size).to_integral_value(rounding=ROUND_FLOOR) * lot_size
    if quantity < min_size:
        raise BloFinDemoExecutionBlocked("Risk-sized quantity is below the contract's minSize.")

    max_market_size = instrument.get("maxMarketSize")
    if max_market_size not in (None, "") and quantity > _decimal(max_market_size, "maxMarketSize"):
        quantity = (_decimal(max_market_size, "maxMarketSize") / lot_size).to_integral_value(rounding=ROUND_FLOOR) * lot_size
    if quantity < min_size:
        raise BloFinDemoExecutionBlocked("Quantity is outside the contract's market-size limits.")

    digest = hashlib.sha256(f"{signal_id}|{inst_id}|{side}".encode("utf-8")).hexdigest()
    client_order_id = digest[:32]
    return {
        "signal_id": signal_id,
        "client_order_id": client_order_id,
        "inst_id": inst_id,
        "side": side,
        "quantity": _format_decimal(quantity),
        "entry_reference_price": float(entry),
        "stop_loss": _format_decimal(stop),
        "take_profit": _format_decimal(target),
        "risk_budget_usdt": float(risk_budget),
        "risk_per_contract_usdt": float(risk_per_contract),
        "notional_usdt": float(quantity * notional_per_contract),
        "estimated_initial_margin_usdt": float(quantity * notional_per_contract / Decimal(int(leverage))),
        "leverage": int(leverage),
        "reward_risk": float(reward_distance / risk_distance),
        "position_side_long_short": side.lower(),
    }


class BloFinDemoTradeClient(BloFinDemoReadOnly):
    """Demo-only write adapter. It exposes only set-leverage and open-order methods."""

    ORDER_PATH = "/api/v1/trade/order"
    LEVERAGE_PATH = "/api/v1/account/set-leverage"
    ALLOWED_POST_PATHS = frozenset({ORDER_PATH, LEVERAGE_PATH})

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("api_key", os.getenv("BLOFIN_DEMO_TRADE_API_KEY", ""))
        kwargs.setdefault("api_secret", os.getenv("BLOFIN_DEMO_TRADE_API_SECRET", ""))
        kwargs.setdefault("passphrase", os.getenv("BLOFIN_DEMO_TRADE_PASSPHRASE", ""))
        super().__init__(**kwargs)

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if path not in self.ALLOWED_POST_PATHS:
            raise BloFinDemoExecutionBlocked("POST endpoint is not allow-listed.")
        if self.base_url != DEMO_REST_URL:
            raise BloFinDemoExecutionBlocked("Only the pinned BloFin Demo host is permitted.")
        if not all((self.api_key, self.api_secret, self.passphrase)):
            raise BloFinDemoError("Demo trade credentials are not configured.")
        timestamp = str(int(self.clock_ms()))
        nonce = str(uuid.uuid4())
        body_text = json.dumps(dict(body), separators=(",", ":"), ensure_ascii=False)
        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": _signature(self.api_secret, "POST", path, timestamp, nonce, body_text),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        _wait_for_trade_write_slot()
        try:
            response = self.session.post(
                f"{self.base_url}{path}",
                data=body_text.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 403:
                raise BloFinDemoHTTPError(403, retry_after_seconds=300.0) from exc
            if status == 429:
                retry_header = getattr(exc.response, "headers", {}).get("Retry-After")
                try:
                    delay = max(1.0, float(retry_header))
                except (TypeError, ValueError):
                    delay = 2.0
                # A POST is never blindly retried. Reconcile by clientOrderId first.
                raise BloFinDemoHTTPError(429, retry_after_seconds=delay) from exc
            raise BloFinDemoError("BloFin Demo trade request failed.") from exc
        except requests.RequestException as exc:
            raise BloFinDemoExecutionUnknown(
                "Demo order outcome is unknown; reconcile by clientOrderId before retrying."
            ) from exc
        except (TypeError, ValueError) as exc:
            raise BloFinDemoResponseError("BloFin Demo returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise BloFinDemoResponseError("BloFin Demo response must be a JSON object.")
        _check_response_codes(payload)
        if "data" not in payload:
            raise BloFinDemoResponseError("BloFin Demo response is missing data.")
        return payload

    def set_futures_leverage(
        self, inst_id: str, leverage: int, *, margin_mode: str = "isolated",
        position_side: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= int(leverage) <= 3:
            raise ValueError("Demo test leverage is restricted to 1x through 3x.")
        if margin_mode not in {"isolated", "cross"}:
            raise ValueError("margin_mode must be isolated or cross.")
        body: dict[str, Any] = {
            "instId": inst_id, "leverage": str(int(leverage)), "marginMode": margin_mode
        }
        if position_side is not None:
            if position_side not in {"net", "long", "short"}:
                raise ValueError("position_side is invalid.")
            body["positionSide"] = position_side
        return self._post(self.LEVERAGE_PATH, body)

    def place_futures_order(
        self, *, inst_id: str, side: str, position_side: str, size: str,
        client_order_id: str, take_profit: str, stop_loss: str,
        margin_mode: str = "isolated",
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", client_order_id or ""):
            raise ValueError("client_order_id must be 1-32 alphanumeric characters.")
        if side not in {"buy", "sell"} or position_side not in {"net", "long", "short"}:
            raise ValueError("Order side or position side is invalid.")
        if margin_mode not in {"isolated", "cross"}:
            raise ValueError("margin_mode must be isolated or cross.")
        qty = _decimal(size, "size")
        tp = _decimal(take_profit, "take_profit")
        sl = _decimal(stop_loss, "stop_loss")
        if min(qty, tp, sl) <= 0:
            raise ValueError("Order size and TP/SL prices must be positive.")
        body = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "positionSide": position_side,
            "side": side,
            "orderType": "market",
            "size": _format_decimal(qty),
            "clientOrderId": client_order_id,
            "tpTriggerPrice": _format_decimal(tp),
            "tpOrderPrice": "-1",
            "tpTriggerPriceType": "mark",
            "slTriggerPrice": _format_decimal(sl),
            "slOrderPrice": "-1",
            "slTriggerPriceType": "mark",
        }
        return self._post(self.ORDER_PATH, body)


class BloFinDemoEventStore:
    """Durable order reservation, append-only events and equity checkpoints."""

    def __init__(self, path: str | None = None) -> None:
        self.backend = storage_backend()
        self._db = connect_database(path)
        self._lock = threading.RLock()
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_orders (
                    client_order_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE,
                    instrument_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    exchange_order_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_execution_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    client_order_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_equity_snapshots (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    equity_usdt REAL NOT NULL,
                    sampled_at INTEGER NOT NULL,
                    utc_day TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_blofin_demo_events_order
                ON blofin_demo_execution_events(client_order_id, sequence)
            """)
            self._db.execute("""
                CREATE TRIGGER IF NOT EXISTS blofin_demo_events_no_update
                BEFORE UPDATE ON blofin_demo_execution_events
                BEGIN SELECT RAISE(ABORT, 'BloFin Demo execution events are append-only'); END
            """)
            self._db.execute("""
                CREATE TRIGGER IF NOT EXISTS blofin_demo_events_no_delete
                BEFORE DELETE ON blofin_demo_execution_events
                BEGIN SELECT RAISE(ABORT, 'BloFin Demo execution events are append-only'); END
            """)

    def _append_event(self, client_order_id: str, event_type: str, details: Mapping[str, Any]) -> None:
        now = int(time.time())
        key = f"{client_order_id}:{event_type}"
        payload = json.dumps(dict(details), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        event_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
        self._db.execute(
            """INSERT OR IGNORE INTO blofin_demo_execution_events
            (event_id, idempotency_key, client_order_id, event_type, occurred_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, key, client_order_id, event_type, now, payload),
        )

    def reserve_order(self, plan: Mapping[str, Any]) -> bool:
        now = int(time.time())
        order_id = str(plan["client_order_id"])
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT client_order_id FROM blofin_demo_orders WHERE signal_id=? OR client_order_id=?",
                (str(plan["signal_id"]), order_id),
            ).fetchone()
            if existing is not None:
                return False
            self._db.execute(
                """INSERT INTO blofin_demo_orders
                (client_order_id, signal_id, instrument_id, side, quantity, entry_price,
                 stop_loss, take_profit, leverage, status, exchange_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', NULL, ?, ?)""",
                (
                    order_id, str(plan["signal_id"]), str(plan["inst_id"]), str(plan["side"]),
                    str(plan["quantity"]), float(plan["entry_reference_price"]),
                    float(plan["stop_loss"]), float(plan["take_profit"]), int(plan["leverage"]),
                    now, now,
                ),
            )
            self._append_event(order_id, "RESERVED", {
                "instrument_id": plan["inst_id"], "side": plan["side"],
                "quantity": plan["quantity"], "leverage": plan["leverage"],
            })
            return True

    def update_order(self, client_order_id: str, status: str, *, exchange_order_id: str | None = None) -> None:
        allowed = {
            "LEVERAGE_SET", "ACCEPTED", "PARTIALLY_FILLED", "FILLED",
            "CANCELED", "REJECTED", "UNKNOWN",
        }
        if status not in allowed:
            raise ValueError("Unsupported order status update.")
        now = int(time.time())
        with self._lock, self._db:
            self._db.execute(
                """UPDATE blofin_demo_orders SET status=?, exchange_order_id=COALESCE(?, exchange_order_id),
                updated_at=? WHERE client_order_id=?""",
                (status, exchange_order_id, now, client_order_id),
            )
            self._append_event(client_order_id, status, {"exchange_order_id_present": bool(exchange_order_id)})

    def equity_guard(self, equity_usdt: float, *, now: int | None = None) -> dict[str, float]:
        equity = _finite(equity_usdt, "equity_usdt", minimum=0.0000001)
        sampled_at = int(time.time()) if now is None else int(now)
        utc_day = time.strftime("%Y-%m-%d", time.gmtime(sampled_at))
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO blofin_demo_equity_snapshots(equity_usdt, sampled_at, utc_day) VALUES (?, ?, ?)",
                (equity, sampled_at, utc_day),
            )
            peak_row = self._db.execute(
                "SELECT MAX(equity_usdt) FROM blofin_demo_equity_snapshots"
            ).fetchone()
            day_row = self._db.execute(
                "SELECT equity_usdt FROM blofin_demo_equity_snapshots WHERE utc_day=? ORDER BY sampled_at ASC, sequence ASC LIMIT 1",
                (utc_day,),
            ).fetchone()
        peak = max(equity, float(peak_row[0] or equity))
        day_open = float(day_row[0] or equity)
        return {
            "equity_usdt": equity,
            "day_open_equity_usdt": day_open,
            "peak_equity_usdt": peak,
            "daily_loss_pct": max(0.0, (day_open - equity) / day_open),
            "drawdown_pct": max(0.0, (peak - equity) / peak),
        }

    def has_any_orders(self) -> bool:
        """Return whether this one-shot Demo service has ever reserved an order."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM blofin_demo_orders LIMIT 1"
            ).fetchone()
        return row is not None

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM blofin_demo_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _equity_from_balance(balance: Any) -> float:
    if isinstance(balance, list) and balance and isinstance(balance[0], Mapping):
        balance = balance[0]
    if not isinstance(balance, Mapping):
        raise BloFinDemoExecutionBlocked("Futures balance response is unavailable.")
    for key in ("totalEquity", "equity", "accountEquity"):
        if balance.get(key) not in (None, ""):
            return _finite(balance[key], "futures_equity", minimum=0.0000001)
    raise BloFinDemoExecutionBlocked("Futures equity field was not present in the API response.")


def _active_position_count(positions: Any) -> int:
    if not isinstance(positions, list):
        raise BloFinDemoExecutionBlocked("Positions response is not a list.")
    count = 0
    for row in positions:
        if not isinstance(row, Mapping):
            raise BloFinDemoExecutionBlocked("Position row is not interpretable.")
        raw = next((row[k] for k in ("positions", "pos", "positionSize", "positionAmt") if k in row), None)
        if raw is None:
            raise BloFinDemoExecutionBlocked("Position size field is missing; refusing to guess.")
        try:
            size = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BloFinDemoExecutionBlocked("Position size field is invalid.") from exc
        if not math.isfinite(size):
            raise BloFinDemoExecutionBlocked("Position size field is non-finite.")
        if abs(size) > 1e-12:
            count += 1
    return count


def _active_order_count(orders: Any) -> int:
    if not isinstance(orders, list):
        raise BloFinDemoExecutionBlocked("Pending-orders response is not a list.")
    return len(orders)


class BloFinDemoExecutor:
    """Conservative Demo executor; it does not run automatically from dashboard scans."""

    def __init__(
        self,
        *,
        client: BloFinDemoTradeClient | None = None,
        store: BloFinDemoEventStore | None = None,
        policy: BloFinDemoRiskPolicy | None = None,
        trading_enabled: bool | None = None,
    ) -> None:
        self.client = client or BloFinDemoTradeClient()
        self.store = store or BloFinDemoEventStore()
        self.policy = policy or BloFinDemoRiskPolicy()
        configured = os.getenv("BLOFIN_DEMO_TRADING_ENABLED", "").strip().lower() == "true"
        self.trading_enabled = configured if trading_enabled is None else bool(trading_enabled)

    def reconcile_unknown_order(self, *, inst_id: str, client_order_id: str) -> dict[str, Any]:
        """Read order state by client id. This method never places or retries an order."""
        order = self.store.get_order(client_order_id)
        if order is None or order.get("instrument_id") != inst_id:
            raise BloFinDemoExecutionBlocked("No matching locally reserved order exists.")
        if order.get("status") not in {"UNKNOWN", "RESERVED", "LEVERAGE_SET"}:
            return {"status": order["status"], "client_order_id": client_order_id}
        detail = self.client.get_order_detail(inst_id, client_order_id=client_order_id)
        if isinstance(detail, list):
            detail = next((row for row in detail if isinstance(row, Mapping)), None)
        if not isinstance(detail, Mapping):
            return {"status": "UNKNOWN", "client_order_id": client_order_id,
                    "reconciliation": "NO_MATCH; no retry was sent"}
        echoed_id = str(detail.get("clientOrderId") or "")
        exchange_order_id = str(detail.get("orderId") or "")
        state = str(detail.get("state") or "").lower()
        if echoed_id != client_order_id or not exchange_order_id:
            return {"status": "UNKNOWN", "client_order_id": client_order_id,
                    "reconciliation": "INCOMPLETE_RECEIPT; no retry was sent"}
        state_map = {
            "live": "ACCEPTED",
            "partially_filled": "PARTIALLY_FILLED",
            "filled": "FILLED",
            "canceled": "CANCELED",
            "order_failed": "REJECTED",
        }
        mapped = state_map.get(state)
        if mapped is None:
            return {"status": "UNKNOWN", "client_order_id": client_order_id,
                    "reconciliation": "UNRECOGNIZED_EXCHANGE_STATE; no retry was sent"}
        self.store.update_order(
            client_order_id, mapped, exchange_order_id=exchange_order_id
        )
        return {
            "status": mapped,
            "environment": "BLOFIN_DEMO",
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_order_id,
            "reconciliation": "READ_ONLY",
        }

    def execute(
        self,
        signal: Any,
        *,
        inst_id: str,
        leverage: int = 1,
        allow_demo_orders: bool = False,
        minimum_size_only: bool = False,
    ) -> dict[str, Any]:
        if not allow_demo_orders or not self.trading_enabled:
            raise BloFinDemoExecutionBlocked("Demo order execution is disabled by the two-key safety gate.")
        if self.client.base_url != DEMO_REST_URL:
            raise BloFinDemoExecutionBlocked("Only the pinned BloFin Demo host is permitted.")
        if self.store.backend != "TURSO_CLOUD_SQLITE":
            raise BloFinDemoExecutionBlocked("Durable Turso storage is required before Demo trading.")
        if self.store.has_any_orders():
            raise BloFinDemoExecutionBlocked("This Demo service is limited to one lifetime order; an order is already reserved.")
        if not all((
            self.client.api_key, self.client.api_secret, self.client.passphrase
        )):
            raise BloFinDemoExecutionBlocked("Separate BLOFIN_DEMO_TRADE_* credentials are required.")
        if int(leverage) > self.policy.max_leverage or int(leverage) < 1:
            raise BloFinDemoExecutionBlocked("Leverage is outside the 1x-3x Demo test range.")

        record = _analysis_record(signal)
        clock = self.client.check_clock_skew(inst_id=inst_id)
        if abs(int(clock.get("offset_ms", 10**12))) >= 60_000:
            raise BloFinDemoExecutionBlocked("Exchange/local clock skew is 60 seconds or more.")

        ticker_rows = self.client.get_tickers(inst_id=inst_id)
        if not isinstance(ticker_rows, list) or not ticker_rows or not isinstance(ticker_rows[0], Mapping):
            raise BloFinDemoExecutionBlocked("Fresh contract ticker is unavailable.")
        last_price = _finite(ticker_rows[0].get("last"), "last_price", minimum=0.0000001)
        signal_entry = _finite(record.get("entry_price_usd", record.get("entry_price")), "entry_price", minimum=0.0000001)
        deviation = abs(last_price - signal_entry) / signal_entry
        if deviation > self.policy.max_entry_deviation_pct:
            raise BloFinDemoExecutionBlocked("Market price moved too far from the signal entry reference.")

        mode = self.client.get_position_mode()
        position_mode = mode.get("positionMode")
        if position_mode not in {"net_mode", "long_short_mode"}:
            raise BloFinDemoExecutionBlocked("Position mode could not be confirmed.")
        instrument = self.client.get_tradable_instrument(inst_id)
        balance = self.client.get_futures_balance(product_type="USDT-FUTURES")
        equity = _equity_from_balance(balance)
        guard = self.store.equity_guard(equity)
        if guard["daily_loss_pct"] >= self.policy.daily_loss_limit_pct:
            raise BloFinDemoExecutionBlocked("Daily equity-loss limit has been reached.")
        if guard["drawdown_pct"] >= self.policy.max_drawdown_pct:
            raise BloFinDemoExecutionBlocked("Maximum account drawdown limit has been reached.")

        positions = self.client.get_positions()
        # Account-wide pending orders can fill on another symbol and consume the
        # concurrency/margin budget, so the safety check is intentionally global.
        pending = self.client.get_open_orders()
        if _active_position_count(positions) >= self.policy.max_concurrent_positions:
            raise BloFinDemoExecutionBlocked("Maximum concurrent-position limit has been reached.")
        if _active_order_count(pending) > 0:
            raise BloFinDemoExecutionBlocked("Pending orders exist; reconcile them before a new entry.")

        plan = build_demo_order_plan(
            record, instrument, inst_id=inst_id, equity_usdt=equity,
            leverage=int(leverage), policy=self.policy,
        )
        if minimum_size_only:
            lot_size = _decimal(instrument.get("lotSize"), "lotSize")
            min_size = _decimal(instrument.get("minSize"), "minSize")
            quantity = (min_size / lot_size).to_integral_value(rounding=ROUND_CEILING) * lot_size
            max_market_size = instrument.get("maxMarketSize")
            if max_market_size not in (None, "") and quantity > _decimal(max_market_size, "maxMarketSize"):
                raise BloFinDemoExecutionBlocked("The contract minimum exceeds its maximum market-order size.")
            contract_value = _decimal(instrument.get("contractValue"), "contractValue")
            entry = _decimal(plan["entry_reference_price"], "entry_reference_price")
            stop = _decimal(plan["stop_loss"], "stop_loss")
            quantity_risk = abs(entry - stop) * contract_value * quantity
            quantity_notional = entry * contract_value * quantity
            quantity_margin = quantity_notional / Decimal(int(leverage))
            if quantity_risk > _decimal(plan["risk_budget_usdt"], "risk_budget_usdt"):
                raise BloFinDemoExecutionBlocked("The minimum contract size exceeds the configured per-trade risk budget.")
            if quantity_margin > equity * Decimal(str(self.policy.max_margin_usage_pct)):
                raise BloFinDemoExecutionBlocked("The minimum contract size exceeds the permitted isolated-margin budget.")
            plan["quantity"] = _format_decimal(quantity)
            plan["risk_per_contract_usdt"] = float(abs(entry - stop) * contract_value)
            plan["notional_usdt"] = float(quantity_notional)
            plan["estimated_initial_margin_usdt"] = float(quantity_margin)
            plan["minimum_size_only"] = True
        if not self.store.reserve_order(plan):
            raise BloFinDemoExecutionBlocked("This signal/order ID was already reserved; duplicate entry blocked.")

        position_side = "net" if position_mode == "net_mode" else plan["position_side_long_short"]
        exchange_side = "buy" if plan["side"] == "LONG" else "sell"
        try:
            self.client.set_futures_leverage(
                inst_id, int(leverage), margin_mode="isolated",
                position_side=None if position_mode == "net_mode" else position_side,
            )
            self.store.update_order(plan["client_order_id"], "LEVERAGE_SET")
        except Exception as exc:
            self.store.update_order(plan["client_order_id"], "REJECTED")
            raise BloFinDemoExecutionBlocked("Leverage setup failed; no entry order was submitted.") from exc

        try:
            response = self.client.place_futures_order(
                inst_id=inst_id,
                side=exchange_side,
                position_side=position_side,
                size=plan["quantity"],
                client_order_id=plan["client_order_id"],
                take_profit=plan["take_profit"],
                stop_loss=plan["stop_loss"],
                margin_mode="isolated",
            )
        except Exception as exc:
            self.store.update_order(plan["client_order_id"], "UNKNOWN")
            raise BloFinDemoExecutionUnknown(
                "Order outcome is unknown. Do not retry; reconcile with BloFin using clientOrderId."
            ) from exc

        data = response.get("data")
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], Mapping) else {}
        if not isinstance(data, Mapping):
            self.store.update_order(plan["client_order_id"], "UNKNOWN")
            raise BloFinDemoExecutionUnknown("BloFin accepted a response without a usable order receipt.")
        order_id = data.get("orderId")
        if not order_id:
            self.store.update_order(plan["client_order_id"], "UNKNOWN")
            raise BloFinDemoExecutionUnknown("Order ID is missing; reconcile before retrying.")
        self.store.update_order(plan["client_order_id"], "ACCEPTED", exchange_order_id=str(order_id))
        return {
            "status": "ACCEPTED",
            "environment": "BLOFIN_DEMO",
            "instrument_id": inst_id,
            "side": plan["side"],
            "quantity_contracts": plan["quantity"],
            "leverage": int(leverage),
            "stop_loss": plan["stop_loss"],
            "take_profit": plan["take_profit"],
            "client_order_id": plan["client_order_id"],
            "exchange_order_id": str(order_id),
            "risk_budget_usdt": plan["risk_budget_usdt"],
            "notional_usdt": plan["notional_usdt"],
            "position_mode": position_mode,
            "orders_enabled": True,
        }
