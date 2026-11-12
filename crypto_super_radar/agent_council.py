"""Zero-cost, deterministic multi-agent council for paper-trade review.

This recreates the *workflow shape* of a multi-agent trading desk (specialist
analysts, bull/bear debate, trader proposal and risk/portfolio veto) using only
CRYPTO SUPER RADAR's already-computed evidence. It does not copy model weights,
call an LLM, fetch extra data, or change the twelve underlying gates.
"""
from __future__ import annotations

import math
from typing import Any

MANDATORY_GATE_IDS = frozenset(range(1, 13))
MIN_CONFIRMATION_SCORE = 75.0
MAX_ESTIMATED_RISK_SCORE = 45.0

_AGENT_GROUPS = (
    ("Data integrity analyst", "تحلیل‌گر صحت داده", (1,)),
    ("Market regime analyst", "تحلیل‌گر رژیم بازار", (2,)),
    ("Technical analyst", "تحلیل‌گر تکنیکال", (3, 4)),
    ("Liquidity and capital-flow analyst", "تحلیل‌گر نقدینگی و جریان سرمایه", (5,)),
    ("On-chain and contract-risk analyst", "تحلیل‌گر آن‌چین و ریسک قرارداد", (6,)),
    ("News and sentiment analyst", "تحلیل‌گر خبر و احساسات", (7, 11)),
    ("Strategy validation analyst", "تحلیل‌گر اعتبارسنجی استراتژی", (8, 12)),
    ("Whale intelligence analyst", "تحلیل‌گر رفتار نهنگ‌ها", (10,)),
    ("Portfolio risk analyst", "مدیر ریسک پرتفوی", (9,)),
)


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _gate_status(gate: Any) -> str:
    status = _enum_text(getattr(gate, "status", None))
    return status if status in {"PASS", "FAIL", "UNKNOWN"} else "UNKNOWN"


def build_native_agent_council_review(result: Any) -> dict[str, Any]:
    """Build an explainable, fail-closed second review from one radar result."""
    raw_gates = getattr(result, "gates", None)
    raw_gates = raw_gates if isinstance(raw_gates, (list, tuple)) else []
    gates: dict[int, str] = {}
    duplicate_ids: list[int] = []
    invalid_ids = 0

    for gate in raw_gates:
        raw_id = getattr(gate, "gate_id", None)
        if isinstance(raw_id, bool):
            invalid_ids += 1
            continue
        try:
            gate_id = int(raw_id)
        except (TypeError, ValueError, OverflowError):
            invalid_ids += 1
            continue
        if gate_id in gates:
            duplicate_ids.append(gate_id)
        else:
            gates[gate_id] = _gate_status(gate)

    missing_ids = sorted(MANDATORY_GATE_IDS - set(gates))
    unexpected_ids = sorted(set(gates) - MANDATORY_GATE_IDS)
    duplicate_ids = sorted(set(duplicate_ids))

    agents: list[dict[str, Any]] = []
    supporting: list[str] = []
    opposing: list[str] = []
    abstaining: list[str] = []

    for name, name_fa, gate_ids in _AGENT_GROUPS:
        statuses = [gates.get(gate_id, "UNKNOWN") for gate_id in gate_ids]
        if "FAIL" in statuses:
            status = "FAIL"
            stance = "BEARISH_OBJECTION"
            opposing.append(name_fa)
        elif all(value == "PASS" for value in statuses):
            status = "PASS"
            stance = "BULLISH_SUPPORT"
            supporting.append(name_fa)
        else:
            status = "UNKNOWN"
            stance = "ABSTAIN"
            abstaining.append(name_fa)
        agents.append({
            "name": name,
            "name_fa": name_fa,
            "gate_ids": list(gate_ids),
            "status": status,
            "stance": stance,
        })

    checks: list[dict[str, str]] = []

    def add_check(label: str, status: str, detail: str) -> None:
        checks.append({"check": label, "status": status, "detail": detail})

    if missing_ids or duplicate_ids or invalid_ids:
        add_check(
            "Complete, unique 12-gate evidence",
            "UNKNOWN",
            "Missing=" + str(missing_ids) + "; duplicate=" + str(duplicate_ids)
            + "; invalid_ids=" + str(invalid_ids),
        )
    elif unexpected_ids:
        add_check(
            "Exact mandatory gate set",
            "FAIL",
            "Unexpected gate IDs=" + str(unexpected_ids),
        )
    else:
        statuses = [gates[gate_id] for gate_id in sorted(MANDATORY_GATE_IDS)]
        if all(value == "PASS" for value in statuses):
            add_check("All 12 mandatory gates", "PASS", "All twelve gates report PASS.")
        elif "FAIL" in statuses:
            add_check("All 12 mandatory gates", "FAIL", "At least one mandatory gate reports FAIL.")
        else:
            add_check("All 12 mandatory gates", "UNKNOWN", "At least one mandatory gate is UNKNOWN.")

    signal = _enum_text(getattr(result, "signal", None)).replace("_", " ")
    if signal == "LONG":
        add_check("Radar trade proposal", "PASS", "Radar proposes LONG.")
    else:
        add_check("Radar trade proposal", "FAIL", "Only a LONG spot proposal can be reviewed for paper entry.")

    confirmation = _finite_number(getattr(result, "confirmation_score", None))
    if confirmation is None:
        add_check("Confirmation score", "UNKNOWN", "No finite confirmation score was supplied.")
    elif confirmation >= MIN_CONFIRMATION_SCORE:
        add_check("Confirmation score", "PASS", f"Score {confirmation:.2f} meets the {MIN_CONFIRMATION_SCORE:.0f} minimum.")
    else:
        add_check("Confirmation score", "FAIL", f"Score {confirmation:.2f} is below the {MIN_CONFIRMATION_SCORE:.0f} minimum.")

    risk = _finite_number(getattr(result, "estimated_risk_score", None))
    if risk is None:
        add_check("Estimated risk", "UNKNOWN", "No finite risk score was supplied.")
    elif risk < MAX_ESTIMATED_RISK_SCORE:
        add_check("Estimated risk", "PASS", f"Risk {risk:.2f} is below the {MAX_ESTIMATED_RISK_SCORE:.0f} ceiling.")
    else:
        add_check("Estimated risk", "FAIL", f"Risk {risk:.2f} is at or above the {MAX_ESTIMATED_RISK_SCORE:.0f} ceiling.")

    stop = _finite_number(getattr(result, "stop_loss", None))
    target = _finite_number(getattr(result, "take_profit", None))
    if stop is None or target is None:
        add_check("Stop-loss and take-profit plan", "UNKNOWN", "Both risk levels must be finite numbers.")
    elif 0 < stop < target:
        add_check("Stop-loss and take-profit plan", "PASS", "Positive LONG stop and target are correctly ordered.")
    else:
        add_check("Stop-loss and take-profit plan", "FAIL", "LONG stop/target levels are not safely ordered.")

    check_statuses = [row["status"] for row in checks]
    if "FAIL" in check_statuses:
        status = "VETO"
        status_fa = "مدیر ریسک ورود آزمایشی را رد کرد"
    elif "UNKNOWN" in check_statuses:
        status = "UNKNOWN"
        status_fa = "شواهد برای تأیید ورود کافی نیست"
    else:
        status = "APPROVED"
        status_fa = "بررسی بومی ورود آزمایشی را تأیید کرد"

    blockers = [
        row["detail"] for row in checks if row["status"] != "PASS"
    ]
    reason_fa = (
        "تمام بررسی‌های شورا عبور کردند؛ کنترل‌های اجرایی ربات هنوز جداگانه اعمال می‌شوند."
        if status == "APPROVED" else
        "شورا ورود را متوقف کرد: " + "؛ ".join(blockers)
    )

    return {
        "version": "native-agent-council-v1",
        "mode": "LOCAL_RULES_ONLY",
        "status": status,
        "status_fa": status_fa,
        "paper_entry_eligible": status == "APPROVED",
        "proposed_signal": signal or "UNKNOWN",
        "agents": agents,
        "debate": {
            "bullish_support": supporting,
            "bearish_objections": opposing,
            "abstentions": abstaining,
        },
        "risk_manager": {
            "status": status,
            "checks": checks,
            "blockers": blockers,
        },
        "reason_fa": reason_fa,
        "safety": {
            "llm_calls": 0,
            "external_data_fetches": 0,
            "changes_to_radar_gates": False,
            "orders_submitted": False,
        },
    }
