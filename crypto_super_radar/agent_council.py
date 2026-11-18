"""Zero-cost, deterministic multi-agent council for paper-trade review.

This recreates the *workflow shape* of a multi-agent trading desk (specialist
analysts, bull/bear debate, trader proposal and risk/portfolio veto) using only
CRYPTO SUPER RADAR's already-computed evidence. It does not copy model weights,
call an LLM, fetch extra data, or change the twelve underlying gates.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

MANDATORY_GATE_IDS = frozenset(range(1, 13))
MAX_ESTIMATED_RISK_SCORE = 45.0
PROFILE_POLICIES = {
    "EASY": {
        "required_gate_ids": frozenset({1, 2, 3, 4, 5, 6, 9}),
        "min_pass_count": 7,
        "min_confirmation_score": 55.0,
    },
    "STANDARD": {
        "required_gate_ids": frozenset({1, 2, 3, 4, 5, 6, 8, 9}),
        "min_pass_count": 9,
        "min_confirmation_score": 65.0,
    },
    "STRICT": {
        "required_gate_ids": MANDATORY_GATE_IDS,
        "min_pass_count": 12,
        "min_confirmation_score": 75.0,
    },
}

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
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            invalid_ids += 1
            continue
        gate_id = raw_id
        if gate_id in gates:
            duplicate_ids.append(gate_id)
        else:
            gates[gate_id] = _gate_status(gate)

    metrics = getattr(result, "metrics", {})
    metrics = metrics if isinstance(metrics, dict) else {}
    execution_profile = _enum_text(metrics.get("execution_profile") or "STRICT")
    if execution_profile not in PROFILE_POLICIES:
        execution_profile = "STRICT"
    profile_policy = PROFILE_POLICIES[execution_profile]
    required_gate_ids = profile_policy["required_gate_ids"]
    missing_ids = sorted(required_gate_ids - set(gates))
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

    if duplicate_ids or invalid_ids:
        add_check(
            "Unique gate evidence",
            "UNKNOWN",
            "duplicate=" + str(duplicate_ids) + "; invalid_ids=" + str(invalid_ids),
        )
    elif unexpected_ids:
        add_check(
            "Known gate identifiers",
            "FAIL",
            "Unexpected gate IDs=" + str(unexpected_ids),
        )
    elif missing_ids:
        add_check(
            "Profile-required gate evidence",
            "UNKNOWN",
            "Missing required gate IDs=" + str(missing_ids),
        )
    else:
        statuses = [gates[gate_id] for gate_id in sorted(required_gate_ids)]
        pass_count = sum(value == "PASS" for value in gates.values())
        min_pass = int(profile_policy["min_pass_count"])
        if all(value == "PASS" for value in statuses) and pass_count >= min_pass:
            add_check(
                "Profile-required gate evidence",
                "PASS",
                f"{execution_profile} requires {sorted(required_gate_ids)} and at least {min_pass} PASS gates; observed {pass_count} PASS.",
            )
        elif any(value == "FAIL" for value in statuses):
            add_check(
                "Profile-required gate evidence",
                "FAIL",
                f"A required {execution_profile} gate failed: "
                + str({gate_id: gates[gate_id] for gate_id in required_gate_ids}),
            )
        else:
            add_check(
                "Profile-required gate evidence",
                "UNKNOWN",
                f"{execution_profile} requires {min_pass} PASS gates; observed {pass_count}.",
            )
        if execution_profile != "STRICT":
            add_check(
                "Additional research gates",
                "PASS",
                "Non-required research gates remain visible as advisory evidence; UNKNOWN/FAIL values were not rewritten as PASS.",
            )

    signal = _enum_text(getattr(result, "signal", None)).replace("_", " ")
    asset_name = str(getattr(result, "asset", "") or "")
    perpetual = asset_name.lower().startswith("perp:")
    if signal == "LONG":
        add_check("Radar trade proposal", "PASS", "Radar proposes LONG.")
    elif signal == "SHORT" and perpetual:
        add_check("Radar trade proposal", "PASS", "Perpetual radar proposes SHORT.")
    else:
        add_check(
            "Radar trade proposal", "FAIL",
            "Only LONG spot or LONG/SHORT perpetual proposals can be reviewed for paper entry.",
        )

    confirmation = _finite_number(getattr(result, "confirmation_score", None))
    if confirmation is None:
        add_check("Confirmation score", "UNKNOWN", "No finite confirmation score was supplied.")
    elif confirmation >= float(profile_policy["min_confirmation_score"]):
        minimum = float(profile_policy["min_confirmation_score"])
        add_check("Confirmation score", "PASS", f"Score {confirmation:.2f} meets the {minimum:.0f} {execution_profile} minimum.")
    else:
        minimum = float(profile_policy["min_confirmation_score"])
        add_check("Confirmation score", "FAIL", f"Score {confirmation:.2f} is below the {minimum:.0f} {execution_profile} minimum.")

    risk = _finite_number(getattr(result, "estimated_risk_score", None))
    if risk is None:
        add_check("Estimated risk", "UNKNOWN", "No finite risk score was supplied.")
    elif risk < MAX_ESTIMATED_RISK_SCORE:
        add_check("Estimated risk", "PASS", f"Risk {risk:.2f} is below the {MAX_ESTIMATED_RISK_SCORE:.0f} ceiling.")
    else:
        add_check("Estimated risk", "FAIL", f"Risk {risk:.2f} is at or above the {MAX_ESTIMATED_RISK_SCORE:.0f} ceiling.")

    entry = _finite_number(getattr(result, "entry_price", None))
    stop = _finite_number(getattr(result, "stop_loss", None))
    target = _finite_number(getattr(result, "take_profit", None))
    if entry is None or stop is None or target is None:
        add_check(
            "Stop-loss and take-profit plan", "UNKNOWN",
            "Finite entry, stop-loss and take-profit levels are all required.",
        )
    elif signal == "LONG" and 0 < stop < entry < target:
        add_check("Stop-loss and take-profit plan", "PASS", "LONG requires stop < entry < take-profit.")
    elif signal == "SHORT" and perpetual and 0 < target < entry < stop:
        add_check("Stop-loss and take-profit plan", "PASS", "SHORT requires take-profit < entry < stop.")
    else:
        add_check("Stop-loss and take-profit plan", "FAIL", "Entry/stop/target ordering does not match the proposed trade direction.")

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
        "version": "native-agent-council-v2",
        "mode": "LOCAL_RULES_ONLY",
        "execution_profile": execution_profile,
        "required_gate_ids": sorted(required_gate_ids),
        "min_pass_count": int(profile_policy["min_pass_count"]),
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


def build_challenge1_observer_report(
    evidence: Mapping[str, Any],
    *,
    signal: str = "UNKNOWN",
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an informational, candle-grounded observer report for Challenge 1.

    This report never authorizes, blocks, sizes, or submits an order. It checks the
    exact completed-candle evidence and execution reconciliation for audit/reporting.
    """
    evidence = evidence if isinstance(evidence, Mapping) else {}
    execution = execution if isinstance(execution, Mapping) else {}
    direction = str(signal or "UNKNOWN").strip().upper()
    if direction not in {"LONG", "SHORT", "NO_TRADE"}:
        direction = "UNKNOWN"

    def number(value: Any) -> float | None:
        return _finite_number(value)

    def stamp(value: Any) -> str:
        return str(value or "").strip().replace("Z", "+00:00")

    def candle_ok(row: Any) -> bool:
        if not isinstance(row, Mapping):
            return False
        vals = {key: number(row.get(key)) for key in ("open", "high", "low", "close", "volume")}
        if any(value is None for value in vals.values()):
            return False
        o, h, l, c, v = (vals[key] for key in ("open", "high", "low", "close", "volume"))
        return (
            bool(stamp(row.get("time_utc") or row.get("timestamp_utc") or row.get("time")))
            and min(o, h, l, c) > 0
            and v >= 0
            and h >= max(o, c, l)
            and l <= min(o, c, h)
        )

    checks: list[dict[str, str]] = []

    def add(check: str, status: str, detail: str) -> None:
        checks.append({"check": check, "status": status, "detail": detail})

    rows_15m = evidence.get("candles_15m")
    rows_15m = rows_15m if isinstance(rows_15m, list) else []
    rows_1h = evidence.get("candles_1h")
    rows_1h = rows_1h if isinstance(rows_1h, list) else []
    valid_15m = [row for row in rows_15m if candle_ok(row)]
    valid_1h = [row for row in rows_1h if candle_ok(row)]
    candle_times = [
        stamp(row.get("time_utc") or row.get("timestamp_utc") or row.get("time"))
        for row in valid_15m
    ]
    ordered = candle_times == sorted(candle_times) and len(candle_times) == len(set(candle_times))
    completed_only = evidence.get("completed_candles_only") is True
    if completed_only and len(valid_15m) >= 5 and len(valid_15m) == len(rows_15m) and ordered:
        add(
            "15m completed-candle integrity", "PASS",
            f"{len(valid_15m)} valid, unique, ascending 15m candles; evidence marks completed_candles_only=true.",
        )
    else:
        add(
            "15m completed-candle integrity", "FAIL",
            f"completed_only={completed_only}; rows={len(rows_15m)}; valid={len(valid_15m)}; ordered_unique={ordered}.",
        )

    confirmation = evidence.get("confirmation_candle")
    if not valid_15m or not isinstance(confirmation, Mapping):
        add("Confirmation candle match", "FAIL", "The final 15m candle or confirmation-candle record is missing.")
    else:
        last = valid_15m[-1]
        time_match = stamp(last.get("time_utc")) == stamp(confirmation.get("open_time_utc"))
        ohlc_match = all(
            number(last.get(key)) is not None
            and number(confirmation.get(key)) is not None
            and abs(float(last[key]) - float(confirmation[key])) <= max(1e-10, abs(float(last[key])) * 1e-9)
            for key in ("open", "high", "low", "close")
        )
        add(
            "Confirmation candle match", "PASS" if time_match and ohlc_match else "FAIL",
            "The final chart candle matches the decision candle." if time_match and ohlc_match
            else f"time_match={time_match}; ohlc_match={ohlc_match}.",
        )

    chosen = evidence.get("chosen_pattern")
    if direction in {"LONG", "SHORT"}:
        expected = "BULLISH" if direction == "LONG" else "BEARISH"
        actual = str(chosen.get("direction") or "").upper() if isinstance(chosen, Mapping) else ""
        if actual == expected:
            add("Selected candlestick pattern", "PASS", f"{chosen.get('name_en') or chosen.get('name') or 'Pattern'} agrees with {direction}.")
        else:
            add("Selected candlestick pattern", "FAIL", f"Expected {expected}; observed {actual or 'missing pattern'}.")
    elif direction == "NO_TRADE":
        add("Selected candlestick pattern", "PASS", "NO_TRADE is retained as an explicit decision; no pattern is fabricated.")
    else:
        add("Selected candlestick pattern", "UNKNOWN", "No valid directional decision was supplied.")

    chart_structure = evidence.get("chart_structure")
    if isinstance(chart_structure, Mapping) and chart_structure:
        structure_status = str(chart_structure.get("status") or "UNKNOWN").upper()
        detail = str(chart_structure.get("pattern") or chart_structure.get("reason") or structure_status)
        add(
            "Chart-structure observer",
            "PASS" if structure_status not in {"UNKNOWN", "ERROR", "UNAVAILABLE"} else "WARNING",
            detail[:500],
        )
    else:
        add("Chart-structure observer", "WARNING", "No structured chart-pattern report was attached.")

    patterns = evidence.get("detected_patterns")
    patterns = patterns if isinstance(patterns, list) else []
    add(
        "Detected-pattern evidence",
        "PASS" if patterns or direction == "NO_TRADE" else "WARNING",
        f"{len(patterns)} detected pattern/context items are preserved.",
    )
    add(
        "1H context candles",
        "PASS" if valid_1h and len(valid_1h) == len(rows_1h) else "WARNING",
        f"{len(valid_1h)} valid 1H context candles; 1H context is advisory and never changes the entry decision.",
    )

    trade_executed = execution.get("trade_executed") is True
    closed = execution.get("closed_confirmed") is True
    order_id = str(execution.get("exchange_order_id") or execution.get("order_id") or "").strip()
    position_id = str(execution.get("position_id") or "").strip()
    if trade_executed:
        if order_id and position_id:
            add("Order/position linkage", "PASS", "The executed Demo trade has both an exchange order ID and an exact position ID.")
        else:
            add("Order/position linkage", "WARNING", f"order_id_present={bool(order_id)}; position_id_present={bool(position_id)}.")
        realized = number(execution.get("realized_net_pnl_usdt"))
        if closed and realized is not None:
            add("Realized outcome evidence", "PASS", "Closed-position exchange history supplied a realized net PnL value.")
        else:
            add(
                "Realized outcome evidence", "WARNING",
                "Position remains open or the final realized PnL has not yet been reconciled; floating PnL is not used as realized PnL.",
            )
    else:
        add("Order/position linkage", "PASS", "No trade was executed; no order or position ID is invented.")

    failures = sum(item["status"] == "FAIL" for item in checks)
    warnings = sum(item["status"] in {"WARNING", "UNKNOWN"} for item in checks)
    status = "INCOMPLETE" if failures else "NEEDS_REVIEW" if warnings else "COMPLETE"
    recommendations = [
        item["detail"] for item in checks if item["status"] != "PASS"
    ]
    return {
        "observer_version": "CH1_NATIVE_OBSERVER_COUNCIL_V1",
        "status": status,
        "execution_role": "OBSERVE_ONLY",
        "entry_gate_enforced": False,
        "trade_gate_impact": False,
        "signal": direction,
        "observers": [
            {"name": "Candle integrity observer", "status": next(x["status"] for x in checks if x["check"] == "15m completed-candle integrity")},
            {"name": "Candlestick pattern observer", "status": next(x["status"] for x in checks if x["check"] == "Selected candlestick pattern")},
            {"name": "Chart structure observer", "status": next(x["status"] for x in checks if x["check"] == "Chart-structure observer")},
            {"name": "Multi-timeframe context observer", "status": next(x["status"] for x in checks if x["check"] == "1H context candles")},
            {"name": "Execution and outcome observer", "status": next(x["status"] for x in checks if x["check"] == "Order/position linkage")},
        ],
        "candle_evidence": {
            "setup_timeframe": "15m",
            "context_timeframe": "1H",
            "completed_candles_only": completed_only,
            "candles_15m_count": len(valid_15m),
            "candles_1h_count": len(valid_1h),
            "confirmation_candle": dict(confirmation) if isinstance(confirmation, Mapping) else None,
            "chosen_pattern": dict(chosen) if isinstance(chosen, Mapping) else None,
            "detected_patterns": patterns[:20],
            "chart_structure": dict(chart_structure) if isinstance(chart_structure, Mapping) else {},
            "volume_confirmation": dict(evidence.get("volume_confirmation")) if isinstance(evidence.get("volume_confirmation"), Mapping) else {},
        },
        "checks": checks,
        "recommendations": recommendations,
        "summary_fa": (
            f"گزارش ناظران Challenge 1: {status}؛ {len(valid_15m)} کندل ۱۵دقیقه‌ای، "
            f"{len(valid_1h)} کندل یک‌ساعته، {len(patterns)} الگو/شاهد ثبت‌شده."
        ),
        "safety": {
            "orders_submitted": False,
            "changes_to_trade_signal": False,
            "changes_to_risk_limits": False,
            "changes_to_legacy_gates": False,
        },
    }
