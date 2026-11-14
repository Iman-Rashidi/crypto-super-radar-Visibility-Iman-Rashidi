"""Evidence-aware crypto fundamental analysis for the Easy trading engine.

This module is deliberately provider-agnostic: it computes only from supplied,
timestamped evidence and never invents missing fundamentals. It does not place
orders or authorize a trade.
"""
from __future__ import annotations

from math import isfinite, sqrt, log1p
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
    result = {"status": "UNKNOWN", "score": None, "known_criteria": [],
              "unknown_criteria": unknown, "reason": "No valid, sourced industry criteria."}
    if scores:
        # Equal weights avoid pretending unsupported sector-specific weights are calibrated.
        score = sum(scores.values()) / len(scores)
        result.update({"status": "PARTIAL" if unknown else "KNOWN", "score": round(score, 2),
                       "known_criteria": sorted(scores)})
    if isinstance(evidence.get("evidence"), Mapping):
        result["evidence"] = dict(evidence["evidence"])
    return result


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


def accounting_principles_analysis(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Assess financial-reporting quality without misapplying industrial accounting to tokens.

    Revenue recognition checks delivery/performance obligation and collectability.
    Inventory valuation is N/A for a protocol/token unless the project actually
    holds or sells inventory. Depreciation is N/A unless material depreciable
    operating assets are reported. Unknown is never treated as a clean audit.
    """
    definitions = {
        "revenue_recognition": {
            "label_fa": "شناسایی درآمد",
            "checks": ("performance_obligation_satisfied", "revenue_basis_disclosed",
                       "fees_separated_from_tokenholder_revenue", "collectability_supported"),
            "note": "Protocol fees, gross revenue and revenue accruing to token holders are different measures.",
            "accepted_methods": ("POINT_IN_TIME", "OVER_TIME"),
            "method_field": "recognition_method",
        },
        "inventory_valuation": {
            "label_fa": "ارزش‌گذاری موجودی",
            "checks": ("inventory_scope_disclosed", "valuation_method_disclosed",
                       "impairment_or_obsolescence_disclosed"),
            "note": "FIFO records oldest inventory costs first; LIFO records newest costs first; weighted average uses average unit cost. For a software protocol with no material inventory this is NOT_APPLICABLE, not a positive score.",
            "accepted_methods": ("FIFO", "LIFO", "WEIGHTED_AVERAGE"),
            "method_field": "valuation_method",
        },
        "depreciation": {
            "label_fa": "استهلاک دارایی‌ها",
            "checks": ("material_asset_register_disclosed", "useful_lives_disclosed",
                       "depreciation_method_disclosed", "impairment_disclosed"),
            "note": "Straight-line spreads cost evenly; declining-balance accelerates early expense; units-of-production follows actual usage. Assess servers, equipment and capitalized development only where material and disclosed.",
            "accepted_methods": ("STRAIGHT_LINE", "DECLINING_BALANCE", "UNITS_OF_PRODUCTION"),
            "method_field": "depreciation_method",
        },
    }
    results = {}
    for name, definition in definitions.items():
        supplied = evidence.get(name)
        supplied = supplied if isinstance(supplied, Mapping) else {}
        applicability = str(supplied.get("applicability") or "UNKNOWN").upper()
        if applicability == "NOT_APPLICABLE":
            results[name] = {"status": "NOT_APPLICABLE", "score": None,
                             "label_fa": definition["label_fa"], "checks": {},
                             "reason": "عدم کاربرد باید با شواهد ساختار پروژه تأیید شود."}
            continue
        checks = {}
        for key in definition["checks"]:
            item = supplied.get(key)
            checks[key] = (str(item).upper() if str(item).upper() in
                           ("PASS", "FAIL", "UNKNOWN") else "UNKNOWN")
        values = list(checks.values())
        status = ("UNKNOWN" if all(value == "UNKNOWN" for value in values) else
                  "FAIL" if "FAIL" in values else
                  "PARTIAL" if "UNKNOWN" in values else "PASS")
        reported_method = str(supplied.get(definition["method_field"]) or "").upper()
        if reported_method not in definition["accepted_methods"]:
            reported_method = None
        results[name] = {"status": status, "score": None,
                         "label_fa": definition["label_fa"], "checks": checks,
                         "reported_method": reported_method,
                         "accepted_methods": list(definition["accepted_methods"]),
                         "note": definition["note"]}
    return {
        "status": "UNKNOWN" if all(x["status"] in ("UNKNOWN", "NOT_APPLICABLE") for x in results.values()) else
                  "FAIL" if any(x["status"] == "FAIL" for x in results.values()) else "PARTIAL",
        "score": None, "principles": results,
        "message_fa": "کیفیت گزارشگری مالی بررسی می‌شود؛ موارد نامرتبط با ماهیت پروژه امتیاز مثبت نمی‌گیرند.",
    }


def market_analysis(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Evidence checklist for market context; no forecast is inferred from missing data."""
    criteria = {
        "gdp_growth": "رشد تولید ناخالص داخلی",
        "inflation": "تورم",
        "interest_rates": "نرخ بهره",
        "employment": "اشتغال",
        "industry_trends": "روندهای صنعت",
        "regulatory_developments": "تحولات مقرراتی",
        "market_risks": "ریسک‌های بازار",
        "market_outlook": "چشم‌انداز بازار",
    }
    observations = {}
    known = 0
    failed = 0
    for key, label in criteria.items():
        item = evidence.get(key)
        item = item if isinstance(item, Mapping) else {}
        status = str(item.get("status") or "UNKNOWN").upper()
        if status not in ("KNOWN", "FAIL", "UNKNOWN"):
            status = "UNKNOWN"
        value = item.get("value")
        source = item.get("source")
        observed_at = item.get("observed_at_utc")
        if status == "KNOWN" and (value is None or not source or not observed_at):
            status = "UNKNOWN"
        known += status == "KNOWN"
        failed += status == "FAIL"
        observations[key] = {"label_fa": label, "status": status,
                              "value": value if status == "KNOWN" else None,
                              "source": source if status == "KNOWN" else None,
                              "observed_at_utc": observed_at if status == "KNOWN" else None}
    coverage = known / len(criteria) * 100
    return {
        "status": "FAIL" if failed else "KNOWN" if known == len(criteria) else
                  "PARTIAL" if known else "UNKNOWN",
        "score": None, "coverage_pct": round(coverage, 2),
        "observations": observations,
        "methodology": ["collect_data", "identify_trends", "assess_market_risks",
                        "form_scenarios"],
        "message_fa": "تحلیل بازار باید همراه تحلیل مالی، بنیادی و تکنیکال انجام شود؛ پیش‌بینی بدون داده معتبر تولید نمی‌شود.",
    }



def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta function."""
    max_iter, epsilon, tiny = 200, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny: d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny: d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny: c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny: d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny: c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon: return h
    raise ArithmeticError("Incomplete beta continued fraction did not converge")

def _regularized_beta(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta I_x(a,b), using only the standard library."""
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    from math import exp, lgamma, log
    front = exp(lgamma(a + b) - lgamma(a) - lgamma(b) + a * log(x) + b * log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b

def event_analysis(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Market-model event study with timestamped sources and Student-t CAR test.

    Each event requires source_url, event_time_utc and observations. Each
    observation has timestamp, asset_return, benchmark_return and window
    (ESTIMATION or EVENT). Returns are decimal fractions, not percentages.
    """
    raw_events = evidence.get("events") if isinstance(evidence, Mapping) else None
    if not isinstance(raw_events, list) or not raw_events:
        return {"status": "UNKNOWN", "score": None, "event_count": 0, "known_events": 0,
                "events": {}, "reason": "رویداد زمان‌دار و داده بازده دارایی/بازار برای پنجره برآورد و رویداد موجود نیست.",
                "message_fa": "برای تحلیل رویداد، خبر تأییدشده، زمان انتشار و بازده تاریخی دارایی و شاخص معیار لازم است."}
    results: dict[str, Any] = {}
    for index, event in enumerate(raw_events):
        if not isinstance(event, Mapping): continue
        event_id = str(event.get("event_id") or f"event_{index + 1}")
        source_url, event_time = event.get("source_url"), event.get("event_time_utc")
        observations = event.get("observations")
        if not isinstance(source_url, str) or not source_url.startswith(("https://", "http://")) or not event_time:
            results[event_id] = {"status": "UNKNOWN", "reason": "منبع و زمان رویداد باید مشخص باشند."}
            continue
        if not isinstance(observations, list):
            results[event_id] = {"status": "UNKNOWN", "reason": "مشاهدات بازده رویداد موجود نیست."}
            continue
        estimation, event_rows, invalid = [], [], False
        for row in observations:
            if not isinstance(row, Mapping) or not row.get("timestamp"):
                invalid = True; break
            asset_return, benchmark_return = _number(row.get("asset_return")), _number(row.get("benchmark_return"))
            if asset_return is None or benchmark_return is None:
                invalid = True; break
            item = (asset_return, benchmark_return, str(row["timestamp"]))
            target = str(row.get("window") or "").upper()
            if target == "ESTIMATION": estimation.append(item)
            elif target == "EVENT": event_rows.append(item)
        if invalid or len(estimation) < 20 or not event_rows:
            results[event_id] = {"status": "UNKNOWN", "reason": "حداقل ۲۰ مشاهده معتبر در پنجره برآورد و یک مشاهده در پنجره رویداد لازم است.",
                                "estimation_observations": len(estimation), "event_observations": len(event_rows)}
            continue
        x_mean = sum(row[1] for row in estimation) / len(estimation)
        y_mean = sum(row[0] for row in estimation) / len(estimation)
        variance_x = sum((row[1] - x_mean) ** 2 for row in estimation)
        if variance_x <= 1e-18:
            results[event_id] = {"status": "UNKNOWN", "reason": "واریانس بازده شاخص معیار برای برآورد مدل کافی نیست."}; continue
        beta = sum((row[1] - x_mean) * (row[0] - y_mean) for row in estimation) / variance_x
        alpha = y_mean - beta * x_mean
        residuals = [row[0] - (alpha + beta * row[1]) for row in estimation]
        degrees_freedom = len(estimation) - 2
        residual_std = sqrt(sum(value * value for value in residuals) / degrees_freedom)
        if residual_std <= 1e-12:
            results[event_id] = {"status": "UNKNOWN", "reason": "واریانس خطای مدل برای آزمون معناداری کافی نیست."}; continue
        abnormal = []
        for actual, benchmark, stamp in event_rows:
            abnormal.append({"timestamp": stamp, "actual_return": actual,
                             "normal_return": alpha + beta * benchmark,
                             "abnormal_return": actual - (alpha + beta * benchmark)})
        car = sum(item["abnormal_return"] for item in abnormal)
        t_stat = car / (residual_std * sqrt(len(abnormal)))
        p_value = _regularized_beta(degrees_freedom / (degrees_freedom + t_stat * t_stat), degrees_freedom / 2.0, 0.5)
        results[event_id] = {
            "status": "KNOWN", "event_type": str(event.get("event_type") or "UNSPECIFIED"),
            "event_time_utc": str(event_time), "source_url": source_url,
            "event_window": event.get("event_window") or {},
            "estimation_observations": len(estimation), "event_observations": len(event_rows),
            "market_model": {"alpha": alpha, "beta": beta, "residual_std": residual_std, "degrees_freedom": degrees_freedom},
            "abnormal_returns": abnormal, "cumulative_abnormal_return": car,
            "t_statistic": t_stat, "p_value_two_sided": p_value,
            "statistically_significant_5pct": p_value < 0.05,
            "interpretation_fa": "بازده غیرعادی تجمعی از نظر آماری معنادار است؛ این نتیجه رابطه علّی را اثبات نمی‌کند." if p_value < 0.05 else "شواهد آماری کافی برای معناداری بازده غیرعادی تجمعی در سطح ۵٪ وجود ندارد.",
            "limitations": ["نتیجه به انتخاب پنجره و شاخص معیار حساس است.", "خطای استاندارد فرض می‌کند خودهمبستگی بازده غیرعادی در پنجره رویداد وجود ندارد.", "هم‌زمانی چند خبر یا رویداد می‌تواند انتساب اثر را مخدوش کند."],
        }
    known = sum(item.get("status") == "KNOWN" for item in results.values())
    return {"status": "KNOWN" if known == len(raw_events) and known else "PARTIAL" if known else "UNKNOWN",
            "score": None, "event_count": len(raw_events), "known_events": known, "events": results,
            "methodology": "OLS market model; abnormal return = actual return - expected normal return; CAR significance via two-sided Student-t test.",
            "message_fa": "اثر رویداد با مقایسه بازده واقعی و بازده عادی مورد انتظار سنجیده می‌شود؛ نتیجه توصیفی است و به‌تنهایی سیگنال معامله نیست."}


def dividend_analysis(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Assess equity dividends from sourced evidence; crypto tokens are N/A.

    Supported inputs: asset_class, current_price, annual_dividend_per_share,
    dividend_yield_pct, dividends_paid, net_income, free_cash_flow,
    dividend_history [{year, dividend_per_share}], price_change_12m_pct,
    source, observed_at_utc. Percentages are expressed as percentage points.
    """
    asset_class = str(evidence.get("asset_class") or "").strip().lower()
    if asset_class in {"crypto", "cryptocurrency", "token", "digital_asset"}:
        return {"status": "NOT_APPLICABLE", "score": None,
                "message_fa": "سود تقسیمی شرکتی برای این دارایی تعریف نشده است؛ این مورد ضعف بنیادی توکن محسوب نمی‌شود."}
    if asset_class not in {"equity", "stock", "share", "reit"}:
        return {"status": "UNKNOWN", "score": None,
                "message_fa": "نوع دارایی مشخص نیست؛ برای جلوگیری از اعمال اشتباه تحلیل سود تقسیمی، وضعیت UNKNOWN است."}
    source, observed = evidence.get("source"), evidence.get("observed_at_utc")
    sourced = isinstance(source, str) and source.startswith(("https://", "http://")) and bool(observed)
    price = _positive(evidence.get("current_price"))
    annual_dividend = _number(evidence.get("annual_dividend_per_share"))
    yield_pct = _number(evidence.get("dividend_yield_pct"))
    if yield_pct is None and annual_dividend is not None and price is not None and price > 0:
        yield_pct = annual_dividend / price * 100
    net_income = _number(evidence.get("net_income"))
    dividends_paid = _number(evidence.get("dividends_paid"))
    payout_ratio = (dividends_paid / net_income * 100
                    if dividends_paid is not None and net_income is not None and net_income > 0 else None)
    coverage = (net_income / dividends_paid
                if net_income is not None and net_income > 0 and dividends_paid is not None and dividends_paid > 0 else None)
    fcf = _number(evidence.get("free_cash_flow"))
    fcf_coverage = (fcf / dividends_paid
                    if fcf is not None and fcf > 0 and dividends_paid is not None and dividends_paid > 0 else None)
    history = evidence.get("dividend_history")
    history = history if isinstance(history, list) else []
    valid_history = []
    for row in history:
        if not isinstance(row, Mapping): continue
        year, amount = _number(row.get("year")), _number(row.get("dividend_per_share"))
        if year is not None and amount is not None and year.is_integer() and amount >= 0:
            valid_history.append((int(year), amount))
    valid_history.sort()
    years = [row[0] for row in valid_history]
    amounts = [row[1] for row in valid_history]
    history_consistent = (len(years) >= 2 and len(set(years)) == len(years)
                          and all(years[i] == years[i - 1] + 1 for i in range(1, len(years)))
                          and all(amount > 0 for amount in amounts))
    dividend_growth_cagr_pct = None
    if history_consistent and amounts[0] > 0 and amounts[-1] > 0:
        dividend_growth_cagr_pct = ((amounts[-1] / amounts[0]) ** (1 / (len(amounts) - 1)) - 1) * 100
    price_change = _number(evidence.get("price_change_12m_pct"))
    dividend_trap = bool(yield_pct is not None and yield_pct >= 8 and price_change is not None and price_change <= -20)
    checks = {
        "dividend_yield": "KNOWN" if yield_pct is not None and sourced else "UNKNOWN",
        "payout_ratio": "KNOWN" if payout_ratio is not None and sourced else "UNKNOWN",
        "dividend_history": "KNOWN" if history_consistent and sourced else "UNKNOWN",
        "dividend_coverage": "KNOWN" if coverage is not None and sourced else "UNKNOWN",
        "dividend_growth": "KNOWN" if dividend_growth_cagr_pct is not None and sourced else "UNKNOWN",
        "dividend_sustainability": "KNOWN" if (payout_ratio is not None or fcf_coverage is not None) and sourced else "UNKNOWN",
        "dividend_trap": "KNOWN" if yield_pct is not None and price_change is not None and sourced else "UNKNOWN",
    }
    known = sum(value == "KNOWN" for value in checks.values())
    status = "KNOWN" if known == len(checks) else "PARTIAL" if known else "UNKNOWN"
    sustainability = "UNKNOWN"
    if payout_ratio is not None:
        sustainability = "RISK" if payout_ratio > 100 else "CAUTION" if payout_ratio > 80 else "HEALTHY"
    if fcf_coverage is not None and fcf_coverage < 1:
        sustainability = "RISK"
    elif fcf_coverage is not None and fcf_coverage < 1.2 and sustainability == "HEALTHY":
        sustainability = "CAUTION"
    return {
        "status": status, "score": None, "applicability": "APPLICABLE",
        "source": source if sourced else None, "observed_at_utc": observed if sourced else None,
        "dividend_yield_pct": yield_pct, "payout_ratio_pct": payout_ratio,
        "dividend_coverage_ratio": coverage, "free_cash_flow_coverage_ratio": fcf_coverage,
        "dividend_history": [{"year": year, "dividend_per_share": amount} for year, amount in valid_history],
        "history_consistent": history_consistent, "dividend_growth_cagr_pct": dividend_growth_cagr_pct,
        "dividend_sustainability": sustainability, "dividend_trap_warning": dividend_trap,
        "checks": checks, "known_checks": known, "total_checks": len(checks),
        "message_fa": ("بازده تقسیمی بالا همراه با افت شدید قیمت شناسایی شد؛ احتمال تله سود تقسیمی را بررسی کنید." if dividend_trap else
                       "نسبت پرداخت و پوشش جریان نقدی باید برای سنجش پایداری سود تقسیمی بررسی شوند."),
        "limitations": ["بازده بالا به‌تنهایی نشانه ارزندگی نیست.",
                        "برای شرکت‌های زیان‌ده، نسبت پرداخت بر مبنای سود خالص قابل محاسبه نیست.",
                        "تاریخچه پرداخت به‌تنهایی تضمین‌کننده پرداخت آینده نیست."],
    }


def mergers_acquisitions_analysis(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Evidence-only M&A diagnostic for listed companies; never a trade signal."""
    asset_class = str(evidence.get("asset_class") or "").strip().lower()
    if asset_class in {"crypto", "cryptocurrency", "token", "digital_asset"}:
        return {"status": "NOT_APPLICABLE", "score": None,
                "message_fa": "این ماژول برای ادغام و تملک شرکت‌هاست؛ برای رویدادهای پروتکلی باید داده رویداد جداگانه ثبت شود."}
    raw_deals = evidence.get("deals")
    if not isinstance(raw_deals, list) or not raw_deals:
        return {"status": "UNKNOWN", "score": None, "deal_count": 0, "known_deals": 0,
                "deals": {}, "message_fa": "خبر تأییدشده معامله، زمان اعلام و شرایط مالی در دسترس نیست."}
    results = {}
    for index, deal in enumerate(raw_deals):
        if not isinstance(deal, Mapping):
            continue
        deal_id = str(deal.get("deal_id") or f"deal_{index + 1}")
        source = deal.get("source_url")
        announced = deal.get("announced_at_utc")
        valid_source = isinstance(source, str) and source.startswith(("https://", "http://")) and bool(announced)
        offer = _positive(deal.get("offer_price_per_share"))
        unaffected = _positive(deal.get("unaffected_price_per_share"))
        premium = ((offer / unaffected - 1) * 100
                   if offer is not None and unaffected is not None else None)
        status = str(deal.get("deal_status") or "UNKNOWN").upper()
        if status not in {"RUMORED", "ANNOUNCED", "PENDING_APPROVAL", "COMPLETED", "TERMINATED", "WITHDRAWN"}:
            status = "UNKNOWN"
        regulatory = str(deal.get("regulatory_status") or "UNKNOWN").upper()
        if regulatory not in {"CLEAR", "REVIEW", "CHALLENGED", "BLOCKED", "UNKNOWN"}:
            regulatory = "UNKNOWN"
        financing = str(deal.get("financing_type") or "UNKNOWN").upper()
        if financing not in {"CASH", "STOCK", "MIXED", "DEBT", "UNKNOWN"}:
            financing = "UNKNOWN"
        integration_risk = str(deal.get("integration_risk") or "UNKNOWN").upper()
        if integration_risk not in {"LOW", "MEDIUM", "HIGH", "UNKNOWN"}:
            integration_risk = "UNKNOWN"
        known = valid_source and status != "UNKNOWN"
        results[deal_id] = {
            "status": "KNOWN" if known else "UNKNOWN",
            "deal_status": status if known else "UNKNOWN",
            "target": deal.get("target") if known else None,
            "acquirer": deal.get("acquirer") if known else None,
            "transaction_value": _positive(deal.get("transaction_value")) if known else None,
            "offer_premium_pct": round(premium, 4) if premium is not None and known else None,
            "synergies": deal.get("synergies") if known else None,
            "financing_type": financing if known else "UNKNOWN",
            "integration_risk": integration_risk if known else "UNKNOWN",
            "regulatory_status": regulatory if known else "UNKNOWN",
            "competitor_impact": deal.get("competitor_impact") if known else None,
            "source_url": source if known else None,
            "announced_at_utc": announced if known else None,
            "review_flags": [
                flag for flag, condition in (
                    ("regulatory_review_or_challenge", regulatory in {"REVIEW", "CHALLENGED", "BLOCKED"}),
                    ("high_integration_risk", integration_risk == "HIGH"),
                    ("debt_financing", financing == "DEBT"),
                ) if condition
            ] if known else [],
        }
    known_count = sum(item["status"] == "KNOWN" for item in results.values())
    return {
        "status": "KNOWN" if known_count == len(results) and known_count else "PARTIAL" if known_count else "UNKNOWN",
        "score": None, "deal_count": len(results), "known_deals": known_count, "deals": results,
        "message_fa": "صرف قیمت هدف و خریدار کافی نیست؛ صرف پیشنهاد، تأمین مالی، هم‌افزایی، ریسک ادغام و بررسی نهادهای ناظر باید جداگانه سنجیده شوند.",
        "limitations": ["صرف پیشنهاد نسبت به قیمت پیش از اعلام، بازده قطعی معامله نیست.",
                        "معامله اعلام‌شده ممکن است اصلاح، مسدود یا لغو شود.",
                        "هم‌افزایی اعلامی تا زمان تحقق، صرفاً برآورد شرکت‌هاست.",
                        "اثر رقبا و تأمین‌کنندگان به داده مستقل نیاز دارد."],
    }


def sector_rotation_analysis(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Rank timestamped sector returns/flows relative to a benchmark; no cycle forecast."""
    phase = evidence.get("economic_phase")
    phase = phase if isinstance(phase, Mapping) else {}
    phase_value = str(phase.get("value") or "UNKNOWN").upper()
    if phase_value not in {"EARLY_EXPANSION", "MID_EXPANSION", "LATE_EXPANSION", "CONTRACTION", "UNKNOWN"}:
        phase_value = "UNKNOWN"
    phase_source = phase.get("source")
    phase_time = phase.get("observed_at_utc")
    phase_known = bool(isinstance(phase_source, str) and phase_source.startswith(("https://", "http://")) and phase_time and phase_value != "UNKNOWN")
    raw_sectors = evidence.get("sectors")
    if not isinstance(raw_sectors, list) or not raw_sectors:
        return {"status": "UNKNOWN", "score": None, "economic_phase": phase_value if phase_known else "UNKNOWN",
                "sectors": [], "leaders": [], "laggards": [],
                "message_fa": "برای سنجش چرخش سرمایه، بازده بخش‌ها، شاخص معیار و داده جریان سرمایه با منبع و زمان لازم است."}
    rows = []
    for item in raw_sectors:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("sector") or "").strip()
        sector_return = _number(item.get("return_pct"))
        benchmark_return = _number(item.get("benchmark_return_pct"))
        flow = _number(item.get("flow_change_pct"))
        source = item.get("source")
        observed = item.get("observed_at_utc")
        sourced = bool(name and sector_return is not None and benchmark_return is not None
                       and isinstance(source, str) and source.startswith(("https://", "http://")) and observed)
        if not sourced:
            continue
        rows.append({
            "sector": name, "return_pct": sector_return,
            "benchmark_return_pct": benchmark_return,
            "relative_strength_pct": round(sector_return - benchmark_return, 4),
            "flow_change_pct": flow, "source": source, "observed_at_utc": observed,
            "flow_status": "KNOWN" if flow is not None else "UNKNOWN",
        })
    rows.sort(key=lambda row: row["relative_strength_pct"], reverse=True)
    if not rows:
        status = "UNKNOWN"
    elif len(rows) != len(raw_sectors):
        status = "PARTIAL"
    else:
        status = "KNOWN"
    return {
        "status": status, "score": None,
        "economic_phase": phase_value if phase_known else "UNKNOWN",
        "economic_phase_source": phase_source if phase_known else None,
        "economic_phase_observed_at_utc": phase_time if phase_known else None,
        "sectors": rows, "leaders": [row["sector"] for row in rows[:3]],
        "laggards": [row["sector"] for row in rows[-3:]][::-1],
        "message_fa": "رتبه‌بندی بر پایه عملکرد نسبیِ داده‌شده است؛ مرحله چرخه اقتصادی به‌تنهایی پیش‌بینی بازده یا توصیه اضافه‌وزن نیست.",
        "limitations": ["عملکرد گذشته تضمین‌کننده عملکرد آینده نیست.",
                        "جریان ETF یا سرمایه باید با منبع و بازه زمانی یکسان مقایسه شود.",
                        "چرخه اقتصادی ممکن است میان کشورها و صنایع متفاوت باشد."],
    }


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
        if isinstance(item, Mapping) and isinstance(item.get("evidence"), Mapping):
            modules[name]["evidence"] = dict(item["evidence"])
    modules["industry"] = industry_analysis(evidence.get("industry", {}))
    # Supporting diagnostics are visible in the Easy Fundamental panel but are
    # deliberately excluded from the seven-module score until calibrated.
    modules["accounting_principles"] = accounting_principles_analysis(
        evidence.get("accounting_principles", {}))
    modules["market_analysis"] = market_analysis(evidence.get("market_analysis", {}))
    modules["event_analysis"] = event_analysis(evidence.get("event_analysis", {}))
    modules["dividend_analysis"] = dividend_analysis(evidence.get("dividend_analysis", {}))\n    modules["mergers_acquisitions"] = mergers_acquisitions_analysis(evidence.get("mergers_acquisitions", {}))\n    modules["sector_rotation"] = sector_rotation_analysis(evidence.get("sector_rotation", {}))
    for name in ("dcf", "comparables"):
        item = evidence.get(name)
        score = _number(item.get("score")) if isinstance(item, Mapping) and item.get("status") == "KNOWN" else None
        valid = score is not None and 0 <= score <= 100
        modules[name] = ({"status": "KNOWN", "score": score} if valid else
                         {"status": "UNKNOWN", "score": None, "reason": "Valid valuation score not supplied."})
        if isinstance(item, Mapping) and isinstance(item.get("evidence"), Mapping):
            modules[name]["evidence"] = dict(item["evidence"])
    known_weight = sum(MODULE_WEIGHTS[name] for name, item in modules.items() if item["score"] is not None)
    coverage = known_weight / sum(MODULE_WEIGHTS.values()) * 100
    score = None
    status = "UNKNOWN"
    if known_weight and coverage >= MIN_COVERAGE_PCT:
        score = round(sum(MODULE_WEIGHTS[name] * item["score"] for name, item in modules.items()
                          if item["score"] is not None) / known_weight, 2)
        # Only the seven weighted modules determine score completeness.
        status = "KNOWN" if all(modules[name]["score"] is not None for name in MODULE_WEIGHTS) else "PARTIAL"
    elif known_weight:
        status = "INSUFFICIENT_COVERAGE"
    return {"status": status, "score": score, "coverage_pct": round(coverage, 2),
            "minimum_coverage_pct": MIN_COVERAGE_PCT, "modules": modules,
            "weights": dict(MODULE_WEIGHTS),
            "trade_authorized": False,
            "message_fa": ("این گزارش فقط ارزیابی بنیادی است و به‌تنهایی مجوز ورود به معامله نیست.")}


__all__ = ["MODULE_WEIGHTS", "MIN_COVERAGE_PCT", "classify_industry",
           "industry_analysis", "accounting_principles_analysis", "market_analysis", "event_analysis", "dividend_analysis",\n           "mergers_acquisitions_analysis", "sector_rotation_analysis",
           "discounted_protocol_cashflows", "comparable_projects",
           "build_fundamental_report"]
