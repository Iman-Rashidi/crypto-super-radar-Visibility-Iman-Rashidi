"""Evidence-complete, fail-closed token-risk sub-gate.

This is a security sub-gate only. It never authorizes a trade and it does not
treat missing/partial/budget-deferred evidence as a zero-risk observation.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

REQUIRED_TOKEN_RISK_CHECKS = (
    "contract_authority",
    "holder_concentration",
    "deployer_history",
    "launch_bundle",
    "liquidity_exit",
)
_ALLOWED_STATUSES = frozenset({"PASS", "FAIL", "UNKNOWN", "NOT_CHECKED_BUDGET", "STALE", "PARTIAL"})


MAX_EVIDENCE_AGE_SECONDS = 300.0
MAX_FUTURE_SKEW_SECONDS = 30.0


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _coverage(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or number > 100:
        return None
    return number


def _normalise_check(name: str, raw: Any, *, now: datetime) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {
            "name": name, "status": "UNKNOWN", "source": None,
            "checked_at": None, "coverage_pct": None, "value": None,
            "reason": "Required evidence record is missing or malformed.",
        }
    source_raw = raw.get("source")
    source = source_raw.strip() if isinstance(source_raw, str) and source_raw.strip() else None
    checked_raw = raw.get("checked_at")
    checked_at = checked_raw.strip() if isinstance(checked_raw, str) and checked_raw.strip() else None
    coverage_pct = _coverage(raw.get("coverage_pct"))
    value = raw.get("value")
    status_raw = raw.get("status")
    status = status_raw.strip().upper() if isinstance(status_raw, str) else "UNKNOWN"
    reason_raw = raw.get("reason")
    reason = reason_raw.strip() if isinstance(reason_raw, str) and reason_raw.strip() else None

    if status not in _ALLOWED_STATUSES:
        status = "UNKNOWN"
        reason = reason or "Unsupported evidence status."
    checked_time = _timestamp(checked_at)
    if not source or checked_time is None:
        status = "UNKNOWN"
        reason = reason or "Evidence source and timezone-aware checked_at are required."
    elif status in {"PASS", "FAIL"}:
        age_seconds = (now - checked_time).total_seconds()
        if age_seconds < -MAX_FUTURE_SKEW_SECONDS or age_seconds > MAX_EVIDENCE_AGE_SECONDS:
            status = "STALE"
            reason = reason or (
                f"Evidence is outside the freshness window ({MAX_EVIDENCE_AGE_SECONDS:.0f}s)."
            )
    if status == "PASS" and (coverage_pct != 100.0 or value is None):
        status = "UNKNOWN"
        reason = reason or "PASS requires 100% coverage and an explicit observed value."
    if status == "FAIL" and value is None:
        status = "UNKNOWN"
        reason = reason or "FAIL requires an explicit source-backed disqualifying value."
    return {
        "name": name, "status": status, "source": source,
        "checked_at": checked_at, "coverage_pct": coverage_pct,
        "value": value, "reason": reason,
    }


def evaluate_token_security(
    evidence: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate all mandatory risk inputs without inventing missing evidence."""
    supplied = evidence if isinstance(evidence, Mapping) else {}
    checked_now = now or datetime.now(timezone.utc)
    if checked_now.tzinfo is None:
        checked_now = checked_now.replace(tzinfo=timezone.utc)
    checked_now = checked_now.astimezone(timezone.utc)
    checks = {
        name: _normalise_check(name, supplied.get(name), now=checked_now)
        for name in REQUIRED_TOKEN_RISK_CHECKS
    }
    failed = [name for name, row in checks.items() if row["status"] == "FAIL"]
    incomplete = [name for name, row in checks.items() if row["status"] != "PASS"]
    if failed:
        status = "FAIL"
        reason = "At least one mandatory token-risk check returned an explicit source-backed disqualifier."
    elif not incomplete:
        status = "PASS"
        reason = "All mandatory token-risk checks passed with complete, source-attributed evidence."
    else:
        status = "UNKNOWN"
        reason = "One or more mandatory token-risk checks are missing, partial, stale or unverified."
    return {
        "security_gate_status": status,
        "required_check_count": len(REQUIRED_TOKEN_RISK_CHECKS),
        "passed_check_count": sum(row["status"] == "PASS" for row in checks.values()),
        "failed_check_count": len(failed),
        "incomplete_check_count": len(incomplete),
        "failed_checks": failed,
        "incomplete_checks": incomplete,
        "checks": checks,
        "reason": reason,
        "trade_authorization": "NO_TRADE",
        "trade_authorization_reason": (
            "Token security is only one mandatory sub-gate; this module never issues BUY or SELL authorization."
        ),
    }
