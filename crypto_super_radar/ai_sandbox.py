"""AI-in-the-loop offline walk-forward simulator for CRYPTO SUPER RADAR.

Reads a local SQLite replay database, applies the deterministic 1H+15M
Challenge 1 strategy, simulates conservative post-only fills/Mark-price stops,
and trains a meta-labeler only on matured training-window net outcomes.

This module is sandbox-only. It has no exchange credentials, no REST write
client, no WebSocket trade client, and cannot place/cancel real orders.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .adaptive_brain import OnlineMetaBrain
from .challenge1_v2 import analyze_signal, atr
from .virtual_blofin import CostModel, InstrumentRules, InsufficientEquityForRisk, SimulationError, VirtualBloFin
from .market_replay import (
    FUNDING_INTERVAL_HOURS, FUNDING_SCHEDULE_TOLERANCE_MS,
    OBSERVED_FUNDING_BLACKOUTS, _classify_funding_schedule_events,
    _is_observed_blackout_gap, _funding_interval_for_delta,
)

DEFAULT_WATCHLIST = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
BAR_MS = 15 * 60_000


class SandboxDataError(RuntimeError):
    pass


def _load_candles(connection: sqlite3.Connection, symbol: str, bar: str, source: str = "LAST") -> pd.DataFrame:
    rows = connection.execute(
        """SELECT ts,open,high,low,close,volume FROM replay_candles
           WHERE instrument_id=? AND bar=? AND source=? ORDER BY ts ASC""",
        (symbol, bar, source),
    ).fetchall()
    if not rows:
        raise SandboxDataError(f"missing {source} {bar} candles for {symbol}")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_numeric(df["ts"], errors="raise").astype("int64")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if df.isna().any().any() or not np.isfinite(df[["open", "high", "low", "close", "volume"]].to_numpy()).all():
        raise SandboxDataError(f"non-finite values in {symbol} {source} {bar}")
    df["timestamp"] = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df.set_index("timestamp").sort_index()


def _require_regular_funding_events(funding: list[Mapping[str, Any]], symbol: str) -> None:
    unsupported = []
    for item in funding:
        kind = str(item.get("fundingKind") or "").strip()
        source = str(item.get("fundingKindSource") or "").strip()
        if kind == "0" and not source:
            # Compatibility for callers carrying an explicit API Kind=0.
            source = "EXPLICIT_API"
        if kind != "0":
            unsupported.append(kind or "MISSING")
        elif source == "EXPLICIT_API":
            continue
        elif source == "SCHEDULE_INFERRED_DYNAMIC_INTERVAL":
            try:
                interval_ms = int(item.get("fundingIntervalMs"))
                timestamp = int(item.get("fundingTime"))
            except (TypeError, ValueError, OverflowError):
                unsupported.append("INFERENCE_METADATA_MISSING")
                continue
            allowed = {hours * 60 * 60_000 for hours in FUNDING_INTERVAL_HOURS}
            remainder = timestamp % interval_ms if interval_ms > 0 else -1
            if not (
                item.get("fundingScheduleVerified") is True
                and interval_ms in allowed
                and min(remainder, interval_ms - remainder) <= FUNDING_SCHEDULE_TOLERANCE_MS
            ):
                unsupported.append("INFERENCE_NOT_VERIFIED")
        else:
            unsupported.append(source or "PROVENANCE_MISSING")
    if unsupported:
        raise SandboxDataError(
            f"only models regular fundingKind=0 or verified schedule-inferred regular funding "
            f"for {symbol}; unsupported classification={sorted(set(unsupported))}"
        )


def _funding_blackout_for_timestamp(timestamp_ms: int) -> dict[str, Any] | None:
    stamp = int(timestamp_ms)
    return next((
        dict(window) for window in OBSERVED_FUNDING_BLACKOUTS
        if int(window["start_ms"]) <= stamp < int(window["end_ms"])
    ), None)


def _load_symbol(connection: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    frames = {
        "15m": _load_candles(connection, symbol, "15m", "LAST"),
        "1H": _load_candles(connection, symbol, "1H", "LAST"),
        "4H": _load_candles(connection, symbol, "4H", "LAST"),
    }
    mark = _load_candles(connection, symbol, "15m", "MARK")
    market = frames["15m"].join(
        mark[["high", "low"]].rename(columns={"high": "mark_high", "low": "mark_low"}),
        how="inner",
    )
    if len(market) < 70:
        raise SandboxDataError(f"insufficient aligned market/mark candles for {symbol}")
    for label, frame, interval in (
        ("15m", frames["15m"], pd.Timedelta(minutes=15)),
        ("1H", frames["1H"], pd.Timedelta(hours=1)),
        ("4H", frames["4H"], pd.Timedelta(hours=4)),
        ("MARK_15m", mark, pd.Timedelta(minutes=15)),
        ("ALIGNED_15m", market, pd.Timedelta(minutes=15)),
    ):
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise SandboxDataError(f"{symbol} {label} timestamps are duplicated or unordered")
        gaps = frame.index.to_series().diff().dropna()
        if not gaps.empty and not (gaps == interval).all():
            raise SandboxDataError(f"{symbol} {label} replay has missing candle intervals")
    market["atr_15m"] = atr(market, 14)
    market = market.dropna(subset=["atr_15m"])
    source_start = max(
        market.index[0],
        frames["1H"].index[0],
        frames["4H"].index[0],
        mark.index[0],
    )
    source_end = min(
        market.index[-1] + pd.Timedelta(minutes=15),
        frames["1H"].index[-1] + pd.Timedelta(hours=1),
        frames["4H"].index[-1] + pd.Timedelta(hours=4),
        mark.index[-1] + pd.Timedelta(minutes=15),
    )
    if source_end <= source_start:
        raise SandboxDataError(f"no common 15m/1H/4H/Mark replay window for {symbol}")
    market["close_ts"] = market.index.view("int64") // 1_000_000 + BAR_MS
    market["ts_ms"] = market.index.view("int64") // 1_000_000
    bars = []
    for _, row in market.iterrows():
        bars.append({
            "ts": int(row["ts_ms"]), "close_ts": int(row["close_ts"]),
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": float(row["volume"]), "mark_high": float(row["mark_high"]),
            "mark_low": float(row["mark_low"]), "atr_15m": float(row["atr_15m"]),
        })
    funding_rows = connection.execute(
        """SELECT funding_time,funding_rate,funding_kind,funding_kind_source,funding_interval_ms
           FROM replay_funding WHERE instrument_id=? ORDER BY funding_time ASC""",
        (symbol,),
    ).fetchall()
    stored_rows = [
        (symbol, int(ts), float(rate), str(kind), str(source))
        for ts, rate, kind, source, _interval in funding_rows
    ]
    verified_rows = _classify_funding_schedule_events(stored_rows, symbol=symbol)
    funding = []
    for stored, verified in zip(funding_rows, verified_rows):
        ts, rate, kind, source, interval_ms = stored
        if interval_ms is None or int(interval_ms) != int(verified[5]):
            raise SandboxDataError(f"stored funding interval provenance changed for {symbol}")
        funding.append({
            "fundingTime": int(ts), "fundingRate": float(rate), "fundingKind": str(kind),
            "fundingKindSource": str(source), "fundingIntervalMs": int(interval_ms),
            "fundingScheduleVerified": str(source) == "SCHEDULE_INFERRED_DYNAMIC_INTERVAL",
        })
    if len(funding) < 3:
        raise SandboxDataError(f"funding history is incomplete for {symbol}")
    _require_regular_funding_events(funding, symbol)
    funding_times = np.asarray([int(item["fundingTime"]) for item in funding], dtype=np.int64)
    funding_rates = np.asarray([float(item["fundingRate"]) for item in funding], dtype=float)
    if (
        not np.isfinite(funding_rates).all()
        or (np.diff(funding_times) <= 0).any()
    ):
        raise SandboxDataError(f"funding history is invalid or unordered for {symbol}")
    funding_deltas = np.diff(funding_times)
    verified_intervals = []
    for left, right, delta in zip(funding_times[:-1], funding_times[1:], funding_deltas):
        if _is_observed_blackout_gap(int(left), int(right)):
            continue
        interval_ms = _funding_interval_for_delta(int(delta))
        if interval_ms is None:
            raise SandboxDataError(
                f"funding history contains an unverified cadence gap for {symbol}: {int(delta)} ms"
            )
        verified_intervals.append(interval_ms)
    if not verified_intervals:
        raise SandboxDataError(f"funding history has no verified cadence for {symbol}")
    median_funding_interval = int(np.median(np.asarray(verified_intervals, dtype=np.int64)))
    source_start_ms = int(source_start.timestamp() * 1000)
    source_end_ms = int(source_end.timestamp() * 1000)
    if (
        int(funding_times[0]) - source_start_ms > int(funding[0]["fundingIntervalMs"] * 1.5)
        or source_end_ms - int(funding_times[-1]) > int(funding[-1]["fundingIntervalMs"] * 1.5)
    ):
        raise SandboxDataError(f"funding history does not cover the full replay window for {symbol}")
    instrument_row = connection.execute(
        "SELECT raw_json FROM replay_instruments WHERE instrument_id=?", (symbol,)
    ).fetchone()
    if not instrument_row:
        raise SandboxDataError(f"instrument contract metadata missing for {symbol}; re-run market downloader")
    rules = InstrumentRules.from_exchange(json.loads(instrument_row[0]))
    frames["15m"] = market[["open", "high", "low", "close", "volume"]].copy()
    return {
        "frames": frames,
        "market": market,
        "bars": bars,
        "funding": funding,
        "funding_interval_median_ms": median_funding_interval,
        "rules": rules,
        "source_start": source_start,
        "source_end": source_end,
    }


def _visible_frame(
    frame: pd.DataFrame,
    asof: pd.Timestamp,
    duration: pd.Timedelta,
) -> pd.DataFrame:
    """Return only candles whose close timestamp is no later than the cutoff."""
    if asof.tzinfo is None:
        raise ValueError("asof must be timezone-aware")
    closed = frame.index + duration <= asof.tz_convert("UTC")
    return frame.loc[closed].copy()


def _visible_bars(bars: list[dict[str, Any]], asof_ms: int) -> list[dict[str, Any]]:
    """Expose only fully closed 15m bars to an in-progress virtual position."""
    return [
        bar for bar in bars
        if int(bar.get("close_ts") or (int(bar["ts"]) + BAR_MS)) <= asof_ms
    ]



def _known_funding_event(
    events: list[dict[str, Any]],
    asof_ms: int,
    *,
    max_age_ms: int | None = None,
) -> dict[str, Any] | None:
    eligible: list[tuple[int, dict[str, Any]]] = []
    for item in events:
        try:
            event_time = int(item["fundingTime"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if event_time <= asof_ms:
            eligible.append((event_time, item))
    if not eligible:
        return None
    event_time, latest = max(eligible, key=lambda pair: pair[0])
    if str(latest.get("fundingKind") or "").strip() != "0":
        return None
    if max_age_ms is not None and (
        isinstance(max_age_ms, bool)
        or not isinstance(max_age_ms, int)
        or max_age_ms <= 0
        or asof_ms - event_time > max_age_ms
    ):
        return None
    try:
        rate = float(latest["fundingRate"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not np.isfinite(rate):
        return None
    return {
        "fundingTime": event_time,
        "fundingRate": rate,
        "fundingKind": "0",
    }


def _known_funding_rate(
    events: list[dict[str, Any]],
    asof_ms: int,
    *,
    max_age_ms: int | None = None,
) -> float | None:
    event = _known_funding_event(events, asof_ms, max_age_ms=max_age_ms)
    return float(event["fundingRate"]) if event is not None else None


def _within_purge_window(
    asof_ms: int,
    boundary_ms: int,
    *,
    max_holding_bars: int = 12,
) -> bool:
    """Block new fold entries whose maximum modeled life touches the next fold."""
    purge_ms = (max_holding_bars + 1) * BAR_MS
    return asof_ms >= boundary_ms - purge_ms


def _calibration_report(observations: list[dict[str, Any]], *, bins: int = 10) -> dict[str, Any]:
    valid = []
    for row in observations:
        try:
            probability = float(row["probability"])
            label = int(row["label"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if np.isfinite(probability) and 0.0 <= probability <= 1.0 and label in (0, 1):
            valid.append((probability, label))
    if not valid:
        return {
            "status": "UNKNOWN",
            "sample_count": 0,
            "brier_score": None,
            "calibration_curve": [],
            "note": "No completed outcomes had a frozen-model probability at signal time.",
        }
    brier = float(sum((probability - label) ** 2 for probability, label in valid) / len(valid))
    curve = []
    for bin_index in range(bins):
        lower = bin_index / bins
        upper = (bin_index + 1) / bins
        rows = [
            (probability, label) for probability, label in valid
            if lower <= probability < upper or (bin_index == bins - 1 and probability == 1.0)
        ]
        if rows:
            curve.append({
                "bin_lower": lower,
                "bin_upper": upper,
                "sample_count": len(rows),
                "mean_predicted_probability": float(np.mean([item[0] for item in rows])),
                "observed_win_rate": float(np.mean([item[1] for item in rows])),
            })
    return {
        "status": "CALCULATED",
        "sample_count": len(valid),
        "brier_score": brier,
        "calibration_curve": curve,
        "note": "Descriptive out-of-sample calibration; small samples and selective signal coverage limit inference.",
    }


def _required_replay_end(start_time: pd.Timestamp, common_end: pd.Timestamp) -> pd.Timestamp:
    required = start_time + pd.DateOffset(months=6)
    # source_end is the close timestamp of the latest fully confirmed 15m bar.
    # It must reach the calendar boundary, not merely the start of the final bar.
    if common_end < required:
        raise SandboxDataError(
            f"replay coverage is shorter than six calendar months: "
            f"start={start_time.isoformat()} end={common_end.isoformat()} "
            f"required_end_at_least={required.isoformat()}"
        )
    return required


def _phase(asof_ms: int, train_end_ms: int, validation_end_ms: int) -> str:
    if asof_ms <= train_end_ms:
        return "TRAIN"
    if asof_ms <= validation_end_ms:
        return "VALIDATION"
    return "OUT_OF_SAMPLE"


def _max_consecutive_losses(trades: list[dict[str, Any]]) -> int:
    streak = best = 0
    for trade in sorted(trades, key=lambda x: x["exit_ts_ms"]):
        if trade["net_pnl_usdt"] < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def _performance_summary(trades: list[dict[str, Any]], initial_equity: float) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda item: int(item.get("exit_ts_ms") or 0))
    pnls = [float(item["net_pnl_usdt"]) for item in ordered if item.get("net_pnl_usdt") is not None]
    wins = sum(pnl > 0 for pnl in pnls)
    losses = sum(pnl < 0 for pnl in pnls)
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
    equity, peak, max_dd, max_dd_pct = float(initial_equity), float(initial_equity), 0.0, 0.0
    for trade in ordered:
        pnl = trade.get("net_pnl_usdt")
        if pnl is None:
            continue
        equity += float(pnl)
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, dd / peak * 100.0)
    r_values = [float(t["net_r_multiple"]) for t in ordered if t.get("net_r_multiple") is not None]
    net = sum(pnls)
    return {
        "completed_trades": len(pnls), "wins": wins, "losses": losses,
        "net_win_rate_pct": wins / len(pnls) * 100.0 if pnls else None,
        "total_net_pnl_usdt": net,
        "total_net_pnl_pct_initial_portfolio_equity": net / initial_equity * 100.0 if initial_equity else None,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "profit_factor_status": "CALCULATED" if gross_loss > 0 else "UNDEFINED_NO_GROSS_LOSS",
        "expectancy_usdt_per_trade": float(np.mean(pnls)) if pnls else None,
        "average_net_r": float(np.mean(r_values)) if r_values else None,
        "maximum_drawdown_usdt": max_dd, "maximum_drawdown_pct": max_dd_pct,
        "total_fees_usdt": sum(float(t.get("fees_usdt") or 0.0) for t in ordered),
        "total_funding_usdt": sum(float(t.get("funding_usdt") or 0.0) for t in ordered),
        "spread_cost_modelled_usdt": sum(float(t.get("spread_cost_usdt") or 0.0) for t in ordered),
        "slippage_cost_modelled_usdt": sum(float(t.get("slippage_cost_usdt") or 0.0) for t in ordered),
    }


def _grouped_performance_summary(
    trades: list[dict[str, Any]], initial_equity: float, symbols: tuple[str, ...],
) -> dict[str, Any]:
    phases = ("TRAIN", "VALIDATION", "OUT_OF_SAMPLE")
    return {
        "portfolio": _performance_summary(trades, initial_equity),
        "by_symbol": {
            symbol: _performance_summary([t for t in trades if t.get("instrument_id") == symbol], initial_equity)
            for symbol in symbols
        },
        "by_phase": {
            phase: _performance_summary([t for t in trades if t.get("phase") == phase], initial_equity)
            for phase in phases
        },
        "by_symbol_and_phase": {
            symbol: {
                phase: _performance_summary([
                    t for t in trades if t.get("instrument_id") == symbol and t.get("phase") == phase
                ], initial_equity) for phase in phases
            } for symbol in symbols
        },
    }


def _shadow_book_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    losers = [t for t in trades if float(t.get("net_pnl_usdt") or 0.0) <= 0]
    winners = [t for t in trades if float(t.get("net_pnl_usdt") or 0.0) > 0]
    return {
        "closed_counterfactual_trades": len(trades),
        "losses_avoided_count": len(losers), "winners_missed_count": len(winners),
        "estimated_losses_avoided_usdt": sum(abs(float(t["net_pnl_usdt"])) for t in losers),
        "estimated_profit_missed_usdt": sum(float(t["net_pnl_usdt"]) for t in winners),
        "interpretation": "Counterfactual only; shadow outcomes are not executable fills.",
    }


def run_sandbox(
    *, db_path: str, initial_equity: float = 1000.0,
    symbols: tuple[str, ...] = DEFAULT_WATCHLIST,
    spread_scenario: float = 0.0002,
    output_path: str = "reports/ai_sandbox_report.json",
    brain_path: str = "reports/trained_brain.json",
) -> dict[str, Any]:
    if not 0 < initial_equity or not np.isfinite(initial_equity):
        raise ValueError("initial_equity must be positive")
    if not 0 <= spread_scenario <= 0.0004:
        raise ValueError("spread_scenario must be between 0 and 0.04%")
    connection = sqlite3.connect(db_path)
    try:
        market = {symbol: _load_symbol(connection, symbol) for symbol in symbols}
    finally:
        connection.close()

    if "BTC-USDT" not in market:
        raise SandboxDataError("BTC-USDT is required as the master-guard market")
    # All five markets must cover the same six-calendar-month replay horizon.
    # The downloader and loader fail closed on missing candles or funding events.
    start_time = max(item["source_start"] for item in market.values())
    common_end = min(item["source_end"] for item in market.values())
    oos_end = _required_replay_end(start_time, common_end)
    train_end = start_time + pd.DateOffset(months=4)
    validation_end = start_time + pd.DateOffset(months=5)
    train_end_ms = int(train_end.timestamp() * 1000)
    validation_end_ms = int(validation_end.timestamp() * 1000)
    times = sorted(
        stamp
        for stamp in set().union(*(set(item["market"].index) for item in market.values()))
        if start_time <= stamp < oos_end
    )
    if len(times) < 100:
        raise SandboxDataError("replay dataset is too small after six-month window alignment")

    costs = CostModel(spread_fraction=spread_scenario)
    exchange = VirtualBloFin(costs=costs, max_holding_bars=12)
    brain = OnlineMetaBrain(warmup_trades=100, min_per_class=20)
    equity = float(initial_equity)
    active_orders: list[dict[str, Any]] = []
    shadow_orders: list[dict[str, Any]] = []
    last_results: dict[str, deque[int]] = {symbol: deque([-1, -1, -1], maxlen=3) for symbol in symbols}
    actual_trades: list[dict[str, Any]] = []
    shadow_trades: list[dict[str, Any]] = []
    closed_training_trades = 0
    training_candle_count = 0
    last_train_candle = 0
    totals = defaultdict(float)
    counts = defaultdict(int)
    model_decisions = defaultdict(int)
    model_freeze_train_count: int | None = None
    calibration_observations: dict[str, list[dict[str, Any]]] = {
        "VALIDATION": [],
        "OUT_OF_SAMPLE": [],
    }

    index_maps = {symbol: {stamp: i for i, stamp in enumerate(item["market"].index)} for symbol, item in market.items()}
    last_indices = {symbol: len(item["bars"]) - 1 for symbol, item in market.items()}

    for stamp in times:
        asof = stamp + pd.Timedelta(minutes=15)
        asof_ms = int(asof.timestamp() * 1000)
        phase = _phase(asof_ms, train_end_ms, validation_end_ms)

        # Advance every submitted virtual order only with data available at
        # this event time. Replaying a trade from its entry is deterministic,
        # but its input is strictly truncated at the latest closed candle.
        completed: list[tuple[dict[str, Any], Any]] = []
        for collection, is_shadow in ((active_orders, False), (shadow_orders, True)):
            survivors: list[dict[str, Any]] = []
            for order in collection:
                symbol = str(order["symbol"])
                if order["phase"] != phase and not order.get("phase_boundary_crossed"):
                    order["phase_boundary_crossed"] = True
                    counts["cross_boundary_positions"] += 1
                row_index = index_maps[symbol].get(stamp)
                if row_index is None or row_index <= int(order["start_index"]):
                    survivors.append(order)
                    continue
                item = market[symbol]
                observed_bars = _visible_bars(item["bars"][:row_index + 1], asof_ms)
                try:
                    result = exchange.simulate_post_only_trade(
                        bars=observed_bars,
                        start_index=int(order["start_index"]),
                        direction=str(order["direction"]),
                        limit_price=order["entry"],
                        stop_price=order["stop_loss"],
                        target_price=order["take_profit"],
                        equity_usdt=float(order["equity_snapshot"]),
                        risk_fraction=0.0025,
                        rules=item["rules"],
                        funding_events=item["funding"],
                        spread_fraction=spread_scenario,
                        funding_blackouts=OBSERVED_FUNDING_BLACKOUTS,
                    )
                except InsufficientEquityForRisk:
                    if order.get("entry_checked"):
                        counts["active_position_simulation_errors"] += 1
                        order["last_error"] = "INSUFFICIENT_EQUITY_FOR_RISK_AFTER_ENTRY"
                        survivors.append(order)
                    else:
                        counts["min_qty_rejections"] += 1
                        if not is_shadow:
                            counts["order_rejections"] += 1
                    continue
                except SimulationError as exc:
                    if order.get("entry_checked"):
                        counts["active_position_simulation_errors"] += 1
                        order["last_error"] = str(exc)
                        survivors.append(order)
                    else:
                        counts["simulation_data_rejections"] += 1
                        if not is_shadow:
                            counts["order_rejections"] += 1
                    continue

                if result.status == "UNFILLED":
                    if order.get("entry_checked"):
                        counts["active_position_state_errors"] += 1
                        order["last_error"] = "FILLED_ORDER_REPLAYED_AS_UNFILLED"
                        survivors.append(order)
                    elif not is_shadow:
                        counts["unfilled_limits"] += 1
                    continue
                if result.status == "REJECTED":
                    if order.get("entry_checked"):
                        counts["active_position_state_errors"] += 1
                        order["last_error"] = "ACTIVE_ORDER_REPLAYED_AS_REJECTED"
                        survivors.append(order)
                    elif not is_shadow:
                        counts["order_rejections"] += 1
                    continue

                if not order.get("entry_checked"):
                    order["entry_checked"] = True
                    order["entry_ts_ms"] = int(result.diagnostics["entry_ts_ms"])
                    if not is_shadow:
                        counts["submitted_virtual_entries"] += 1

                if result.status == "CLOSED":
                    completed.append((order, result))
                    continue
                if result.status == "OPEN":
                    order["last_status"] = result.exit_reason
                    order["last_observed_through_ts_ms"] = int(
                        result.diagnostics.get("observed_through_ts_ms", asof_ms)
                    )
                    survivors.append(order)
                    continue

                # Unknown simulator states remain reserved and cannot be
                # silently counted as a close or as a successful outcome.
                counts["unknown_simulator_states"] += 1
                order["last_error"] = f"UNKNOWN_SIMULATOR_STATUS:{result.status}"
                order["last_status"] = result.exit_reason or "UNKNOWN"
                observed = result.diagnostics.get("observed_through_ts_ms")
                if observed is not None:
                    order["last_observed_through_ts_ms"] = int(observed)
                if result.exit_reason == "FUNDING_DATA_GAP_BLACKOUT":
                    order["entry_checked"] = True
                    if result.diagnostics.get("entry_ts_ms") is not None:
                        order["entry_ts_ms"] = int(result.diagnostics["entry_ts_ms"])
                    if not order.get("blackout_unknown_counted"):
                        counts["funding_blackout_unresolved_positions"] += 1
                        order["blackout_unknown_counted"] = True
                survivors.append(order)

            collection[:] = survivors

        for order, result in sorted(
            completed,
            key=lambda pair: (
                int(pair[1].diagnostics.get("exit_ts_ms", asof_ms)),
                str(pair[0]["symbol"]),
                str(pair[0]["signal_id"]),
            ),
        ):
            exit_ts_ms = int(result.diagnostics["exit_ts_ms"])
            if order["shadow"]:
                shadow_trades.append({
                    "signal_id": order["signal_id"],
                    "instrument_id": order["symbol"],
                    "direction": order["direction"],
                    "phase": order["phase"],
                    "phase_boundary_crossed": bool(order.get("phase_boundary_crossed")),
                    "model_decision": order["model_decision"],
                    "model_probability": order.get("model_probability"),
                    "features": dict(order["features"]),
                    "entry_ts_ms": int(result.diagnostics["entry_ts_ms"]),
                    "exit_ts_ms": exit_ts_ms,
                    "exit_reason": result.exit_reason,
                    "quantity_contracts": result.quantity_contracts,
                    "bars_held": result.bars_held,
                    "tp1_filled": result.tp1_filled,
                    "net_rr_at_entry": result.net_rr_at_entry,
                    "gross_pnl_before_execution_costs_usdt": float(
                        result.diagnostics.get("gross_realized_pnl_usdt", 0.0)
                    ),
                    "net_pnl_usdt": result.net_pnl_usdt,
                    "fees_usdt": result.fees_usdt,
                    "funding_usdt": result.funding_usdt,
                    "spread_cost_usdt": float(result.diagnostics["spread_cost_usdt"]),
                    "slippage_cost_usdt": float(result.diagnostics["slippage_cost_usdt"]),
                    "veto_correct": result.net_pnl_usdt <= 0,
                    "source": "COUNTERFACTUAL_SHADOW",
                })
                if (
                    order["phase"] in calibration_observations
                    and order.get("model_probability") is not None
                    and not order.get("phase_boundary_crossed")
                ):
                    calibration_observations[order["phase"]].append({
                        "probability": float(order["model_probability"]),
                        "label": 1 if result.net_pnl_usdt > 0 else 0,
                    })
                if order.get("phase_boundary_crossed"):
                    counts["cross_boundary_closed_trades"] += 1
                if result.net_pnl_usdt <= 0:
                    totals["estimated_losses_avoided_usdt"] += abs(result.net_pnl_usdt)
                    counts["shadow_correct_vetoes"] += 1
                else:
                    counts["shadow_missed_winners"] += 1
                continue

            spread_cost = result.diagnostics.get("spread_cost_usdt")
            slippage_cost = result.diagnostics.get("slippage_cost_usdt")
            if spread_cost is None or slippage_cost is None:
                raise SandboxDataError(
                    f"closed virtual trade {order['signal_id']} lacks explicit spread/slippage reconciliation"
                )
            spread_cost = float(spread_cost)
            slippage_cost = float(slippage_cost)
            if not np.isfinite(spread_cost) or spread_cost < 0 or not np.isfinite(slippage_cost) or slippage_cost < 0:
                raise SandboxDataError(
                    f"closed virtual trade {order['signal_id']} has invalid spread/slippage cost values"
                )
            equity += result.net_pnl_usdt
            totals["fees_usdt"] += result.fees_usdt
            totals["funding_usdt"] += result.funding_usdt
            totals["spread_cost_usdt"] += spread_cost
            totals["slippage_cost_usdt"] += slippage_cost
            totals["gross_pnl_usdt"] += float(result.diagnostics.get("gross_realized_pnl_usdt", 0.0))
            actual_trades.append({
                "signal_id": order["signal_id"],
                "instrument_id": order["symbol"],
                "direction": order["direction"],
                "phase": order["phase"],
                "phase_boundary_crossed": bool(order.get("phase_boundary_crossed")),
                "model_probability": order.get("model_probability"),
                "entry_ts_ms": int(result.diagnostics["entry_ts_ms"]),
                "exit_ts_ms": exit_ts_ms,
                "exit_reason": result.exit_reason,
                "net_pnl_usdt": result.net_pnl_usdt,
                "risk_budget_usdt": float(result.diagnostics.get("risk_budget_usdt", 0.0)),
                "all_in_stop_risk_usdt": float(result.diagnostics.get("all_in_stop_risk_usdt", 0.0)),
                "net_r_multiple": (
                    float(result.net_pnl_usdt) / float(result.diagnostics["all_in_stop_risk_usdt"])
                    if result.diagnostics.get("all_in_stop_risk_usdt") else None
                ),
                "fees_usdt": result.fees_usdt,
                "funding_usdt": result.funding_usdt,
                "spread_cost_usdt": spread_cost,
                "slippage_cost_usdt": slippage_cost,
                "tp1_filled": result.tp1_filled,
                "net_rr_at_entry": result.net_rr_at_entry,
                "model_decision": order["model_decision"],
            })
            if (
                order["phase"] in calibration_observations
                and order.get("model_probability") is not None
                and not order.get("phase_boundary_crossed")
            ):
                calibration_observations[order["phase"]].append({
                    "probability": float(order["model_probability"]),
                    "label": 1 if result.net_pnl_usdt > 0 else 0,
                })
            if order.get("phase_boundary_crossed"):
                counts["cross_boundary_closed_trades"] += 1
            last_results[order["symbol"]].append(1 if result.net_pnl_usdt > 0 else 0)
            if result.net_pnl_usdt > 0:
                counts["wins"] += 1
            else:
                counts["losses"] += 1
            if order["phase"] == "TRAIN" and exit_ts_ms <= train_end_ms:
                brain.add_finalized_outcome(
                    signal_id=order["signal_id"],
                    instrument_id=order["symbol"],
                    candle_time_utc=datetime.fromtimestamp(
                        int(result.diagnostics["entry_ts_ms"]) / 1000, timezone.utc
                    ).isoformat(),
                    features=order["features"],
                    net_pnl_usdt=result.net_pnl_usdt,
                    fees_complete=True,
                    funding_complete=True,
                    source="EXECUTED_TRAINING_TRADE",
                )
                closed_training_trades += 1

        # Only matured TRAIN labels can update weights. Retry readiness checks
        # during validation if a training trade crossed the calendar boundary.
        if phase != "TRAIN" and not brain.trained and closed_training_trades >= 100 and brain.examples:
            terminal_train_status = brain.train()
            if terminal_train_status.get("status") == "TRAINED":
                counts["training_rounds"] += 1

        if phase == "TRAIN":
            training_candle_count += 1
            if closed_training_trades >= 100 and training_candle_count - last_train_candle >= 50:
                train_status = brain.train()
                if train_status.get("status") == "TRAINED":
                    counts["training_rounds"] += 1
                last_train_candle = training_candle_count
        else:
            if model_freeze_train_count is None:
                model_freeze_train_count = brain.train_count
            elif brain.train_count != model_freeze_train_count:
                raise SandboxDataError("model weights changed after the TRAIN phase freeze")

        for symbol in symbols:
            item = market[symbol]
            row_index = index_maps[symbol].get(stamp)
            if row_index is None or row_index < 0:
                continue
            phase_boundary_ms = (
                train_end_ms if phase == "TRAIN"
                else validation_end_ms if phase == "VALIDATION"
                else None
            )
            if _funding_blackout_for_timestamp(asof_ms) is not None:
                counts["funding_blackout_new_entry_blocks"] += 1
                continue
            if phase_boundary_ms is not None and _within_purge_window(
                asof_ms, phase_boundary_ms, max_holding_bars=12
            ):
                counts["purged_boundary_signal_blocks"] += 1
                continue
            counts["strategy_checks"] += 1
            latest_interval = next((
                int(event["fundingIntervalMs"])
                for event in reversed(item["funding"])
                if int(event["fundingTime"]) <= asof_ms
            ), int(item["funding_interval_median_ms"]))
            funding_max_age_ms = int(latest_interval * 1.5)
            funding_event = _known_funding_event(
                item["funding"],
                asof_ms,
                max_age_ms=funding_max_age_ms,
            )
            funding_rate = float(funding_event["fundingRate"]) if funding_event is not None else None
            funding_kind = str(funding_event["fundingKind"]) if funding_event is not None else None
            funding_event_time_ms = int(funding_event["fundingTime"]) if funding_event is not None else None
            if funding_event is None:
                counts["missing_funding_rate_signals"] += 1
            try:
                signal = analyze_signal(
                    instrument_id=symbol,
                    frame_1h=_visible_frame(item["frames"]["1H"], asof, pd.Timedelta(hours=1)),
                    frame_4h=_visible_frame(item["frames"]["4H"], asof, pd.Timedelta(hours=4)),
                    frame_15m=_visible_frame(item["frames"]["15m"], asof, pd.Timedelta(minutes=15)),
                    btc_15m=_visible_frame(
                        market["BTC-USDT"]["frames"]["15m"], asof, pd.Timedelta(minutes=15)
                    ),
                    asof=asof,
                    funding_rate=funding_rate,
                    funding_kind=funding_kind,
                    funding_event_time_ms=funding_event_time_ms,
                    funding_max_age_ms=funding_max_age_ms,
                    spread_fraction=spread_scenario,
                    last_three_results=tuple(last_results[symbol]),
                    tp_r=2.4,
                )
            except (ValueError, KeyError, IndexError):
                counts["strategy_data_errors"] += 1
                continue
            if signal.get("signal") not in {"LONG", "SHORT"}:
                counts["no_trade_decisions"] += 1
                continue
            counts["qualified_signals"] += 1
            features = dict(signal["features"])
            decision = brain.evaluate(features)
            model_probability: float | None = None
            if phase == "TRAIN":
                model_decision, veto = "TRAINING_BASELINE", False
            elif brain.trained and decision.get("status") == "READY":
                model_decision = str(decision["decision"])
                veto = model_decision == "VETO"
                model_probability = float(decision["probability"])
            else:
                model_decision, veto = "MODEL_NOT_READY_BASELINE", False
                counts["model_not_ready_signals"] += 1
            model_decisions[model_decision] += 1

            # Actual positions and pending actual entry limits share the strict
            # two-slot portfolio cap. Counterfactual Shadow Book orders are
            # tracked separately and never reserve live-simulation capacity.
            if veto:
                if any(x["symbol"] == symbol for x in shadow_orders):
                    counts["shadow_duplicate_blocks"] += 1
                    continue
            else:
                if any(x["symbol"] == symbol for x in active_orders) or len(active_orders) >= 2:
                    counts["portfolio_or_duplicate_blocks"] += 1
                    continue

            order = {
                "symbol": symbol,
                "signal_id": signal["signal_id"],
                "direction": signal["signal"],
                "entry": signal["entry"],
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "start_index": row_index,
                "equity_snapshot": equity,
                "features": features,
                "phase": phase,
                "model_decision": model_decision,
                "model_probability": model_probability,
                "shadow": bool(veto),
                "entry_checked": False,
                "submitted_ts_ms": asof_ms,
            }
            if veto:
                counts["vetoed_signals"] += 1
                shadow_orders.append(order)
            else:
                active_orders.append(order)

    cross_boundary_open_positions = sum(
        1 for item in active_orders if item.get("phase_boundary_crossed")
    )
    cross_boundary_open_shadow_orders = sum(
        1 for item in shadow_orders if item.get("phase_boundary_crossed")
    )
    counts["cross_boundary_open_positions"] = cross_boundary_open_positions
    counts["cross_boundary_open_shadow_orders"] = cross_boundary_open_shadow_orders
    counts["unresolved_at_dataset_end"] = sum(1 for item in active_orders if item.get("entry_checked"))
    counts["pending_entries_at_dataset_end"] = sum(1 for item in active_orders if not item.get("entry_checked"))
    counts["shadow_unresolved_at_dataset_end"] = len(shadow_orders)
    unresolved_positions = [
        {
            "signal_id": item["signal_id"],
            "instrument_id": item["symbol"],
            "status": item.get("last_status", "ENTRY_PENDING"),
            "phase": item["phase"],
            "phase_boundary_crossed": bool(item.get("phase_boundary_crossed")),
            "last_observed_through_ts_ms": item.get("last_observed_through_ts_ms"),
            "last_error": item.get("last_error"),
        }
        for item in active_orders
    ]
    unresolved_shadow_positions = [
        {
            "signal_id": item["signal_id"],
            "instrument_id": item["symbol"],
            "direction": item["direction"],
            "phase": item["phase"],
            "status": item.get("last_status", "ENTRY_PENDING"),
            "phase_boundary_crossed": bool(item.get("phase_boundary_crossed")),
            "model_probability": item.get("model_probability"),
            "last_observed_through_ts_ms": item.get("last_observed_through_ts_ms"),
            "last_error": item.get("last_error"),
        }
        for item in shadow_orders
    ]
    if brain.trained:
        brain.save_json(brain_path)

    wins, losses = int(counts["wins"]), int(counts["losses"])
    total_closed = wins + losses
    gross_positive = sum(
        max(
            0.0,
            float(trade["net_pnl_usdt"])
            + float(trade["fees_usdt"])
            + float(trade["funding_usdt"])
            + float(trade["spread_cost_usdt"])
            + float(trade["slippage_cost_usdt"]),
        )
        for trade in actual_trades
    )
    fee_ratio = totals["fees_usdt"] / gross_positive if gross_positive > 0 else None
    attempts = int(counts["qualified_signals"])
    report = {
        "report_version": "CHALLENGE1_AI_SANDBOX_V1",
        "environment": "OFFLINE_VIRTUAL_BLOFIN_DEMO_MODEL",
        "network_order_writes": False,
        "data_source": "LOCAL_SQLITE_REPLAY_DOWNLOADED_FROM_BLOFIN_DEMO_PUBLIC_API",
        "spread_assumption": {"fraction": spread_scenario, "quality": "MODELLED_NOT_HISTORICAL_ORDERBOOK"},
        "periods": {
            "start_utc": min(times).isoformat(), "end_utc": max(times).isoformat(),
            "training_end_utc": train_end.isoformat(), "validation_end_utc": validation_end.isoformat(),
            "phases": "first four months TRAIN; fifth month VALIDATION; sixth month OUT_OF_SAMPLE; model frozen after TRAIN",
        },
        "equity": {"initial_usdt": initial_equity, "final_realized_usdt": equity, "net_pnl_usdt": equity - initial_equity},
        "trade_metrics": {
            "closed_trades": total_closed, "wins": wins, "losses": losses,
            "funding_blackout_new_entry_blocks": counts["funding_blackout_new_entry_blocks"],
            "funding_blackout_unresolved_positions": counts["funding_blackout_unresolved_positions"],
            "net_win_rate_pct": (wins / total_closed * 100.0) if total_closed else None,
            "gross_pnl_before_costs_usdt": totals["gross_pnl_usdt"],
            "fees_usdt": totals["fees_usdt"], "funding_usdt": totals["funding_usdt"],
            "spread_cost_usdt": totals["spread_cost_usdt"],
            "slippage_cost_usdt": totals["slippage_cost_usdt"],
            "total_explicit_costs_usdt": (
                totals["fees_usdt"] + totals["funding_usdt"]
                + totals["spread_cost_usdt"] + totals["slippage_cost_usdt"]
            ),
            "fee_to_gross_positive_profit_ratio": fee_ratio,
            "order_rejection_rate_pct": (counts["order_rejections"] / attempts * 100.0) if attempts else None,
            "unfilled_limit_rate_pct": (counts["unfilled_limits"] / (counts["unfilled_limits"] + counts["submitted_virtual_entries"]) * 100.0)
                if (counts["unfilled_limits"] + counts["submitted_virtual_entries"]) else None,
            "max_consecutive_losses": _max_consecutive_losses(actual_trades),
            "state_sync_mismatches": None,
            "state_sync_note": "Not applicable: offline simulator has no exchange state to reconcile.",
            "unresolved_at_dataset_end": counts["unresolved_at_dataset_end"],
            "pending_entries_at_dataset_end": counts["pending_entries_at_dataset_end"],
            "shadow_unresolved_at_dataset_end": counts["shadow_unresolved_at_dataset_end"],
        },
        "quantitative_metrics": _grouped_performance_summary(actual_trades, initial_equity, symbols),
        "shadow_book_metrics": _shadow_book_summary(shadow_trades),
        "funding_data_quality": {
            "explicit_funding_kind_events": sum(
                1 for item in market.values() for event in item["funding"]
                if event.get("fundingKindSource") == "EXPLICIT_API"
            ),
            "schedule_inferred_events": sum(
                1 for item in market.values() for event in item["funding"]
                if event.get("fundingKindSource") == "SCHEDULE_INFERRED_DYNAMIC_INTERVAL"
            ),
            "schedule_inference_is_not_explicit_exchange_kind": True,
            "supported_observed_intervals_hours": list(FUNDING_INTERVAL_HOURS),
            "blackout_windows": [dict(window) for window in OBSERVED_FUNDING_BLACKOUTS],
            "new_entries_blocked_inside_blackout": True,
            "positions_overlapping_blackout_keep_unknown_net_pnl": True,
            "classification_note": (
                "Absent Kind is inferred from observed 1/2/4/8-hour cadence within 10 seconds; "
                "inferred labels remain distinct from explicit API kind."
            ),
        },
        "ai_audit": {
            "model_version": "csr-online-meta-sgd-v1", "trained": brain.trained,
            "model_sha256": brain.model_sha256(),
            "max_examples": brain.max_examples,
            "training_closed_trade_count": closed_training_trades,
            "training_examples": len(brain.examples), "training_rounds": counts["training_rounds"],
            "decisions": dict(model_decisions),
            "vetoed_signals": counts["vetoed_signals"],
            "shadow_correct_vetoes": counts["shadow_correct_vetoes"],
            "shadow_missed_winners": counts["shadow_missed_winners"],
            "estimated_losses_avoided_usdt": totals["estimated_losses_avoided_usdt"],
            "saved_capital_is_counterfactual_estimate": True,
            "feature_coefficients_not_causal_importance": brain.feature_importance(),
            "feature_importance_interpretation": "Associational model coefficients only; not causal attribution.",
        },
        "diagnostics": dict(counts),
        "trade_ledger": sorted(actual_trades, key=lambda x: x["exit_ts_ms"]),
        "shadow_ledger": sorted(shadow_trades, key=lambda x: x["exit_ts_ms"]),
        "open_positions_at_dataset_end": unresolved_positions,
        "open_shadow_positions_at_dataset_end": unresolved_shadow_positions,
        "walk_forward_audit": {
            "train_start_utc": start_time.isoformat(),
            "train_end_utc": train_end.isoformat(),
            "validation_end_utc": validation_end.isoformat(),
            "oos_end_utc": oos_end.isoformat(),
            "purge_window_minutes": 195,
            "purged_boundary_signal_blocks": counts["purged_boundary_signal_blocks"],
            "cross_boundary_positions": counts["cross_boundary_positions"],
            "cross_boundary_closed_trades": counts["cross_boundary_closed_trades"],
            "cross_boundary_open_positions": cross_boundary_open_positions,
            "cross_boundary_open_shadow_orders": cross_boundary_open_shadow_orders,
            "status": (
                "PASS"
                if counts["cross_boundary_positions"] == 0
                and counts["unresolved_at_dataset_end"] == 0
                and counts["pending_entries_at_dataset_end"] == 0
                and counts["shadow_unresolved_at_dataset_end"] == 0
                else "BLOCKED"
            ),
            "model_frozen_after_training": True,
            "model_train_count_at_freeze": model_freeze_train_count,
        },
        "model_calibration": {
            "VALIDATION": _calibration_report(calibration_observations["VALIDATION"]),
            "OUT_OF_SAMPLE": _calibration_report(calibration_observations["OUT_OF_SAMPLE"]),
        },
        "lookahead_audit": {
            "policy": "BAR_BY_BAR_ASOF_CUTOFF",
            "future_candles_passed_to_strategy": False,
            "future_candles_passed_to_virtual_position": False,
            "position_exit_time_pre_reserved": False,
            "training_labels_used_only_after_exit_time": True,
            "all_signal_decisions_use_closed_candles": True,
            "funding_gate_input": "LATEST_PAST_SETTLEMENT_RATE_PROXY",
        },
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline AI-in-the-loop BloFin candle replay; no exchange orders")
    parser.add_argument("--db", default="data/blofin_replay.sqlite3")
    parser.add_argument("--equity", type=float, default=1000.0)
    parser.add_argument("--spread", type=float, default=0.0002, help="MODELED scenario spread fraction, not historical L2")
    parser.add_argument("--output", default="reports/ai_sandbox_report.json")
    parser.add_argument("--brain", default="reports/trained_brain.json")
    args = parser.parse_args()
    report = run_sandbox(db_path=args.db, initial_equity=args.equity,
                         spread_scenario=args.spread, output_path=args.output, brain_path=args.brain)
    print(json.dumps({
        "equity": report["equity"],
        "trade_metrics": report["trade_metrics"],
        "ai_audit": report["ai_audit"],
        "diagnostics": report["diagnostics"],
        "report_path": args.output,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
