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

from .adaptive_brain import OnlineMetaBrain, ShadowPatternBrain
from .pattern_intelligence_v3 import PATTERN_FAMILIES, advance_pattern_lifecycle, analyze_unified_patterns_v3, lifecycle_outcomes, pattern_family_performance
from .challenge1_v2 import (
    analyze_signal, atr, prepare_signal_context, prepared_signal_history_ready,
)
from .virtual_blofin import CostModel, InstrumentRules, InsufficientEquityForRisk, SimulationError, VirtualBloFin
from .market_replay import (
    REGULAR_FUNDING_INTERVAL_MS, FUNDING_SCHEDULE_TOLERANCE_MS,
    OBSERVED_FUNDING_BLACKOUTS, OBSERVED_PRICE_CANDLE_BLACKOUTS,
    _classify_funding_schedule_events, _is_observed_blackout_gap,
    _is_price_candle_blackout, _is_regular_utc_funding_boundary,
)



def _price_candle_entry_block_status(
    timestamp_ms: int, blackout_window: Mapping[str, Any] | None,
    *, indicator_rewarm_complete: bool,
) -> str | None:
    """Return why a new simulated entry must be blocked during a price gap or rewarm."""
    if blackout_window is None:
        return None
    stamp = int(timestamp_ms)
    start = int(blackout_window["start_ms"])
    end = int(blackout_window["end_exclusive_ms"])
    if start <= stamp < end:
        return "PRICE_CANDLE_BLACKOUT"
    if stamp >= end and not indicator_rewarm_complete:
        return "PRICE_CANDLE_BLACKOUT_INDICATOR_REWARM"
    return None


def _ambiguous_price_candle_position(
    order: Mapping[str, Any], blackout_window: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Mark a pre-gap order Ambiguous, without inventing fill, exit, or PnL."""
    if blackout_window is None:
        return None
    symbol = str(order.get("symbol") or "").upper()
    submitted_ms = int(order.get("submitted_ts_ms") or 0)
    if symbol != str(blackout_window["instrument_id"]).upper():
        return None
    if submitted_ms >= int(blackout_window["start_ms"]):
        return None
    return {
        "signal_id": order.get("signal_id"),
        "instrument_id": symbol,
        "direction": order.get("direction"),
        "phase": order.get("phase"),
        "status": "AMBIGUOUS_PRICE_CANDLE_BLACKOUT",
        "position_state_before_gap": "FILLED_OR_OPEN" if order.get("entry_checked") else "ENTRY_PENDING",
        "entry_checked_before_gap": bool(order.get("entry_checked")),
        "entry_ts_ms": order.get("entry_ts_ms"),
        "submitted_ts_ms": submitted_ms,
        "last_observed_through_ts_ms": order.get("last_observed_through_ts_ms"),
        "blackout_start_ms": int(blackout_window["start_ms"]),
        "blackout_end_exclusive_ms": int(blackout_window["end_exclusive_ms"]),
        "exit_ts_ms": None,
        "exit_price": None,
        "net_pnl_usdt": None,
        "synthetic_candles_used": False,
        "reason": "Price path is absent; stop/target/fill outcome cannot be inferred.",
    }



def _pattern_trade_excursions(bars:list[dict[str,Any]],*,direction:str,entry_price:float,entry_ts_ms:int,exit_ts_ms:int)->dict[str,float|None]:
    """Closed Mark-candle MFE/MAE proxies over the observed holding interval."""
    if not np.isfinite(entry_price) or entry_price<=0: return {"mfe_pct":None,"mae_pct":None,"excursion_bars":0}
    visible=[r for r in bars if int(r.get("close_ts") or (int(r["ts"])+BAR_MS))<=exit_ts_ms and int(r.get("close_ts") or (int(r["ts"])+BAR_MS))>entry_ts_ms]
    hi=[float(r["mark_high"]) for r in visible if r.get("mark_high") is not None]; lo=[float(r["mark_low"]) for r in visible if r.get("mark_low") is not None]
    if not hi or not lo: return {"mfe_pct":None,"mae_pct":None,"excursion_bars":len(visible)}
    if direction=="LONG": mfe=max(0,max((v-entry_price)/entry_price for v in hi)); mae=max(0,max((entry_price-v)/entry_price for v in lo))
    else: mfe=max(0,max((entry_price-v)/entry_price for v in lo)); mae=max(0,max((v-entry_price)/entry_price for v in hi))
    return {"mfe_pct":round(mfe*100,6),"mae_pct":round(mae*100,6),"excursion_bars":len(visible)}

DEFAULT_WATCHLIST = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
BAR_MS = 15 * 60_000


class SandboxDataError(RuntimeError):
    pass


def _record_block_reason(
    global_counts: dict[str, int],
    by_symbol: dict[str, dict[str, int]],
    symbol: str,
    reason: Any,
) -> None:
    """Keep every skipped/rejected opportunity auditable without changing decisions."""
    label = str(reason or "UNSPECIFIED_BLOCK_REASON").strip()[:240]
    symbol_key = str(symbol).strip().upper()
    global_counts[label] = int(global_counts.get(label, 0)) + 1
    bucket = by_symbol.setdefault(symbol_key, {})
    bucket[label] = int(bucket.get(label, 0)) + 1


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
        elif source == "SCHEDULE_INFERRED_UTC_8H_BOUNDARY":
            try:
                interval_ms = int(item.get("fundingIntervalMs"))
                timestamp = int(item.get("fundingTime"))
            except (TypeError, ValueError, OverflowError):
                unsupported.append("INFERENCE_METADATA_MISSING")
                continue
            if not (
                item.get("fundingScheduleVerified") is True
                and interval_ms == REGULAR_FUNDING_INTERVAL_MS
                and _is_regular_utc_funding_boundary(timestamp)
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


def _strategy_history_ready(
    frame_1h: pd.DataFrame,
    frame_4h: pd.DataFrame,
    frame_15m: pd.DataFrame,
    btc_15m: pd.DataFrame,
) -> bool:
    """Mirror analyze_signal's minimum closed-history requirements before invoking it."""
    return (
        len(frame_1h) >= 205
        and len(frame_4h) >= 205
        and len(frame_15m) >= 70
        and len(btc_15m) >= 40
    )


def _datetime_index_to_epoch_ms(index: pd.DatetimeIndex) -> np.ndarray:
    """Return UTC Unix milliseconds independent of Pandas datetime resolution."""
    values = pd.DatetimeIndex(index)
    if values.tz is None:
        raise SandboxDataError("market candle index must be timezone-aware")
    try:
        # Pandas 3.x can retain microsecond/millisecond storage units. Normalize
        # to ns explicitly before interpreting the backing int64 values.
        epoch_ns = values.tz_convert("UTC").as_unit("ns").asi8
    except (TypeError, ValueError, OverflowError) as exc:
        raise SandboxDataError("market candle index cannot be normalized to UTC nanoseconds") from exc
    return np.asarray(epoch_ns // 1_000_000, dtype=np.int64)


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
    price_blackout_observed = False
    matched_price_blackouts: dict[tuple[str, int], dict[str, Any]] = {}
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
        bad_positions = np.flatnonzero((gaps != interval).to_numpy())
        if len(bad_positions):
            approved = False
            if symbol == "BTC-USDT" and label in {"15m", "1H", "4H", "MARK_15m", "ALIGNED_15m"} and len(bad_positions) == 1:
                position = int(bad_positions[0])
                left_ms = int(frame.index[position].timestamp() * 1000)
                right_ms = int(frame.index[position + 1].timestamp() * 1000)
                interval_ms = int(interval.total_seconds() * 1000)
                missing_count = int((right_ms - left_ms) // interval_ms - 1)
                first_missing_ms = left_ms + interval_ms
                last_missing_ms = right_ms - interval_ms
                observed_bar = "15m" if label in {"MARK_15m", "ALIGNED_15m"} else label
                observed_source = "MARK" if label == "MARK_15m" else "LAST"
                for window in OBSERVED_PRICE_CANDLE_BLACKOUTS:
                    if (
                        window["instrument_id"] == symbol
                        and window["bar"] == observed_bar
                        and window["source"] == observed_source
                        and int(window["start_ms"]) == first_missing_ms
                        and int(window["last_missing_ms"]) == last_missing_ms
                        and int(window["missing_intervals"]) == missing_count
                    ):
                        matched_price_blackouts[(symbol, first_missing_ms)] = dict(window)
                        approved = True
                        price_blackout_observed = True
                        break
            if not approved:
                first_bad = int(bad_positions[0])
                left = frame.index[first_bad]
                right = frame.index[first_bad + 1]
                raise SandboxDataError(
                    f"{symbol} {label} replay has unregistered missing candle intervals: "
                    f"left={left.isoformat()} right={right.isoformat()}"
                )
    market["atr_15m"] = atr(market, 14)
    if price_blackout_observed:
        approved_15m_windows = [
            window for window in matched_price_blackouts.values()
            if window["bar"] == "15m" and window["source"] == "LAST"
        ]
        if approved_15m_windows:
            recovery_start = pd.to_datetime(
                max(int(window["end_exclusive_ms"]) for window in approved_15m_windows),
                unit="ms", utc=True,
            )
            post_gap = market.loc[market.index >= recovery_start]
            market.loc[post_gap.index, "atr_15m"] = atr(post_gap, 14)
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
    timestamp_ms = _datetime_index_to_epoch_ms(market.index)
    market["close_ts"] = timestamp_ms + BAR_MS
    market["ts_ms"] = timestamp_ms
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
            "fundingScheduleVerified": str(source) == "SCHEDULE_INFERRED_UTC_8H_BOUNDARY",
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
        if abs(int(delta) - REGULAR_FUNDING_INTERVAL_MS) > 2 * FUNDING_SCHEDULE_TOLERANCE_MS:
            raise SandboxDataError(
                f"funding history skips or breaks the fixed 8-hour UTC cadence for {symbol}: {int(delta)} ms"
            )
        verified_intervals.append(REGULAR_FUNDING_INTERVAL_MS)
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
        "price_candle_blackouts": list(matched_price_blackouts.values()),
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


V3_PATTERN_BRAIN_SCORE_FLOOR = 0.45
V3_TOXIC_PATTERN_NAMES = {
    "symmetrical triangle",
    "tweezer bottom",
    "rising wedge",
}
V3_PRIORITY_PATTERN_DIRECTIONS = {
    "bullish engulfing": "BULLISH",
    "descending channel": "BEARISH",
    "three inside up": "BULLISH",
    "hanging man": "BEARISH",
}


def _v3_pattern_entry_gate(
    *,
    detections: list[Mapping[str, Any]],
    expected_direction: str,
    brain_evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    """Causal post-TRAIN filter; it never changes training labels or sizing."""
    side = str(expected_direction).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("expected_direction must be LONG or SHORT")
    wanted_pattern_direction = "BULLISH" if side == "LONG" else "BEARISH"
    active = [
        item for item in detections
        if str(item.get("lifecycle_status") or item.get("state") or "").upper()
        not in {"INVALIDATED", "EXPIRED"}
    ]
    toxic = sorted({
        str(item.get("name_en") or item.get("name") or "").strip()
        for item in active
        if str(item.get("name_en") or item.get("name") or "").strip().lower()
        in V3_TOXIC_PATTERN_NAMES
    })
    sweep = sorted({
        str(item.get("name_en") or item.get("name") or "Liquidity Sweep").strip()
        for item in active
        if str(item.get("family") or "").lower() == "liquidity_sweep"
        or "liquidity sweep" in str(item.get("name_en") or item.get("name") or "").lower()
    })
    score = brain_evaluation.get("score_0_1")
    try:
        score_value = float(score) if score is not None else None
    except (TypeError, ValueError, OverflowError):
        score_value = None
    if score_value is not None and not np.isfinite(score_value):
        score_value = None
    priority = sorted({
        name for item in active
        for name in [str(item.get("name_en") or item.get("name") or "").strip()]
        if name.lower() in V3_PRIORITY_PATTERN_DIRECTIONS
        and str(item.get("direction") or "").upper()
        == V3_PRIORITY_PATTERN_DIRECTIONS[name.lower()]
        == wanted_pattern_direction
    })
    priority_bonus = min(0.10, 0.05 * len(priority))
    adjusted_score = min(1.0, score_value + priority_bonus) if score_value is not None else None

    if toxic or sweep:
        reasons = []
        if toxic:
            reasons.append("TOXIC_PATTERN:" + ",".join(toxic))
        if sweep:
            reasons.append("LIQUIDITY_SWEEP_HIGH_FALSE_BREAK_RATE:" + ",".join(sweep))
        return {
            "decision": "VETO", "reason": "|".join(reasons),
            "toxic_patterns": toxic, "liquidity_sweeps": sweep,
            "priority_patterns": priority, "priority_bonus": priority_bonus,
            "brain_score": score_value, "adjusted_brain_score": adjusted_score,
            "score_floor": V3_PATTERN_BRAIN_SCORE_FLOOR,
        }
    if adjusted_score is not None and adjusted_score < V3_PATTERN_BRAIN_SCORE_FLOOR:
        return {
            "decision": "VETO", "reason": "V3_PATTERN_BRAIN_LOW_SCORE",
            "toxic_patterns": [], "liquidity_sweeps": [],
            "priority_patterns": priority, "priority_bonus": priority_bonus,
            "brain_score": score_value, "adjusted_brain_score": adjusted_score,
            "score_floor": V3_PATTERN_BRAIN_SCORE_FLOOR,
        }
    return {
        "decision": "ALLOW", "reason": "NO_V3_VETO",
        "toxic_patterns": [], "liquidity_sweeps": [],
        "priority_patterns": priority, "priority_bonus": priority_bonus,
        "brain_score": score_value, "adjusted_brain_score": adjusted_score,
        "score_floor": V3_PATTERN_BRAIN_SCORE_FLOOR,
    }


def _v3_pattern_shadow_observation(
    *,
    detections: list[Mapping[str, Any]],
    expected_direction: str,
    brain_evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    """Record what V3 would recommend without allowing it to affect entries."""
    suggestion = _v3_pattern_entry_gate(
        detections=detections,
        expected_direction=expected_direction,
        brain_evaluation=brain_evaluation,
    )
    return {
        **suggestion,
        "decision": "SHADOW_ONLY",
        "reason": "TELEMETRY_ONLY_NO_ENTRY_VETO",
        "suggested_decision": suggestion["decision"],
        "suggested_reason": suggestion["reason"],
        "would_veto": suggestion["decision"] == "VETO",
        "telemetry_only": True,
    }


def _v3_pattern_research_observation(
    *,
    detections: list[Mapping[str, Any]],
    expected_direction: str,
    brain_evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    """Explicit opt-in research mode; this result is used only in offline replay."""
    suggestion = _v3_pattern_entry_gate(
        detections=detections,
        expected_direction=expected_direction,
        brain_evaluation=brain_evaluation,
    )
    would_veto = suggestion["decision"] == "VETO"
    return {
        **suggestion,
        "decision": "RESEARCH_FILTER",
        "reason": "V3_RESEARCH_FILTER_VETO" if would_veto else "V3_RESEARCH_FILTER_ALLOW",
        "suggested_decision": suggestion["decision"],
        "suggested_reason": suggestion["reason"],
        "would_veto": would_veto,
        "telemetry_only": False,
        "research_only": True,
    }


def _actual_entry_veto(
    *,
    model_veto: bool,
    pattern_observation: Mapping[str, Any],
    v3_research_filter: bool = False,
) -> bool:
    """V3 vetoes are accepted only behind the explicit offline research switch."""
    if v3_research_filter:
        if pattern_observation.get("decision") != "RESEARCH_FILTER":
            raise ValueError("V3 research observation must use RESEARCH_FILTER mode")
        return bool(model_veto or pattern_observation.get("would_veto"))
    if pattern_observation.get("decision") != "SHADOW_ONLY":
        raise ValueError("V3 pattern observation must be SHADOW_ONLY")
    return bool(model_veto)


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
    max_holding_bars: int = 12,
    tp1_enabled: bool = True,
    tp1_reward_r_multiple: float = 1.2,
    tp1_fraction: float = 0.5,
    stop_atr_multiplier_overrides: Mapping[str, float] | None = None,
    v3_research_filter: bool = False,
) -> dict[str, Any]:
    stop_overrides: dict[str, float] = {}
    for raw_symbol, raw_multiplier in (stop_atr_multiplier_overrides or {}).items():
        symbol_key = str(raw_symbol or "").strip().upper()
        if symbol_key not in symbols:
            raise ValueError(f"stop ATR override symbol is not in selected watchlist: {symbol_key}")
        multiplier = float(raw_multiplier)
        if not np.isfinite(multiplier) or not 0.5 <= multiplier <= 4.0:
            raise ValueError(f"stop ATR override for {symbol_key} must be between 0.5 and 4.0")
        stop_overrides[symbol_key] = multiplier
    if not 0 < initial_equity or not np.isfinite(initial_equity):
        raise ValueError("initial_equity must be positive")
    if not 0 <= spread_scenario <= 0.0004:
        raise ValueError("spread_scenario must be between 0 and 0.04%")
    if not 1 <= max_holding_bars <= 96:
        raise ValueError("max_holding_bars must be 1..96")
    connection = sqlite3.connect(db_path)
    try:
        market = {symbol: _load_symbol(connection, symbol) for symbol in symbols}
    finally:
        connection.close()

    if "BTC-USDT" not in market:
        raise SandboxDataError("BTC-USDT is required as the master-guard market")

    # Validate once and build causal indicator/pivot caches outside the 15m loop.
    btc_frame_15m = market["BTC-USDT"]["frames"]["15m"]
    price_blackout_windows = list(market["BTC-USDT"].get("price_candle_blackouts") or [])
    price_blackout_window = (
        {
            "instrument_id": "BTC-USDT",
            "start_ms": min(int(window["start_ms"]) for window in price_blackout_windows),
            "end_exclusive_ms": max(int(window["end_exclusive_ms"]) for window in price_blackout_windows),
            "timeframe_windows": price_blackout_windows,
        }
        if price_blackout_windows else None
    )
    price_blackout_recovery_ts = (
        pd.to_datetime(int(price_blackout_window["end_exclusive_ms"]), unit="ms", utc=True)
        if price_blackout_window else None
    )
    signal_frames = {symbol: dict(item["frames"]) for symbol, item in market.items()}
    prepared_contexts = {
        symbol: prepare_signal_context(
            item["frames"]["1H"], item["frames"]["4H"],
            item["frames"]["15m"], btc_frame_15m,
        )
        for symbol, item in market.items()
    }
    price_blackout_context_rewarmed = price_blackout_window is None
    # Selected markets must cover the same six-calendar-month replay horizon.
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
    exchange = VirtualBloFin(
        costs=costs, max_holding_bars=max_holding_bars,
        tp1_enabled=tp1_enabled, tp1_reward_r_multiple=tp1_reward_r_multiple,
        tp1_fraction=tp1_fraction,
    )
    brain = OnlineMetaBrain(warmup_trades=100, min_per_class=20)
    pattern_brain = ShadowPatternBrain(warmup_trades=20, min_per_class=5)
    pattern_lifecycle: dict[str, dict[str, Any]] = {}
    closed_pattern_training_trades = 0
    last_pattern_brain_train_examples = 0
    pattern_brain_freeze_train_count: int | None = None
    pattern_brain_freeze_metadata: dict[str, Any] | None = None
    v3_capacity_releases: list[dict[str, Any]] = []
    equity = float(initial_equity)
    active_orders: list[dict[str, Any]] = []
    shadow_orders: list[dict[str, Any]] = []
    last_results: dict[str, deque[int]] = {symbol: deque([-1, -1, -1], maxlen=3) for symbol in symbols}
    actual_trades: list[dict[str, Any]] = []
    shadow_trades: list[dict[str, Any]] = []
    unknown_pnl_trades: list[dict[str, Any]] = []
    unknown_shadow_pnl_trades: list[dict[str, Any]] = []
    ambiguous_price_gap_positions: list[dict[str, Any]] = []
    ambiguous_shadow_price_gap_positions: list[dict[str, Any]] = []
    closed_training_trades = 0
    training_candle_count = 0
    last_train_candle = 0
    totals = defaultdict(float)
    counts = defaultdict(int)
    model_decisions = defaultdict(int)
    strategy_error_reasons: dict[str, int] = defaultdict(int)
    block_reasons: dict[str, int] = defaultdict(int)
    block_reasons_by_symbol: dict[str, dict[str, int]] = defaultdict(dict)
    model_freeze_train_count: int | None = None
    calibration_observations: dict[str, list[dict[str, Any]]] = {
        "VALIDATION": [],
        "OUT_OF_SAMPLE": [],
    }

    index_maps = {symbol: {stamp: i for i, stamp in enumerate(item["market"].index)} for symbol, item in market.items()}
    last_indices = {symbol: len(item["bars"]) - 1 for symbol, item in market.items()}

    next_progress_pct = 10
    print(f"[WALK_FORWARD_PROGRESS] 0% (0/{len(times)} timestamps)", flush=True)
    for time_index, stamp in enumerate(times, start=1):
        progress_pct = int(time_index * 100 / len(times))
        while progress_pct >= next_progress_pct:
            print(
                f"[WALK_FORWARD_PROGRESS] {next_progress_pct}% "
                f"({time_index}/{len(times)} timestamps)",
                flush=True,
            )
            next_progress_pct += 10
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
                ambiguous_record = _ambiguous_price_candle_position(order, price_blackout_window)
                if ambiguous_record is not None and int(stamp.timestamp() * 1000) >= int(price_blackout_window["end_exclusive_ms"]):
                    if is_shadow:
                        ambiguous_shadow_price_gap_positions.append(ambiguous_record)
                        counts["price_blackout_ambiguous_shadow_positions"] += 1
                    else:
                        ambiguous_price_gap_positions.append(ambiguous_record)
                        counts["price_blackout_ambiguous_positions"] += 1
                    continue
                if order["phase"] != phase and not order.get("phase_boundary_crossed"):
                    order["phase_boundary_crossed"] = True
                    counts["cross_boundary_positions"] += 1
                row_index = index_maps[symbol].get(stamp)
                if row_index is None or row_index <= int(order["start_index"]):
                    survivors.append(order)
                    continue
                item = market[symbol]
                advance_pattern_lifecycle(pattern_lifecycle, instrument_id=symbol, frames=item["frames"], asof=asof)
                # The simulator only reads the entry candle and subsequent
                # holding candles. Rebase that bounded slice to index zero;
                # every supplied candle is closed by the current as-of time.
                order_start_index = int(order["start_index"])
                observed_bars = item["bars"][order_start_index:row_index + 1]
                if (
                    not observed_bars
                    or int(observed_bars[-1].get("close_ts") or (
                        int(observed_bars[-1]["ts"]) + BAR_MS
                    )) > asof_ms
                ):
                    raise SandboxDataError(
                        f"virtual position received a candle beyond as-of for {symbol}"
                    )
                try:
                    result = exchange.simulate_post_only_trade(
                        bars=observed_bars,
                        start_index=0,
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
                            _record_block_reason(
                                block_reasons, block_reasons_by_symbol, symbol,
                                "INSUFFICIENT_EQUITY_FOR_ALL_IN_STOP_RISK",
                            )
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
                            _record_block_reason(
                                block_reasons, block_reasons_by_symbol, symbol,
                                f"SIMULATION_DATA_REJECTION: {str(exc)}",
                            )
                    continue

                if result.status == "UNFILLED":
                    if order.get("entry_checked"):
                        counts["active_position_state_errors"] += 1
                        order["last_error"] = "FILLED_ORDER_REPLAYED_AS_UNFILLED"
                        survivors.append(order)
                    elif not is_shadow:
                        counts["unfilled_limits"] += 1
                        _record_block_reason(
                            block_reasons, block_reasons_by_symbol, symbol,
                            f"POST_ONLY_LIMIT_UNFILLED: {result.exit_reason}",
                        )
                    continue
                if result.status == "REJECTED":
                    if order.get("entry_checked"):
                        counts["active_position_state_errors"] += 1
                        order["last_error"] = "ACTIVE_ORDER_REPLAYED_AS_REJECTED"
                        survivors.append(order)
                    elif not is_shadow:
                        counts["order_rejections"] += 1
                        if result.exit_reason == "FUNDING_DATA_GAP_BLACKOUT":
                            counts["funding_blackout_fill_entry_blocks"] += 1
                        _record_block_reason(
                            block_reasons, block_reasons_by_symbol, symbol,
                            f"SIMULATOR_REJECTED: {result.exit_reason}",
                        )
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

                # A price-confirmed close with an unobservable funding charge
                # releases capacity, but its net PnL remains UNKNOWN.
                if result.status == "UNKNOWN" and result.diagnostics.get("position_closed") is True:
                    record = {
                        "signal_id": order["signal_id"],
                        "instrument_id": order["symbol"],
                        "direction": order["direction"],
                        "phase": order["phase"],
                        "model_decision": order["model_decision"],
                        "entry_ts_ms": result.diagnostics.get("entry_ts_ms"),
                        "exit_ts_ms": result.diagnostics.get("exit_ts_ms"),
                        "market_exit_reason": result.diagnostics.get("market_exit_reason"),
                        "unknown_reason": result.diagnostics.get("pnl_unknown_reason"),
                        "net_pnl_usdt": None,
                        "fees_usdt": result.fees_usdt,
                        "known_funding_usdt": result.funding_usdt,
                        "funding_blackouts": result.diagnostics.get("funding_blackouts", []),
                        "pattern_features": dict(order.get("pattern_features") or {}),
                        "pattern_lifecycle_outcomes": lifecycle_outcomes(pattern_lifecycle, order.get("pattern_family_keys") or {}),
                    }
                    if is_shadow:
                        unknown_shadow_pnl_trades.append(record)
                        counts["funding_blackout_closed_shadow_pnl_unknown"] += 1
                    else:
                        unknown_pnl_trades.append(record)
                        counts["funding_blackout_closed_pnl_unknown"] += 1
                    continue

                # Unknown states without a confirmed price exit remain reserved.
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
                    "veto_source": order.get("veto_source"),
                    "veto_reason": order.get("veto_reason"),
                    "pattern_brain_score": (order.get("pattern_gate") or {}).get("brain_score"),
                    "pattern_brain_adjusted_score": (order.get("pattern_gate") or {}).get("adjusted_brain_score"),
                    "pattern_priority_bonus": (order.get("pattern_gate") or {}).get("priority_bonus", 0.0),
                    "priority_patterns": list((order.get("pattern_gate") or {}).get("priority_patterns") or []),
                    "pattern_gate_telemetry": dict(order.get("pattern_gate") or {}),
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
                "entry_price": float(result.filled_entry or order["entry"]),
                "stop_loss_price": float(order["stop_loss"]),
                "take_profit_price": float(order["take_profit"]),
                "atr_15m_at_signal": float(order["signal_atr_15m"]),
                "stop_distance_pct": abs(float(result.filled_entry or order["entry"]) - float(order["stop_loss"])) / float(result.filled_entry or order["entry"]) * 100.0,
                "stop_distance_atr": abs(float(result.filled_entry or order["entry"]) - float(order["stop_loss"])) / float(order["signal_atr_15m"]) if float(order["signal_atr_15m"]) > 0 else None,
                "exit_price": float(result.average_exit) if result.average_exit is not None else None,
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
                "tp1_fill_quantity": float(result.diagnostics.get("tp1_fill_quantity", 0.0)),
                "tp1_fill_price": result.diagnostics.get("tp1_fill_price"),
                "tp1_realized_gross_pnl_usdt": float(result.diagnostics.get("tp1_realized_gross_pnl_usdt", 0.0)),
                "tp1_fee_usdt": float(result.diagnostics.get("tp1_fee_usdt", 0.0)),
                "net_rr_at_entry": result.net_rr_at_entry,
                "model_decision": order["model_decision"],
                "capacity_replacement_for_signal_id": order.get("capacity_replacement_for_signal_id"),
                "capacity_released_by_v3_at_ms": order.get("capacity_released_by_v3_at_ms"),
                "pattern_features": dict(order.get("pattern_features") or {}),
                "pattern_detections": list(order.get("pattern_detections") or []),
                "market_regime": order.get("pattern_market_regime","UNKNOWN"),
                "pattern_brain_shadow_score": (order.get("pattern_brain_evaluation") or {}).get("score_0_1"),
                "pattern_gate_telemetry": dict(order.get("pattern_gate") or {}),
                "pattern_lifecycle_outcomes": lifecycle_outcomes(pattern_lifecycle, order.get("pattern_family_keys") or {}),
                **_pattern_trade_excursions(market[order["symbol"]]["bars"],direction=order["direction"],entry_price=float(result.filled_entry or order["entry"]),entry_ts_ms=int(result.diagnostics["entry_ts_ms"]),exit_ts_ms=exit_ts_ms),
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
                label=pattern_brain.add_finalized_outcome(signal_id=order["signal_id"],instrument_id=order["symbol"],
                    candle_time_utc=datetime.fromtimestamp(int(result.diagnostics["entry_ts_ms"])/1000,timezone.utc).isoformat(),
                    features=order.get("pattern_features") or {},net_pnl_usdt=result.net_pnl_usdt,fees_complete=True,funding_complete=True)
                if label.get("inserted"): closed_pattern_training_trades += 1

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
        if phase=="TRAIN" and len(pattern_brain.examples)>=pattern_brain.warmup_trades and len(pattern_brain.examples)-last_pattern_brain_train_examples>=5:
            train_state=pattern_brain.train()
            if train_state.get("status")=="TRAINED": counts["pattern_training_rounds"]+=1; last_pattern_brain_train_examples=len(pattern_brain.examples)
        if phase!="TRAIN":
            if pattern_brain_freeze_train_count is None:
                if not pattern_brain.trained and len(pattern_brain.examples)>=pattern_brain.warmup_trades:
                    train_state=pattern_brain.train()
                    if train_state.get("status")=="TRAINED": counts["pattern_training_rounds"]+=1
                pattern_brain_freeze_metadata = pattern_brain.freeze()
                pattern_brain_freeze_train_count=pattern_brain.train_count
            elif pattern_brain.train_count!=pattern_brain_freeze_train_count: raise SandboxDataError("V3 pattern brain changed after TRAIN freeze")
            if model_freeze_train_count is None: model_freeze_train_count=brain.train_count
            elif brain.train_count!=model_freeze_train_count: raise SandboxDataError("model weights changed after the TRAIN phase freeze")

        if price_blackout_window is not None and not price_blackout_context_rewarmed:
            recovery_ms = int(price_blackout_window["end_exclusive_ms"])
            if asof_ms >= recovery_ms:
                btc_post_gap = btc_frame_15m.loc[btc_frame_15m.index >= price_blackout_recovery_ts]
                btc_post_gap_closed = int(
                    ((btc_post_gap.index + pd.Timedelta(minutes=15)) <= asof).sum()
                )
                if btc_post_gap_closed >= 70:
                    refreshed_contexts: dict[str, dict[str, Any]] = {}
                    refreshed_signal_frames: dict[str, dict[str, pd.DataFrame]] = {}
                    btc_active_frames = dict(market["BTC-USDT"]["frames"])
                    for timeframe in ("15m", "1H", "4H"):
                        matching = [
                            window for window in price_blackout_windows
                            if window["bar"] == timeframe and window["source"] == "LAST"
                        ]
                        if matching:
                            timeframe_recovery = pd.to_datetime(
                                max(int(window["end_exclusive_ms"]) for window in matching),
                                unit="ms", utc=True,
                            )
                            btc_active_frames[timeframe] = btc_active_frames[timeframe].loc[
                                btc_active_frames[timeframe].index >= timeframe_recovery
                            ].copy()
                    btc_post_gap = btc_active_frames["15m"]
                    for refresh_symbol, refresh_item in market.items():
                        active_frames = (
                            dict(btc_active_frames) if refresh_symbol == "BTC-USDT"
                            else dict(refresh_item["frames"])
                        )
                        refreshed_signal_frames[refresh_symbol] = active_frames
                        refreshed_contexts[refresh_symbol] = prepare_signal_context(
                            active_frames["1H"], active_frames["4H"],
                            active_frames["15m"], btc_post_gap,
                        )
                    prepared_contexts = refreshed_contexts
                    signal_frames = refreshed_signal_frames
                    pattern_lifecycle = {
                        key: value for key, value in pattern_lifecycle.items()
                        if value.get("instrument_id") != "BTC-USDT"
                    }
                    price_blackout_context_rewarmed = True
                    counts["price_blackout_indicator_rewarm_completed"] += 1
                    counts["price_blackout_indicator_rewarm_btc_15m_closed_bars"] = btc_post_gap_closed

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
            price_entry_block = _price_candle_entry_block_status(
                asof_ms, price_blackout_window,
                indicator_rewarm_complete=price_blackout_context_rewarmed,
            )
            if price_entry_block is not None:
                counts["price_blackout_new_entry_blocks"] += 1
                _record_block_reason(
                    block_reasons, block_reasons_by_symbol, symbol,
                    f"{price_entry_block}: NEW_ENTRY_BLOCKED",
                )
                continue
            if _funding_blackout_for_timestamp(asof_ms) is not None:
                counts["funding_blackout_new_entry_blocks"] += 1
                _record_block_reason(
                    block_reasons, block_reasons_by_symbol, symbol,
                    "EXCHANGE_DATA_GAP_BLACKOUT: NEW_ENTRY_BLOCKED",
                )
                continue
            if phase_boundary_ms is not None and _within_purge_window(
                asof_ms, phase_boundary_ms, max_holding_bars=max_holding_bars
            ):
                counts["purged_boundary_signal_blocks"] += 1
                _record_block_reason(
                    block_reasons, block_reasons_by_symbol, symbol,
                    f"PURGED_WALK_FORWARD_{(max_holding_bars + 1) * 15}_MINUTE_BOUNDARY",
                )
                continue
            precomputed_context = prepared_contexts[symbol]
            if not prepared_signal_history_ready(precomputed_context, asof):
                counts["indicator_warmup_blocks"] += 1
                _record_block_reason(
                    block_reasons, block_reasons_by_symbol, symbol,
                    "INDICATOR_WARMUP_MINIMUM_CLOSED_HISTORY",
                )
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
                    frame_1h=signal_frames[symbol]["1H"],
                    frame_4h=signal_frames[symbol]["4H"],
                    frame_15m=signal_frames[symbol]["15m"],
                    btc_15m=(
                        signal_frames["BTC-USDT"]["15m"]
                        if price_blackout_context_rewarmed else btc_frame_15m
                    ),
                    asof=asof,
                    funding_rate=funding_rate,
                    funding_kind=funding_kind,
                    funding_event_time_ms=funding_event_time_ms,
                    funding_max_age_ms=funding_max_age_ms,
                    spread_fraction=spread_scenario,
                    last_three_results=tuple(last_results[symbol]),
                    tp_r=2.4,
                    stop_atr_multiplier_override=stop_overrides.get(symbol),
                    precomputed_context=precomputed_context,
                )
            except (ValueError, KeyError, IndexError) as exc:
                counts["strategy_data_errors"] += 1
                reason = f"{type(exc).__name__}: {str(exc)}"[:240]
                strategy_error_reasons[reason] += 1
                continue
            if signal.get("signal") not in {"LONG", "SHORT"}:
                counts["no_trade_decisions"] += 1
                _record_block_reason(
                    block_reasons, block_reasons_by_symbol, symbol,
                    f"STRATEGY_NO_TRADE: {signal.get('reason')}",
                )
                continue
            counts["qualified_signals"] += 1
            features=dict(signal["features"])
            try:
                snapshot=analyze_unified_patterns_v3(instrument_id=symbol,frames=signal_frames[symbol],asof=asof,expected_direction=signal["signal"],lifecycle_state=pattern_lifecycle)
                pattern_features=dict(snapshot["features"]); pattern_features.update(snapshot.get("pattern_name_features") or {})
                pattern_family_keys=dict(snapshot["family_lifecycle_keys"]); pattern_detections=list(snapshot["detections"]); pattern_market_regime=snapshot.get("market_regime","UNKNOWN")
            except (ValueError,KeyError,TypeError,IndexError):
                counts["pattern_analysis_errors"]+=1; pattern_features={x:0 for x in PATTERN_FAMILIES}; pattern_family_keys={x:[] for x in PATTERN_FAMILIES}; pattern_detections=[]; pattern_market_regime="UNKNOWN"
            pattern_brain_evaluation=pattern_brain.evaluate(pattern_features); counts["pattern_shadow_observations"]+=1
            decision=brain.evaluate(features)
            model_probability: float | None = None
            if phase == "TRAIN":
                model_decision, model_veto = "TRAINING_BASELINE", False
            elif brain.trained and decision.get("status") == "READY":
                model_decision = str(decision["decision"])
                model_veto = model_decision == "VETO"
                model_probability = float(decision["probability"])
            else:
                model_decision, model_veto = "MODEL_NOT_READY_BASELINE", False
                counts["model_not_ready_signals"] += 1
            model_decisions[model_decision] += 1

            # Learn only from matured TRAIN outcomes, then freeze before
            # Validation/OOS. The V3 score is uncalibrated, not a probability.
            if phase != "TRAIN" and pattern_brain.trained:
                if v3_research_filter:
                    pattern_gate = _v3_pattern_research_observation(
                        detections=pattern_detections,
                        expected_direction=signal["signal"],
                        brain_evaluation=pattern_brain_evaluation,
                    )
                else:
                    pattern_gate = _v3_pattern_shadow_observation(
                        detections=pattern_detections,
                        expected_direction=signal["signal"],
                        brain_evaluation=pattern_brain_evaluation,
                    )
            else:
                pattern_gate = {
                    "decision": "RESEARCH_FILTER" if v3_research_filter else "SHADOW_ONLY",
                    "reason": "V3_RESEARCH_FILTER_NOT_READY" if v3_research_filter else "TELEMETRY_ONLY_NO_ENTRY_VETO",
                    "suggested_decision": "NOT_EVALUATED",
                    "suggested_reason": "TRAINING_OR_PATTERN_BRAIN_NOT_READY",
                    "would_veto": False,
                    "telemetry_only": not v3_research_filter,
                    "research_only": bool(v3_research_filter),
                    "toxic_patterns": [], "liquidity_sweeps": [],
                    "priority_patterns": [], "priority_bonus": 0.0,
                    "brain_score": pattern_brain_evaluation.get("score_0_1"),
                    "adjusted_brain_score": pattern_brain_evaluation.get("score_0_1"),
                    "score_floor": V3_PATTERN_BRAIN_SCORE_FLOOR,
                }
            pattern_veto_suggestion = bool(pattern_gate.get("would_veto"))
            veto = _actual_entry_veto(
                model_veto=model_veto,
                pattern_observation=pattern_gate,
                v3_research_filter=v3_research_filter,
            )
            veto_sources = []
            if model_veto:
                veto_sources.append("V2_META_BRAIN")
            if v3_research_filter and pattern_veto_suggestion:
                veto_sources.append("V3_RESEARCH_FILTER")
            if pattern_veto_suggestion:
                counts["v3_pattern_veto_suggestions"] += 1
                if v3_research_filter:
                    counts["v3_research_filter_entry_vetoes"] += 1
                    if len(active_orders) < 2:
                        v3_capacity_releases.append({
                            "signal_id": signal["signal_id"],
                            "released_ts_ms": asof_ms,
                            "symbol": symbol,
                        })
                        counts["v3_research_filter_capacity_slots_released"] += 1
                if pattern_gate.get("toxic_patterns") or pattern_gate.get("liquidity_sweeps"):
                    counts["v3_toxic_pattern_veto_suggestions"] += 1
                elif pattern_gate.get("suggested_reason") == "V3_PATTERN_BRAIN_LOW_SCORE":
                    counts["v3_brain_low_score_suggestions"] += 1
            if pattern_gate.get("priority_patterns"):
                counts["v3_priority_supported_signals"] += 1
            veto_source = "+".join(veto_sources) if veto_sources else None

            # Actual positions and pending actual entry limits share the strict
            # two-slot portfolio cap. Counterfactual Shadow Book orders are
            # tracked separately and never reserve live-simulation capacity.
            if veto:
                if any(x["symbol"] == symbol for x in shadow_orders):
                    counts["shadow_duplicate_blocks"] += 1
                    _record_block_reason(
                        block_reasons, block_reasons_by_symbol, symbol,
                        "SHADOW_DUPLICATE_PENDING_OR_ACTIVE_SIGNAL",
                    )
                    continue
            else:
                if any(x["symbol"] == symbol for x in active_orders):
                    counts["portfolio_or_duplicate_blocks"] += 1
                    _record_block_reason(
                        block_reasons, block_reasons_by_symbol, symbol,
                        "DUPLICATE_SYMBOL_ALREADY_PENDING_OR_ACTIVE",
                    )
                    continue
                if len(active_orders) >= 2:
                    counts["portfolio_or_duplicate_blocks"] += 1
                    _record_block_reason(
                        block_reasons, block_reasons_by_symbol, symbol,
                        "MAX_TWO_CONCURRENT_SIMULATED_POSITIONS",
                    )
                    continue

            atr_row = item["market"].loc[stamp]
            signal_atr_15m = float(atr_row["atr_15m"])
            order = {
                "symbol": symbol,
                "signal_id": signal["signal_id"],
                "direction": signal["signal"],
                "entry": signal["entry"],
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "signal_atr_15m": signal_atr_15m,
                "start_index": row_index,
                "equity_snapshot": equity,
                "features": features,
                "phase": phase,
                "model_decision": model_decision,
                "model_probability": model_probability,
                "shadow": bool(veto),
                "entry_checked": False,
                "submitted_ts_ms": asof_ms,
                "pattern_features":pattern_features,"pattern_family_keys":pattern_family_keys,"pattern_detections":pattern_detections,
                "pattern_market_regime":pattern_market_regime,"pattern_brain_evaluation":pattern_brain_evaluation,"pattern_gate":pattern_gate,"pattern_snapshot_asof":asof.isoformat(),
                "veto_source":veto_source,
                "veto_reason":model_decision if model_veto else None,
                "pattern_veto_suggestion_reason":pattern_gate.get("suggested_reason") if pattern_veto_suggestion else None,
                "capacity_replacement_for_signal_id":None,
                "capacity_released_by_v3_at_ms":None,
            }
            if veto:
                counts["vetoed_signals"] += 1
                if v3_research_filter and pattern_veto_suggestion:
                    order["veto_reason"] = pattern_gate.get("suggested_reason")
                shadow_orders.append(order)
            else:
                if v3_research_filter and not model_veto and v3_capacity_releases:
                    released = v3_capacity_releases.pop(0)
                    order["capacity_replacement_for_signal_id"] = released["signal_id"]
                    order["capacity_released_by_v3_at_ms"] = released["released_ts_ms"]
                    counts["v3_replacement_entries"] += 1
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
        "experiment_configuration": {
            "symbols": list(symbols),
            "max_holding_bars": max_holding_bars,
            "purge_window_minutes": (max_holding_bars + 1) * 15,
            "tp1_enabled": bool(tp1_enabled),
            "tp1_reward_r_multiple": tp1_reward_r_multiple,
            "tp1_fraction": tp1_fraction,
            "stop_atr_multiplier_overrides": dict(stop_overrides),
            "v3_pattern_mode": "SHADOW_TELEMETRY_ONLY_NO_ENTRY_VETO",
        },
        "periods": {
            "start_utc": min(times).isoformat(), "end_utc": max(times).isoformat(),
            "training_end_utc": train_end.isoformat(), "validation_end_utc": validation_end.isoformat(),
            "phases": "first four months TRAIN; fifth month VALIDATION; sixth month OUT_OF_SAMPLE; model frozen after TRAIN",
        },
        "equity": {"initial_usdt": initial_equity, "final_realized_usdt": equity, "net_pnl_usdt": equity - initial_equity},
        "trade_metrics": {
            "closed_trades": total_closed, "wins": wins, "losses": losses,
            "funding_blackout_new_entry_blocks": counts["funding_blackout_new_entry_blocks"],
            "funding_blackout_fill_entry_blocks": counts["funding_blackout_fill_entry_blocks"],
            "funding_blackout_closed_pnl_unknown": counts["funding_blackout_closed_pnl_unknown"],
            "funding_blackout_closed_shadow_pnl_unknown": counts["funding_blackout_closed_shadow_pnl_unknown"],
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
        "pattern_intelligence_v3_shadow_calibration": {
            "mode": "SHADOW_TELEMETRY_ONLY_NO_ENTRY_VETO",
            "training_is_unfiltered": True,
            "pattern_brain_trained": pattern_brain.trained,
            "training_examples": len(pattern_brain.examples),
            "training_wins": sum(1 for row in pattern_brain.examples if row["label"] == 1),
            "training_losses": sum(1 for row in pattern_brain.examples if row["label"] == 0),
            "training_rounds": counts["pattern_training_rounds"],
            "frozen_before_validation_oos": pattern_brain_freeze_train_count is not None,
            "model_score_floor": V3_PATTERN_BRAIN_SCORE_FLOOR,
            "score_is_calibrated_probability": False,
            "toxic_pattern_names": sorted(V3_TOXIC_PATTERN_NAMES),
            "liquidity_sweep_policy": "OBSERVE_ONLY: candidate sweep and toxic-pattern flags are logged but never block an entry.",
            "priority_pattern_directions": V3_PRIORITY_PATTERN_DIRECTIONS,
            "suggestion_counts": {
                "hypothetical_veto_suggestions": counts["v3_pattern_veto_suggestions"],
                "toxic_or_sweep_suggestions": counts["v3_toxic_pattern_veto_suggestions"],
                "low_brain_score_suggestions": counts["v3_brain_low_score_suggestions"],
                "priority_supported_signals": counts["v3_priority_supported_signals"],
                "actual_v3_entry_vetoes": 0,
            },
            "counterfactual": {
                "available": False,
                "closed_vetoed_trades": 0,
                "interpretation": "Not generated because V3 is telemetry-only and no V3-suggested entry is suppressed.",
            },
            "decision_policy": "V3 patterns, scores, veto suggestions, and priority bonuses are telemetry only; the actual entry decision is independent of all V3 outputs.",
        },
        "pattern_intelligence_v3_shadow": {
            "mode": "RESEARCH_FILTER_ISOLATED_TRAIN_ONLY" if v3_research_filter else "SHADOW_TELEMETRY_ONLY_NO_ENTRY_VETO",
            "research_filter_enabled": bool(v3_research_filter),
            "default_mode_unchanged": not bool(v3_research_filter),
            "timeframes": ["15m", "1H", "4H"],
            "completed_candles_only": True,
            "future_candles_used": False,
            "observations": counts["pattern_shadow_observations"],
            "analysis_errors": counts["pattern_analysis_errors"],
            "training_scope": "Matured TRAIN outcomes only; named-feature vocabulary and weights are frozen before Validation/OOS.",
            "trade_scope": "V2-qualified entries with fully reconciled closed PnL; patterns tagged at entry.",
            "family_performance": {
                "ALL": pattern_family_performance(actual_trades),
                "TRAIN": pattern_family_performance(actual_trades, "TRAIN"),
                "VALIDATION": pattern_family_performance(actual_trades, "VALIDATION"),
                "OUT_OF_SAMPLE": pattern_family_performance(actual_trades, "OUT_OF_SAMPLE"),
            },
            "by_market_regime": {
                regime: pattern_family_performance(actual_trades, regime=regime)
                for regime in ("TRENDING", "RANGING", "HIGH_VOLATILITY")
            },
            "adaptive_brain": {
                "model": pattern_brain.MODEL_VERSION,
                "trained": pattern_brain.trained,
                "warmup_trades": pattern_brain.warmup_trades,
                "min_per_class": pattern_brain.min_per_class,
                "training_examples": len(pattern_brain.examples),
                "training_closed_trades": closed_pattern_training_trades,
                "training_rounds": counts["pattern_training_rounds"],
                "wins": sum(1 for x in pattern_brain.examples if x["label"] == 1),
                "losses": sum(1 for x in pattern_brain.examples if x["label"] == 0),
                "feature_weights": pattern_brain.feature_weights(),
                "feature_names": list(pattern_brain.feature_names),
                "feature_count": len(pattern_brain.feature_names),
                "pattern_name_feature_count": sum(name.startswith("pattern_name::") for name in pattern_brain.feature_names),
                "model_version": pattern_brain.MODEL_VERSION,
                "training_timestamp": pattern_brain.training_timestamp,
                "model_hash": pattern_brain.model_hash,
                "freeze_metadata": pattern_brain_freeze_metadata,
                "frozen_after_train": pattern_brain.frozen and pattern_brain_freeze_train_count is not None,
            },
            "suggestion_counts": {
                "hypothetical_veto_suggestions": counts["v3_pattern_veto_suggestions"],
                "toxic_or_sweep_suggestions": counts["v3_toxic_pattern_veto_suggestions"],
                "low_brain_score_suggestions": counts["v3_brain_low_score_suggestions"],
                "priority_supported_signals": counts["v3_priority_supported_signals"],
                "actual_v3_entry_vetoes": counts["v3_research_filter_entry_vetoes"],
                "capacity_slots_released": counts["v3_research_filter_capacity_slots_released"],
                "replacement_entries": counts["v3_replacement_entries"],
            },
            "counterfactual": {
                "available": bool(v3_research_filter),
                "closed_vetoed_trades": sum(
                    1 for row in shadow_trades
                    if "V3_RESEARCH_FILTER" in str(row.get("veto_source") or "").split("+")
                ),
                "rejected_candidate_ledger": [
                    row for row in shadow_trades
                    if "V3_RESEARCH_FILTER" in str(row.get("veto_source") or "").split("+")
                ],
                "replacement_trade_ledger": [
                    row for row in actual_trades
                    if row.get("capacity_replacement_for_signal_id")
                ],
                "interpretation": (
                    "Rejected V3 candidates are replayed in the separate counterfactual book; later accepted entries can use the freed two-slot portfolio capacity."
                    if v3_research_filter else
                    "Not active in the default Shadow mode; suggestions do not alter entries."
                ),
            },
            "decision_policy": (
                "Isolated offline research filter may veto V3-suggested entries; no exchange order path exists."
                if v3_research_filter else
                "V3 patterns, scores, veto suggestions and priority bonuses are telemetry only; actual entries are independent of V3."
            ),
            "metric_semantics": "Expectancy=mean net USDT; MFE/MAE=closed Mark-candle excursion %; false-break rate=observed invalidations after confirmation.",
        },
        "funding_data_quality": {
            "explicit_funding_kind_events": sum(
                1 for item in market.values() for event in item["funding"]
                if event.get("fundingKindSource") == "EXPLICIT_API"
            ),
            "schedule_inferred_events": sum(
                1 for item in market.values() for event in item["funding"]
                if event.get("fundingKindSource") == "SCHEDULE_INFERRED_UTC_8H_BOUNDARY"
            ),
            "schedule_inference_is_not_explicit_exchange_kind": True,
            "supported_observed_intervals_hours": [REGULAR_FUNDING_INTERVAL_MS // 3_600_000],
            "blackout_windows": [dict(window) for window in OBSERVED_FUNDING_BLACKOUTS],
            "new_entries_blocked_inside_blackout": True,
            "positions_overlapping_blackout_keep_unknown_net_pnl": True,
            "classification_note": (
                "Absent Kind is accepted only on UTC 00:00/08:00/16:00 boundaries within 10 seconds; "
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
        "block_reasons": {
            "total": sum(block_reasons.values()),
            "by_reason": dict(block_reasons),
            "by_symbol": {
                symbol: dict(reasons) for symbol, reasons in block_reasons_by_symbol.items()
            },
            "semantics": "Counts skipped signals, rejected simulated entries and blocked portfolio opportunities; these are not necessarily unique signal IDs.",
        },
        "strategy_data_quality": {
            "indicator_warmup_blocks": counts["indicator_warmup_blocks"],
            "strategy_data_errors": counts["strategy_data_errors"],
            "strategy_error_reasons": dict(strategy_error_reasons),
            "minimum_closed_history_rows": {"1H": 205, "4H": 205, "15m": 70, "BTC_15m": 40},
            "warmup_blocks_are_not_market_data_corruption": True,
        },
        "trade_ledger": sorted(actual_trades, key=lambda x: x["exit_ts_ms"]),
        "shadow_ledger": sorted(shadow_trades, key=lambda x: x["exit_ts_ms"]),
        "open_positions_at_dataset_end": unresolved_positions,
        "open_shadow_positions_at_dataset_end": unresolved_shadow_positions,
        "closed_positions_with_unknown_net_pnl": unknown_pnl_trades,
        "closed_shadow_positions_with_unknown_net_pnl": unknown_shadow_pnl_trades,
        "walk_forward_audit": {
            "train_start_utc": start_time.isoformat(),
            "train_end_utc": train_end.isoformat(),
            "validation_end_utc": validation_end.isoformat(),
            "oos_end_utc": oos_end.isoformat(),
            "purge_window_minutes": (max_holding_bars + 1) * 15,
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
                 and counts["funding_blackout_closed_pnl_unknown"] == 0
                 and counts["funding_blackout_closed_shadow_pnl_unknown"] == 0
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
    parser.add_argument("--max-holding-bars", type=int, default=12)
    parser.add_argument("--tp1-disabled", action="store_true")
    parser.add_argument("--tp1-r", type=float, default=1.2)
    parser.add_argument("--tp1-fraction", type=float, default=0.5)
    parser.add_argument("--symbols", default=",".join(DEFAULT_WATCHLIST))
    parser.add_argument("--eth-stop-atr", type=float, default=None)
    parser.add_argument("--sol-stop-atr", type=float, default=None)
    parser.add_argument(
        "--v3-research-filter",
        action="store_true",
        help="Enable isolated TRAIN-only V3 entry vetoes in this offline replay only; default remains Shadow.",
    )
    args = parser.parse_args()
    stop_overrides = {}
    if args.eth_stop_atr is not None:
        stop_overrides["ETH-USDT"] = args.eth_stop_atr
    if args.sol_stop_atr is not None:
        stop_overrides["SOL-USDT"] = args.sol_stop_atr
    report = run_sandbox(
        db_path=args.db, initial_equity=args.equity, symbols=tuple(x.strip() for x in args.symbols.split(",") if x.strip()),
        spread_scenario=args.spread, output_path=args.output, brain_path=args.brain,
        max_holding_bars=args.max_holding_bars, tp1_enabled=not args.tp1_disabled,
        tp1_reward_r_multiple=args.tp1_r, tp1_fraction=args.tp1_fraction,
        stop_atr_multiplier_overrides=stop_overrides,
        v3_research_filter=args.v3_research_filter,
    )
    print(json.dumps({
        "equity": report["equity"],
        "trade_metrics": report["trade_metrics"],
        "ai_audit": report["ai_audit"],
        "diagnostics": report["diagnostics"],
        "report_path": args.output,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
