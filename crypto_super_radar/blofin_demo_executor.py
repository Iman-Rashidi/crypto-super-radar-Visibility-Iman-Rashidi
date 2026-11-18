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
from .learning_experience_memory import LearningExperienceMemory


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


def _position_margin_usdt(position: Mapping[str, Any]) -> Decimal:
    """Read the correct BloFin margin field for the position's margin mode."""
    margin_mode = str(position.get("marginMode") or "").strip().lower()
    if margin_mode == "isolated":
        field = "margin"
        description = "isolated margin"
    elif margin_mode == "cross":
        field = "initialMargin"
        description = "cross initial margin"
    else:
        raise BloFinDemoExecutionBlocked("Active position margin mode is unknown; aggregate margin cannot be confirmed.")
    raw_value = position.get(field)
    if raw_value in (None, ""):
        raise BloFinDemoExecutionBlocked(f"Active position {description} is missing; aggregate margin cannot be confirmed.")
    try:
        value = Decimal(str(raw_value))
    except Exception as exc:
        raise BloFinDemoExecutionBlocked(f"Active position {description} is invalid.") from exc
    if not value.is_finite() or value < 0:
        raise BloFinDemoExecutionBlocked(f"Active position {description} is invalid.")
    return value


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


def _challenge1_experience_id(instrument_id: str, challenge_record: Mapping[str, Any]) -> str:
    """Return a stable identity for one instrument's immutable closed-candle evidence."""
    evidence = challenge_record.get("challenge1_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("Challenge 1 evidence is required for learning memory.")
    candle = evidence.get("confirmation_candle")
    if not isinstance(candle, Mapping):
        raise ValueError("A completed confirmation candle is required for learning memory.")
    candle_time = str(candle.get("open_time_utc") or challenge_record.get("signal_bar_open_utc") or "")
    if not candle_time:
        raise ValueError("Completed candle timestamp is required for learning memory.")
    # One immutable identity per instrument and completed setup candle.
    # Advisory 1H context and mutable rationale must not create a second experience.
    identity = {
        "engine_version": str(evidence.get("decision_engine_version") or "CHALLENGE1_UNKNOWN"),
        "instrument_id": str(instrument_id).strip().upper(),
        "setup_timeframe": str(evidence.get("setup_timeframe") or "15m"),
        "signal_bar_open_utc": candle_time,
    }
    canonical = json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    return "blofin-ch1-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class BloFinDemoRiskPolicy:
    risk_per_trade_pct: float = 0.0025
    daily_loss_limit_pct: float = 0.01
    max_drawdown_pct: float = 0.05
    max_margin_usage_pct: float = 0.10
    max_leverage: int = 3
    max_concurrent_positions: int = 4
    max_total_margin_usage_pct: float = 0.25
    max_orders_per_utc_day: int | None = None
    min_seconds_between_orders: int = 0
    min_confirmation_score: float = 75.0
    max_risk_score: float = 45.0
    min_reward_risk: float = 1.8
    max_entry_deviation_pct: float = 0.005

    def __post_init__(self) -> None:
        values = (
            self.risk_per_trade_pct, self.daily_loss_limit_pct, self.max_drawdown_pct,
            self.max_margin_usage_pct, self.max_total_margin_usage_pct,
            self.min_confirmation_score, self.max_risk_score,
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
            raise ValueError("Per-position margin usage must be in (0, 25%]")
        if not 0 < self.max_total_margin_usage_pct <= 0.50:
            raise ValueError("Aggregate margin usage must be in (0, 50%]")
        if self.max_orders_per_utc_day is not None and (
            isinstance(self.max_orders_per_utc_day, bool)
            or not 1 <= int(self.max_orders_per_utc_day) <= 5
        ):
            raise ValueError("An optional Demo daily order limit must be between 1 and 5")
        if isinstance(self.min_seconds_between_orders, bool) or not 0 <= int(self.min_seconds_between_orders) <= 86400:
            raise ValueError("Minimum seconds between Demo orders must be between 0 and 86400")
        if isinstance(self.max_leverage, bool) or not 1 <= int(self.max_leverage) <= 3:
            raise ValueError("Demo test leverage is restricted to 1x through 3x")
        if not 1 <= int(self.max_concurrent_positions) <= 4:
            raise ValueError("Concurrent positions must be between 1 and 4")
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
    """Validate Challenge 1 evidence and size Demo contracts from stop risk."""
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

    execution_profile = str(record.get("execution_profile") or "").strip().upper()
    if execution_profile != "CHALLENGE_1":
        raise BloFinDemoExecutionBlocked("Only Challenge 1 strategy evidence is allowed for BloFin Demo orders.")
    if execution_profile == "CHALLENGE_1":
        evidence = record.get("challenge1_evidence")
        if not isinstance(evidence, Mapping):
            raise BloFinDemoExecutionBlocked("Challenge 1 evidence is missing.")
        chosen = evidence.get("chosen_pattern")
        confirmation = evidence.get("confirmation_candle")
        volume = confirmation.get("volume_confirmation") if isinstance(confirmation, Mapping) else None
        expected_pattern_direction = "BULLISH" if side == "LONG" else "BEARISH"
        engine_version = str(evidence.get("decision_engine_version") or "")
        expected_setup_timeframe = "5m" if engine_version == "CHALLENGE_1_LOCAL_RULES_V3" else "15m"
        if (
            evidence.get("decision_engine_version") not in {
                "CHALLENGE_1_LOCAL_RULES_V1",
                "CHALLENGE_1_LOCAL_RULES_V2",
                "CHALLENGE_1_LOCAL_RULES_V3",
            }
            or evidence.get("completed_candles_only") is not True
            or evidence.get("legacy_12_gates_used_for_entry") is not False
            or evidence.get("external_ai_required") is not False
            or evidence.get("tinyfish_required") is not False
            or evidence.get("decision") != side
            or evidence.get("setup_timeframe") != expected_setup_timeframe
            or (engine_version == "CHALLENGE_1_LOCAL_RULES_V3" and not isinstance(evidence.get("candles_5m"), list))
            or not isinstance(evidence.get("detected_patterns"), list)
            or not evidence.get("detected_patterns")
            or not isinstance(chosen, Mapping)
            or chosen.get("direction") != expected_pattern_direction
            or not isinstance(confirmation, Mapping)
            or not str(confirmation.get("open_time_utc") or "").strip()
            or not isinstance(volume, Mapping)
        ):
            raise BloFinDemoExecutionBlocked(
                "Challenge 1 pattern/closed-candle evidence is incomplete or inconsistent."
            )
        # V2 deliberately treats volume and 1H context as advisory. Requiring
        # volume.confirmed here would silently veto valid V2 candle-pattern setups.
    else:
        profile_rules = {
            "EASY": {"required": {1, 2, 3, 4, 5, 6, 9}, "minimum_pass": 7, "minimum_score": 55.0},
            "STANDARD": {"required": {1, 2, 3, 4, 5, 6, 8, 9}, "minimum_pass": 9, "minimum_score": 65.0},
            "STRICT": {"required": set(range(1, 13)), "minimum_pass": 12, "minimum_score": 75.0},
        }
        if execution_profile not in profile_rules:
            raise BloFinDemoExecutionBlocked("Unknown execution profile; select CHALLENGE_1, EASY, STANDARD or STRICT.")
        profile_rule = profile_rules[execution_profile]
        score = _finite(record.get("confirmation_score"), "confirmation_score")
        risk_score = _finite(record.get("estimated_risk_score"), "estimated_risk_score")
        minimum_score = max(float(profile_rule["minimum_score"]), float(policy.min_confirmation_score))
        if score < minimum_score or risk_score > policy.max_risk_score:
            raise BloFinDemoExecutionBlocked("Signal score/risk score did not pass the selected execution profile.")

        raw_gates = record.get("gates")
        if not isinstance(raw_gates, list) or len(raw_gates) != 12:
            raise BloFinDemoExecutionBlocked("The execution profile requires a complete 12-row gate report; missing evidence is not fabricated.")
        gate_statuses: dict[int, str] = {}
        for gate in raw_gates:
            if not isinstance(gate, Mapping):
                raise BloFinDemoExecutionBlocked("Gate report contains an invalid row.")
            gate_id = gate.get("gate_id")
            if isinstance(gate_id, bool) or not isinstance(gate_id, int) or gate_id in gate_statuses or gate_id not in range(1, 13):
                raise BloFinDemoExecutionBlocked("Gate IDs must be unique and within 1-12.")
            gate_statuses[gate_id] = str(gate.get("status", "")).upper()
        if set(gate_statuses) != set(range(1, 13)):
            raise BloFinDemoExecutionBlocked("The gate report is incomplete.")
        gate_pass = sum(value == "PASS" for value in gate_statuses.values())
        gate_fail = sum(value == "FAIL" for value in gate_statuses.values())
        gate_unknown = sum(value == "UNKNOWN" for value in gate_statuses.values())
        failed_required = sorted(gate_id for gate_id in profile_rule["required"] if gate_statuses[gate_id] != "PASS")
        if failed_required or gate_pass < int(profile_rule["minimum_pass"]):
            raise BloFinDemoExecutionBlocked(
                f"{execution_profile} profile blocked: required gates not PASS={failed_required}; "
                f"observed PASS={gate_pass}, FAIL={gate_fail}, UNKNOWN={gate_unknown}."
            )

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
    """Demo-only write adapter with allow-listed leverage, open-order and targeted close-position methods."""

    ORDER_PATH = "/api/v1/trade/order"
    LEVERAGE_PATH = "/api/v1/account/set-leverage"
    CLOSE_POSITION_PATH = "/api/v1/trade/close-position"
    CANCEL_ORDER_PATH = "/api/v1/trade/cancel-order"
    ALLOWED_POST_PATHS = frozenset({ORDER_PATH, LEVERAGE_PATH, CLOSE_POSITION_PATH, CANCEL_ORDER_PATH})

    def __init__(self, **kwargs: Any) -> None:
        # Prefer a dedicated Demo Trade key, but reuse the already-configured
        # Demo API key when it was explicitly granted TRADE permission as well.
        kwargs.setdefault(
            "api_key",
            os.getenv("BLOFIN_DEMO_TRADE_API_KEY") or os.getenv("BLOFIN_DEMO_API_KEY", ""),
        )
        kwargs.setdefault(
            "api_secret",
            os.getenv("BLOFIN_DEMO_TRADE_API_SECRET") or os.getenv("BLOFIN_DEMO_API_SECRET", ""),
        )
        kwargs.setdefault(
            "passphrase",
            os.getenv("BLOFIN_DEMO_TRADE_PASSPHRASE") or os.getenv("BLOFIN_DEMO_PASSPHRASE", ""),
        )
        super().__init__(**kwargs)
        self._trade_permission_verified = False

    def verify_trade_permission(self) -> dict[str, Any]:
        """Confirm this exact Demo key is read/write before any write operation."""
        info = self.get_api_key_info()
        if info.get("readOnly") != 0 or info.get("trade_permission") is not True:
            self._trade_permission_verified = False
            raise BloFinDemoExecutionBlocked(
                "BloFin API key is read-only (readOnly=1); enable Trade/Read-Write permission. "
                "No write request was sent."
            )
        self._trade_permission_verified = True
        return {"readOnly": 0, "trade_permission": True}

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if path not in self.ALLOWED_POST_PATHS:
            raise BloFinDemoExecutionBlocked("POST endpoint is not allow-listed.")
        if self.base_url != DEMO_REST_URL:
            raise BloFinDemoExecutionBlocked("Only the pinned BloFin Demo host is permitted.")
        if not all((self.api_key, self.api_secret, self.passphrase)):
            raise BloFinDemoError("Demo trade credentials are not configured.")
        # Direct POST calls cannot bypass the dashboard or executor readiness checks.
        if not self._trade_permission_verified:
            self.verify_trade_permission()
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

    def cancel_futures_order(self, *, inst_id: str, order_id: str) -> dict[str, Any]:
        """Cancel exactly one verified pending Demo order; never cancel all orders."""
        if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", inst_id or ""):
            raise ValueError("Only a USDT linear Demo perpetual order can be canceled.")
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", order_id or ""):
            raise ValueError("order_id must be a non-empty exchange order identifier.")
        return self._post(self.CANCEL_ORDER_PATH, {"instId": inst_id, "orderId": order_id})

    def close_futures_position(
        self, *, inst_id: str, margin_mode: str, position_side: str,
        client_order_id: str, position_id: str | None = None,
    ) -> dict[str, Any]:
        """Close one already-identified Demo position at market, never close-all."""
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", client_order_id or ""):
            raise ValueError("client_order_id must be 1-32 alphanumeric characters.")
        if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", inst_id or ""):
            raise ValueError("Only a USDT linear Demo perpetual can be closed.")
        if margin_mode not in {"isolated", "cross"} or position_side not in {"net", "long", "short"}:
            raise ValueError("Close requires a supported margin mode and a known position side.")
        if position_id is not None and not re.fullmatch(r"[A-Za-z0-9]{1,64}", position_id):
            raise ValueError("position_id is invalid.")
        body: dict[str, Any] = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "positionSide": position_side,
            "clientOrderId": client_order_id,
        }
        if position_id:
            body["positionId"] = position_id
        return self._post(self.CLOSE_POSITION_PATH, body)


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
                    experience_id TEXT,
                    outcome_recorded_at INTEGER,
                    position_history_id TEXT,
                    position_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            order_columns = {
                str(row["name"])
                for row in self._db.execute("PRAGMA table_info(blofin_demo_orders)").fetchall()
            }
            for column_name, column_type in (
                ("experience_id", "TEXT"),
                ("outcome_recorded_at", "INTEGER"),
                ("position_history_id", "TEXT"),
                ("position_id", "TEXT"),
            ):
                if column_name not in order_columns:
                    self._db.execute(
                        f"ALTER TABLE blofin_demo_orders ADD COLUMN {column_name} {column_type}"
                    )
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
                CREATE TABLE IF NOT EXISTS blofin_demo_autopilot_state (
                    state_key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
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
        # The decision journal shares the order-ledger connection and lock,
        # making a decision and its order reservation one transaction.
        self.learning_memory = LearningExperienceMemory(self._db, lock=self._lock)

    @staticmethod
    def _same_confirmation_candle(saved: Any, current: Any) -> bool:
        if not isinstance(saved, Mapping) or not isinstance(current, Mapping):
            return False
        saved_time = str(saved.get("open_time_utc") or "").strip().replace("Z", "+00:00")
        current_time = str(current.get("open_time_utc") or "").strip().replace("Z", "+00:00")
        if not saved_time or saved_time != current_time:
            return False
        for field in ("open", "high", "low", "close"):
            left_raw, right_raw = saved.get(field), current.get(field)
            if left_raw in (None, "") and right_raw in (None, ""):
                continue
            if left_raw in (None, "") or right_raw in (None, ""):
                return False
            try:
                left, right = float(left_raw), float(right_raw)
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(left) or not math.isfinite(right):
                return False
            if abs(left - right) > max(1e-10, abs(left) * 1e-9):
                return False
        return True

    def _find_challenge1_decision_for_candle(
        self, instrument_id: str, candle_time: str,
    ) -> tuple[str, dict[str, Any]] | None:
        """Find a previously saved decision for this exact instrument/candle."""
        instrument = str(instrument_id).strip().upper()
        rows = self._db.execute(
            """SELECT experience_id, payload_json
               FROM learning_experience_events
               WHERE engine_id='BLOFIN_DEMO_CHALLENGE1'
                 AND challenge_id='CHALLENGE_1_BLOFIN_DEMO'
                 AND environment='BLOFIN_DEMO'
                 AND event_type='DECISION_RECORDED'
               ORDER BY sequence DESC LIMIT 10000"""
        ).fetchall()
        expected = str(candle_time or "").strip().replace("Z", "+00:00")
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            input_snapshot = payload.get("input_snapshot") if isinstance(payload, dict) else {}
            if not isinstance(input_snapshot, Mapping):
                continue
            saved_instrument = str(input_snapshot.get("instrument_id") or "").strip().upper()
            if saved_instrument != instrument:
                continue
            saved_time = str(input_snapshot.get("signal_bar_open_utc") or "").strip().replace("Z", "+00:00")
            if saved_time == expected:
                return str(row["experience_id"]), payload
        return None

    def get_challenge1_record(self, experience_id: str) -> dict[str, Any] | None:
        """Rebuild the immutable strategy decision saved for one completed candle."""
        experience = self.learning_memory.get_experience(str(experience_id))
        decision_event = next(
            (item for item in experience.get("events", []) if item.get("event_type") == "DECISION_RECORDED"),
            None,
        )
        if not isinstance(decision_event, Mapping):
            return None
        payload = decision_event.get("payload")
        if not isinstance(payload, Mapping):
            return None
        decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
        input_snapshot = payload.get("input_snapshot") if isinstance(payload.get("input_snapshot"), Mapping) else {}
        evidence = input_snapshot.get("challenge1_evidence")
        if not isinstance(evidence, Mapping):
            return None
        return {
            "signal": str(decision.get("signal") or "NO_TRADE").upper(),
            "signal_id": decision.get("signal_id"),
            "instrument_id": input_snapshot.get("instrument_id"),
            "inst_id": input_snapshot.get("instrument_id"),
            "execution_profile": "CHALLENGE_1",
            "signal_bar_open_utc": input_snapshot.get("signal_bar_open_utc"),
            "entry_price_usd": decision.get("entry_price"),
            "stop_loss_usd": decision.get("stop_loss"),
            "take_profit_usd": decision.get("take_profit"),
            "reason_fa": decision.get("reason") or "",
            "execution": {"status": "PASS" if decision.get("signal") in {"LONG", "SHORT"} else "NO_TRADE"},
            "challenge1_evidence": dict(evidence),
            "learning_experience_id": str(experience_id),
            "orders_submitted": False,
            "trade_executed": False,
        }

    def record_challenge1_decision(
        self, *, instrument_id: str, challenge_record: Mapping[str, Any],
    ) -> str:
        """Persist one immutable decision per completed 15m candle, idempotently."""
        if not isinstance(challenge_record, Mapping):
            raise TypeError("challenge_record must be a mapping")
        evidence = challenge_record.get("challenge1_evidence")
        evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
        candle = evidence.get("confirmation_candle")
        candle_time = (
            str(candle.get("open_time_utc") or challenge_record.get("signal_bar_open_utc") or "")
            if isinstance(candle, Mapping) else str(challenge_record.get("signal_bar_open_utc") or "")
        )
        if not candle_time:
            raise ValueError("A completed confirmation candle timestamp is required.")
        instrument = str(instrument_id).strip().upper()
        existing = self._find_challenge1_decision_for_candle(instrument, candle_time)
        if existing is not None:
            experience_id, payload = existing
            input_snapshot = payload.get("input_snapshot") if isinstance(payload, Mapping) else {}
            saved_evidence = input_snapshot.get("challenge1_evidence") if isinstance(input_snapshot, Mapping) else {}
            saved_candle = saved_evidence.get("confirmation_candle") if isinstance(saved_evidence, Mapping) else None
            if not self._same_confirmation_candle(saved_candle, candle):
                raise BloFinDemoExecutionBlocked(
                    "The same completed candle timestamp has conflicting immutable OHLC data; no order was submitted."
                )
            # The original 15m candle decision remains authoritative. Do not append
            # a second event when optional 1H context, sentiment, or rationale changes.
            return experience_id

        experience_id = _challenge1_experience_id(instrument, challenge_record)
        signal = str(challenge_record.get("signal") or "NO_TRADE").strip().upper()
        signal_bar = str(challenge_record.get("signal_bar_open_utc") or candle_time)
        decision = {
            "signal": signal,
            "side": signal if signal in {"LONG", "SHORT"} else None,
            "signal_id": challenge_record.get("signal_id"),
            "entry_price": challenge_record.get("entry_price_usd"),
            "stop_loss": challenge_record.get("stop_loss_usd"),
            "take_profit": challenge_record.get("take_profit_usd"),
            "reason": str(challenge_record.get("reason_fa") or ""),
        }
        input_snapshot = {
            "instrument_id": instrument,
            "signal_bar_open_utc": signal_bar,
            "setup_timeframe": str(evidence.get("setup_timeframe") or "15m"),
            "context_timeframe": str(evidence.get("context_timeframe") or "1H"),
            "completed_candles_only": evidence.get("completed_candles_only") is True,
            "future_data_in_input": False,
            "challenge1_evidence": evidence,
        }
        with self._lock, self._db:
            self.learning_memory.record_decision(
                experience_id=experience_id,
                event_id=experience_id + "-decision",
                engine_id="BLOFIN_DEMO_CHALLENGE1",
                challenge_id="CHALLENGE_1_BLOFIN_DEMO",
                cycle_id=experience_id,
                decision_mode="BOT_ONLY",
                environment="BLOFIN_DEMO",
                decision=decision,
                input_snapshot=input_snapshot,
                market="CEX_FUTURES",
                symbol=instrument,
                timeframe=str(evidence.get("setup_timeframe") or "15m").upper(),
                model_version=str(evidence.get("decision_engine_version") or "CHALLENGE1_UNKNOWN"),
                dataset_version="BLOFIN_DEMO_COMPLETED_CANDLES",
            )
        return experience_id

    def record_experience_not_executed(
        self, *, experience_id: str, instrument_id: str, signal: str,
        stage: str, reason: str,
    ) -> dict[str, Any]:
        """Close a decision with an explicit no-order label; never attach floating PnL."""
        evidence = {
            "source": "BLOFIN_DEMO_FINAL_EXECUTION_DECISION",
            "instrument_id": str(instrument_id).strip().upper(),
            "signal": str(signal or "NO_TRADE").strip().upper(),
            "execution_stage": str(stage or "NO_TRADE").strip().upper(),
            "reason": str(reason or "")[:500],
            "orders_submitted": False,
            "trade_executed": False,
            "realized_pnl_source": "NONE_NOT_EXECUTED",
        }
        with self._lock, self._db:
            return self.learning_memory.record_outcome(
                experience_id=experience_id,
                event_id=experience_id + "-not-executed",
                decision_correctness="UNKNOWN",
                realized_result="NOT_EXECUTED",
                realized_net_pnl=None,
                pnl_currency="USDT",
                final=True,
                evidence=evidence,
            )

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
                 stop_loss, take_profit, leverage, status, exchange_order_id, experience_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', NULL, ?, ?, ?)""",
                (
                    order_id, str(plan["signal_id"]), str(plan["inst_id"]), str(plan["side"]),
                    str(plan["quantity"]), float(plan["entry_reference_price"]),
                    float(plan["stop_loss"]), float(plan["take_profit"]), int(plan["leverage"]),
                    str(plan.get("experience_id") or "") or None, now, now,
                ),
            )
            self._append_event(order_id, "RESERVED", {
                "instrument_id": plan["inst_id"], "side": plan["side"],
                "quantity": plan["quantity"], "leverage": plan["leverage"],
                "learning_experience_id_present": bool(plan.get("experience_id")),
            })
            return True

    def update_order(self, client_order_id: str, status: str, *, exchange_order_id: str | None = None) -> None:
        allowed = {
            "LEVERAGE_SET", "PENDING_RECONCILE", "ACCEPTED",
            "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNKNOWN",
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

    def record_manual_action(
        self, action_id: str, event_type: str, details: Mapping[str, Any]
    ) -> None:
        """Append one manual account-management action without mutating prior events."""
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", str(action_id or "")):
            raise ValueError("action_id must be a stable opaque identifier.")
        if event_type not in {"MANUAL_ORDER_CANCELED", "MANUAL_ORDER_SUBMISSION_UNKNOWN", "MANUAL_POSITION_CLOSE_REQUESTED", "MANUAL_POSITION_CLOSE_ACCEPTED", "AUTO_POSITION_CLOSE_REQUESTED", "AUTO_POSITION_CLOSE_ACCEPTED", "AUTO_POSITION_CLOSE_UNKNOWN"}:
            raise ValueError("Unsupported manual action event type.")
        with self._lock, self._db:
            self._append_event(action_id, event_type, details)

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
        """Return whether this Demo service has ever reserved an order."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM blofin_demo_orders LIMIT 1"
            ).fetchone()
        return row is not None

    def save_autopilot_state(self, state: Mapping[str, Any]) -> None:
        """Persist the latest operational AutoPilot snapshot in the configured durable store."""
        if not isinstance(state, Mapping):
            raise TypeError("AutoPilot state must be a mapping.")
        state_json = json.dumps(
            dict(state), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        now = int(time.time())
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO blofin_demo_autopilot_state(state_key, state_json, updated_at)
                VALUES ('default', ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at""",
                (state_json, now),
            )

    def load_autopilot_state(self) -> dict[str, Any] | None:
        """Load the last operational AutoPilot snapshot, if one was saved."""
        with self._lock:
            row = self._db.execute(
                "SELECT state_json FROM blofin_demo_autopilot_state WHERE state_key='default'"
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["state_json"])
        except (TypeError, ValueError, KeyError):
            return None
        return payload if isinstance(payload, dict) else None

    def has_signal(self, signal_id: str) -> bool:
        """Return whether this exact completed-candle signal has ever been reserved."""
        if not isinstance(signal_id, str) or not signal_id.strip():
            return False
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM blofin_demo_orders WHERE signal_id=? LIMIT 1",
                (signal_id.strip(),),
            ).fetchone()
        return row is not None

    def trade_budget_status(self, max_orders_per_utc_day: int | None = None, *, now: int | None = None) -> dict[str, Any]:
        """Report Demo order history without imposing a quota unless explicitly configured."""
        sampled_at = int(time.time()) if now is None else int(now)
        utc_day_start = sampled_at - (sampled_at % 86400)
        with self._lock:
            today_row = self._db.execute(
                "SELECT COUNT(*) FROM blofin_demo_orders WHERE created_at>=?",
                (utc_day_start,),
            ).fetchone()
            total_row = self._db.execute(
                "SELECT COUNT(*) FROM blofin_demo_orders"
            ).fetchone()
            latest_row = self._db.execute(
                "SELECT created_at, status FROM blofin_demo_orders ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            unresolved_row = self._db.execute(
                "SELECT COUNT(*) FROM blofin_demo_orders WHERE status IN ('UNKNOWN','RESERVED','LEVERAGE_SET','PENDING_RECONCILE')"
            ).fetchone()
        orders_today = int(today_row[0] or 0)
        orders_total = int(total_row[0] or 0)
        daily_limit = None if max_orders_per_utc_day is None else int(max_orders_per_utc_day)
        latest_created_at = int(latest_row["created_at"]) if latest_row is not None else None
        latest_status = str(latest_row["status"]) if latest_row is not None else None
        age = max(0, sampled_at - latest_created_at) if latest_created_at is not None else None
        return {
            "orders_today": orders_today,
            "orders_total": orders_total,
            "max_orders_per_utc_day": daily_limit,
            "daily_capacity_available": daily_limit is None or orders_today < daily_limit,
            "latest_order_age_seconds": age,
            "latest_order_status": latest_status,
            "unresolved_orders": int(unresolved_row[0] or 0),
            "utc_day_start": utc_day_start,
        }

    def latest_order(self) -> dict[str, Any] | None:
        """Return the newest reserved order without exposing account credentials."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM blofin_demo_orders ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def recent_events(self, client_order_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return append-only lifecycle events for the selected Demo order."""
        bounded = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._db.execute(
                """SELECT sequence, event_type, occurred_at, payload_json
                FROM blofin_demo_execution_events
                WHERE client_order_id=? ORDER BY sequence DESC LIMIT ?""",
                (client_order_id, bounded),
            ).fetchall()
        events = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                payload = {}
            events.append({
                "sequence": row["sequence"], "event_type": row["event_type"],
                "occurred_at": row["occurred_at"], "payload": payload,
            })
        return events

    def recent_all_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent append-only lifecycle events across every local Demo order."""
        bounded = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._db.execute(
                """SELECT sequence, client_order_id, event_type, occurred_at, payload_json
                FROM blofin_demo_execution_events ORDER BY sequence DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
        events = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                payload = {}
            events.append({
                "sequence": row["sequence"],
                "client_order_id": row["client_order_id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "payload": payload,
            })
        return events

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM blofin_demo_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_order_for_experience(self, experience_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM blofin_demo_orders WHERE experience_id=? ORDER BY created_at DESC LIMIT 1",
                (experience_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def has_final_experience(self, experience_id: str) -> bool:
        """Return whether this closed-candle decision already has an immutable outcome."""
        experience = self.learning_memory.get_experience(experience_id)
        return any(event.get("event_type") == "OUTCOME_LABELLED" for event in experience["events"])

    def record_closed_position_outcome(
        self, *, instrument_id: str, history_rows: Any,
        partial_rows: Any = None, client_order_id: str | None = None,
        position_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record a final outcome for the exact Demo order/position, never the latest same-symbol order."""
        instrument = str(instrument_id or "").strip().upper()
        order_key = str(client_order_id or "").strip()
        position_key = str(position_id or "").strip()
        with self._lock:
            if order_key:
                order_row = self._db.execute(
                    "SELECT * FROM blofin_demo_orders WHERE client_order_id=? AND instrument_id=? "
                    "AND experience_id IS NOT NULL AND status IN ('ACCEPTED','PARTIALLY_FILLED','FILLED')",
                    (order_key, instrument),
                ).fetchone()
            elif position_key:
                order_row = self._db.execute(
                    "SELECT * FROM blofin_demo_orders WHERE position_id=? AND instrument_id=? "
                    "AND experience_id IS NOT NULL AND status IN ('ACCEPTED','PARTIALLY_FILLED','FILLED') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (position_key, instrument),
                ).fetchone()
            else:
                # Backwards-compatible path for existing offline callers/tests.
                order_row = self._db.execute(
                    "SELECT * FROM blofin_demo_orders WHERE instrument_id=? AND experience_id IS NOT NULL "
                    "AND status IN ('ACCEPTED','PARTIALLY_FILLED','FILLED') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (instrument,),
                ).fetchone()
            if order_row is None:
                return None
            order = dict(order_row)
            if order.get("outcome_recorded_at") is not None:
                return {"status": "ALREADY_RECORDED", "experience_id": order.get("experience_id")}
        saved_position_id = str(order.get("position_id") or "")
        if position_key and saved_position_id != position_key:
            return {
                "status": "POSITION_ID_MISMATCH",
                "experience_id": order.get("experience_id"),
            }
        position_id = saved_position_id
        if not position_id:
            return {
                "status": "WAITING_FOR_EXCHANGE_POSITION_LINK",
                "experience_id": order.get("experience_id"),
            }
        expected_side = "buy" if str(order.get("side") or "").upper() == "LONG" else "sell"
        try:
            order_created_ms = int(order.get("created_at") or 0) * 1000
            reference_entry = float(order.get("entry_price"))
            expected_quantity = float(order.get("quantity"))
        except (TypeError, ValueError, OverflowError):
            return {"status": "WAITING_FOR_VALID_LOCAL_ORDER"}
        if not all(math.isfinite(value) and value > 0 for value in (
            reference_entry, expected_quantity
        )):
            return {"status": "WAITING_FOR_VALID_LOCAL_ORDER"}

        # The final close record alone may represent only the last partial fill.
        # Sum partial-close and full-close history for the same exchange positionId.
        combined_rows = []
        for state, source_rows in (
            ("PARTIALLY_CLOSED", partial_rows or []),
            ("CLOSED", history_rows or []),
        ):
            for row in source_rows:
                if not isinstance(row, Mapping):
                    continue
                if (
                    str(row.get("positionId") or "") != position_id
                    or str(row.get("instId") or "").upper() != instrument
                    or str(row.get("side") or "").lower() != expected_side
                ):
                    continue
                try:
                    opened_ms = int(row.get("createTime") or 0)
                    open_price = float(row.get("openAveragePrice"))
                    close_quantity = float(row.get("closePositions"))
                    total_quantity = float(row.get("maxPositions"))
                    pnl = float(row.get("realizedPnl"))
                    fee = float(row.get("fee") or 0.0)
                    history_id = str(row.get("historyId") or "")
                    values = (open_price, close_quantity, total_quantity, pnl, fee)
                    if not all(math.isfinite(value) for value in values):
                        continue
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    opened_ms < order_created_ms - 60_000
                    or open_price <= 0
                    or abs(open_price - reference_entry) / reference_entry > 0.005
                    or close_quantity <= 0
                    or total_quantity + 1e-9 < close_quantity
                    or not history_id
                ):
                    continue
                combined_rows.append({
                    "history_id": history_id,
                    "state": state,
                    "close_quantity": close_quantity,
                    "realized_pnl": pnl,
                    "trading_fee": fee,
                    "position_row": dict(row),
                })
        if not any(item["state"] == "CLOSED" for item in combined_rows):
            return {
                "status": "WAITING_FOR_FINAL_CLOSED_POSITION",
                "experience_id": order.get("experience_id"),
                "matched_partial_rows": len(combined_rows),
            }
        quantity_tolerance = max(1e-9, abs(expected_quantity) * 1e-6)
        total_closed = sum(item["close_quantity"] for item in combined_rows)
        if abs(total_closed - expected_quantity) > quantity_tolerance:
            return {
                "status": "WAITING_FOR_FULL_POSITION_SIZE_RECONCILIATION",
                "experience_id": order.get("experience_id"),
                "expected_quantity": expected_quantity,
                "observed_closed_quantity": total_closed,
            }
        history_ids = sorted({item["history_id"] for item in combined_rows})
        realized_pnl = sum(item["realized_pnl"] for item in combined_rows)
        trading_fees = sum(item["trading_fee"] for item in combined_rows)
        net_before_funding = realized_pnl - trading_fees
        if not math.isfinite(net_before_funding):
            raise ValueError("Closed-position PnL after reported trading fees is not finite.")
        result = (
            "PROFIT" if net_before_funding > 1e-8
            else "LOSS" if net_before_funding < -1e-8
            else "BREAKEVEN"
        )
        evidence = {
            "source": "BLOFIN_DEMO_POSITION_ID_LINKED_HISTORY",
            "instrument_id": instrument,
            "position_id": position_id,
            "history_ids": history_ids,
            "closed_history_ids": [
                item["history_id"] for item in combined_rows if item["state"] == "CLOSED"
            ],
            "partial_history_ids": [
                item["history_id"] for item in combined_rows if item["state"] == "PARTIALLY_CLOSED"
            ],
            "original_order_id": order.get("exchange_order_id"),
            "expected_quantity": expected_quantity,
            "observed_closed_quantity": total_closed,
            "exchange_realized_pnl_before_trading_fees": realized_pnl,
            "exchange_trading_fees": trading_fees,
            "realized_pnl_after_reported_trading_fees": net_before_funding,
            "funding_fees_included": False,
            "funding_fee_attribution": "NOT_YET_RECONCILED",
            "decision_correctness": "UNKNOWN_PENDING_INDEPENDENT_REVIEW",
            "mark_to_market_pnl_used": False,
            "orders_submitted": True,
            "trade_executed": True,
        }
        experience_id = str(order["experience_id"])
        now = int(time.time())
        with self._lock, self._db:
            latest = self._db.execute(
                "SELECT outcome_recorded_at FROM blofin_demo_orders WHERE client_order_id=?",
                (order["client_order_id"],),
            ).fetchone()
            if latest is None or latest["outcome_recorded_at"] is not None:
                return {"status": "ALREADY_RECORDED", "experience_id": experience_id}
            saved = self.learning_memory.record_outcome(
                experience_id=experience_id,
                event_id=experience_id + "-outcome",
                decision_correctness="UNKNOWN",
                realized_result=result,
                realized_net_pnl=net_before_funding,
                pnl_currency="USDT",
                final=True,
                evidence=evidence,
            )
            self._db.execute(
                "UPDATE blofin_demo_orders SET outcome_recorded_at=?, position_history_id=?, updated_at=? "
                "WHERE client_order_id=?",
                (now, ",".join(history_ids), now, order["client_order_id"]),
            )
            self._append_event(order["client_order_id"], "OUTCOME_RECORDED", {
                "experience_id": experience_id,
                "position_id": position_id,
                "history_ids": history_ids,
                "realized_result": result,
                "realized_pnl_after_reported_trading_fees": net_before_funding,
                "funding_fee_attribution": "NOT_YET_RECONCILED",
            })
        return {
            "status": "RECORDED",
            "experience_id": experience_id,
            "position_id": position_id,
            "history_ids": history_ids,
            "realized_result": result,
            "realized_pnl_after_reported_trading_fees": net_before_funding,
            "funding_fee_attribution": "NOT_YET_RECONCILED",
            "learning_memory_status": saved.get("event_type"),
        }

    def orders_pending_learning_outcome(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM blofin_demo_orders
                   WHERE experience_id IS NOT NULL AND experience_id != ''
                     AND position_id IS NOT NULL AND position_id != ''
                     AND outcome_recorded_at IS NULL
                     AND status IN ('ACCEPTED','PARTIALLY_FILLED','FILLED')
                   ORDER BY updated_at ASC LIMIT ?""",
                (bounded,),
            ).fetchall()
        return [dict(row) for row in rows]


    def pending_learning_outcome_count(self) -> int:
        """Count executed Demo experiences still awaiting exchange-finalized outcome data."""
        with self._lock:
            row = self._db.execute(
                """SELECT COUNT(*) FROM blofin_demo_orders
                   WHERE experience_id IS NOT NULL AND experience_id != ''
                     AND outcome_recorded_at IS NULL
                     AND status IN ('ACCEPTED','PARTIALLY_FILLED','FILLED')"""
            ).fetchone()
        return int(row[0] or 0)

    def attach_position_id(self, experience_id: str, position_id: str) -> bool:
        experience_key = str(experience_id or "").strip()
        position_key = str(position_id or "").strip()
        if not experience_key or not position_key:
            return False
        with self._lock, self._db:
            order = self.get_order_for_experience(experience_key)
            if order is None:
                return False
            existing = str(order.get("position_id") or "")
            if existing and existing != position_key:
                raise BloFinDemoExecutionBlocked(
                    "A different exchange position is already linked to this learning experience."
                )
            self._db.execute(
                "UPDATE blofin_demo_orders SET position_id=?, updated_at=? WHERE client_order_id=?",
                (position_key, int(time.time()), order["client_order_id"]),
            )
            self._append_event(
                str(order["client_order_id"]), "POSITION_LINKED",
                {"position_id": position_key},
            )
        return True

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
        if order.get("status") not in {"UNKNOWN", "RESERVED", "LEVERAGE_SET", "PENDING_RECONCILE"}:
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
        cancel_check: Any | None = None,
        submission_lock: Any | None = None,
    ) -> dict[str, Any]:
        if not allow_demo_orders or not self.trading_enabled:
            raise BloFinDemoExecutionBlocked("Demo order execution is disabled by the two-key safety gate.")
        if self.client.base_url != DEMO_REST_URL:
            raise BloFinDemoExecutionBlocked("Only the pinned BloFin Demo host is permitted.")
        if self.store.backend != "TURSO_CLOUD_SQLITE":
            raise BloFinDemoExecutionBlocked("Durable Turso storage is required before Demo trading.")
        budget = self.store.trade_budget_status(self.policy.max_orders_per_utc_day)
        if (
            self.policy.max_orders_per_utc_day is not None
            and budget["orders_today"] >= self.policy.max_orders_per_utc_day
        ):
            raise BloFinDemoExecutionBlocked("The configured daily Demo order limit has been reached.")
        if budget["unresolved_orders"] > 0:
            raise BloFinDemoExecutionBlocked("An earlier Demo order has an unresolved outcome; reconcile it before another entry.")
        # A pending realized-outcome label is an audit/learning task, not proof of
        # current exchange exposure. Do not let an incomplete history record freeze
        # every market indefinitely. The fresh account-wide positions and open-orders
        # checks below remain the hard entry blockers; unresolved order submissions
        # are still blocked above and must be reconciled before another entry.
        latest_age = budget.get("latest_order_age_seconds")
        if (
            self.policy.min_seconds_between_orders > 0
            and latest_age is not None
            and latest_age < self.policy.min_seconds_between_orders
        ):
            raise BloFinDemoExecutionBlocked("The minimum cooldown between Demo orders has not elapsed.")
        if not all((
            self.client.api_key, self.client.api_secret, self.client.passphrase
        )):
            raise BloFinDemoExecutionBlocked("Separate BLOFIN_DEMO_TRADE_* credentials are required.")
        try:
            self.client.verify_trade_permission()
        except BloFinDemoExecutionBlocked:
            raise
        except Exception as exc:
            raise BloFinDemoExecutionBlocked(
                "BloFin Trade permission could not be verified; no order was sent."
            ) from exc
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
        # Account-wide checks are global. Only one active position per contract is
        # allowed; concurrent positions must use different instruments.
        if not isinstance(positions, list):
            raise BloFinDemoExecutionBlocked("Positions response is not a list.")
        active_position_rows = []
        active_initial_margin = Decimal("0")
        for row in positions:
            if not isinstance(row, Mapping):
                raise BloFinDemoExecutionBlocked("Position metadata is unreadable; no new entry is allowed.")
            raw_size = next((row.get(k) for k in ("positions", "pos", "positionSize", "positionAmt") if k in row), None)
            if raw_size is None:
                raise BloFinDemoExecutionBlocked("Position size is missing; no new entry is allowed.")
            try:
                size_value = Decimal(str(raw_size))
            except Exception as exc:
                raise BloFinDemoExecutionBlocked("Position size is invalid; no new entry is allowed.") from exc
            if not size_value.is_finite():
                raise BloFinDemoExecutionBlocked("Position size is non-finite; no new entry is allowed.")
            if abs(size_value) <= Decimal("1e-12"):
                continue
            active_position_rows.append(row)
            active_inst = str(row.get("instId") or "").strip().upper()
            if not active_inst:
                raise BloFinDemoExecutionBlocked("An active position has no instrument ID; aggregate risk cannot be confirmed.")
            if active_inst == inst_id:
                raise BloFinDemoExecutionBlocked("A position is already open for this contract.")
            # BloFin returns "margin" for isolated positions and
            # "initialMargin" for cross positions. The latter is not the
            # isolated position-margin field.
            active_initial_margin += _position_margin_usdt(row)

        if len(active_position_rows) >= self.policy.max_concurrent_positions:
            raise BloFinDemoExecutionBlocked("Maximum concurrent-position limit has been reached.")
        pending = self.client.get_open_orders()
        if _active_order_count(pending) > 0:
            raise BloFinDemoExecutionBlocked("Pending orders exist; reconcile them before a new entry.")

        plan = build_demo_order_plan(
            record, instrument, inst_id=inst_id, equity_usdt=equity,
            leverage=int(leverage), policy=self.policy,
        )
        plan["experience_id"] = str(record.get("learning_experience_id") or "") or None
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
            if quantity_margin > Decimal(str(equity)) * Decimal(str(self.policy.max_margin_usage_pct)):
                raise BloFinDemoExecutionBlocked("The minimum contract size exceeds the permitted isolated-margin budget.")
            plan["quantity"] = _format_decimal(quantity)
            plan["risk_per_contract_usdt"] = float(abs(entry - stop) * contract_value)
            plan["notional_usdt"] = float(quantity_notional)
            plan["estimated_initial_margin_usdt"] = float(quantity_margin)
            plan["minimum_size_only"] = True
        projected_margin = active_initial_margin + Decimal(str(plan.get("estimated_initial_margin_usdt") or 0))
        aggregate_margin_limit = Decimal(str(equity)) * Decimal(str(self.policy.max_total_margin_usage_pct))
        if projected_margin > aggregate_margin_limit:
            raise BloFinDemoExecutionBlocked("Aggregate initial margin would exceed the account-wide margin cap.")
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

        def submit_reserved_order() -> dict[str, Any]:
            # The final cancellation check runs after all slow preflight calls and
            # immediately before the one exchange write. The caller may hold a
            # shared lock so stop requests can distinguish an in-flight write.
            if cancel_check is not None and bool(cancel_check()):
                self.store.update_order(plan["client_order_id"], "REJECTED")
                raise BloFinDemoExecutionBlocked(
                    "Automated entry was stopped before the BloFin Demo order was submitted."
                )
            self.store.update_order(plan["client_order_id"], "PENDING_RECONCILE")
            try:
                return self.client.place_futures_order(
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

        if submission_lock is None:
            response = submit_reserved_order()
        else:
            with submission_lock:
                response = submit_reserved_order()

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
