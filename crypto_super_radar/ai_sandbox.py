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
from typing import Any

import numpy as np
import pandas as pd

from .adaptive_brain import OnlineMetaBrain
from .challenge1_v2 import analyze_signal, atr
from .virtual_blofin import CostModel, InstrumentRules, InsufficientEquityForRisk, SimulationError, VirtualBloFin

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
    market["atr_15m"] = atr(market, 14)
    market = market.dropna(subset=["atr_15m"])
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
        "SELECT funding_time,funding_rate,funding_kind FROM replay_funding WHERE instrument_id=? ORDER BY funding_time ASC",
        (symbol,),
    ).fetchall()
    funding = [
        {"fundingTime": int(ts), "fundingRate": float(rate), "fundingKind": str(kind)}
        for ts, rate, kind in funding_rows
    ]
    instrument_row = connection.execute(
        "SELECT raw_json FROM replay_instruments WHERE instrument_id=?", (symbol,)
    ).fetchone()
    if not instrument_row:
        raise SandboxDataError(f"instrument contract metadata missing for {symbol}; re-run market downloader")
    rules = InstrumentRules.from_exchange(json.loads(instrument_row[0]))
    frames["15m"] = market[["open", "high", "low", "close", "volume"]].copy()
    return {"frames": frames, "market": market, "bars": bars, "funding": funding, "rules": rules}


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



def _known_funding_rate(events: list[dict[str, Any]], asof_ms: int) -> float | None:
    eligible = [item for item in events if int(item["fundingTime"]) <= asof_ms]
    if not eligible:
        return None
    return float(eligible[-1]["fundingRate"])


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
    times = sorted(set().union(*(set(item["market"].index) for item in market.values())))
    if len(times) < 100:
        raise SandboxDataError("replay dataset is too small")
    start_time = min(times)
    train_end = start_time + pd.DateOffset(months=4)
    validation_end = start_time + pd.DateOffset(months=5)
    train_end_ms = int(train_end.timestamp() * 1000)
    validation_end_ms = int(validation_end.timestamp() * 1000)

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
                    "exit_ts_ms": exit_ts_ms,
                    "net_pnl_usdt": result.net_pnl_usdt,
                    "veto_correct": result.net_pnl_usdt <= 0,
                    "source": "COUNTERFACTUAL_SHADOW",
                })
                if result.net_pnl_usdt <= 0:
                    totals["estimated_losses_avoided_usdt"] += abs(result.net_pnl_usdt)
                    counts["shadow_correct_vetoes"] += 1
                else:
                    counts["shadow_missed_winners"] += 1
                continue

            equity += result.net_pnl_usdt
            totals["fees_usdt"] += result.fees_usdt
            totals["funding_usdt"] += result.funding_usdt
            totals["gross_pnl_usdt"] += float(result.diagnostics.get("gross_realized_pnl_usdt", 0.0))
            actual_trades.append({
                "signal_id": order["signal_id"],
                "instrument_id": order["symbol"],
                "direction": order["direction"],
                "entry_ts_ms": int(result.diagnostics["entry_ts_ms"]),
                "exit_ts_ms": exit_ts_ms,
                "exit_reason": result.exit_reason,
                "net_pnl_usdt": result.net_pnl_usdt,
                "fees_usdt": result.fees_usdt,
                "funding_usdt": result.funding_usdt,
                "tp1_filled": result.tp1_filled,
                "net_rr_at_entry": result.net_rr_at_entry,
                "model_decision": order["model_decision"],
            })
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

        for symbol in symbols:
            item = market[symbol]
            row_index = index_maps[symbol].get(stamp)
            if row_index is None or row_index < 0:
                continue
            counts["strategy_checks"] += 1
            funding_rate = _known_funding_rate(item["funding"], asof_ms)
            if funding_rate is None:
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
            if phase == "TRAIN":
                model_decision, veto = "TRAINING_BASELINE", False
            elif brain.trained and decision.get("status") == "READY":
                model_decision = str(decision["decision"])
                veto = model_decision == "VETO"
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
                "shadow": bool(veto),
                "entry_checked": False,
                "submitted_ts_ms": asof_ms,
            }
            if veto:
                counts["vetoed_signals"] += 1
                shadow_orders.append(order)
            else:
                active_orders.append(order)

    counts["unresolved_at_dataset_end"] = sum(1 for item in active_orders if item.get("entry_checked"))
    counts["pending_entries_at_dataset_end"] = sum(1 for item in active_orders if not item.get("entry_checked"))
    counts["shadow_unresolved_at_dataset_end"] = len(shadow_orders)
    unresolved_positions = [
        {
            "signal_id": item["signal_id"],
            "instrument_id": item["symbol"],
            "status": item.get("last_status", "ENTRY_PENDING"),
            "last_observed_through_ts_ms": item.get("last_observed_through_ts_ms"),
        }
        for item in active_orders
    ]
    if brain.trained:
        brain.save_json(brain_path)

    wins, losses = int(counts["wins"]), int(counts["losses"])
    total_closed = wins + losses
    gross_positive = sum(
        max(0.0, float(trade["net_pnl_usdt"]) + float(trade["fees_usdt"]) + float(trade["funding_usdt"]))
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
            "net_win_rate_pct": (wins / total_closed * 100.0) if total_closed else None,
            "gross_pnl_before_costs_usdt": totals["gross_pnl_usdt"],
            "fees_usdt": totals["fees_usdt"], "funding_usdt": totals["funding_usdt"],
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
        "ai_audit": {
            "model_version": "csr-online-meta-sgd-v1", "trained": brain.trained,
            "training_closed_trade_count": closed_training_trades,
            "training_examples": len(brain.examples), "training_rounds": counts["training_rounds"],
            "decisions": dict(model_decisions),
            "vetoed_signals": counts["vetoed_signals"],
            "shadow_correct_vetoes": counts["shadow_correct_vetoes"],
            "shadow_missed_winners": counts["shadow_missed_winners"],
            "estimated_losses_avoided_usdt": totals["estimated_losses_avoided_usdt"],
            "saved_capital_is_counterfactual_estimate": True,
            "feature_coefficients_not_causal_importance": brain.feature_importance(),
        },
        "diagnostics": dict(counts),
        "trade_ledger": sorted(actual_trades, key=lambda x: x["exit_ts_ms"]),
        "shadow_ledger": sorted(shadow_trades, key=lambda x: x["exit_ts_ms"]),
        "open_positions_at_dataset_end": unresolved_positions,
        "lookahead_audit": {
            "policy": "BAR_BY_BAR_ASOF_CUTOFF",
            "future_candles_passed_to_strategy": False,
            "future_candles_passed_to_virtual_position": False,
            "position_exit_time_pre_reserved": False,
            "training_labels_used_only_after_exit_time": True,
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
