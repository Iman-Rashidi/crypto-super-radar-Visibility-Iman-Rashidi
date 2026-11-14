"""Evidence-aware crypto fundamental analysis for the Easy trading engine.

This module is deliberately provider-agnostic: it computes only from supplied,
timestamped evidence and never invents missing fundamentals. It does not place
orders or authorize a trade.
"""
from __future__ import annotations

from math import isfinite
from statistics import median
from typing import Any, Mapping, Sequence

MODULE_WEIGHTS = {
    "macro": 15.0,
    "tokenomics": 15.0,
    "onchain": 15.0,
    "project": 15.0,
    "industry": 15.0,
    "dcf": 10.0,
    "comparables": 15.0,
}
MIN_COVERAGE_PCT = 70.0
VALUATION_METRICS = ("market_cap_to_revenue", "fdv_to_revenue", "market_cap_to_tvl", "fdv_to_tvl")


def _number(value: Any) -> float | None:
    """Return a finite float, otherwise None (never coerce missing data to zero)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _positive(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and result > 0 else None


def classify_industry(project: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a project's declared sector and expose missing classification."""
    sector = str(project.get("sector") or "").strip().lower()
    subsector = str(project.get("subsector") or "").strip().lower()
    if not sector:
        return {"status": "UNKNOWN", "sector": None, "subsector": None,
                "reason": "Project sector is not supplied by a verified source."}
    return {"status": "KNOWN", "sector": sector, "subsector": subsector or None}


def industry_analysis(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Score supplied sector evidence; each criterion must explicitly carry a score."""
    criteria = ("market_growth", "market_share", "competition", "entry_barriers",
                "technology_trend", "regulatory_environment", "adoption")
    scores: dict[str, float] = {}
    unknown: list[str] = []
    for key in criteria:
        item = evidence.get(key)
        value = _number(item.get("score")) if isinstance(item, Mapping) and item.get("status") == "KNOWN" else None
        if value is None or not 0 <= value <= 100:
            unknown.append(key)
        else:
            scores[key] = value
    if not scores:
        return {"status": "UNKNOWN", "score": None, "known_criteria": [],
                "unknown_criteria": unknown, "reason": "No valid, sourced industry criteria."}
    # Equal weights avoid pretending unsupported sector-specific weights are calibrated.
    score = sum(scores.values()) / len(scores)
    return {"status": "PARTIAL" if unknown else "KNOWN", "score": round(score, 2),
            "known_criteria": sorted(scores), "unknown_criteria": unknown}


def discounted_protocol_cashflows(
    cashflows: Sequence[Any],
    *,
    discount_rate: Any,
    terminal_growth_rate: Any = 0.0,
    token_value_capture_ratio: Any = None,
    net_treasury_value: Any = 0.0,
) -> dict[str, Any]:
    """Estimate token-attributable value from annual protocol cashflows.

    Cashflows must be forecast net cashflows attributable to the protocol.
    The token capture ratio must be explicitly supplied; protocol revenue alone
    is not assumed to accrue to token holders. Rates are decimals (e.g. 0.20).
    """
    rate = _number(discount_rate)
    growth = _number(terminal_growth_rate)
    capture = _number(token_value_capture_ratio)
    treasury = _number(net_treasury_value)
    flows = [_number(x) for x in cashflows]
    if (not flows or any(x is None for x in flows) or rate is None or growth is None
            or capture is None or treasury is None):
        return {"status": "UNKNOWN", "estimated_value": None,
                "reason": "DCF requires complete cashflows, discount/growth rates, token value-capture ratio and treasury value."}
    if not (0 <= growth < rate) or rate <= 0 or not 0 <= capture <= 1:
        return {"status": "UNKNOWN", "estimated_value": None,
                "reason": "DCF assumptions invalid: require 0 <= growth < discount rate and 0 <= token capture <= 1."}
    if any(flow < 0 for flow in flows):
        return {"status": "UNKNOWN", "estimated_value": None,
                "reason": "Negative forecast cashflows require an explicit turnaround model; this simple DCF rejects them."}
    captured = [float(flow) * capture for flow in flows]
    pv_flows = sum(flow / ((1 + rate) ** year) for year, flow in enumerate(captured, start=1))
    terminal = captured[-1] * (1 + growth) / (rate - growth)
    pv_terminal = terminal / ((1 + rate) ** len(captured))
    value = pv_flows + pv_terminal + treasury
    return {"status": "KNOWN", "estimated_value": round(value, 8),
            "present_value_cashflows": round(pv_flows, 8),
            "present_value_terminal": round(pv_terminal, 8),
            "net_treasury_value": round(treasury, 8),
            "token_value_capture_ratio": capture,
            "assumptions": {"discount_rate": rate, "terminal_growth_rate": growth,
                            "forecast_years": len(flows)},
            "warning": "Scenario estimate, not a market price or guaranteed fair value."}


def comparable_projects(project: Mapping[str, Any], peers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare like-for-like projects using valid positive denominator multiples."""
    sector = str(project.get("sector") or "").strip().lower()
    project_values: dict[str, float] = {}
    peer_values: dict[str, list[float]] = {key: [] for key in VALUATION_METRICS}
    exclusions = 0
    for key in VALUATION_METRICS:
        numerator_key, denominator_key = key.split("_to_")
        numerator = _positive(project.get(numerator_key))
        denominator = _positive(project.get(denominator_key))
        if numerator is not None and denominator is not None:
            project_values[key] = numerator / denominator
    for peer in peers:
        if not sector or str(peer.get("sector") or "").strip().lower() != sector:
            exclusions += 1
            continue
        for key in VALUATION_METRICS:
            numerator_key, denominator_key = key.split("_to_")
            numerator = _positive(peer.get(numerator_key))
            denominator = _positive(peer.get(denominator_key))
            if numerator is not None and denominator is not None:
                peer_values[key].append(numerator / denominator)
    medians = {key: round(median(values), 8) for key, values in peer_values.items() if values}
    relative = {}
    for key, value in project_values.items():
        if key in medians and medians[key] > 0:
            relative[key] = {"project_multiple": round(value, 8),
                             "peer_median": medians[key],
                             "relative_to_peers_pct": round((value / medians[key] - 1) * 100, 2)}
    status = "KNOWN" if relative else "UNKNOWN"
    return {"status": status, "sector": sector or None, "peer_count": len(peers) - exclusions,
            "excluded_non_comparable_peers": exclusions, "project_multiples": project_values,
            "peer_medians": medians, "relative_valuation": relative,
            "reason": None if relative else "No valid same-sector peer multiple comparison."}


def build_fundamental_report(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate seven evidence modules with explicit coverage and no fabricated scores.

    Each non-derived module must use {status: KNOWN, score: 0..100}.
    DCF/comparables can be passed as precomputed module outputs; absent outputs
    remain UNKNOWN. A score is withheld when evidence coverage is below 70%.
    """
    modules: dict[str, dict[str, Any]] = {}
    for name in ("macro", "tokenomics", "onchain", "project"):
        item = evidence.get(name)
        score = _number(item.get("score")) if isinstance(item, Mapping) and item.get("status") == "KNOWN" else None
        valid = score is not None and 0 <= score <= 100
        modules[name] = ({"status": "KNOWN", "score": score} if valid else
                         {"status": "UNKNOWN", "score": None, "reason": "Valid sourced score not supplied."})
    modules["industry"] = industry_analysis(evidence.get("industry", {}))
    for name in ("dcf", "comparables"):
        item = evidence.get(name)
        score = _number(item.get("score")) if isinstance(item, Mapping) and item.get("status") == "KNOWN" else None
        valid = score is not None and 0 <= score <= 100
        modules[name] = ({"status": "KNOWN", "score": score} if valid else
                         {"status": "UNKNOWN", "score": None, "reason": "Valid valuation score not supplied."})
    known_weight = sum(MODULE_WEIGHTS[name] for name, item in modules.items() if item["score"] is not None)
    coverage = known_weight / sum(MODULE_WEIGHTS.values()) * 100
    score = None
    status = "UNKNOWN"
    if known_weight and coverage >= MIN_COVERAGE_PCT:
        score = round(sum(MODULE_WEIGHTS[name] * item["score"] for name, item in modules.items()
                          if item["score"] is not None) / known_weight, 2)
        status = "KNOWN" if all(item["score"] is not None for item in modules.values()) else "PARTIAL"
    elif known_weight:
        status = "INSUFFICIENT_COVERAGE"
    return {"status": status, "score": score, "coverage_pct": round(coverage, 2),
            "minimum_coverage_pct": MIN_COVERAGE_PCT, "modules": modules,
            "weights": dict(MODULE_WEIGHTS),
            "trade_authorized": False,
            "message_fa": ("این گزارش فقط ارزیابی بنیادی است و به‌تنهایی مجوز ورود به معامله نیست.")}


__all__ = ["MODULE_WEIGHTS", "MIN_COVERAGE_PCT", "classify_industry",
           "industry_analysis", "discounted_protocol_cashflows",
           "comparable_projects", "build_fundamental_report"]
