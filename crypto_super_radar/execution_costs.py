"""Per-fill execution-cost decomposition with explicit completeness.

Spread, adverse slippage, fees and funding remain separate. Missing data is
UNKNOWN, not zero. Quote provenance is mandatory.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


def _number(value: Any, field: str, *, allow_zero: bool = True, allow_negative: bool = False) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} is missing or invalid")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} is missing or invalid") from exc
    if not math.isfinite(number) or (not allow_negative and number < 0) or (not allow_zero and number == 0):
        raise ValueError(f"{field} is missing or invalid")
    return number


def calculate_fill_costs(
    *,
    side: str,
    quantity: Any,
    bid: Any,
    ask: Any,
    fill_price: Any,
    fee_paid_usd: Any,
    funding_applicable: bool,
    funding_paid_usd: Any = None,
    quote_quality: str,
) -> dict[str, Any]:
    """Calculate one fill's spread, adverse slippage, fee and funding costs."""
    side_name = str(side or "").strip().upper()
    if side_name not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if not isinstance(funding_applicable, bool):
        raise ValueError("funding_applicable must be an explicit boolean")
    quote_kind = str(quote_quality or "").strip().upper()
    if quote_kind not in {"OBSERVED", "MODELLED"}:
        raise ValueError("quote_quality must be OBSERVED or MODELLED")

    qty = _number(quantity, "quantity", allow_zero=False)
    best_bid = _number(bid, "bid", allow_zero=False)
    best_ask = _number(ask, "ask", allow_zero=False)
    fill = _number(fill_price, "fill_price", allow_zero=False)
    fee = _number(fee_paid_usd, "fee_paid_usd")
    if best_ask < best_bid:
        raise ValueError("ask must be greater than or equal to bid")

    midpoint = (best_bid + best_ask) / 2.0
    half_spread = (best_ask - best_bid) / 2.0
    spread_cost = qty * half_spread
    touch = best_ask if side_name == "BUY" else best_bid
    adverse_per_unit = max(0.0, fill - touch) if side_name == "BUY" else max(0.0, touch - fill)
    slippage_cost = qty * adverse_per_unit

    if funding_applicable:
        funding = _number(funding_paid_usd, "funding_paid_usd", allow_negative=True)
        funding_status = "RECORDED"
    else:
        if funding_paid_usd not in (None, 0, 0.0):
            raise ValueError("funding_paid_usd must be null/zero when funding is not applicable")
        funding = 0.0
        funding_status = "NOT_APPLICABLE"

    total = spread_cost + slippage_cost + fee + funding
    return {
        "status": "COMPLETE",
        "side": side_name,
        "quantity": qty,
        "bid": best_bid,
        "ask": best_ask,
        "midpoint": midpoint,
        "fill_price": fill,
        "quote_quality": quote_kind,
        "spread_cost_usd": round(spread_cost, 10),
        "slippage_cost_usd": round(slippage_cost, 10),
        "fee_cost_usd": round(fee, 10),
        "funding_cost_usd": round(funding, 10),
        "funding_status": funding_status,
        "total_execution_cost_usd": round(total, 10),
        "cost_components": ["spread", "adverse_slippage", "fee", "funding"],
    }


def summarize_fill_costs(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate execution costs only when every component has full coverage."""
    materialized = list(rows)
    normalized = [row for row in materialized if isinstance(row, Mapping)]
    components = ("spread_cost_usd", "slippage_cost_usd", "fee_cost_usd", "funding_cost_usd")
    totals: dict[str, float | None] = {}
    coverage: dict[str, float | None] = {}
    for component in components:
        values: list[float] = []
        missing = 0
        for row in normalized:
            value = row.get(component)
            if component == "funding_cost_usd" and row.get("funding_status") == "NOT_APPLICABLE" and value == 0:
                values.append(0.0)
                continue
            if isinstance(value, bool) or value is None:
                missing += 1
                continue
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                missing += 1
                continue
            if not math.isfinite(number):
                missing += 1
                continue
            values.append(number)
        coverage[component] = round((len(normalized) - missing) / len(normalized) * 100.0, 4) if normalized else None
        totals[component] = round(sum(values), 10) if normalized and missing == 0 else None

    complete = bool(normalized) and all(value == 100.0 for value in coverage.values())
    return {
        "status": "NO_DATA" if not materialized else "COMPLETE" if complete and not invalid_rows(materialized) else "INCOMPLETE",
        "fill_count": len(normalized),
        "invalid_rows_excluded": invalid_rows(materialized),
        "component_coverage_pct": coverage,
        "component_totals_usd": totals,
        "total_execution_cost_usd": (
            round(sum(float(totals[key]) for key in components), 10)
            if complete and not invalid_rows(materialized) else None
        ),
        "performance_claim_allowed": False,
    }


def invalid_rows(rows: list[Any]) -> int:
    return sum(not isinstance(row, Mapping) for row in rows)
