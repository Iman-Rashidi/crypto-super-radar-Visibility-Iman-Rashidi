"""Declarative policy for the two independent Demo strategy profiles.

The result is an eligibility assessment only. It never submits an order and
cannot override exchange account rules, stop/target protection or STOP BOT.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class EngineProfile:
    engine_id: str
    strategy_risk_pct: float
    requires_12_gates: bool
    description: str


AGGRESSIVE = EngineProfile(
    engine_id="AGGRESSIVE_HIGH_RISK",
    strategy_risk_pct=0.010,
    requires_12_gates=False,
    description="Opportunity-oriented Demo experiments; skips only the 12-gate strategy screen.",
)
STRICT = EngineProfile(
    engine_id="STRICT_12_GATE",
    strategy_risk_pct=0.005,
    requires_12_gates=True,
    description="Every one of the twelve mandatory gates must PASS exactly once.",
)
SHARED_PORTFOLIO_RISK_CAP_PCT = 0.020
ENGINE_PROFILES = {item.engine_id: item for item in (AGGRESSIVE, STRICT)}


def _gates_pass(gates: Any) -> bool:
    if not isinstance(gates, (list, tuple)) or len(gates) != 12:
        return False
    seen: set[int] = set()
    for gate in gates:
        gate_id = getattr(gate, "gate_id", None)
        status = getattr(gate, "status", None)
        status = str(getattr(status, "value", status)).upper()
        if isinstance(gate_id, bool) or not isinstance(gate_id, int):
            return False
        if gate_id in seen or status != "PASS":
            return False
        seen.add(gate_id)
    return seen == set(range(1, 13))


def evaluate_demo_candidate(
    engine_id: str, *, gates: Any, current_open_risk_pct: float,
    data_valid: bool, account_valid: bool, instrument_supported: bool,
    protective_orders_ready: bool, stop_target_valid: bool,
    nemotron_status: str, duplicate_signal: bool = False,
    stop_bot: bool = False,
) -> dict[str, Any]:
    """Evaluate universal guards and profile-specific strategy screen, without ordering."""
    profile = ENGINE_PROFILES.get(str(engine_id).upper())
    if profile is None:
        return {"eligible": False, "reason": "UNKNOWN_ENGINE", "execution_authorized": False}
    try:
        current = float(current_open_risk_pct)
    except (TypeError, ValueError, OverflowError):
        current = float("nan")
    if not math.isfinite(current) or current < 0:
        return {"eligible": False, "reason": "INVALID_PORTFOLIO_RISK", "execution_authorized": False}
    blockers = []
    if stop_bot:
        blockers.append("STOP_BOT_ACTIVE")
    if not data_valid:
        blockers.append("MARKET_DATA_INVALID")
    if not account_valid:
        blockers.append("DEMO_ACCOUNT_INVALID")
    if not instrument_supported:
        blockers.append("INSTRUMENT_UNSUPPORTED")
    if not protective_orders_ready:
        blockers.append("EXCHANGE_PROTECTIVE_ORDERS_NOT_READY")
    if not stop_target_valid:
        blockers.append("STOP_OR_TARGET_INVALID")
    if str(nemotron_status).upper() != "APPROVED":
        blockers.append("NEMOTRON_NOT_APPROVED")
    if duplicate_signal:
        blockers.append("DUPLICATE_SIGNAL_CANDLE")
    if profile.requires_12_gates and not _gates_pass(gates):
        blockers.append("STRICT_12_GATE_SCREEN_FAILED")
    budget = min(profile.strategy_risk_pct, SHARED_PORTFOLIO_RISK_CAP_PCT - current)
    if budget <= 0:
        blockers.append("SHARED_PORTFOLIO_RISK_CAP_REACHED")
        budget = 0.0
    return {
        "engine_id": profile.engine_id,
        "profile_risk_pct": profile.strategy_risk_pct,
        "available_risk_budget_pct": round(max(0.0, budget), 6),
        "shared_portfolio_risk_cap_pct": SHARED_PORTFOLIO_RISK_CAP_PCT,
        "strategy_gate_mode": "STRICT_12_REQUIRED" if profile.requires_12_gates else "SKIPPED_BY_AGGRESSIVE_PROFILE",
        "gates_reported_as_pass": False if not profile.requires_12_gates else _gates_pass(gates),
        "eligible": not blockers,
        "blockers": blockers,
        "reason": "POLICY_ELIGIBLE_ONLY" if not blockers else blockers[0],
        "execution_authorized": False,
        "note": "Eligibility is not order authorization; a separately implemented and tested Demo executor is required.",
    }
