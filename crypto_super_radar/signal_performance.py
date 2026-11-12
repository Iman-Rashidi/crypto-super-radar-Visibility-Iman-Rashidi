"""Cost-aware performance analytics for the paper-trading ledger.

Metrics are descriptive only. A minimum sample threshold prevents a handful of
trades from being presented as evidence of a durable trading edge.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def summarize_closed_trades(
    trades: Iterable[Mapping[str, Any]],
    *,
    initial_equity_usd: float = 10_000.0,
    min_sample_size: int = 30,
) -> dict[str, Any]:
    """Summarize realized paper trades after recorded costs.

    R-multiples are calculated only when an immutable initial stop is available.
    Slippage is not independently attributed by the existing ledger, so it is
    never fabricated as a separate cost field.
    """
    if isinstance(min_sample_size, bool) or not isinstance(min_sample_size, int) or min_sample_size < 1:
        raise ValueError("min_sample_size must be a positive integer")
    initial_equity = _finite(initial_equity_usd)
    if initial_equity is None or initial_equity <= 0:
        raise ValueError("initial_equity_usd must be positive and finite")

    normalized: list[dict[str, float | None]] = []
    invalid_rows = 0
    fee_incomplete_rows = 0
    known_recorded_fees = 0.0
    slippage_incomplete_rows = 0
    known_recorded_slippage = 0.0
    spread_incomplete_rows = 0
    known_recorded_spread = 0.0
    funding_incomplete_rows = 0
    known_recorded_funding = 0.0
    for trade in trades:
        net = _finite(trade.get("net_pnl"))
        gross = _finite(trade.get("gross_pnl"))
        if net is None or gross is None:
            invalid_rows += 1
            continue
        entry_fee = _finite(trade.get("entry_fee"))
        exit_fee = _finite(trade.get("exit_fee"))
        quantity = _finite(trade.get("quantity"))
        entry = _finite(trade.get("entry_price"))
        initial_stop = _finite(trade.get("initial_stop_loss"))
        # A missing/invalid component is UNKNOWN, not a zero-fee observation.
        known_recorded_fees += sum(
            fee for fee in (entry_fee, exit_fee) if fee is not None
        )
        fee_total = (
            entry_fee + exit_fee
            if entry_fee is not None and exit_fee is not None
            else None
        )
        if fee_total is None:
            fee_incomplete_rows += 1

        entry_slippage = _finite(trade.get("entry_slippage_usd"))
        exit_slippage = _finite(trade.get("exit_slippage_usd"))
        if entry_slippage is not None and entry_slippage < 0:
            entry_slippage = None
        if exit_slippage is not None and exit_slippage < 0:
            exit_slippage = None
        slippage_total = (
            entry_slippage + exit_slippage
            if entry_slippage is not None and exit_slippage is not None
            else None
        )
        known_recorded_slippage += sum(
            value for value in (entry_slippage, exit_slippage) if value is not None
        )
        if slippage_total is None:
            slippage_incomplete_rows += 1

        spread_cost = _finite(trade.get("spread_cost_usd"))
        if spread_cost is None or spread_cost < 0:
            spread_cost = None
            spread_incomplete_rows += 1
        else:
            known_recorded_spread += spread_cost

        funding_status = str(trade.get("funding_status") or "").strip().upper()
        funding_cost = _finite(trade.get("funding_cost_usd"))
        if not (
            (funding_status == "NOT_APPLICABLE" and funding_cost == 0)
            or (funding_status == "RECORDED" and funding_cost is not None)
        ):
            funding_cost = None
            funding_incomplete_rows += 1
        if funding_cost is not None:
            known_recorded_funding += funding_cost

        risk_usd = (
            quantity * abs(entry - initial_stop)
            if quantity is not None and quantity > 0
            and entry is not None and initial_stop is not None
            and initial_stop > 0 and entry != initial_stop
            else None
        )
        normalized.append({
            "net": net,
            "gross": gross,
            "fees": fee_total,
            "entry_slippage": entry_slippage,
            "exit_slippage": exit_slippage,
            "slippage": slippage_total,
            "spread": spread_cost,
            "funding": funding_cost,
            "risk_usd": risk_usd,
        })

    count = len(normalized)
    if not count:
        return {
            "status": "INSUFFICIENT_SAMPLE",
            "sample_size": 0,
            "minimum_sample_size": min_sample_size,
            "invalid_rows_excluded": invalid_rows,
            "performance_claim_allowed": False,
            "fee_data_status": "NO_DATA",
            "fee_coverage_pct": None,
            "fee_incomplete_rows": 0,
            "known_recorded_fees_usd": None,
            "total_recorded_fees_usd": None,
            "slippage_data_status": "NO_DATA",
            "slippage_coverage_pct": None,
            "slippage_incomplete_rows": 0,
            "known_recorded_slippage_usd": None,
            "total_recorded_slippage_usd": None,
            "spread_data_status": "NO_DATA",
            "spread_coverage_pct": None,
            "known_recorded_spread_usd": None,
            "total_recorded_spread_usd": None,
            "funding_data_status": "NO_DATA",
            "funding_coverage_pct": None,
            "known_recorded_funding_usd": None,
            "total_recorded_funding_usd": None,
            "execution_cost_attribution_status": "NO_DATA",
            "reason": "No valid closed paper trades are recorded yet.",
            "slippage_attribution": "NO_DATA",
        }

    wins = [row for row in normalized if row["net"] > 0]
    losses = [row for row in normalized if row["net"] < 0]
    gross_losses = sum(float(row["net"]) for row in losses)
    total_net = sum(float(row["net"]) for row in normalized)
    total_gross = sum(float(row["gross"]) for row in normalized)
    gross_profit = sum(max(0.0, float(row["gross"])) for row in normalized)

    equity = initial_equity
    peak = initial_equity
    max_drawdown_pct = 0.0
    for row in normalized:
        equity += float(row["net"])
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, (peak - equity) / peak * 100.0)

    r_values = [
        float(row["net"]) / float(row["risk_usd"])
        for row in normalized if row["risk_usd"] is not None and float(row["risk_usd"]) > 0
    ]
    ready = count >= min_sample_size
    return {
        "status": "REVIEWABLE" if ready else "INSUFFICIENT_SAMPLE",
        "sample_size": count,
        "minimum_sample_size": min_sample_size,
        "invalid_rows_excluded": invalid_rows,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": count - len(wins) - len(losses),
        "win_rate_pct": round(len(wins) / count * 100.0, 4),
        "gross_pnl_usd": round(total_gross, 8),
        "net_pnl_usd": round(total_net, 8),
        "net_expectancy_usd_per_trade": round(total_net / count, 8),
        "profit_factor_net": (
            round(sum(float(row["net"]) for row in wins) / abs(gross_losses), 6)
            if gross_losses < 0 else None
        ),
        "fee_data_status": "COMPLETE" if fee_incomplete_rows == 0 else "INCOMPLETE",
        "fee_coverage_pct": round((count - fee_incomplete_rows) / count * 100.0, 4),
        "fee_incomplete_rows": fee_incomplete_rows,
        # Partial observed components are visible, but never mislabeled as a total.
        "known_recorded_fees_usd": round(known_recorded_fees, 8),
        "total_recorded_fees_usd": (
            round(known_recorded_fees, 8) if fee_incomplete_rows == 0 else None
        ),
        "fees_as_pct_of_gross_profit": (
            round(known_recorded_fees / gross_profit * 100.0, 4)
            if fee_incomplete_rows == 0 and gross_profit > 0 else None
        ),
        "slippage_data_status": "COMPLETE" if slippage_incomplete_rows == 0 else "INCOMPLETE",
        "slippage_coverage_pct": round((count - slippage_incomplete_rows) / count * 100.0, 4),
        "slippage_incomplete_rows": slippage_incomplete_rows,
        "known_recorded_slippage_usd": round(known_recorded_slippage, 8),
        "total_recorded_slippage_usd": (
            round(known_recorded_slippage, 8) if slippage_incomplete_rows == 0 else None
        ),
        "spread_data_status": "COMPLETE" if spread_incomplete_rows == 0 else "INCOMPLETE",
        "spread_coverage_pct": round((count - spread_incomplete_rows) / count * 100.0, 4),
        "known_recorded_spread_usd": round(known_recorded_spread, 8),
        "total_recorded_spread_usd": (
            round(known_recorded_spread, 8) if spread_incomplete_rows == 0 else None
        ),
        "funding_data_status": "COMPLETE" if funding_incomplete_rows == 0 else "INCOMPLETE",
        "funding_coverage_pct": round((count - funding_incomplete_rows) / count * 100.0, 4),
        "known_recorded_funding_usd": round(known_recorded_funding, 8),
        "total_recorded_funding_usd": (
            round(known_recorded_funding, 8) if funding_incomplete_rows == 0 else None
        ),
        "execution_cost_attribution_status": (
            "COMPLETE" if fee_incomplete_rows == 0 and slippage_incomplete_rows == 0
            and spread_incomplete_rows == 0 and funding_incomplete_rows == 0
            else "INCOMPLETE"
        ),
        "ending_equity_usd": round(equity, 8),
        "net_return_pct": round((equity / initial_equity - 1.0) * 100.0, 4),
        "max_realized_drawdown_pct": round(max_drawdown_pct, 4),
        "r_sample_size": len(r_values),
        "net_expectancy_r": round(sum(r_values) / len(r_values), 6) if r_values else None,
        "performance_claim_allowed": False,
        "reason": (
            "Descriptive paper results only; this sample is not proof of future profitability."
            if ready else
            f"Need at least {min_sample_size} valid closed trades before performance review."
        ),
        "slippage_attribution": (
            "RECORDED_PER_FILL" if slippage_incomplete_rows == 0
            else "INCOMPLETE; missing entry or exit slippage is not treated as zero."
        ),
        "spread_attribution": (
            "RECORDED_PER_FILL" if spread_incomplete_rows == 0
            else "INCOMPLETE; current paper spot source does not provide a bid/ask spread quote."
        ),
    }
