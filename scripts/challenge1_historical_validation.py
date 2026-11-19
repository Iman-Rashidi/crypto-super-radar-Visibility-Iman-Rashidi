#!/usr/bin/env python3
"""Out-of-sample historical validation of the exact Challenge 1 candle engine.

This is a conditional strategy test: historical AI reviewer decisions cannot be
reconstructed, so both reviewer rounds are explicitly assumed to approve.
No credentials, private account endpoints, or order endpoints are used.
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from crypto_super_radar.challenge1_entry_evaluation import (
    validate_challenge1_entry_revalidation,
)
from crypto_super_radar.challenge1_strategy import (
    DEFAULT_CHALLENGE1_INSTRUMENTS,
    analyze_blofin_challenge1,
)

BASE_URL = "https://openapi.blofin.com"
BAR_MS = 300_000
LOOKBACK_DAYS = 180
OOS_FRACTION = 0.30
CANDLE_LIMIT = 1440
FUNDING_LIMIT = 100
START_EQUITY = 10_000.0
RISK_PER_TRADE = 0.005
MAX_LEVERAGE = 1.0
TAKER_FEE_BPS = 6.0
SLIPPAGE_BPS = 3.0
STRESS_FEE_BPS = 10.0
STRESS_SLIPPAGE_BPS = 5.0
MIN_OOS_TRADES = 100
MIN_PROFIT_FACTOR = 1.20
MAX_REALIZED_DRAWDOWN_PCT = 10.0
MIN_PROFITABLE_SYMBOLS = 3
REPORT_PATH = Path("reports/challenge1_profitability_validation.json")


def _request(session: requests.Session, path: str, params: dict[str, Any]) -> list:
    url = BASE_URL + path
    for attempt in range(6):
        try:
            response = session.get(url, params=params, timeout=(8, 30))
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(min(2 ** attempt, 20))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                raise RuntimeError(f"BloFin public API rejected {path}: {payload!r}")
            data = payload.get("data")
            if not isinstance(data, list):
                raise RuntimeError(f"BloFin public API returned malformed data for {path}")
            return data
        except (requests.RequestException, ValueError):
            if attempt == 5:
                raise
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"BloFin public API retry limit reached for {path}")


def fetch_instrument_meta(session: requests.Session, instrument: str) -> dict:
    rows = _request(session, "/api/v1/market/instruments", {"instId": instrument})
    selected = next((item for item in rows if item.get("instId") == instrument), None)
    if not isinstance(selected, dict):
        raise RuntimeError(f"{instrument}: BloFin contract metadata is unavailable")
    try:
        meta = {
            "contract_value": float(selected["contractValue"]),
            "lot_size": float(selected["lotSize"]),
            "min_size": float(selected["minSize"]),
            "tick_size": float(selected["tickSize"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{instrument}: invalid BloFin contract metadata") from exc
    if any(not math.isfinite(v) or v <= 0 for v in meta.values()):
        raise RuntimeError(f"{instrument}: non-positive BloFin contract metadata")
    return meta


def _floor_to_step(value: float, step: float) -> float:
    return math.floor((value / step) + 1e-10) * step


def _round_protective_price(value: float, tick: float, direction: str, role: str) -> float:
    # Round protective levels away from entry to avoid optimistic price rounding.
    round_up = (direction == "LONG" and role == "TARGET") or (
        direction == "SHORT" and role == "STOP"
    )
    units = value / tick
    rounded = math.ceil(units - 1e-10) if round_up else math.floor(units + 1e-10)
    return rounded * tick


def fetch_candles(session: requests.Session, instrument: str, cutoff_ms: int) -> list[list]:
    rows: dict[int, list] = {}
    cursor = None
    previous_oldest = None
    for _ in range(80):
        params: dict[str, Any] = {
            "instId": instrument, "bar": "5m", "limit": str(CANDLE_LIMIT),
        }
        if cursor is not None:
            params["after"] = str(cursor)
        page = _request(session, "/api/v1/market/candles", params)
        if not page:
            break
        stamps = []
        for item in page:
            if not isinstance(item, list) or len(item) < 9:
                continue
            try:
                stamp = int(item[0])
            except (TypeError, ValueError):
                continue
            if stamp >= cutoff_ms and str(item[8]) == "1":
                rows[stamp] = item
            stamps.append(stamp)
        if not stamps:
            break
        oldest = min(stamps)
        if oldest <= cutoff_ms or oldest == previous_oldest:
            break
        previous_oldest = oldest
        cursor = oldest
        time.sleep(0.08)
    ordered = [rows[key] for key in sorted(rows)]
    if len(ordered) < 100:
        raise RuntimeError(f"{instrument}: only {len(ordered)} closed 5m candles retrieved")
    return ordered


def fetch_funding(session: requests.Session, instrument: str, cutoff_ms: int) -> list[dict]:
    rows: dict[int, dict] = {}
    cursor = None
    previous_oldest = None
    for _ in range(20):
        params: dict[str, Any] = {"instId": instrument, "limit": str(FUNDING_LIMIT)}
        if cursor is not None:
            params["after"] = str(cursor)
        page = _request(session, "/api/v1/market/funding-rate-history", params)
        if not page:
            break
        stamps = []
        for item in page:
            try:
                stamp = int(item["fundingTime"])
                rate = float(item["fundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            if stamp >= cutoff_ms and math.isfinite(rate):
                rows[stamp] = {"timestamp_ms": stamp, "rate": rate}
            stamps.append(stamp)
        if not stamps:
            break
        oldest = min(stamps)
        if oldest <= cutoff_ms or oldest == previous_oldest:
            break
        previous_oldest = oldest
        cursor = oldest
        time.sleep(0.08)
    return [rows[key] for key in sorted(rows)]


class HistoricalBloFinReader:
    def __init__(self, candles: list[list], index: int):
        self.candles = candles
        self.index = index

    def get_candles(self, inst_id: str, *, bar: str, limit: int) -> list[list]:
        if bar != "5m":
            raise AssertionError(f"Unexpected timeframe requested: {bar}")
        return self.candles[max(0, self.index - limit + 1):self.index + 1]


def _analyse_at(candles: list[list], index: int, instrument: str) -> dict:
    reader = HistoricalBloFinReader(candles, index)
    now_ms = int(candles[index][0]) + BAR_MS
    return analyze_blofin_challenge1(reader, instrument, now_ms=now_ms)


def _wilson_interval(wins: int, count: int) -> tuple[float | None, float | None]:
    if count <= 0:
        return None, None
    z = 1.959963984540054
    p = wins / count
    den = 1 + z * z / count
    center = (p + z * z / (2 * count)) / den
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * count)) / count) / den
    return max(0.0, center - margin), min(1.0, center + margin)


def _bootstrap_mean_ci(
    trades: list[dict], seed: int = 20261119, block_days: int = 3,
) -> tuple[float | None, float | None]:
    # Resample contiguous 3-day blocks, not individual trades, to preserve
    # short-horizon serial dependence and same-day cross-asset clustering.
    grouped: dict[int, list[float]] = defaultdict(list)
    for trade in trades:
        value = trade.get("net_r")
        if value is None or not math.isfinite(float(value)):
            continue
        day = int(trade["exit_time_ms"]) // 86_400_000
        grouped[day].append(float(value))
    days = sorted(grouped)
    if len(days) < 10:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(5000):
        sample: list[float] = []
        while len(sample) < sum(len(grouped[day]) for day in days):
            start = rng.randrange(len(days))
            for offset in range(block_days):
                day = days[(start + offset) % len(days)]
                sample.extend(grouped[day])
        means.append(sum(sample) / len(sample))
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def _entry_candidate(candles: list[list], index: int, instrument: str) -> dict | None:
    """Require initial signal, a full new 5m candle, and unchanged selected setup."""
    if index + 2 >= len(candles):
        return None
    try:
        initial = _analyse_at(candles, index, instrument)
        if initial.get("signal") not in {"LONG", "SHORT"}:
            return None
        refreshed = _analyse_at(candles, index + 1, instrument)
        if refreshed.get("signal") not in {"LONG", "SHORT"}:
            return None
        proof = validate_challenge1_entry_revalidation(
            initial, refreshed, elapsed_seconds=300.0,
        )
        if proof.get("status") != "PASS":
            return None
        return {
            "instrument": instrument,
            "direction": refreshed["signal"],
            "signal_id": refreshed.get("signal_id"),
            "signal_bar": refreshed.get("signal_bar_open_utc"),
            "entry_index": index + 2,
            "stop": float(refreshed["stop_loss_usd"]),
            "target": float(refreshed["take_profit_usd"]),
            "pattern": (refreshed.get("challenge1_evidence") or {}).get("chosen_pattern", {}).get("name_en"),
            "proof": proof,
        }
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return None


def _funding_cost(
    funding: list[dict], start_ms: int, end_ms: int, quantity: float,
    entry_price: float, direction: str,
) -> float:
    sign = 1.0 if direction == "LONG" else -1.0
    notional = quantity * entry_price
    return sum(
        notional * float(item["rate"]) * sign
        for item in funding
        if start_ms < int(item["timestamp_ms"]) <= end_ms
    )


def _simulate_symbol(
    instrument: str,
    candles: list[list],
    funding: list[dict],
    oos_start: int,
    *,
    fee_bps: float,
    slippage_bps: float,
    allocation: float,
    contract_meta: dict,
) -> tuple[list[dict], dict]:
    candidates: dict[int, dict] = {}
    analysis_errors = 0
    for index in range(oos_start, len(candles) - 2):
        try:
            candidate = _entry_candidate(candles, index, instrument)
        except Exception:
            analysis_errors += 1
            candidate = None
        if candidate is not None:
            candidates[candidate["entry_index"]] = candidate

    trades: list[dict] = []
    equity = allocation
    position = None
    skipped_position_open = 0
    skipped_invalid_plan = 0
    fee_rate = fee_bps / 10_000.0
    slip_rate = slippage_bps / 10_000.0

    for index in range(oos_start + 2, len(candles)):
        row = candles[index]
        ts, op, hi, lo, cl = int(row[0]), *map(float, row[1:5])
        was_open_at_bar_start = position is not None

        if position is not None:
            p = position
            direction = p["direction"]
            if direction == "LONG":
                stop_hit, target_hit = lo <= p["stop"], hi >= p["target"]
                if stop_hit:
                    raw_exit = min(op, p["stop"])
                    reason = "STOP_LOSS" if op > p["stop"] else "STOP_GAP"
                elif target_hit:
                    raw_exit, reason = p["target"], "TAKE_PROFIT"
                else:
                    raw_exit = None
            else:
                stop_hit, target_hit = hi >= p["stop"], lo <= p["target"]
                if stop_hit:
                    raw_exit = max(op, p["stop"])
                    reason = "STOP_LOSS" if op < p["stop"] else "STOP_GAP"
                elif target_hit:
                    raw_exit, reason = p["target"], "TAKE_PROFIT"
                else:
                    raw_exit = None

            if raw_exit is not None:
                exit_fill = raw_exit * (1 - slip_rate if direction == "LONG" else 1 + slip_rate)
                exit_fee = p["quantity"] * exit_fill * fee_rate
                gross = p["quantity"] * (exit_fill - p["entry_fill"]) * (1 if direction == "LONG" else -1)
                funding_cost = _funding_cost(
                    funding, p["entry_time_ms"], ts, p["quantity"], p["entry_fill"], direction,
                )
                net = gross - p["entry_fee"] - exit_fee - funding_cost
                slippage_cost = (
                    abs(p["entry_fill"] - p["raw_entry"]) * p["quantity"]
                    + abs(exit_fill - raw_exit) * p["quantity"]
                )
                trades.append({
                    "instrument": instrument, "direction": direction,
                    "signal_id": p["signal_id"], "pattern": p["pattern"],
                    "entry_time_ms": p["entry_time_ms"], "exit_time_ms": ts,
                    "raw_entry": p["raw_entry"], "entry_fill": p["entry_fill"],
                    "raw_exit": raw_exit, "exit_fill": exit_fill,
                    "stop": p["stop"], "target": p["target"],
                    "quantity_base": p["quantity"], "contracts": p["contracts"],
                    "contract_value_base": contract_value,
                    "risk_budget_usd": p["risk_budget"],
                    "gross_pnl_usd": gross,
                    "entry_fee_usd": p["entry_fee"], "exit_fee_usd": exit_fee,
                    "funding_cost_usd": funding_cost, "slippage_cost_usd": slippage_cost,
                    "net_pnl_usd": net, "net_r": net / p["risk_budget"] if p["risk_budget"] else None,
                    "exit_reason": reason,
                })
                equity += net
                position = None

        # If a position existed at the bar open, never re-enter on the same bar
        # after an intrabar exit. This avoids optimistic same-candle sequencing.
        if position is None and not was_open_at_bar_start and index in candidates:
            candidate = candidates[index]
            direction = candidate["direction"]
            raw_entry = op
            entry_fill = raw_entry * (1 + slip_rate if direction == "LONG" else 1 - slip_rate)
            stop = _round_protective_price(
                candidate["stop"], contract_meta["tick_size"], direction, "STOP",
            )
            target = _round_protective_price(
                candidate["target"], contract_meta["tick_size"], direction, "TARGET",
            )
            valid = (
                entry_fill > stop and target > entry_fill
                if direction == "LONG"
                else entry_fill < stop and target < entry_fill
            )
            if not valid:
                skipped_invalid_plan += 1
                continue
            stop_distance = abs(entry_fill - stop)
            risk_budget = equity * RISK_PER_TRADE
            base_quantity = min(risk_budget / stop_distance, equity / entry_fill / MAX_LEVERAGE)
            contract_value = contract_meta["contract_value"]
            lot_size = contract_meta["lot_size"]
            contracts = _floor_to_step(base_quantity / contract_value, lot_size)
            if contracts < contract_meta["min_size"]:
                skipped_invalid_plan += 1
                continue
            quantity = contracts * contract_value
            if not math.isfinite(quantity) or quantity <= 0:
                skipped_invalid_plan += 1
                continue
            entry_fee = quantity * entry_fill * fee_rate
            position = {
                "direction": direction, "signal_id": candidate["signal_id"],
                "pattern": candidate["pattern"], "entry_time_ms": ts,
                "raw_entry": raw_entry, "entry_fill": entry_fill,
                "stop": stop, "target": target, "quantity": quantity,
                "contracts": contracts, "entry_fee": entry_fee, "risk_budget": risk_budget,
            }

    open_position = None
    if position is not None:
        last = candles[-1]
        last_ts, last_close = int(last[0]), float(last[4])
        direction = position["direction"]
        mark_gross = position["quantity"] * (last_close - position["entry_fill"]) * (1 if direction == "LONG" else -1)
        mark_funding = _funding_cost(
            funding, position["entry_time_ms"], last_ts,
            position["quantity"], position["entry_fill"], direction,
        )
        mark_exit_fee = position["quantity"] * last_close * fee_rate
        mark_net = mark_gross - position["entry_fee"] - mark_exit_fee - mark_funding
        open_position = {
            "instrument": instrument, "direction": direction,
            "entry_time_ms": position["entry_time_ms"],
            "mark_time_ms": last_ts, "entry_fill": position["entry_fill"],
            "mark_price": last_close, "unrealized_net_pnl_estimate_usd": mark_net,
        }

    expected_funding = max(1, int((LOOKBACK_DAYS * 24) / 8))
    funding_coverage = min(1.0, len(funding) / expected_funding)
    return trades, {
        "instrument": instrument, "candles": len(candles),
        "oos_start_index": oos_start, "oos_candles": len(candles) - oos_start,
        "closed_trades": len(trades), "analysis_errors": analysis_errors,
        "skipped_invalid_plans": skipped_invalid_plan,
        "funding_events_observed": len(funding),
        "funding_event_coverage_estimate_pct": round(funding_coverage * 100, 2),
        "ending_allocated_equity_usd": round(equity, 4),
        "open_position_at_end": open_position,
    }


def _metrics(trades: list[dict], initial_equity: float) -> dict:
    count = len(trades)
    wins = [t for t in trades if t["net_pnl_usd"] > 0]
    losses = [t for t in trades if t["net_pnl_usd"] < 0]
    total_net = sum(t["net_pnl_usd"] for t in trades)
    gross_wins = sum(t["net_pnl_usd"] for t in wins)
    gross_losses = abs(sum(t["net_pnl_usd"] for t in losses))
    pf = gross_wins / gross_losses if gross_losses > 0 else None
    r_values = [float(t["net_r"]) for t in trades if t.get("net_r") is not None]
    ci_low, ci_high = _bootstrap_mean_ci(trades)
    win_low, win_high = _wilson_interval(len(wins), count)
    equity = initial_equity
    peak = initial_equity
    max_dd = 0.0
    for trade in sorted(trades, key=lambda t: (t["exit_time_ms"], t["instrument"])):
        equity += trade["net_pnl_usd"]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
    profitable_symbols = sum(
        1 for symbol in DEFAULT_CHALLENGE1_INSTRUMENTS
        if sum(t["net_pnl_usd"] for t in trades if t["instrument"] == symbol) > 0
    )
    return {
        "closed_trades": count, "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / count * 100, 3) if count else None,
        "win_rate_wilson_95pct": [
            round(win_low * 100, 3), round(win_high * 100, 3)
        ] if win_low is not None else None,
        "net_pnl_usd": round(total_net, 4),
        "net_return_pct_on_10000": round(total_net / initial_equity * 100, 4),
        "net_expectancy_usd_per_trade": round(total_net / count, 6) if count else None,
        "profit_factor_net": round(pf, 5) if pf is not None else None,
        "mean_net_r": round(sum(r_values) / len(r_values), 6) if r_values else None,
        "bootstrap_95pct_ci_mean_net_r": [
            round(ci_low, 6), round(ci_high, 6)
        ] if ci_low is not None else None,
        "bootstrap_method": "5000-resample moving block bootstrap, 3 consecutive UTC exit days per block",
        "realized_close_to_close_max_drawdown_pct": round(max_dd, 4),
        "profitable_instruments": profitable_symbols,
        "total_fees_usd": round(sum(t["entry_fee_usd"] + t["exit_fee_usd"] for t in trades), 4),
        "total_slippage_usd": round(sum(t["slippage_cost_usd"] for t in trades), 4),
        "total_funding_cost_usd": round(sum(t["funding_cost_usd"] for t in trades), 4),
    }


def main() -> int:
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    session = requests.Session()
    session.headers.update({"User-Agent": "CRYPTO-SUPER-RADAR-Challenge1-Validation/1.0"})
    all_candles: dict[str, list[list]] = {}
    all_funding: dict[str, list[dict]] = {}
    contract_metadata: dict[str, dict] = {}
    quality: dict[str, dict] = {}

    for instrument in DEFAULT_CHALLENGE1_INSTRUMENTS:
        candles = fetch_candles(session, instrument, cutoff_ms)
        funding = fetch_funding(session, instrument, cutoff_ms)
        contract_meta = fetch_instrument_meta(session, instrument)
        stamps = [int(row[0]) for row in candles]
        gaps = sum(1 for a, b in zip(stamps, stamps[1:]) if b - a != BAR_MS)
        expected = max(1, int((now_ms - cutoff_ms) / BAR_MS))
        coverage = min(1.0, len(candles) / expected)
        if gaps:
            raise RuntimeError(f"{instrument}: historical 5m data contains {gaps} missing intervals; refusing biased backtest.")
        if coverage < 0.985:
            raise RuntimeError(f"{instrument}: only {coverage:.2%} candle coverage; refusing biased backtest.")
        all_candles[instrument] = candles
        all_funding[instrument] = funding
        contract_metadata[instrument] = contract_meta
        quality[instrument] = {
            "blofin_contract_metadata": contract_meta,
            "closed_5m_candles": len(candles),
            "missing_5m_intervals": gaps,
            "estimated_candle_coverage_pct": round(coverage * 100, 3),
            "funding_events": len(funding),
        }

    # Use the shortest common range, preserving a chronological 70/30 split.
    common_length = min(len(rows) for rows in all_candles.values())
    for instrument in all_candles:
        all_candles[instrument] = all_candles[instrument][-common_length:]
    oos_start = int(common_length * (1.0 - OOS_FRACTION))
    allocation = START_EQUITY / len(DEFAULT_CHALLENGE1_INSTRUMENTS)

    base_trades: list[dict] = []
    stress_trades: list[dict] = []
    per_symbol = {}
    for instrument in DEFAULT_CHALLENGE1_INSTRUMENTS:
        trades, detail = _simulate_symbol(
            instrument, all_candles[instrument], all_funding[instrument], oos_start,
            fee_bps=TAKER_FEE_BPS, slippage_bps=SLIPPAGE_BPS, allocation=allocation,
            contract_meta=contract_metadata[instrument],
        )
        stress, _ = _simulate_symbol(
            instrument, all_candles[instrument], all_funding[instrument], oos_start,
            fee_bps=STRESS_FEE_BPS, slippage_bps=STRESS_SLIPPAGE_BPS, allocation=allocation,
            contract_meta=contract_metadata[instrument],
        )
        base_trades.extend(trades)
        stress_trades.extend(stress)
        per_symbol[instrument] = detail

    base_trades.sort(key=lambda t: (t["exit_time_ms"], t["instrument"]))
    stress_trades.sort(key=lambda t: (t["exit_time_ms"], t["instrument"]))
    base = _metrics(base_trades, START_EQUITY)
    stress = _metrics(stress_trades, START_EQUITY)

    funding_coverage_ok = all(
        per_symbol[s]["funding_event_coverage_estimate_pct"] >= 95.0
        for s in per_symbol
    )
    enough_trades = base["closed_trades"] >= MIN_OOS_TRADES
    statistical_edge = (
        base["bootstrap_95pct_ci_mean_net_r"] is not None
        and base["bootstrap_95pct_ci_mean_net_r"][0] > 0
    )
    passed = bool(
        enough_trades and statistical_edge
        and base["net_pnl_usd"] > 0
        and base["profit_factor_net"] is not None
        and base["profit_factor_net"] >= MIN_PROFIT_FACTOR
        and base["realized_close_to_close_max_drawdown_pct"] <= MAX_REALIZED_DRAWDOWN_PCT
        and base["profitable_instruments"] >= MIN_PROFITABLE_SYMBOLS
        and funding_coverage_ok
        and stress["net_pnl_usd"] > 0
    )
    report = {
        "report_version": "CHALLENGE1_HISTORICAL_OOS_V1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "market_data_source": "BloFin public live-market REST API; no account credentials",
        "strategy_source": "Exact crypto_super_radar.challenge1_strategy.analyze_blofin_challenge1",
        "entry_confirmation": "Exact validate_challenge1_entry_revalidation; 300 seconds and next completed 5m candle",
        "sample_period_days": LOOKBACK_DAYS,
        "walk_forward": {
            "method": "chronological 70% history / 30% untouched out-of-sample",
            "oos_fraction": OOS_FRACTION,
            "in_sample_used_for_parameter_optimization": False,
            "timeframe": "5m",
        },
        "historical_ai_assumption": {
            "status": "ASSUMED_UNANIMOUS_APPROVAL",
            "details": "Both independent AI reviewers are assumed to approve both historical review rounds. Historical LLM decisions cannot be reconstructed. This is an optimistic conditional test, not a full live-system backtest.",
        },
        "execution_assumptions": {
            "leverage_cap": MAX_LEVERAGE,
            "risk_per_trade_of_allocated_equity": RISK_PER_TRADE,
            "max_concurrent_positions": "one per instrument; five independent equal capital sleeves",
            "base_taker_fee_bps_per_side": TAKER_FEE_BPS,
            "base_slippage_bps_per_side": SLIPPAGE_BPS,
            "stress_taker_fee_bps_per_side": STRESS_FEE_BPS,
            "stress_slippage_bps_per_side": STRESS_SLIPPAGE_BPS,
            "same_bar_stop_and_target": "stop loss first",
            "entry": "next 5m candle open after the final reviewer round, with adverse slippage",
            "exit": "TP/SL OHLC trigger, adverse slippage applied; gap-through-stop fills at worse open",
            "funding": "actual BloFin historical funding rate events; notional held constant at entry price for attribution",
            "open_positions_at_end": "marked and reported separately; not forced closed or counted as realized trades",
        },
        "acceptance_thresholds": {
            "minimum_oos_closed_trades": MIN_OOS_TRADES,
            "minimum_net_profit_factor": MIN_PROFIT_FACTOR,
            "bootstrap_95pct_lower_bound_mean_net_r": "> 0",
            "maximum_realized_drawdown_pct": MAX_REALIZED_DRAWDOWN_PCT,
            "minimum_profitable_instruments": MIN_PROFITABLE_SYMBOLS,
            "funding_coverage_pct": ">= 95% estimated 8-hour schedule",
            "stress_scenario_must_remain_net_profitable": True,
        },
        "data_quality": quality,
        "base_cost_scenario": base,
        "stress_cost_scenario": stress,
        "per_instrument": per_symbol,
        "closed_trades": base_trades,
        "performance_claim_allowed": False,
        "historical_strategy_test_status": "PASS" if passed else (
            "INSUFFICIENT_SAMPLE" if not enough_trades else "FAIL"
        ),
        "final_profitability_proof": "NOT_PROVEN",
        "reason": (
            "A positive historical result is conditional on assumed unanimous AI approval and cannot establish future profitability. Forward BloFin Demo evidence is still required."
            if passed else
            "The predefined out-of-sample criteria were not all met; no profitability claim is permitted."
        ),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "historical_strategy_test_status": report["historical_strategy_test_status"],
        "final_profitability_proof": report["final_profitability_proof"],
        "data_quality": quality,
        "base_cost_scenario": base,
        "stress_cost_scenario": stress,
        "funding_coverage_ok": funding_coverage_ok,
        "per_instrument": per_symbol,
        "report_path": str(REPORT_PATH),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"CHALLENGE1_HISTORICAL_VALIDATION_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
