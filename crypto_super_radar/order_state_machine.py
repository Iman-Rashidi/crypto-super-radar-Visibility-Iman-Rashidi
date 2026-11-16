"""Explicit order lifecycle states and evidence-gated transition validation.

This module is deliberately independent of exchange adapters. It only validates
state changes requested by a caller; it never submits, retries, or cancels orders.
"""
from __future__ import annotations

import math
from datetime import datetime
from collections.abc import Mapping
from typing import Any


class TransitionValidationError(ValueError):
    """Raised when a lifecycle transition is not allowed or lacks evidence."""


class OrderStateMachine:
    """Allow-list of order and position lifecycle transitions."""

    ALLOWED: dict[str, frozenset[str]] = {
        "NONE": frozenset({"INTENT_RECORDED"}),
        "INTENT_RECORDED": frozenset({"VALIDATED", "REJECTED", "UNKNOWN", "HALTED"}),
        "VALIDATED": frozenset({"SUBMITTING", "REJECTED", "UNKNOWN", "HALTED"}),
        "SUBMITTING": frozenset({"SUBMITTED", "REJECTED", "UNKNOWN", "HALTED"}),
        "SUBMITTED": frozenset({
            "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNKNOWN", "HALTED"
        }),
        "ACKNOWLEDGED": frozenset({
            "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNKNOWN", "HALTED"
        }),
        "PARTIALLY_FILLED": frozenset({
            "PARTIALLY_FILLED", "FILLED", "CANCELED", "UNKNOWN", "HALTED"
        }),
        # UNKNOWN deliberately has no path back to SUBMITTING or SUBMITTED.
        # It can leave this state only through verified reconciliation.
        "UNKNOWN": frozenset({
            "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELED",
            "REJECTED", "POSITION_OPEN_UNPROTECTED", "HALTED"
        }),
        "FILLED": frozenset({"POSITION_OPEN_UNPROTECTED", "HALTED"}),
        "POSITION_OPEN_UNPROTECTED": frozenset({
            "POSITION_MANAGED", "EMERGENCY_CLOSE_PENDING", "HALTED", "UNKNOWN"
        }),
        "POSITION_MANAGED": frozenset({"CLOSED", "UNKNOWN", "HALTED"}),
        "EMERGENCY_CLOSE_PENDING": frozenset({"CLOSED", "UNKNOWN", "HALTED"}),
        "CANCELED": frozenset(),
        "REJECTED": frozenset(),
        "CLOSED": frozenset(),
        "HALTED": frozenset(),
    }

    @classmethod
    def allows(cls, from_state: str, to_state: str) -> bool:
        return to_state in cls.ALLOWED.get(str(from_state).upper(), frozenset())

    @classmethod
    def allowed_next(cls, state: str) -> tuple[str, ...]:
        return tuple(sorted(cls.ALLOWED.get(str(state).upper(), frozenset())))


class TransitionValidator:
    """Add evidence requirements to the state machine's transition allow-list."""

    def __init__(self, state_machine: type[OrderStateMachine] = OrderStateMachine):
        self.state_machine = state_machine

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TransitionValidationError(f"{field} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _positive_number(value: Any, field: str) -> float:
        if isinstance(value, bool) or value is None:
            raise TransitionValidationError(f"{field} must be a finite positive number")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TransitionValidationError(f"{field} must be a finite positive number") from exc
        if not math.isfinite(number) or number <= 0:
            raise TransitionValidationError(f"{field} must be a finite positive number")
        return number

    def validate(self, from_state: str, to_state: str, evidence: Mapping[str, Any] | None = None) -> None:
        source = str(from_state or "").strip().upper()
        target = str(to_state or "").strip().upper()
        if not source or not target or not self.state_machine.allows(source, target):
            raise TransitionValidationError(f"invalid lifecycle transition: {source or 'NONE'} -> {target or 'EMPTY'}")

        proof = evidence if isinstance(evidence, Mapping) else {}
        if source == "UNKNOWN":
            self._validate_reconciliation(target, proof)
        if source == "POSITION_OPEN_UNPROTECTED" and target == "POSITION_MANAGED":
            self._validate_protection(proof)

    def _validate_reconciliation(self, target: str, evidence: Mapping[str, Any]) -> None:
        if str(evidence.get("kind", "")).upper() != "RECONCILIATION":
            raise TransitionValidationError("UNKNOWN can leave only through RECONCILIATION evidence")
        if evidence.get("verified") is not True:
            raise TransitionValidationError("UNKNOWN requires verified reconciliation evidence")
        self._required_text(evidence.get("source"), "reconciliation source")
        self._required_text(evidence.get("reference_id"), "reconciliation reference_id")
        self._required_text(evidence.get("observed_at_utc"), "reconciliation observed_at_utc")
        observed = str(evidence["observed_at_utc"]).strip()
        try:
            parsed_observed = datetime.fromisoformat(observed[:-1] + "+00:00" if observed.endswith("Z") else observed)
        except ValueError as exc:
            raise TransitionValidationError("reconciliation timestamp must be valid ISO-8601") from exc
        if parsed_observed.tzinfo is None or parsed_observed.utcoffset() is None or parsed_observed.utcoffset().total_seconds() != 0:
            raise TransitionValidationError("reconciliation timestamp must be UTC")
        observed_status = str(evidence.get("exchange_status", "")).strip().upper()
        expected = {
            "ACKNOWLEDGED": {"NEW", "OPEN", "ACKNOWLEDGED"},
            "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "PARTIALLY FILLED"},
            "FILLED": {"FILLED"},
            "CANCELED": {"CANCELED", "CANCELLED"},
            "REJECTED": {"REJECTED", "NOT_FOUND"},
            "POSITION_OPEN_UNPROTECTED": {"POSITION_OPEN", "OPEN_POSITION"},
            "HALTED": {"HALTED", "MANUAL_INTERVENTION_REQUIRED"},
        }
        if target in expected and observed_status not in expected[target]:
            raise TransitionValidationError(
                f"reconciliation exchange_status does not support transition to {target}"
            )

    def _validate_protection(self, evidence: Mapping[str, Any]) -> None:
        if str(evidence.get("kind", "")).upper() != "PROTECTION_VERIFICATION":
            raise TransitionValidationError("POSITION_MANAGED requires PROTECTION_VERIFICATION evidence")
        if evidence.get("independent_check") is not True:
            raise TransitionValidationError("TP/SL must be independently verified")
        self._required_text(evidence.get("source"), "protection source")
        self._required_text(evidence.get("verified_at_utc"), "protection verified_at_utc")
        verified_at = str(evidence["verified_at_utc"]).strip()
        try:
            parsed_verified = datetime.fromisoformat(verified_at[:-1] + "+00:00" if verified_at.endswith("Z") else verified_at)
        except ValueError as exc:
            raise TransitionValidationError("protection verification timestamp must be valid ISO-8601") from exc
        if parsed_verified.tzinfo is None or parsed_verified.utcoffset() is None or parsed_verified.utcoffset().total_seconds() != 0:
            raise TransitionValidationError("protection verification timestamp must be UTC")
        position_id = self._required_text(evidence.get("position_id"), "position_id")
        position_side = self._required_text(evidence.get("position_side"), "position_side").upper()
        if position_side not in {"LONG", "SHORT"}:
            raise TransitionValidationError("position_side must be LONG or SHORT")
        position_quantity = self._positive_number(evidence.get("position_quantity"), "position_quantity")
        entry_price = self._positive_number(evidence.get("entry_price"), "entry_price")
        expected_close_side = "SELL" if position_side == "LONG" else "BUY"
        protection = evidence.get("protection")
        if not isinstance(protection, Mapping):
            raise TransitionValidationError("both TP and SL protection records are required")

        order_ids: set[str] = set()
        quantities: list[float] = []
        for leg in ("tp", "sl"):
            item = protection.get(leg)
            if not isinstance(item, Mapping):
                raise TransitionValidationError(f"{leg.upper()} verification record is required")
            order_id = self._required_text(item.get("order_id"), f"{leg.upper()} order_id")
            if order_id in order_ids:
                raise TransitionValidationError("TP and SL must have distinct exchange order IDs")
            order_ids.add(order_id)
            if str(item.get("status", "")).strip().upper() != "ACTIVE":
                raise TransitionValidationError(f"{leg.upper()} order is not ACTIVE")
            if str(item.get("position_id", "")).strip() != position_id:
                raise TransitionValidationError(f"{leg.upper()} is not linked to the same position")
            if str(item.get("side", "")).strip().upper() != expected_close_side:
                raise TransitionValidationError(f"{leg.upper()} order side does not close the {position_side} position")
            quantity = self._positive_number(item.get("quantity"), f"{leg.upper()} quantity")
            quantities.append(quantity)
            trigger_price = self._positive_number(item.get("trigger_price"), f"{leg.upper()} trigger_price")
            if position_side == "LONG":
                valid_direction = trigger_price > entry_price if leg == "tp" else trigger_price < entry_price
            else:
                valid_direction = trigger_price < entry_price if leg == "tp" else trigger_price > entry_price
            if not valid_direction:
                raise TransitionValidationError(f"{leg.upper()} trigger is on the wrong side of entry price")

        for quantity in quantities:
            if not math.isclose(quantity, position_quantity, rel_tol=1e-9, abs_tol=1e-12):
                raise TransitionValidationError("TP/SL quantity does not match the verified position size")
