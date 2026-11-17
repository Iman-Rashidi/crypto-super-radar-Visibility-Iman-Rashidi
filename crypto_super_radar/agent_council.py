"""Gate-free deterministic safety council for CRYPTO SUPER RADAR paper review.

The former twelve-gate framework is retired globally. Entry review is based on
the strategy's actual candle/pattern evidence plus independent execution-risk
checks. This module never submits or changes orders.
"""
from __future__ import annotations

import math
from typing import Any

MAX_ESTIMATED_RISK_SCORE = 45.0


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


def build_native_agent_council_review(result: Any) -> dict[str, Any]:
    """Review directional strategy output without consulting the retired gates."""
    metrics = getattr(result, "metrics", {})
    metrics = metrics if isinstance(metrics, dict) else {}
    signal = _enum_text(getattr(result, "signal", None)).replace("_", " ")
    asset_name = str(getattr(result, "asset", "") or "")
    perpetual = asset_name.lower().startswith("perp:")
    checks: list[dict[str, str]] = []

    def add_check(label: str, status: str, detail: str) -> None:
        checks.append({"check": label, "status": status, "detail": detail})

    if signal == "LONG":
        add_check("Strategy direction", "PASS", "Strategy proposes LONG.")
    elif signal == "SHORT" and perpetual:
        add_check("Strategy direction", "PASS", "Perpetual strategy proposes SHORT.")
    else:
        add_check("Strategy direction", "FAIL", "A directional LONG or eligible perpetual SHORT is required.")

    risk = _finite_number(getattr(result, "estimated_risk_score", None))
    if risk is None:
        add_check("Estimated execution risk", "UNKNOWN", "No finite execution-risk estimate was supplied.")
    elif risk < MAX_ESTIMATED_RISK_SCORE:
        add_check("Estimated execution risk", "PASS", f"Risk {risk:.2f} is below the {MAX_ESTIMATED_RISK_SCORE:.0f} ceiling.")
    else:
        add_check("Estimated execution risk", "FAIL", f"Risk {risk:.2f} is at or above the {MAX_ESTIMATED_RISK_SCORE:.0f} ceiling.")

    entry = _finite_number(getattr(result, "entry_price", None))
    stop = _finite_number(getattr(result, "stop_loss", None))
    target = _finite_number(getattr(result, "take_profit", None))
    if entry is None or stop is None or target is None:
        add_check("Entry and protective exit plan", "UNKNOWN", "Finite entry, stop-loss and take-profit levels are required.")
    elif signal == "LONG" and 0 < stop < entry < target:
        add_check("Entry and protective exit plan", "PASS", "LONG stop < entry < take-profit.")
    elif signal == "SHORT" and perpetual and 0 < target < entry < stop:
        add_check("Entry and protective exit plan", "PASS", "SHORT take-profit < entry < stop.")
    else:
        add_check("Entry and protective exit plan", "FAIL", "Entry/stop/target ordering does not match the strategy direction.")

    # Candle/pattern evidence is supplied by the strategy and must be explicit.
    # Older radar profiles may not yet publish this evidence, so they remain
    # ineligible rather than being approved from the retired gate score.
    pattern = metrics.get("challenge1_evidence")
    if isinstance(pattern, dict):
        pattern_status = str(pattern.get("validation_status") or "").upper()
        if pattern_status == "PASS":
            add_check("Candle and chart-pattern evidence", "PASS", "Strategy's closed-candle pattern evidence passed.")
        elif pattern_status == "FAIL":
            add_check("Candle and chart-pattern evidence", "FAIL", str(pattern.get("reason") or "Pattern evidence failed."))
        else:
            add_check("Candle and chart-pattern evidence", "UNKNOWN", "Closed-candle pattern evidence has not been validated.")
    else:
        add_check("Candle and chart-pattern evidence", "UNKNOWN", "No validated closed-candle pattern evidence is attached to this strategy result.")

    statuses = [item["status"] for item in checks]
    status = "VETO" if "FAIL" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "APPROVED"
    status_fa = {
        "APPROVED": "بررسی مستقل استراتژی و کنترل‌های ریسک تأیید شد",
        "VETO": "بررسی مستقل، ورود را به دلیل ریسک یا جهت نامعتبر رد کرد",
        "UNKNOWN": "شواهد کندلی/الگویی یا کنترل‌های اجرایی هنوز کامل نیست",
    }[status]
    blockers = [item["detail"] for item in checks if item["status"] != "PASS"]
    return {
        "version": "native-pattern-risk-council-v3",
        "mode": "LOCAL_RULES_ONLY",
        "execution_profile": str(metrics.get("execution_profile") or "PATTERN_BASED").upper(),
        "required_gate_ids": [],
        "retired_gate_framework": True,
        "status": status,
        "status_fa": status_fa,
        "paper_entry_eligible": status == "APPROVED",
        "proposed_signal": signal or "UNKNOWN",
        "agents": [],
        "debate": {"bullish_support": [], "bearish_objections": [], "abstentions": []},
        "risk_manager": {"status": status, "checks": checks, "blockers": blockers},
        "reason_fa": "؛ ".join(blockers) if blockers else "الگوی بسته‌شده و کنترل‌های ریسک عبور کردند.",
        "safety": {
            "llm_calls": 0,
            "external_data_fetches": 0,
            "changes_to_radar_gates": False,
            "orders_submitted": False,
        },
    }
