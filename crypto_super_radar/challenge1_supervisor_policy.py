"""Fail-closed policy for active Challenge 1 position supervisors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


_ALLOWED_ACTIONS = {
    "HOLD_POSITION",
    "TIGHTEN_STOP",
    "CLOSE_POSITION",
    "URGENT_RISK_ALERT",
    "REQUEST_DATA",
}


def _decimal_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return format(number.normalize(), "f")


def coordinate_position_supervisors(
    reviewers: Sequence[Mapping[str, Any]],
    *,
    evidence_hash: str,
    experience_id: str,
    position_id: str,
    current_position_id: str,
    now_utc: datetime | None = None,
    max_result_age_seconds: float = 30.0,
) -> dict[str, Any]:
    """Authorize only exact, fresh, independently validated two-reviewer votes.

    The coordinator never executes an exchange action. Execution is delegated to
    a Demo-only gateway which re-reads the live position and protective orders.
    """
    base = {
        "experience_id": experience_id,
        "position_id": position_id,
        "evidence_hash": evidence_hash,
        "execution_authorized": False,
        "action": "NO_ACTION",
        "reason_code": "",
    }
    if not experience_id or not position_id or position_id != current_position_id:
        return {**base, "status": "BLOCKED", "reason_code": "POSITION_LINK_MISMATCH"}
    if not evidence_hash:
        return {**base, "status": "BLOCKED", "reason_code": "EVIDENCE_HASH_MISSING"}
    if not isinstance(reviewers, Sequence) or len(reviewers) != 2:
        return {**base, "status": "UNKNOWN", "reason_code": "TWO_REVIEWERS_REQUIRED"}

    by_id: dict[str, Mapping[str, Any]] = {}
    for item in reviewers:
        if not isinstance(item, Mapping):
            return {**base, "status": "UNKNOWN", "reason_code": "MALFORMED_REVIEWER"}
        reviewer_id = str(item.get("reviewer_id") or "").strip().lower()
        if reviewer_id not in {"nemotron", "second_ai"} or reviewer_id in by_id:
            return {**base, "status": "UNKNOWN", "reason_code": "REVIEWER_IDENTITY_INVALID"}
        by_id[reviewer_id] = item
    if set(by_id) != {"nemotron", "second_ai"}:
        return {**base, "status": "UNKNOWN", "reason_code": "TWO_DISTINCT_REVIEWERS_REQUIRED"}

    sampled_at = now_utc or datetime.now(timezone.utc)
    if sampled_at.tzinfo is None:
        sampled_at = sampled_at.replace(tzinfo=timezone.utc)
    for reviewer_id, item in by_id.items():
        if (
            str(item.get("status") or "").upper() != "REVIEWED"
            or item.get("response_validated") is not True
            or item.get("model_verified") is not True
            or item.get("request_attempted") is not True
            or str(item.get("evidence_hash") or "") != evidence_hash
        ):
            return {
                **base,
                "status": "UNKNOWN",
                "reason_code": f"{reviewer_id.upper()}_RESULT_NOT_FRESH_AND_VALID",
            }
        completed_raw = item.get("completed_at_utc")
        try:
            completed_at = datetime.fromisoformat(str(completed_raw).replace("Z", "+00:00"))
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            age_seconds = (sampled_at - completed_at.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            age_seconds = float("inf")
        if age_seconds < -5.0 or age_seconds > max_result_age_seconds:
            return {
                **base,
                "status": "UNKNOWN",
                "reason_code": f"{reviewer_id.upper()}_RESULT_STALE",
                "result_age_seconds": age_seconds if age_seconds != float("inf") else None,
            }
        action = str(item.get("action") or "").strip().upper()
        if action not in _ALLOWED_ACTIONS:
            return {
                **base,
                "status": "REJECTED",
                "reason_code": f"{reviewer_id.upper()}_ACTION_NOT_ALLOWED",
            }

    first = by_id["nemotron"]
    second = by_id["second_ai"]
    a_action = str(first.get("action") or "").strip().upper()
    b_action = str(second.get("action") or "").strip().upper()

    # A model must not pair a benign verdict with a destructive action,
    # or claim a critical data gap while authorizing execution.
    for reviewer_id, item in by_id.items():
        verdict = str(item.get("verdict") or "").strip().upper()
        action = str(item.get("action") or "").strip().upper()
        if verdict not in {"SUPPORTS_DECISION", "CONCERNS", "INSUFFICIENT_DATA"}:
            return {
                **base,
                "status": "REJECTED",
                "reason_code": f"{reviewer_id.upper()}_VERDICT_INVALID",
            }
        expected = {
            "HOLD_POSITION": "SUPPORTS_DECISION",
            "TIGHTEN_STOP": "CONCERNS",
            "CLOSE_POSITION": "CONCERNS",
            "URGENT_RISK_ALERT": "CONCERNS",
            "REQUEST_DATA": "INSUFFICIENT_DATA",
        }[action]
        if verdict != expected:
            return {
                **base,
                "status": "REJECTED",
                "reason_code": f"{reviewer_id.upper()}_ACTION_VERDICT_CONTRADICTION",
            }
        if action in {"TIGHTEN_STOP", "CLOSE_POSITION", "URGENT_RISK_ALERT"}:
            flags = item.get("risk_flags")
            if not isinstance(flags, list) or not any(isinstance(flag, str) and flag.strip() for flag in flags):
                return {
                    **base,
                    "status": "REJECTED",
                    "reason_code": f"{reviewer_id.upper()}_RISK_EVIDENCE_MISSING",
                }

    if a_action == "URGENT_RISK_ALERT" or b_action == "URGENT_RISK_ALERT":
        return {
            **base,
            "status": "RISK_ALERT",
            "action": "URGENT_RISK_ALERT",
            "reason_code": "SUPERVISOR_RAISED_URGENT_RISK_ALERT",
            "block_new_entries": True,
        }
    if a_action != b_action:
        return {
            **base,
            "status": "DISAGREEMENT",
            "reason_code": "SUPERVISOR_ACTIONS_DISAGREE",
        }
    if a_action == "REQUEST_DATA":
        return {
            **base,
            "status": "DATA_REQUIRED",
            "reason_code": "SUPERVISORS_REQUESTED_MORE_DATA",
        }
    if a_action == "HOLD_POSITION":
        return {
            **base,
            "status": "AGREED_NO_ACTION",
            "action": "HOLD_POSITION",
            "reason_code": "BOTH_SUPERVISORS_AGREE_HOLD",
        }
    if a_action == "CLOSE_POSITION":
        return {
            **base,
            "status": "AUTHORIZED",
            "action": "CLOSE_POSITION",
            "execution_authorized": True,
            "reason_code": "BOTH_SUPERVISORS_AGREE_CLOSE",
        }
    if a_action == "TIGHTEN_STOP":
        stop_a = _decimal_text(first.get("proposed_stop_loss_price"))
        stop_b = _decimal_text(second.get("proposed_stop_loss_price"))
        if not stop_a or not stop_b:
            return {
                **base,
                "status": "REJECTED",
                "reason_code": "STOP_PRICE_MISSING_OR_INVALID",
            }
        if stop_a != stop_b:
            return {
                **base,
                "status": "DISAGREEMENT",
                "reason_code": "SUPERVISORS_PROPOSE_DIFFERENT_STOPS",
            }
        return {
            **base,
            "status": "AUTHORIZED",
            "action": "TIGHTEN_STOP",
            "proposed_stop_loss_price": stop_a,
            "execution_authorized": True,
            "reason_code": "BOTH_SUPERVISORS_AGREE_TIGHTEN_STOP",
        }
    return {**base, "status": "REJECTED", "reason_code": "ACTION_NOT_IMPLEMENTED"}
