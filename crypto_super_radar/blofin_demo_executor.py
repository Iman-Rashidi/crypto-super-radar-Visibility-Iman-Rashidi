"""Guarded BloFin Demo Futures executor.

This module has no production exchange host, no transfer/withdrawal methods,
and no default order capability. A real Demo order requires both an explicit
per-call opt-in and BLOFIN_DEMO_TRADING_ENABLED=true, separate Demo trade
credentials, and durable Turso-backed event storage.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping

import requests

from .blofin_demo_readonly import (
    BloFinDemoError,
    BloFinDemoHTTPError,
    BloFinDemoReadOnly,
    BloFinDemoResponseError,
    DEMO_REST_URL,
    DEMO_TRADE_CREDENTIAL_ENV,
    _resolve_env_credential,
    _check_response_codes,
    _signature,
)
from .database import connect_database, storage_backend
from .challenge1_strategy import challenge1_entry_evidence_ready
from .challenge1_v2_trade_controls import v2_entry_capacity
from .challenge1_v2 import challenge1_v2_execution_evidence_ready
from .learning_experience_memory import LearningExperienceMemory


GROQ_ALLOWED_MODELS = frozenset({"openai/gpt-oss-20b", "qwen/qwen3.8-27b"})


class BloFinDemoExecutionBlocked(RuntimeError):
    """A safety gate prevented Demo execution."""


class BloFinDemoExecutionUnknown(RuntimeError):
    """An order request may have reached BloFin; reconcile before any retry."""


class BloFinDemoOrderRejected(BloFinDemoResponseError):
    """BloFin returned an explicit non-zero business code for the order request."""


_TRADE_WRITE_LOCK = threading.Lock()
_TRADE_WRITE_TIMES: deque[float] = deque()


def _wait_for_trade_write_slot() -> None:
    """Keep this process below BloFin's 30 requests/10s trading endpoint cap."""
    while True:
        with _TRADE_WRITE_LOCK:
            now = time.monotonic()
            while _TRADE_WRITE_TIMES and now - _TRADE_WRITE_TIMES[0] >= 10.0:
                _TRADE_WRITE_TIMES.popleft()
            if len(_TRADE_WRITE_TIMES) < 25:
                _TRADE_WRITE_TIMES.append(now)
                return
            delay = max(0.05, 10.0 - (now - _TRADE_WRITE_TIMES[0]) + 0.05)
        time.sleep(delay)


def _finite(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{field} is outside the permitted numeric range")
    return number


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} is not a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _analysis_record(signal: Any) -> dict[str, Any]:
    if hasattr(signal, "to_record") and callable(signal.to_record):
        record = signal.to_record()
    elif isinstance(signal, Mapping):
        record = dict(signal)
    else:
        raise ValueError("signal must be an AnalysisResult or mapping")
    if not isinstance(record, dict):
        raise ValueError("signal record is invalid")
    return record


def _challenge1_two_reviewer_approval_ready(record: Mapping[str, Any]) -> tuple[bool, str]:
    """Require two independent, live, verified AI approvals bound to one Challenge 1 signal."""
    execution_profile = str(record.get("execution_profile") or "").strip().upper()
    if execution_profile == "CHALLENGE_1_V2":
        ready, reason = challenge1_v2_execution_evidence_ready(record)
        if not ready:
            return False, "V2 analysis evidence is incomplete: " + reason
        evidence = record.get("challenge1_v2_evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        wave = evidence.get("elliott_wave")
        wave = wave if isinstance(wave, Mapping) else {}
    elif execution_profile == "CHALLENGE_1":
        evidence = record.get("challenge1_evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        wave = evidence.get("elliott_wave")
        wave = wave if isinstance(wave, Mapping) else {}
    else:
        return False, "Unknown Challenge 1 execution profile."
    side = str(record.get("signal") or "").strip().upper()
    wave_phase = str(wave.get("phase") or "").strip().upper()
    wave_direction = str(wave.get("pattern") or "").strip().upper()
    allowed_phases = {
        "WAVES_1_AND_2_CONFIRMED", "WAVE_3_CONFIRMED",
        "WAVE_4_CONFIRMED", "FIVE_WAVE_IMPULSE_CONFIRMED",
    }
    expected_direction = "BULLISH" if side == "LONG" else "BEARISH" if side == "SHORT" else ""
    if (
        side not in {"LONG", "SHORT"}
        or wave_phase not in allowed_phases
        or wave_direction != expected_direction
    ):
        return False, "A valid directional signal and aligned confirmed Elliott phase are required."

    consensus = record.get("ai_consensus")
    if not isinstance(consensus, Mapping):
        return False, "The verified Groq reviewer consensus record is missing."
    signal_id = str(record.get("signal_id") or "").strip()
    consensus_signal_id = str(consensus.get("reviewed_signal_id") or "").strip()
    if not signal_id or consensus_signal_id != signal_id:
        return False, "The Groq reviewer approval is not bound to this exact signal_id."
    try:
        consensus_reviewed_at = datetime.fromisoformat(
            str(consensus.get("reviewed_at_utc") or "").replace("Z", "+00:00")
        )
        if consensus_reviewed_at.tzinfo is None:
            return False, "The reviewer approval timestamp has no timezone."
        approval_age = (datetime.now(timezone.utc) - consensus_reviewed_at.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return False, "The reviewer approval timestamp is missing or invalid."
    if approval_age < -30 or approval_age > 300:
        return False, "The reviewer approval is outside the five-minute validity window."

    if execution_profile == "CHALLENGE_1_V2":
        required_groq_consensus = (
            consensus.get("status") == "APPROVED_BY_GROQ"
            and consensus.get("decision") == "AUTHORIZE_DEMO_EXECUTION_WITH_ONE_REVIEWER"
            and consensus.get("single_reviewer_approval") is True
            and consensus.get("single_groq_reviewer_approval") is True
            and consensus.get("two_reviewer_approval") is False
            and consensus.get("entry_gate_enforced") is True
            and consensus.get("reviewer_id") == "groq"
            and consensus.get("reviewer_verified") is True
            and consensus.get("provider_is_groq") is True
            and consensus.get("model_is_groq_allowlisted") is True
            and consensus.get("reviewer_approved") is True
            and consensus.get("reviewed_signal_id") == signal_id
            and consensus.get("expected_side") == side
            and consensus.get("elliott_wave_phase") == wave_phase
            and consensus.get("elliott_wave_direction") == wave_direction
        )
        if not required_groq_consensus:
            return False, "The recorded single-Groq approval is not valid for this V2 signal."
        raw_reviews = record.get("ai_reviews")
        if not isinstance(raw_reviews, list) or len(raw_reviews) != 1:
            return False, "Exactly one Groq reviewer record is required for Challenge 1 V2."
        item = raw_reviews[0]
        if not isinstance(item, Mapping):
            return False, "The Groq reviewer record is malformed."
        review = item.get("review")
        if not isinstance(review, Mapping):
            return False, "The Groq reviewer response is missing."
        if str(item.get("reviewer_id") or review.get("reviewer_id") or "").strip().lower() != "groq":
            return False, "The sole V2 reviewer must be Groq."
        provider = str(review.get("provider") or "").strip().casefold()
        model = str(review.get("model") or "").strip()
        returned_model = str(review.get("returned_model") or "").strip()
        model_identity = model.split(":", 1)[0].casefold()
        returned_identity = returned_model.split(":", 1)[0].casefold()
        allowed_groq_models = GROQ_ALLOWED_MODELS
        try:
            reviewer_time = datetime.fromisoformat(
                str(review.get("reviewed_at_utc") or "").replace("Z", "+00:00")
            )
            reviewer_age = (
                datetime.now(timezone.utc) - reviewer_time.astimezone(timezone.utc)
            ).total_seconds() if reviewer_time.tzinfo is not None else float("inf")
        except (TypeError, ValueError):
            reviewer_age = float("inf")
        if str(review.get("reviewed_signal_id") or "").strip() != signal_id:
            return False, "Groq approval is bound to a different signal."
        valid_groq_review = (
            "groq" in provider
            and model_identity in allowed_groq_models
            and returned_identity == model_identity
            and str(review.get("status") or "").strip().upper() == "APPROVED"
            and review.get("paper_entry_eligible") is True
            and review.get("request_attempted") is True
            and str(review.get("response_source") or "").strip().upper() == "LIVE_API"
            and review.get("response_validated") is True
            and review.get("model_verified") is True
            and str(review.get("reviewed_signal_id") or "").strip() == signal_id
            and -30 <= reviewer_age <= 300
            and str(review.get("expected_side") or "").strip().upper() == side
            and str(review.get("trader_proposal") or "").strip().upper() == side
            and str(review.get("elliott_wave_phase") or "").strip().upper() == wave_phase
            and str(review.get("elliott_wave_direction") or "").strip().upper() == wave_direction
        )
        if not valid_groq_review:
            return False, "Groq did not provide a fresh, verified live approval aligned with this exact signal and Elliott phase."
        return True, "The sole verified Groq reviewer approved the same V2 signal and Elliott phase."

    required_consensus = (
        consensus.get("status") == "APPROVED_BY_BOTH"
        and consensus.get("decision") == "AUTHORIZE_DEMO_EXECUTION"
        and consensus.get("two_reviewer_approval") is True
        and consensus.get("entry_gate_enforced") is True
        and consensus.get("reviewed_signal_id") == signal_id
        and consensus.get("primary_verified") is True
        and consensus.get("second_verified") is True
        and consensus.get("primary_approved") is True
        and consensus.get("second_approved") is True
        and consensus.get("providers_independent") is True
        and consensus.get("models_independent") is True
        and consensus.get("expected_side") == side
        and consensus.get("elliott_wave_phase") == wave_phase
        and consensus.get("elliott_wave_direction") == wave_direction
        and str(consensus.get("primary_status") or "").upper() == "APPROVED"
        and str(consensus.get("second_status") or "").upper() == "APPROVED"
    )
    if not required_consensus:
        return False, "The recorded consensus is not a verified two-reviewer approval for this signal and wave."

    raw_reviews = record.get("ai_reviews")
    if not isinstance(raw_reviews, list) or len(raw_reviews) != 2:
        return False, "Exactly two independent reviewer records are required."
    verified: dict[str, Mapping[str, Any]] = {}
    for item in raw_reviews:
        if not isinstance(item, Mapping):
            return False, "A reviewer record is malformed."
        review = item.get("review")
        if not isinstance(review, Mapping):
            return False, "A reviewer response is missing."
        reviewer_id = str(item.get("reviewer_id") or review.get("reviewer_id") or "").strip().lower()
        if reviewer_id not in {"nemotron", "second_ai"} or reviewer_id in verified:
            return False, "Reviewer identities must be exactly Nemotron and the independent second reviewer."
        verified[reviewer_id] = review
    if set(verified) != {"nemotron", "second_ai"}:
        return False, "One of the two required reviewers is missing."

    providers: set[str] = set()
    models: set[str] = set()
    returned_models: set[str] = set()
    for reviewer_id in ("nemotron", "second_ai"):
        review = verified[reviewer_id]
        provider = str(review.get("provider") or "").strip().casefold()
        model = str(review.get("model") or "").strip()
        returned_model = str(review.get("returned_model") or "").strip()
        returned_identity = returned_model.split(":", 1)[0].casefold()
        configured_identity = model.split(":", 1)[0].casefold()
        reviewer_signal_id = str(review.get("reviewed_signal_id") or "").strip()
        try:
            reviewer_reviewed_at = datetime.fromisoformat(
                str(review.get("reviewed_at_utc") or "").replace("Z", "+00:00")
            )
            reviewer_age = (
                datetime.now(timezone.utc) - reviewer_reviewed_at.astimezone(timezone.utc)
            ).total_seconds() if reviewer_reviewed_at.tzinfo is not None else float("inf")
        except (TypeError, ValueError):
            reviewer_age = float("inf")
        if reviewer_signal_id != signal_id or reviewer_age < -30 or reviewer_age > 300:
            return False, f"{reviewer_id} approval is stale or belongs to a different signal."
        valid = (
            str(review.get("status") or "").strip().upper() == "APPROVED"
            and review.get("paper_entry_eligible") is True
            and review.get("request_attempted") is True
            and str(review.get("response_source") or "").strip().upper() == "LIVE_API"
            and review.get("response_validated") is True
            and review.get("model_verified") is True
            and bool(provider and model and returned_model)
            and configured_identity == returned_identity
            and str(review.get("expected_side") or "").strip().upper() == side
            and str(review.get("trader_proposal") or "").strip().upper() == side
            and str(review.get("elliott_wave_phase") or "").strip().upper() == wave_phase
            and str(review.get("elliott_wave_direction") or "").strip().upper() == wave_direction
        )
        if not valid:
            return False, f"{reviewer_id} did not provide a verified live approval aligned with the signal and Elliott wave."
        providers.add(provider)
        models.add(configured_identity)
        returned_models.add(returned_identity)

    if len(providers) != 2 or len(models) != 2 or len(returned_models) != 2:
        return False, "The two approvals must come from distinct providers and distinct verified models."
    return True, "Both independent live AI reviewers approved the same signal and Elliott phase."


def _challenge1_v2_experience_id(instrument_id: str, challenge_record: Mapping[str, Any]) -> str:
    """Return a stable identity for one immutable V2 multi-timeframe evidence packet."""
    evidence = challenge_record.get("challenge1_v2_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("Challenge 1 V2 evidence is required for learning memory.")
    signal_id = str(challenge_record.get("signal_id") or "").strip()
    if not signal_id or evidence.get("signal_id") != signal_id:
        raise ValueError("V2 signal_id is required and must match its evidence packet.")
    identity = {
        "engine_version": str(evidence.get("decision_engine_version") or "CHALLENGE1_V2_UNKNOWN"),
        "instrument_id": str(instrument_id).strip().upper(),
        "signal_id": signal_id,
        "signal_candle_open_utc": evidence.get("signal_candle_open_utc"),
        "context_1h_candle_open_utc": evidence.get("context_1h_candle_open_utc"),
        "context_4h_candle_open_utc": evidence.get("context_4h_candle_open_utc"),
        "data_asof_utc": evidence.get("data_asof_utc"),
        "elliott_wave": evidence.get("elliott_wave"),
        "funding_rate": evidence.get("funding_rate"),
        "funding_kind": evidence.get("funding_kind"),
        "funding_event_time_ms": evidence.get("funding_event_time_ms"),
        "funding_max_age_ms": evidence.get("funding_max_age_ms"),
    }
    canonical = json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    return "blofin-ch1v2-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _challenge1_experience_id(instrument_id: str, challenge_record: Mapping[str, Any]) -> str:
    """Return a stable identity for one instrument's immutable closed-candle evidence."""
    evidence = challenge_record.get("challenge1_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("Challenge 1 evidence is required for learning memory.")
    candle = evidence.get("confirmation_candle")
    if not isinstance(candle, Mapping):
        raise ValueError("A completed confirmation candle is required for learning memory.")
    candle_time = str(candle.get("open_time_utc") or challenge_record.get("signal_bar_open_utc") or "")
    if not candle_time:
        raise ValueError("Completed candle timestamp is required for learning memory.")
    identity = {
        "engine_version": str(evidence.get("decision_engine_version") or "CHALLENGE1_UNKNOWN"),
        "instrument_id": str(instrument_id).strip().upper(),
        "setup_timeframe": str(evidence.get("setup_timeframe") or "5m"),
        "context_timeframe": evidence.get("context_timeframe"),
        "signal_bar_open_utc": candle_time,
        "confirmation_candle": dict(candle),
        "detected_patterns": evidence.get("detected_patterns") or [],
        "chosen_pattern": evidence.get("chosen_pattern"),
        "chart_structure": evidence.get("chart_structure") or {},
        "context_patterns": evidence.get("context_patterns") or [],
        "volume_confirmation": evidence.get("volume_confirmation") or {},
    }
    canonical = json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    return "blofin-ch1-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class BloFinDemoRiskPolicy:
    risk_per_trade_pct: float = 0.0025
    daily_loss_limit_pct: float = 0.01
    max_drawdown_pct: float = 0.05
    max_margin_usage_pct: float = 0.10
    max_aggregate_risk_pct: float = 0.005
    max_position_notional_usdt: float | None = None
    max_leverage: int = 3
    max_concurrent_positions: int = 1
    max_orders_per_utc_day: int | None = None
    min_seconds_between_orders: int = 0
    min_confirmation_score: float = 75.0
    max_risk_score: float = 45.0
    min_reward_risk: float = 1.8
    max_entry_deviation_pct: float = 0.005
    taker_fee_rate: float = 0.0006
    estimated_slippage_bps_per_side: float = 2.0

    def __post_init__(self) -> None:
        values = (
            self.risk_per_trade_pct, self.daily_loss_limit_pct, self.max_drawdown_pct,
            self.max_margin_usage_pct, self.max_aggregate_risk_pct, self.min_confirmation_score, self.max_risk_score,
            self.min_reward_risk, self.max_entry_deviation_pct,
            self.taker_fee_rate, self.estimated_slippage_bps_per_side,
        )
        if not all(math.isfinite(float(v)) for v in values):
            raise ValueError("Risk policy values must be finite")
        if not 0 < self.risk_per_trade_pct <= 0.005:
            raise ValueError("Per-trade risk must be in (0, 0.5%]")
        if not 0 < self.daily_loss_limit_pct <= 0.03:
            raise ValueError("Daily loss limit must be in (0, 3%]")
        if not 0 < self.max_drawdown_pct <= 0.15:
            raise ValueError("Maximum drawdown must be in (0, 15%]")
        if not 0 < self.max_margin_usage_pct <= 0.25:
            raise ValueError("Margin usage must be in (0, 25%]")
        if not 0 < self.max_aggregate_risk_pct <= 0.01:
            raise ValueError("Aggregate risk must be in (0, 1%]")
        if self.max_position_notional_usdt is not None:
            if isinstance(self.max_position_notional_usdt, bool):
                raise ValueError("Maximum position notional must be a positive amount up to 5000 USDT")
            max_notional = float(self.max_position_notional_usdt)
            if not math.isfinite(max_notional) or not 0 < max_notional <= 5000:
                raise ValueError("Maximum position notional must be a positive amount up to 5000 USDT")
        if self.max_orders_per_utc_day is not None and (
            isinstance(self.max_orders_per_utc_day, bool)
            or not 1 <= int(self.max_orders_per_utc_day) <= 5
        ):
            raise ValueError("An optional Demo daily order limit must be between 1 and 5")
        if isinstance(self.min_seconds_between_orders, bool) or not 0 <= int(self.min_seconds_between_orders) <= 86400:
            raise ValueError("Minimum seconds between Demo orders must be between 0 and 86400")
        if isinstance(self.max_leverage, bool) or not 1 <= int(self.max_leverage) <= 3:
            raise ValueError("Demo test leverage is restricted to 1x through 3x")
        if isinstance(self.max_concurrent_positions, bool) or not 1 <= int(self.max_concurrent_positions) <= 4:
            raise ValueError("Concurrent positions must be between 1 and 4")
        if self.min_reward_risk < 1.0 or self.max_entry_deviation_pct <= 0:
            raise ValueError("Reward/risk or entry-deviation policy is invalid")
        if not 0 <= self.taker_fee_rate <= 0.01:
            raise ValueError("Taker fee rate must be between 0 and 1%.")
        if not 0 <= self.estimated_slippage_bps_per_side <= 100:
            raise ValueError("Estimated slippage must be between 0 and 100 bps per side.")


def build_demo_order_plan(
    signal: Any,
    instrument: Mapping[str, Any],
    *,
    inst_id: str,
    equity_usdt: float,
    leverage: int = 1,
    policy: BloFinDemoRiskPolicy | None = None,
    market_quote: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate Challenge 1 evidence and size Demo contracts from stop risk."""
    policy = policy or BloFinDemoRiskPolicy()
    record = _analysis_record(signal)
    side = str(record.get("signal", "")).upper()
    if side not in {"LONG", "SHORT"}:
        raise BloFinDemoExecutionBlocked("Only LONG or SHORT signals may enter Demo execution.")
    if str(record.get("inst_id") or record.get("instrument_id") or inst_id) != inst_id:
        raise BloFinDemoExecutionBlocked("Signal instrument does not match the requested contract.")
    signal_id = str(record.get("signal_id") or record.get("signal_bar") or "").strip()
    if not signal_id:
        raise BloFinDemoExecutionBlocked("A stable signal_id or signal_bar is required for idempotency.")

    execution_profile = str(record.get("execution_profile") or "").strip().upper()
    if execution_profile == "CHALLENGE_1":
        eligible, reason = challenge1_entry_evidence_ready(record)
        if not eligible:
            raise BloFinDemoExecutionBlocked(
                "Challenge 1 5m-only pattern evidence is incomplete or inconsistent: " + reason
            )
    elif execution_profile == "CHALLENGE_1_V2":
        eligible, reason = challenge1_v2_execution_evidence_ready(record)
        if not eligible:
            raise BloFinDemoExecutionBlocked(
                "Challenge 1 V2 multi-timeframe evidence is incomplete or inconsistent: " + reason
            )
        score = _finite(record.get("confidence_score"), "confidence_score")
        if score < max(70.0, float(policy.min_confirmation_score)):
            raise BloFinDemoExecutionBlocked(
                "Challenge 1 V2 confluence score is below the configured Demo minimum."
            )
    else:
        raise BloFinDemoExecutionBlocked(
            "Only the explicitly validated CHALLENGE_1 or CHALLENGE_1_V2 profiles are allowed."
        )

    candle_guard = None
    if execution_profile == "CHALLENGE_1_V2":
        v2_evidence = record.get("challenge1_v2_evidence")
        if not isinstance(v2_evidence, Mapping):
            raise BloFinDemoExecutionBlocked("V2 candle identity is required for duplicate-candle protection.")
        setup_timeframe = str(v2_evidence.get("setup_timeframe") or "").strip().lower()
        entry_timeframe = str(v2_evidence.get("entry_execution_timeframe") or "").strip().lower()
        candle_open_utc = str(v2_evidence.get("signal_candle_open_utc") or "").strip()
        if setup_timeframe == "1h" and entry_timeframe == "15m":
            # Standard V2 setup is detected on 1H, but its signal identity is
            # the completed 15m execution candle; deduplicate on that candle.
            candle_timeframe = "15m"
        elif setup_timeframe in {"5m", "15m"} and entry_timeframe in {"", "15m"}:
            # Early-W3 uses its 5m signal candle; ordinary 15m setups use 15m.
            candle_timeframe = setup_timeframe
        else:
            raise BloFinDemoExecutionBlocked(
                "V2 setup/entry timeframe pair is unsupported for candle identity."
            )
        if not candle_open_utc:
            raise BloFinDemoExecutionBlocked("V2 closed-candle timestamp is required.")
        candle_guard = {
            "instrument_id": inst_id.strip().upper(),
            "timeframe": candle_timeframe,
            "candle_open_utc": candle_open_utc,
        }

    if instrument.get("instId") != inst_id or instrument.get("apiPermission") != "full":
        raise BloFinDemoExecutionBlocked("Contract is not explicitly marked apiPermission=full.")
    if instrument.get("state") != "live" or instrument.get("contractType") != "linear":
        raise BloFinDemoExecutionBlocked("Only live linear contracts are supported.")
    if instrument.get("settleCurrency") != "USDT":
        raise BloFinDemoExecutionBlocked("Only USDT-settled linear contracts are supported in this phase.")

    signal_entry = _decimal(
        record.get("entry_price_usd", record.get("entry_price", record.get("entry"))),
        "entry",
    )
    if market_quote is not None:
        bid = _decimal(market_quote.get("bidPrice"), "best_bid")
        ask = _decimal(market_quote.get("askPrice"), "best_ask")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise BloFinDemoExecutionBlocked("Fresh best bid/ask quote is invalid.")
        entry = ask if side == "LONG" else bid
    else:
        entry = signal_entry
    stop = _decimal(record.get("stop_loss_usd", record.get("stop_loss")), "stop_loss")
    target = _decimal(record.get("take_profit_usd", record.get("take_profit")), "take_profit")
    if entry <= 0 or stop <= 0 or target <= 0:
        raise BloFinDemoExecutionBlocked("Entry, stop-loss and take-profit prices must be positive.")
    if side == "LONG":
        if not stop < entry < target:
            raise BloFinDemoExecutionBlocked("LONG requires stop < entry < take-profit.")
        risk_distance, reward_distance = entry - stop, target - entry
    else:
        if not target < entry < stop:
            raise BloFinDemoExecutionBlocked("SHORT requires take-profit < entry < stop.")
        risk_distance, reward_distance = stop - entry, entry - target
    reward_risk = reward_distance / risk_distance
    if reward_risk < Decimal(str(policy.min_reward_risk)):
        raise BloFinDemoExecutionBlocked("Net reward/risk is below the configured minimum.")

    if isinstance(leverage, bool) or int(leverage) != leverage or not 1 <= int(leverage) <= policy.max_leverage:
        raise BloFinDemoExecutionBlocked("Leverage must be an integer from 1x to 3x.")
    equity = _decimal(equity_usdt, "equity")
    if equity <= 0:
        raise BloFinDemoExecutionBlocked("Futures equity must be positive.")

    contract_value = _decimal(instrument.get("contractValue"), "contractValue")
    min_size = _decimal(instrument.get("minSize"), "minSize")
    lot_size = _decimal(instrument.get("lotSize"), "lotSize")
    tick_size = _decimal(instrument.get("tickSize"), "tickSize")
    if min(contract_value, min_size, lot_size, tick_size) <= 0:
        raise BloFinDemoExecutionBlocked("Contract size/tick metadata is invalid.")

    # Round protective trigger prices in the conservative direction.
    if side == "LONG":
        # Stop is rounded toward entry to avoid increasing planned loss.
        stop = (stop / tick_size).to_integral_value(rounding=ROUND_CEILING) * tick_size
        target = (target / tick_size).to_integral_value(rounding=ROUND_CEILING) * tick_size
        risk_distance, reward_distance = entry - stop, target - entry
    else:
        # For shorts, a lower stop and lower target are the conservative tick directions.
        stop = (stop / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size
        target = (target / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size
        risk_distance, reward_distance = stop - entry, entry - target
    if risk_distance <= 0 or reward_distance / risk_distance < Decimal(str(policy.min_reward_risk)):
        raise BloFinDemoExecutionBlocked("Tick rounding made the stop/target invalid or reward/risk too small.")

    risk_budget = equity * Decimal(str(policy.risk_per_trade_pct))
    risk_per_contract = risk_distance * contract_value
    risk_quantity = risk_budget / risk_per_contract
    margin_budget = equity * Decimal(str(policy.max_margin_usage_pct))
    notional_per_contract = entry * contract_value
    margin_quantity = margin_budget * Decimal(int(leverage)) / notional_per_contract
    quantity_before_lot = min(risk_quantity, margin_quantity)
    if policy.max_position_notional_usdt is not None:
        notional_cap_quantity = (
            Decimal(str(policy.max_position_notional_usdt)) / notional_per_contract
        )
        quantity_before_lot = min(quantity_before_lot, notional_cap_quantity)
    quantity = (quantity_before_lot / lot_size).to_integral_value(rounding=ROUND_FLOOR) * lot_size
    if quantity < min_size:
        raise BloFinDemoExecutionBlocked("Risk-sized quantity is below the contract's minSize.")

    max_market_size = instrument.get("maxMarketSize")
    if max_market_size not in (None, "") and quantity > _decimal(max_market_size, "maxMarketSize"):
        quantity = (_decimal(max_market_size, "maxMarketSize") / lot_size).to_integral_value(rounding=ROUND_FLOOR) * lot_size
    if quantity < min_size:
        raise BloFinDemoExecutionBlocked("Quantity is outside the contract's market-size limits.")

    digest = hashlib.sha256(f"{signal_id}|{inst_id}|{side}".encode("utf-8")).hexdigest()
    client_order_id = digest[:32]
    reserved_risk = quantity * risk_per_contract
    reserved_margin = quantity * notional_per_contract / Decimal(int(leverage))
    aggregate_risk_limit = equity * min(
        Decimal(str(policy.max_aggregate_risk_pct)),
        Decimal(str(policy.daily_loss_limit_pct)) / Decimal("2"),
    )
    aggregate_margin_limit = equity * Decimal(str(policy.max_margin_usage_pct))
    return {
        "signal_id": signal_id,
        "execution_profile": execution_profile,
        "candle_guard_instrument_id": candle_guard["instrument_id"] if candle_guard else None,
        "candle_guard_timeframe": candle_guard["timeframe"] if candle_guard else None,
        "candle_guard_open_utc": candle_guard["candle_open_utc"] if candle_guard else None,
        "client_order_id": client_order_id,
        "inst_id": inst_id,
        "side": side,
        "quantity": _format_decimal(quantity),
        "entry_reference_price": float(entry),
        "stop_loss": _format_decimal(stop),
        "take_profit": _format_decimal(target),
        "risk_budget_usdt": float(risk_budget),
        "risk_per_contract_usdt": float(risk_per_contract),
        "notional_usdt": float(quantity * notional_per_contract),
        "max_position_notional_usdt": policy.max_position_notional_usdt,
        "estimated_initial_margin_usdt": float(reserved_margin),
        "reserved_risk_usdt": _format_decimal(reserved_risk),
        "reserved_margin_usdt": _format_decimal(reserved_margin),
        "aggregate_risk_limit_usdt": _format_decimal(aggregate_risk_limit),
        "aggregate_margin_limit_usdt": _format_decimal(aggregate_margin_limit),
        "leverage": int(leverage),
        "reward_risk": float(reward_distance / risk_distance),
        "position_side_long_short": side.lower(),
    }



def apply_demo_execution_cost_gate(
    plan: dict[str, Any],
    instrument: Mapping[str, Any],
    market_quote: Mapping[str, Any],
    *,
    policy: BloFinDemoRiskPolicy | None = None,
) -> dict[str, Any]:
    """Fail closed unless net reward/risk clears the threshold after modelled Taker costs."""
    policy = policy or BloFinDemoRiskPolicy()
    if not isinstance(plan, dict) or not isinstance(instrument, Mapping) or not isinstance(market_quote, Mapping):
        raise BloFinDemoExecutionBlocked("Complete order plan, contract metadata and market quote are required.")
    try:
        bid = _decimal(market_quote.get("bidPrice"), "best_bid")
        ask = _decimal(market_quote.get("askPrice"), "best_ask")
        entry = _decimal(plan.get("entry_reference_price"), "entry_reference_price")
        stop = _decimal(plan.get("stop_loss"), "stop_loss")
        target = _decimal(plan.get("take_profit"), "take_profit")
        quantity = _decimal(plan.get("quantity"), "quantity")
        contract_value = _decimal(instrument.get("contractValue"), "contractValue")
    except Exception as exc:
        raise BloFinDemoExecutionBlocked("Execution-cost inputs are incomplete or invalid.") from exc
    if bid <= 0 or ask <= 0 or ask < bid or min(entry, stop, target, quantity, contract_value) <= 0:
        raise BloFinDemoExecutionBlocked("Execution-cost inputs contain invalid prices or size.")

    side = str(plan.get("side") or "").upper()
    if side == "LONG":
        gross_reward = (target - entry) * quantity * contract_value
        gross_risk = (entry - stop) * quantity * contract_value
    elif side == "SHORT":
        gross_reward = (entry - target) * quantity * contract_value
        gross_risk = (stop - entry) * quantity * contract_value
    else:
        raise BloFinDemoExecutionBlocked("Execution-cost gate received an invalid direction.")
    if gross_reward <= 0 or gross_risk <= 0:
        raise BloFinDemoExecutionBlocked("Net reward/risk cannot be evaluated from invalid price levels.")

    midpoint = (bid + ask) / Decimal("2")
    half_spread_fraction = (ask - bid) / (ask + bid) if (ask + bid) > 0 else Decimal("0")
    fee_rate = Decimal(str(policy.taker_fee_rate))
    slippage_rate = Decimal(str(policy.estimated_slippage_bps_per_side)) / Decimal("10000")
    turnover_multiplier = quantity * contract_value

    def round_trip_cost(exit_price: Decimal) -> dict[str, Decimal]:
        turnover = (entry + exit_price) * turnover_multiplier
        fee = turnover * fee_rate
        # Entry is already priced at the executable ask/bid; only model the
        # opposite-side spread crossing on the exit fill here.
        spread = exit_price * turnover_multiplier * half_spread_fraction
        slippage = turnover * slippage_rate
        return {"fee": fee, "spread": spread, "slippage": slippage, "total": fee + spread + slippage}

    target_cost = round_trip_cost(target)
    stop_cost = round_trip_cost(stop)
    net_reward = gross_reward - target_cost["total"]
    net_risk = gross_risk + stop_cost["total"]
    if net_reward <= 0 or net_risk <= 0:
        raise BloFinDemoExecutionBlocked("Estimated fees, spread and slippage consume the trade's reward.")
    net_reward_risk = net_reward / net_risk
    if net_reward_risk < Decimal(str(policy.min_reward_risk)):
        raise BloFinDemoExecutionBlocked(
            "Net reward/risk after estimated Taker fees, spread and slippage is below the configured minimum "
            f"({float(net_reward_risk):.4f} < {policy.min_reward_risk:.4f})."
        )

    # Fee/slippage/spread-adjusted break-even, solved for the opposite-side exit.
    # Funding is intentionally excluded and disclosed in the plan.
    cost_rate = fee_rate + slippage_rate
    if side == "LONG":
        denominator = Decimal("1") - cost_rate - half_spread_fraction
        if denominator <= 0:
            raise BloFinDemoExecutionBlocked("Net break-even denominator is invalid.")
        net_break_even = entry * (Decimal("1") + cost_rate) / denominator
    else:
        denominator = Decimal("1") + cost_rate + half_spread_fraction
        net_break_even = entry * (Decimal("1") - cost_rate) / denominator
    plan["net_break_even_price"] = _format_decimal(net_break_even)
    plan["net_reward_risk"] = float(net_reward_risk)
    plan["execution_cost_estimate"] = {
        "status": "MODELLED_PRETRADE",
        "fee_role": "TAKER",
        "taker_fee_rate_per_side": float(fee_rate),
        "slippage_bps_per_side": float(policy.estimated_slippage_bps_per_side),
        "bid": float(bid),
        "ask": float(ask),
        "spread_pct": float((ask - bid) / midpoint * Decimal("100")) if midpoint > 0 else None,
        "target_path": {
            "gross_reward_usdt": float(gross_reward),
            "estimated_fee_usdt": float(target_cost["fee"]),
            "estimated_spread_usdt": float(target_cost["spread"]),
            "estimated_slippage_usdt": float(target_cost["slippage"]),
            "estimated_total_cost_usdt": float(target_cost["total"]),
            "net_reward_usdt": float(net_reward),
        },
        "stop_path": {
            "gross_risk_usdt": float(gross_risk),
            "estimated_fee_usdt": float(stop_cost["fee"]),
            "estimated_spread_usdt": float(stop_cost["spread"]),
            "estimated_slippage_usdt": float(stop_cost["slippage"]),
            "estimated_total_cost_usdt": float(stop_cost["total"]),
            "net_risk_usdt": float(net_risk),
        },
        "funding_status": "NOT_INCLUDED_HOLD_DURATION_AND_RATE_UNKNOWN",
        "assumption_note": "2 bps per-side slippage is a configurable model assumption, not an observed guarantee.",
    }
    return plan


class BloFinDemoTradeClient(BloFinDemoReadOnly):
    """Demo-only write adapter with allow-listed leverage, open-order and targeted close-position methods."""

    ORDER_PATH = "/api/v1/trade/order"
    LEVERAGE_PATH = "/api/v1/account/set-leverage"
    CLOSE_POSITION_PATH = "/api/v1/trade/close-position"
    AMEND_TPSL_PATH = "/api/v1/trade/amend-tpsl"
    ALLOWED_POST_PATHS = frozenset({
        ORDER_PATH, LEVERAGE_PATH, CLOSE_POSITION_PATH, AMEND_TPSL_PATH,
    })

    def __init__(self, **kwargs: Any) -> None:
        # Prefer a dedicated Demo Trade override. Otherwise use the unified
        # BloFin account credentials, followed by legacy Demo-prefixed aliases.
        for field_name in ("api_key", "api_secret", "passphrase"):
            if kwargs.get(field_name) is None:
                kwargs[field_name] = _resolve_env_credential(
                    DEMO_TRADE_CREDENTIAL_ENV[field_name]
                )[0]
        super().__init__(**kwargs)
        self._trade_permission_verified = False

    def verify_trade_permission(self) -> dict[str, Any]:
        """Confirm this exact Demo key is read/write before any write operation."""
        info = self.get_api_key_info()
        if info.get("readOnly") != 0 or info.get("trade_permission") is not True:
            self._trade_permission_verified = False
            raise BloFinDemoExecutionBlocked(
                "BloFin API key is read-only (readOnly=1); enable Trade/Read-Write permission. "
                "No write request was sent."
            )
        self._trade_permission_verified = True
        return {"readOnly": 0, "trade_permission": True}

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if path not in self.ALLOWED_POST_PATHS:
            raise BloFinDemoExecutionBlocked("POST endpoint is not allow-listed.")
        if self.base_url != DEMO_REST_URL:
            raise BloFinDemoExecutionBlocked("Only the pinned BloFin Demo host is permitted.")
        if not all((self.api_key, self.api_secret, self.passphrase)):
            raise BloFinDemoError("Demo trade credentials are not configured.")
        # Direct POST calls cannot bypass the dashboard or executor readiness checks.
        if not self._trade_permission_verified:
            self.verify_trade_permission()
        timestamp = str(int(self.clock_ms()))
        nonce = str(uuid.uuid4())
        body_text = json.dumps(dict(body), separators=(",", ":"), ensure_ascii=False)
        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": _signature(self.api_secret, "POST", path, timestamp, nonce, body_text),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        _wait_for_trade_write_slot()
        try:
            response = self.session.post(
                f"{self.base_url}{path}",
                data=body_text.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if 300 <= status_code < 400:
                raise BloFinDemoHTTPError(status_code)
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 403:
                raise BloFinDemoHTTPError(403, retry_after_seconds=300.0) from exc
            if status == 429:
                retry_header = getattr(exc.response, "headers", {}).get("Retry-After")
                try:
                    delay = max(1.0, float(retry_header))
                except (TypeError, ValueError):
                    delay = 2.0
                # A POST is never blindly retried. Reconcile by clientOrderId first.
                raise BloFinDemoHTTPError(429, retry_after_seconds=delay) from exc
            raise BloFinDemoError("BloFin Demo trade request failed.") from exc
        except requests.RequestException as exc:
            raise BloFinDemoExecutionUnknown(
                "Demo order outcome is unknown; reconcile by clientOrderId before retrying."
            ) from exc
        except (TypeError, ValueError) as exc:
            raise BloFinDemoResponseError("BloFin Demo returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise BloFinDemoResponseError("BloFin Demo response must be a JSON object.")
        try:
            _check_response_codes(payload)
        except BloFinDemoResponseError as exc:
            data = payload.get("data")
            rows = data if isinstance(data, list) else [data]
            top_code = payload.get("code")
            explicit_nonzero = top_code not in (None, "", "0", 0, "401", 401)
            explicit_nonzero = explicit_nonzero or any(
                isinstance(row, Mapping)
                and row.get("code") not in (None, "", "0", 0, "401", 401)
                for row in rows
            )
            if explicit_nonzero:
                raise BloFinDemoOrderRejected(
                    "BloFin returned an explicit non-zero order business code."
                ) from exc
            raise
        if "data" not in payload:
            raise BloFinDemoResponseError("BloFin Demo response is missing data.")
        return payload

    def set_futures_leverage(
        self, inst_id: str, leverage: int, *, margin_mode: str = "isolated",
        position_side: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= int(leverage) <= 3:
            raise ValueError("Demo test leverage is restricted to 1x through 3x.")
        if margin_mode not in {"isolated", "cross"}:
            raise ValueError("margin_mode must be isolated or cross.")
        body: dict[str, Any] = {
            "instId": inst_id, "leverage": str(int(leverage)), "marginMode": margin_mode
        }
        if position_side is not None:
            if position_side not in {"net", "long", "short"}:
                raise ValueError("position_side is invalid.")
            body["positionSide"] = position_side
        return self._post(self.LEVERAGE_PATH, body)

    def place_futures_order(
        self, *, inst_id: str, side: str, position_side: str, size: str,
        client_order_id: str, take_profit: str | None, stop_loss: str,
        margin_mode: str = "isolated",
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", client_order_id or ""):
            raise ValueError("client_order_id must be 1-32 alphanumeric characters.")
        if side not in {"buy", "sell"} or position_side not in {"net", "long", "short"}:
            raise ValueError("Order side or position side is invalid.")
        if margin_mode not in {"isolated", "cross"}:
            raise ValueError("margin_mode must be isolated or cross.")
        qty = _decimal(size, "size")
        tp = None if take_profit in (None, "") else _decimal(take_profit, "take_profit")
        sl = _decimal(stop_loss, "stop_loss")
        if qty <= 0 or sl <= 0 or (tp is not None and tp <= 0):
            raise ValueError("Order size and supplied TP/SL prices must be positive.")
        body = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "positionSide": position_side,
            "side": side,
            "orderType": "market",
            "size": _format_decimal(qty),
            "clientOrderId": client_order_id,
            "slTriggerPrice": _format_decimal(sl),
            "slOrderPrice": "-1",
            "slTriggerPriceType": "mark",
        }
        if tp is not None:
            body.update({"tpTriggerPrice": _format_decimal(tp), "tpOrderPrice": "-1", "tpTriggerPriceType": "mark"})
        return self._post(self.ORDER_PATH, body)

    def close_futures_position(
        self, *, inst_id: str, margin_mode: str, position_side: str,
        client_order_id: str, position_id: str | None = None,
    ) -> dict[str, Any]:
        """Close one already-identified Demo position at market, never close-all."""
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", client_order_id or ""):
            raise ValueError("client_order_id must be 1-32 alphanumeric characters.")
        if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", inst_id or ""):
            raise ValueError("Only a USDT linear Demo perpetual can be closed.")
        if margin_mode != "isolated" or position_side not in {"net", "long", "short"}:
            raise ValueError("Smoke-test close requires isolated margin and a known position side.")
        if position_id is not None and not re.fullmatch(r"[A-Za-z0-9]{1,64}", position_id):
            raise ValueError("position_id is invalid.")
        body: dict[str, Any] = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "positionSide": position_side,
            "clientOrderId": client_order_id,
        }
        if position_id:
            body["positionId"] = position_id
        return self._post(self.CLOSE_POSITION_PATH, body)

    @staticmethod
    def _exit_client_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", value or ""):
            raise ValueError("client_order_id must be 1-32 alphanumeric characters.")
        return value

    def _net_position_for_exit(
        self, *, inst_id: str, position_id: str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", inst_id or ""):
            raise ValueError("Only a USDT linear Demo perpetual is supported.")
        mode = self.get_position_mode()
        if mode.get("positionMode") != "net_mode":
            raise BloFinDemoExecutionBlocked(
                "reduceOnly exits are blocked outside One-Way net_mode; BloFin ignores reduceOnly in Hedge mode."
            )
        rows = self.get_positions(inst_id=inst_id)
        if not isinstance(rows, list):
            raise BloFinDemoResponseError("Position response is not a list.")
        matches = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("instId") or "").upper() != inst_id:
                continue
            if str(row.get("positionSide") or "").lower() != "net":
                continue
            returned_id = str(row.get("positionId") or "").strip()
            if position_id and returned_id != position_id:
                continue
            size = _decimal(row.get("positions"), "position_size")
            if size != 0:
                matches.append((dict(row), size))
        if len(matches) != 1:
            raise BloFinDemoExecutionBlocked(
                "An exact single open net position could not be identified; exit request blocked."
            )
        row, signed_size = matches[0]
        if position_id and str(row.get("positionId") or "").strip() != position_id:
            raise BloFinDemoExecutionBlocked("Exchange position identity does not match the journal.")
        row["_signed_size_decimal"] = signed_size
        return row

    def partial_close_reduce_only(
        self, *, inst_id: str, client_order_id: str,
        position_id: str | None = None,
    ) -> dict[str, Any]:
        """Close exactly half of one net-mode Demo position, or fail closed."""
        self._exit_client_id(client_order_id)
        position = self._net_position_for_exit(inst_id=inst_id, position_id=position_id)
        signed_size = position["_signed_size_decimal"]
        instrument = self.get_tradable_instrument(inst_id)
        lot = _decimal(instrument.get("lotSize"), "lotSize")
        minimum = _decimal(instrument.get("minSize"), "minSize")
        half = abs(signed_size) / Decimal("2")
        quantity = (half / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
        if quantity != half or quantity < minimum:
            raise BloFinDemoExecutionBlocked(
                "An exact 50% partial close cannot be represented at this contract's lotSize/minSize."
            )
        side = "sell" if signed_size > 0 else "buy"
        body = {
            "instId": inst_id,
            "marginMode": str(position.get("marginMode") or "isolated"),
            "positionSide": "net",
            "side": side,
            "orderType": "market",
            "size": _format_decimal(quantity),
            "reduceOnly": "true",
            "clientOrderId": client_order_id,
        }
        return self._post(self.ORDER_PATH, body)

    def amend_or_replace_sl(
        self, *, inst_id: str, tpsl_id: str, new_stop_loss: str,
        request_id: str, position_id: str | None = None,
    ) -> dict[str, Any]:
        """Amend the existing exchange SL in place; never cancel first and expose the position."""
        self._exit_client_id(request_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tpsl_id or ""):
            raise ValueError("tpsl_id is invalid.")
        stop = _decimal(new_stop_loss, "new_stop_loss")
        if stop <= 0:
            raise ValueError("new_stop_loss must be positive.")
        position = self._net_position_for_exit(inst_id=inst_id, position_id=position_id)
        active = self.get_open_tpsl_orders(inst_id=inst_id, tpsl_id=tpsl_id, limit=100)
        if not isinstance(active, list):
            raise BloFinDemoResponseError("Active TP/SL response is not a list.")
        expected_size = abs(position["_signed_size_decimal"])
        matches = []
        for row in active:
            if not isinstance(row, Mapping):
                continue
            if (
                str(row.get("tpslId") or "") != tpsl_id
                or str(row.get("instId") or "").upper() != inst_id
                or str(row.get("positionSide") or "").lower() != "net"
                or str(row.get("state") or "").lower() != "live"
                or str(row.get("marginMode") or "").lower() != "isolated"
                or str(row.get("side") or "").lower() != (
                    "sell" if position["_signed_size_decimal"] > 0 else "buy"
                )
            ):
                continue
            size_values = [
                value for value in (row.get("size"), row.get("actualSize"))
                if value not in (None, "")
            ]
            if not size_values:
                continue
            try:
                if any(_decimal(value, "tpsl_size") != expected_size for value in size_values):
                    continue
            except ValueError:
                continue
            matches.append(row)
        if len(matches) != 1 or not matches[0].get("slTriggerPrice"):
            raise BloFinDemoExecutionBlocked(
                "The exact active protective SL could not be proven; amendment blocked."
            )
        payload = self._post(self.AMEND_TPSL_PATH, {
            "instId": inst_id,
            "tpslId": tpsl_id,
            "requestId": request_id,
            "newSlTriggerPrice": _format_decimal(stop),
        })
        verified = self.get_open_tpsl_orders(inst_id=inst_id, tpsl_id=tpsl_id, limit=100)
        if not isinstance(verified, list):
            raise BloFinDemoExecutionUnknown(
                "SL amendment was submitted but active protection could not be re-read."
            )
        exact = [
            row for row in verified
            if isinstance(row, Mapping)
            and str(row.get("tpslId") or "") == tpsl_id
            and str(row.get("instId") or "").upper() == inst_id
            and str(row.get("positionSide") or "").lower() == "net"
            and str(row.get("state") or "").lower() == "live"
            and str(row.get("marginMode") or "").lower() == "isolated"
            and str(row.get("side") or "").lower() == (
                "sell" if position["_signed_size_decimal"] > 0 else "buy"
            )
            and _decimal(row.get("slTriggerPrice"), "verified_stop_loss") == stop
            and any(
                value not in (None, "")
                and _decimal(value, "verified_tpsl_size") == abs(position["_signed_size_decimal"])
                for value in (row.get("size"), row.get("actualSize"))
            )
        ]
        if len(exact) != 1:
            raise BloFinDemoExecutionUnknown(
                "SL amendment receipt was returned but the active exchange stop could not be verified."
            )
        return {**payload, "sl_verified": True, "tpsl_id": tpsl_id}

    def place_limit_exit_reduce_only(
        self, *, inst_id: str, limit_price: str, client_order_id: str,
        position_id: str | None = None,
    ) -> dict[str, Any]:
        """Place a reduce-only limit for the full remaining net-mode Demo position."""
        self._exit_client_id(client_order_id)
        price = _decimal(limit_price, "limit_price")
        if price <= 0:
            raise ValueError("limit_price must be positive.")
        position = self._net_position_for_exit(inst_id=inst_id, position_id=position_id)
        signed_size = position["_signed_size_decimal"]
        instrument = self.get_tradable_instrument(inst_id)
        lot = _decimal(instrument.get("lotSize"), "lotSize")
        minimum = _decimal(instrument.get("minSize"), "minSize")
        quantity = abs(signed_size)
        if quantity < minimum or quantity % lot != 0:
            raise BloFinDemoExecutionBlocked(
                "The remaining position size violates the contract lotSize/minSize."
            )
        tick = _decimal(instrument.get("tickSize"), "tickSize")
        if price % tick != 0:
            raise BloFinDemoExecutionBlocked("Limit exit price does not align with tickSize.")
        side = "sell" if signed_size > 0 else "buy"
        body = {
            "instId": inst_id,
            "marginMode": str(position.get("marginMode") or "isolated"),
            "positionSide": "net",
            "side": side,
            "orderType": "limit",
            "price": _format_decimal(price),
            "size": _format_decimal(quantity),
            "reduceOnly": "true",
            "clientOrderId": client_order_id,
        }
        return self._post(self.ORDER_PATH, body)


class BloFinDemoEventStore:
    """Durable order reservation, append-only events and equity checkpoints."""

    def __init__(self, path: str | None = None) -> None:
        self.backend = storage_backend()
        self._db = connect_database(path)
        self._lock = threading.RLock()
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_orders (
                    client_order_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE,
                    instrument_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    exchange_tp_required INTEGER NOT NULL DEFAULT 1,
                    leverage INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    exchange_order_id TEXT,
                    experience_id TEXT,
                    outcome_recorded_at INTEGER,
                    position_history_id TEXT,
                    position_id TEXT,
                    position_side TEXT,
                    filled_size TEXT,
                    critical_reason TEXT,
                    reserved_risk_usdt TEXT,
                    reserved_margin_usdt TEXT,
                    notional_usdt TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            order_columns = {
                str(row["name"])
                for row in self._db.execute("PRAGMA table_info(blofin_demo_orders)").fetchall()
            }
            for column_name, column_type in (
                ("experience_id", "TEXT"),
                ("outcome_recorded_at", "INTEGER"),
                ("position_history_id", "TEXT"),
                ("position_id", "TEXT"),
                ("position_side", "TEXT"),
                ("filled_size", "TEXT"),
                ("critical_reason", "TEXT"),
                ("reserved_risk_usdt", "TEXT"),
                ("reserved_margin_usdt", "TEXT"),
                ("notional_usdt", "TEXT"),
                ("exchange_tp_required", "INTEGER NOT NULL DEFAULT 1"),
            ):
                if column_name not in order_columns:
                    self._db.execute(
                        f"ALTER TABLE blofin_demo_orders ADD COLUMN {column_name} {column_type}"
                    )
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_execution_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    client_order_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_equity_snapshots (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    equity_usdt REAL NOT NULL,
                    sampled_at INTEGER NOT NULL,
                    utc_day TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_autopilot_state (
                    state_key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_single_shot_lock (
                    lock_name TEXT PRIMARY KEY,
                    max_lifetime_orders INTEGER NOT NULL,
                    consumed_orders INTEGER NOT NULL,
                    client_order_id TEXT,
                    updated_at INTEGER NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_candle_reservations (
                    instrument_id TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    candle_open_utc TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (instrument_id, timeframe, candle_open_utc)
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS challenge1_v2_position_exit_state (
                    position_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    instrument_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    initial_stop_loss TEXT NOT NULL,
                    original_size TEXT NOT NULL,
                    remaining_size TEXT NOT NULL,
                    tp1_executed INTEGER NOT NULL DEFAULT 0,
                    candles_elapsed INTEGER NOT NULL DEFAULT 0,
                    exit_order_id TEXT,
                    sl_tpsl_id TEXT,
                    status TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_blofin_demo_events_order
                ON blofin_demo_execution_events(client_order_id, sequence)
            """)
            self._db.execute("""
                CREATE TRIGGER IF NOT EXISTS blofin_demo_events_no_update
                BEFORE UPDATE ON blofin_demo_execution_events
                BEGIN SELECT RAISE(ABORT, 'BloFin Demo execution events are append-only'); END
            """)
            self._db.execute("""
                CREATE TRIGGER IF NOT EXISTS blofin_demo_events_no_delete
                BEFORE DELETE ON blofin_demo_execution_events
                BEGIN SELECT RAISE(ABORT, 'BloFin Demo execution events are append-only'); END
            """)
        # The decision journal shares the order-ledger connection and lock,
        # making a decision and its order reservation one transaction.
        self.learning_memory = LearningExperienceMemory(self._db, lock=self._lock)

    def record_challenge1_decision(
        self, *, instrument_id: str, challenge_record: Mapping[str, Any],
    ) -> str:
        """Persist one closed-candle Challenge 1 decision as an immutable event."""
        if not isinstance(challenge_record, Mapping):
            raise TypeError("challenge_record must be a mapping")
        experience_id = _challenge1_experience_id(instrument_id, challenge_record)
        evidence = challenge_record.get("challenge1_evidence")
        evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
        signal = str(challenge_record.get("signal") or "NO_TRADE").strip().upper()
        signal_bar = str(challenge_record.get("signal_bar_open_utc") or "")
        decision = {
            "signal": signal,
            "side": signal if signal in {"LONG", "SHORT"} else None,
            "signal_id": challenge_record.get("signal_id"),
            "entry_price": challenge_record.get("entry_price_usd"),
            "stop_loss": challenge_record.get("stop_loss_usd"),
            "take_profit": challenge_record.get("take_profit_usd"),
            "reason": str(challenge_record.get("reason_fa") or ""),
        }
        input_snapshot = {
            "instrument_id": str(instrument_id).strip().upper(),
            "signal_bar_open_utc": signal_bar,
            "setup_timeframe": str(evidence.get("setup_timeframe") or "5m"),
            "context_timeframe": evidence.get("context_timeframe"),
            "completed_candles_only": evidence.get("completed_candles_only") is True,
            "future_data_in_input": False,
            "challenge1_evidence": evidence,
        }
        with self._lock, self._db:
            self.learning_memory.record_decision(
                experience_id=experience_id,
                event_id=experience_id + "-decision",
                engine_id="BLOFIN_DEMO_CHALLENGE1",
                challenge_id="CHALLENGE_1_BLOFIN_DEMO",
                cycle_id=experience_id,
                decision_mode="BOT_ONLY",
                environment="BLOFIN_DEMO",
                decision=decision,
                input_snapshot=input_snapshot,
                market="CEX_FUTURES",
                symbol=str(instrument_id).strip().upper(),
                timeframe="5M",
                model_version=str(evidence.get("decision_engine_version") or "CHALLENGE_1_LOCAL_RULES_V4_5M_ONLY"),
                dataset_version="BLOFIN_DEMO_COMPLETED_CANDLES",
            )
        return experience_id

    def record_challenge1_v2_decision(
        self, *, instrument_id: str, challenge_record: Mapping[str, Any],
    ) -> str:
        """Persist one immutable V2 1H/4H/15m decision with its funding proof."""
        if not isinstance(challenge_record, Mapping):
            raise TypeError("challenge_record must be a mapping")
        ready, reason = challenge1_v2_execution_evidence_ready(challenge_record)
        if not ready:
            raise ValueError("Challenge 1 V2 evidence cannot be journalled as a qualified decision: " + reason)
        quorum_ready, quorum_reason = _challenge1_two_reviewer_approval_ready(challenge_record)
        if not quorum_ready:
            raise ValueError("Challenge 1 V2 decision lacks two independent live approvals: " + quorum_reason)
        experience_id = _challenge1_v2_experience_id(instrument_id, challenge_record)
        evidence = challenge_record.get("challenge1_v2_evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("Challenge 1 V2 evidence mapping is missing.")
        signal = str(challenge_record.get("signal") or "NO_TRADE").strip().upper()
        signal_bar = str(evidence.get("signal_candle_open_utc") or "")
        decision = {
            "signal": signal,
            "side": signal if signal in {"LONG", "SHORT"} else None,
            "signal_id": challenge_record.get("signal_id"),
            "entry_price": challenge_record.get("entry"),
            "stop_loss": challenge_record.get("stop_loss"),
            "take_profit": challenge_record.get("take_profit"),
            "confidence_score": challenge_record.get("confidence_score"),
            "reason": str(challenge_record.get("reason") or ""),
        }
        input_snapshot = {
            "instrument_id": str(instrument_id).strip().upper(),
            "signal_bar_open_utc": signal_bar,
            "setup_timeframe": "15m",
            "context_timeframes": ["1H", "4H"],
            "completed_candles_only": True,
            "data_asof_utc": evidence.get("data_asof_utc"),
            "future_data_in_input": False,
            "challenge1_v2_evidence": dict(evidence),
            "ai_consensus": dict(challenge_record.get("ai_consensus") or {}),
            "ai_reviews": [
                {
                    "reviewer_id": str(item.get("reviewer_id") or ""),
                    "review": dict(item.get("review") or {}),
                }
                for item in challenge_record.get("ai_reviews", [])
                if isinstance(item, Mapping) and isinstance(item.get("review"), Mapping)
            ],
            "funding_rate": challenge_record.get("funding_rate"),
            "funding_kind": challenge_record.get("funding_kind"),
            "funding_event_time_ms": challenge_record.get("funding_event_time_ms"),
            "funding_max_age_ms": challenge_record.get("funding_max_age_ms"),
            "features": dict(challenge_record.get("features") or {}),
        }
        with self._lock, self._db:
            self.learning_memory.record_decision(
                experience_id=experience_id,
                event_id=experience_id + "-decision",
                engine_id="BLOFIN_DEMO_CHALLENGE1_V2",
                challenge_id="CHALLENGE_1_V2_BLOFIN_DEMO",
                cycle_id=experience_id,
                decision_mode="BOT_ONLY",
                environment="BLOFIN_DEMO",
                decision=decision,
                input_snapshot=input_snapshot,
                market="CEX_FUTURES",
                symbol=str(instrument_id).strip().upper(),
                timeframe="15M",
                model_version=str(evidence.get("decision_engine_version") or "CHALLENGE1_V2_1H_4H_15M"),
                dataset_version="BLOFIN_DEMO_V2_COMPLETED_CANDLES_AND_FUNDING",
            )
        return experience_id


    def record_challenge1_analysis_failure(
        self, *, cycle_id: str, instrument_id: str, failure_stage: str,
        error_type: str, reason: str, timestamp_utc: str | None = None,
    ) -> str:
        """Persist a failed analysis attempt as UNKNOWN/NOT_EXECUTED without fabricated market data."""
        cycle_key = str(cycle_id or "").strip()
        instrument = str(instrument_id or "").strip().upper()
        stage = str(failure_stage or "ANALYSIS_FAILED").strip().upper()
        kind = str(error_type or "UNKNOWN").strip()[:80]
        detail = str(reason or "")[:240]
        if not cycle_key or not instrument:
            raise ValueError("cycle_id and instrument_id are required")
        identity = hashlib.sha256(
            f"challenge1-analysis-failure:{cycle_key}:{instrument}".encode("utf-8")
        ).hexdigest()
        experience_id = "ch1-failure-" + identity[:32]
        occurred_at = timestamp_utc or datetime.now(timezone.utc).isoformat()
        decision = {
            "signal": "NO_TRADE",
            "side": None,
            "decision_class": "DATA_INSUFFICIENT",
            "reason": detail,
        }
        input_snapshot = {
            "instrument_id": instrument,
            "analysis_attempt_id": cycle_key,
            "analysis_attempted_at_utc": occurred_at,
            "setup_timeframe": "5m",
            "context_timeframe": None,
            "data_quality": "UNKNOWN",
            "analysis_completed": False,
            "failure_stage": stage,
            "error_type": kind,
            "candle_timestamp_utc": None,
            "ohlcv_available": False,
            "future_data_in_input": False,
        }
        with self._lock, self._db:
            self.learning_memory.record_decision(
                experience_id=experience_id,
                event_id=experience_id + "-decision",
                engine_id="BLOFIN_DEMO_CHALLENGE1",
                challenge_id="CHALLENGE_1_BLOFIN_DEMO",
                cycle_id=cycle_key,
                decision_mode="BOT_ONLY",
                environment="BLOFIN_DEMO",
                decision=decision,
                input_snapshot=input_snapshot,
                market="CEX_FUTURES",
                symbol=instrument,
                timeframe="5M",
                model_version="CHALLENGE_1_LOCAL_RULES_V4_5M_ONLY",
                dataset_version="BLOFIN_DEMO_ANALYSIS_FAILURE",
                timestamp_utc=occurred_at,
            )
            self.learning_memory.record_outcome(
                experience_id=experience_id,
                event_id=experience_id + "-outcome",
                decision_correctness="UNKNOWN",
                realized_result="NOT_EXECUTED",
                realized_net_pnl=None,
                pnl_currency="USDT",
                final=True,
                evidence={
                    "source": "BLOFIN_DEMO_CHALLENGE1_ANALYSIS_FAILURE",
                    "instrument_id": instrument,
                    "failure_stage": stage,
                    "error_type": kind,
                    "reason": detail,
                    "orders_submitted": False,
                    "trade_executed": False,
                    "realized_pnl_source": "NONE_NOT_EXECUTED",
                },
                timestamp_utc=occurred_at,
            )
        return experience_id

    def record_experience_not_executed(
        self, *, experience_id: str, instrument_id: str, signal: str,
        stage: str, reason: str,
    ) -> dict[str, Any]:
        """Close a decision with an explicit no-order label; never attach floating PnL."""
        evidence = {
            "source": "BLOFIN_DEMO_FINAL_EXECUTION_DECISION",
            "instrument_id": str(instrument_id).strip().upper(),
            "signal": str(signal or "NO_TRADE").strip().upper(),
            "execution_stage": str(stage or "NO_TRADE").strip().upper(),
            "reason": str(reason or "")[:500],
            "orders_submitted": False,
            "trade_executed": False,
            "realized_pnl_source": "NONE_NOT_EXECUTED",
        }
        with self._lock, self._db:
            return self.learning_memory.record_outcome(
                experience_id=experience_id,
                event_id=experience_id + "-not-executed",
                decision_correctness="UNKNOWN",
                realized_result="NOT_EXECUTED",
                realized_net_pnl=None,
                pnl_currency="USDT",
                final=True,
                evidence=evidence,
            )

    def _append_event(self, client_order_id: str, event_type: str, details: Mapping[str, Any]) -> None:
        now = int(time.time())
        key = f"{client_order_id}:{event_type}"
        payload = json.dumps(dict(details), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        event_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
        self._db.execute(
            """INSERT OR IGNORE INTO blofin_demo_execution_events
            (event_id, idempotency_key, client_order_id, event_type, occurred_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, key, client_order_id, event_type, now, payload),
        )

    def single_shot_lock_status(self) -> dict[str, Any]:
        """Return the durable one-shot reservation state without changing it."""
        row = self._db.execute(
            "SELECT lock_name, max_lifetime_orders, consumed_orders, "
            "client_order_id, updated_at FROM blofin_demo_single_shot_lock "
            "WHERE lock_name='SINGLE_SHOT_DEMO_TEST'"
        ).fetchone()
        if row is None:
            return {
                "enabled": False, "locked": False, "max_lifetime_orders": 1,
                "consumed_orders": 0, "client_order_id": None,
            }
        maximum = int(row["max_lifetime_orders"])
        consumed = int(row["consumed_orders"])
        return {
            "enabled": True, "locked": consumed >= maximum,
            "max_lifetime_orders": maximum, "consumed_orders": consumed,
            "client_order_id": row["client_order_id"],
            "updated_at": row["updated_at"],
        }

    def reset_single_shot_lock(self, *, manual_confirmation: bool = False) -> dict[str, Any]:
        """Reset the lifetime lock only after an explicit manual operator confirmation."""
        if manual_confirmation is not True:
            raise BloFinDemoExecutionBlocked(
                "Single-shot lock reset requires explicit manual_confirmation=True."
            )
        now = int(time.time())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """INSERT INTO blofin_demo_single_shot_lock
                       (lock_name, max_lifetime_orders, consumed_orders, client_order_id, updated_at)
                       VALUES ('SINGLE_SHOT_DEMO_TEST', 1, 0, NULL, ?)
                       ON CONFLICT(lock_name) DO UPDATE SET
                         max_lifetime_orders=1, consumed_orders=0,
                         client_order_id=NULL, updated_at=excluded.updated_at""",
                    (now,),
                )
                reset_event_id = f"SINGLE_SHOT_DEMO_TEST_RESET_{now}_{uuid.uuid4().hex[:8]}"
                self._append_event(
                    reset_event_id, "SINGLE_SHOT_LOCK_RESET",
                    {"manual_confirmation": True, "max_lifetime_orders": 1},
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self.single_shot_lock_status()

    def reserve_order(
        self,
        plan: Mapping[str, Any],
        *,
        max_concurrent_positions: int = 1,
        external_open_positions: int = 0,
        external_pending_orders: int = 0,
        single_shot: bool = False,
        v2_autopilot: bool = False,
        max_lifetime_orders: int = 1,
    ) -> bool:
        """Atomically reserve one account-wide slot across independent DB connections.

        BEGIN IMMEDIATE is intentional: the capacity read and order/event inserts
        must share one serialized write transaction. A process-local lock is not
        sufficient when several Render instances share Turso.
        """
        for value, field in (
            (max_concurrent_positions, "max_concurrent_positions"),
            (external_open_positions, "external_open_positions"),
            (external_pending_orders, "external_pending_orders"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer.")
        if not 1 <= max_concurrent_positions <= 4:
            raise ValueError("max_concurrent_positions must be between 1 and 4.")
        if single_shot and (
            isinstance(max_lifetime_orders, bool)
            or not isinstance(max_lifetime_orders, int)
            or max_lifetime_orders != 1
        ):
            raise ValueError("Single-shot Demo execution is restricted to max_lifetime_orders=1.")

        now = int(time.time())
        order_id = str(plan["client_order_id"])
        try:
            new_risk = _decimal(plan.get("reserved_risk_usdt"), "reserved_risk_usdt")
            new_margin = _decimal(plan.get("reserved_margin_usdt"), "reserved_margin_usdt")
            leverage_value = _decimal(plan.get("leverage"), "leverage")
            if leverage_value != leverage_value.to_integral_value() or leverage_value <= 0:
                raise ValueError("leverage must be a positive integer")
            expected_notional = new_margin * leverage_value
            notional_value = _decimal(
                plan.get("notional_usdt", expected_notional), "notional_usdt"
            )
            risk_cap = _decimal(plan.get("aggregate_risk_limit_usdt"), "aggregate_risk_limit_usdt")
            margin_cap = _decimal(plan.get("aggregate_margin_limit_usdt"), "aggregate_margin_limit_usdt")
        except Exception as exc:
            raise BloFinDemoExecutionBlocked(
                "Validated aggregate risk and margin reservations are required."
            ) from exc
        if min(new_risk, new_margin, notional_value, risk_cap, margin_cap) <= 0:
            raise BloFinDemoExecutionBlocked("Aggregate risk, margin and notional reservations must be positive.")
        if abs(notional_value - expected_notional) > max(expected_notional, Decimal("1")) * Decimal("0.00000001"):
            raise BloFinDemoExecutionBlocked("Notional does not reconcile to reserved margin multiplied by leverage.")

        active_statuses = (
            "'RESERVED','LEVERAGE_SET','PENDING_RECONCILE','UNKNOWN',"
            "'ACCEPTED','PARTIALLY_FILLED','FILLED','CRITICAL_UNPROTECTED'"
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT client_order_id FROM blofin_demo_orders "
                    "WHERE signal_id=? OR client_order_id=?",
                    (str(plan["signal_id"]), order_id),
                ).fetchone()
                if existing is not None:
                    self._db.rollback()
                    return False

                v2_controls_enabled = (
                    bool(v2_autopilot)
                    and str(plan.get("execution_profile") or "").strip().upper() == "CHALLENGE_1_V2"
                    and os.getenv("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED", "").strip().lower() == "true"
                )
                candle_guard = None
                if v2_controls_enabled:
                    candle_guard = (
                        str(plan.get("candle_guard_instrument_id") or "").strip().upper(),
                        str(plan.get("candle_guard_timeframe") or "").strip().lower(),
                        str(plan.get("candle_guard_open_utc") or "").strip(),
                    )
                    if not all(candle_guard):
                        self._db.rollback()
                        return False
                lock_row = self._db.execute(
                    "SELECT max_lifetime_orders, consumed_orders FROM "
                    "blofin_demo_single_shot_lock WHERE lock_name='SINGLE_SHOT_DEMO_TEST'"
                ).fetchone() if not v2_controls_enabled else None
                if lock_row is not None:
                    try:
                        lock_maximum = int(lock_row["max_lifetime_orders"])
                        lock_consumed = int(lock_row["consumed_orders"])
                    except (TypeError, ValueError, OverflowError):
                        self._db.rollback()
                        return False
                    if lock_maximum != 1 or lock_consumed >= lock_maximum or lock_consumed < 0:
                        self._db.rollback()
                        return False
                # The lifetime one-shot lock remains available to legacy/manual
                # execution only. V2 uses durable signal_id uniqueness, an active-
                # instrument duplicate guard, and the account-wide four-slot cap.
                # Never consume or re-check the legacy lifetime lock for V2.
                legacy_single_shot = bool(single_shot and not v2_controls_enabled)
                if legacy_single_shot:
                    if lock_row is None:
                        self._db.execute(
                            """INSERT INTO blofin_demo_single_shot_lock
                               (lock_name, max_lifetime_orders, consumed_orders, client_order_id, updated_at)
                               VALUES ('SINGLE_SHOT_DEMO_TEST', 1, 1, ?, ?)""",
                            (order_id, now),
                        )
                    else:
                        self._db.execute(
                            """UPDATE blofin_demo_single_shot_lock
                               SET consumed_orders=consumed_orders+1,
                                   client_order_id=?, updated_at=?
                               WHERE lock_name='SINGLE_SHOT_DEMO_TEST'
                                 AND consumed_orders=0 AND max_lifetime_orders=1""",
                            (order_id, now),
                        )
                        if self._db.execute("SELECT changes()").fetchone()[0] != 1:
                            self._db.rollback()
                            return False

                active_rows = self._db.execute(
                    f"""SELECT client_order_id, instrument_id, reserved_risk_usdt, reserved_margin_usdt
                        FROM blofin_demo_orders WHERE status IN ({active_statuses})"""
                ).fetchall()
                candidate_symbol = str(plan.get("inst_id") or "").strip().upper()
                if v2_controls_enabled and any(
                    str(row["instrument_id"] or "").strip().upper() == candidate_symbol
                    for row in active_rows
                ):
                    self._db.rollback()
                    return False
                local_active = len(active_rows)
                # Reconcile exchange positions against durable V2 reservations. Any
                # untracked exchange position/order remains a hard blocker.
                if external_open_positions > local_active or external_pending_orders > 0:
                    self._db.rollback()
                    return False
                observed_total = max(local_active, external_open_positions) + external_pending_orders
                if observed_total >= max_concurrent_positions:
                    self._db.rollback()
                    return False

                reserved_risk = Decimal("0")
                reserved_margin = Decimal("0")
                for row in active_rows:
                    try:
                        row_risk = _decimal(row["reserved_risk_usdt"], "existing_reserved_risk")
                        row_margin = _decimal(row["reserved_margin_usdt"], "existing_reserved_margin")
                    except Exception:
                        # Legacy active orders without exposure snapshots cannot be
                        # safely added to the aggregate budget. Preserve them and block.
                        self._db.rollback()
                        return False
                    if row_risk <= 0 or row_margin <= 0:
                        self._db.rollback()
                        return False
                    reserved_risk += row_risk
                    reserved_margin += row_margin

                if reserved_risk + new_risk > risk_cap or reserved_margin + new_margin > margin_cap:
                    self._db.rollback()
                    return False

                if candle_guard is not None:
                    self._db.execute(
                        """INSERT OR IGNORE INTO blofin_demo_candle_reservations
                           (instrument_id, timeframe, candle_open_utc, signal_id, client_order_id, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (*candle_guard, str(plan["signal_id"]), order_id, now),
                    )
                    if self._db.execute("SELECT changes()").fetchone()[0] != 1:
                        self._db.rollback()
                        return False

                self._db.execute(
                    """INSERT INTO blofin_demo_orders
                    (client_order_id, signal_id, instrument_id, side, quantity, entry_price,
                     stop_loss, take_profit, exchange_tp_required, leverage, status, exchange_order_id, experience_id,
                     position_side, filled_size, critical_reason,
                     reserved_risk_usdt, reserved_margin_usdt, notional_usdt, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)""",
                    (
                        order_id, str(plan["signal_id"]), str(plan["inst_id"]), str(plan["side"]),
                        str(plan["quantity"]), float(plan["entry_reference_price"]),
                        float(plan["stop_loss"]), float(plan["take_profit"]),
                        int(bool(plan.get("exchange_tp_required", True))), int(plan["leverage"]),
                        str(plan.get("experience_id") or "") or None,
                        str(plan.get("position_side") or plan.get("position_side_long_short") or "net"),
                        _format_decimal(new_risk), _format_decimal(new_margin),
                        _format_decimal(notional_value), now, now,
                    ),
                )
                self._append_event(order_id, "RESERVED", {
                    "instrument_id": plan["inst_id"], "side": plan["side"],
                    "quantity": plan["quantity"], "leverage": plan["leverage"],
                    "reserved_risk_usdt": _format_decimal(new_risk),
                    "reserved_margin_usdt": _format_decimal(new_margin),
                    "notional_usdt": _format_decimal(notional_value),
                    "aggregate_reserved_risk_usdt_after": _format_decimal(reserved_risk + new_risk),
                    "aggregate_reserved_margin_usdt_after": _format_decimal(reserved_margin + new_margin),
                    "aggregate_risk_limit_usdt": _format_decimal(risk_cap),
                    "aggregate_margin_limit_usdt": _format_decimal(margin_cap),
                    "learning_experience_id_present": bool(plan.get("experience_id")),
                    "single_shot_lock_claimed": bool(legacy_single_shot),
                    "single_shot_max_lifetime_orders": 1 if legacy_single_shot else None,
                    "candle_guard": ({
                        "instrument_id": candle_guard[0],
                        "timeframe": candle_guard[1],
                        "candle_open_utc": candle_guard[2],
                    } if candle_guard is not None else None),
                    "capacity_snapshot": {
                        "local_active_reservations": local_active,
                        "exchange_open_positions": external_open_positions,
                        "exchange_pending_orders": external_pending_orders,
                        "max_concurrent_positions": max_concurrent_positions,
                    },
                })
                self._db.commit()
                return True
            except Exception:
                self._db.rollback()
                raise

    def update_order(
        self, client_order_id: str, status: str, *,
        exchange_order_id: str | None = None,
        position_id: str | None = None,
        filled_size: str | None = None,
        critical_reason: str | None = None,
    ) -> None:
        allowed = {
            "LEVERAGE_SET", "PENDING_RECONCILE", "ACCEPTED", "PARTIALLY_FILLED",
            "FILLED", "CRITICAL_UNPROTECTED", "CANCELED", "REJECTED",
            "UNKNOWN", "CLOSED",
        }
        if status not in allowed:
            raise ValueError("Unsupported order status update.")
        if filled_size is not None:
            filled_size = _format_decimal(_decimal(filled_size, "filled_size"))
        now = int(time.time())
        with self._lock, self._db:
            self._db.execute(
                """UPDATE blofin_demo_orders
                   SET status=?,
                       exchange_order_id=COALESCE(?, exchange_order_id),
                       position_id=COALESCE(?, position_id),
                       filled_size=COALESCE(?, filled_size),
                       critical_reason=COALESCE(?, critical_reason),
                       updated_at=?
                   WHERE client_order_id=?""",
                (status, exchange_order_id, position_id, filled_size, critical_reason, now, client_order_id),
            )
            self._append_event(client_order_id, status, {
                "exchange_order_id_present": bool(exchange_order_id),
                "position_id_present": bool(position_id),
                "filled_size": filled_size,
                "critical_reason": critical_reason,
            })

    def save_v2_position_exit_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Durably upsert the V2 exit state for one exact exchange position."""
        required = (
            "position_id", "client_order_id", "instrument_id", "side",
            "entry_price", "initial_stop_loss", "original_size", "remaining_size",
        )
        if not isinstance(state, Mapping) or any(not str(state.get(key) or "").strip() for key in required):
            raise ValueError("V2 exit state is missing required position identity or sizing fields.")
        position_id = str(state["position_id"]).strip()
        client_order_id = str(state["client_order_id"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", position_id):
            raise ValueError("position_id is invalid.")
        BloFinDemoTradeClient._exit_client_id(client_order_id)
        instrument_id = str(state["instrument_id"]).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", instrument_id):
            raise ValueError("instrument_id is invalid.")
        side = str(state["side"]).strip().upper()
        if side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT.")
        entry = _decimal(state["entry_price"], "entry_price")
        stop = _decimal(state["initial_stop_loss"], "initial_stop_loss")
        original = _decimal(state["original_size"], "original_size")
        remaining = _decimal(state["remaining_size"], "remaining_size")
        if min(entry, stop, original, remaining) <= 0 or remaining > original:
            raise ValueError("V2 exit state prices or sizes are outside the permitted range.")
        tp1 = state.get("tp1_executed") is True
        bars = state.get("candles_elapsed", 0)
        if isinstance(bars, bool) or not isinstance(bars, int) or bars < 0:
            raise ValueError("candles_elapsed must be a non-negative integer.")
        exit_order_id = str(state.get("exit_order_id") or "").strip() or None
        sl_tpsl_id = str(state.get("sl_tpsl_id") or "").strip() or None
        status = str(state.get("status") or "OPEN").strip().upper()
        if status not in {"OPEN", "TP1_PENDING", "TP1_EXECUTED", "TIME_EXIT_PENDING", "CLOSED", "HALTED"}:
            raise ValueError("Unsupported V2 exit state status.")
        safe = dict(state)
        safe.update({
            "position_id": position_id, "client_order_id": client_order_id,
            "instrument_id": instrument_id, "side": side,
            "entry_price": _format_decimal(entry),
            "initial_stop_loss": _format_decimal(stop),
            "original_size": _format_decimal(original),
            "remaining_size": _format_decimal(remaining),
            "tp1_executed": tp1, "candles_elapsed": bars,
            "exit_order_id": exit_order_id, "sl_tpsl_id": sl_tpsl_id,
            "status": status,
        })
        now = int(time.time())
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT client_order_id, instrument_id, side FROM challenge1_v2_position_exit_state WHERE position_id=?",
                (position_id,),
            ).fetchone()
            if existing is not None and (
                str(existing["client_order_id"]) != client_order_id
                or str(existing["instrument_id"]) != instrument_id
                or str(existing["side"]).upper() != side
            ):
                raise BloFinDemoExecutionBlocked("V2 exit state identity cannot be reassigned to another order.")
            self._db.execute("""
                INSERT INTO challenge1_v2_position_exit_state (
                    position_id, client_order_id, instrument_id, side, entry_price,
                    initial_stop_loss, original_size, remaining_size, tp1_executed,
                    candles_elapsed, exit_order_id, sl_tpsl_id, status, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    remaining_size=excluded.remaining_size,
                    tp1_executed=excluded.tp1_executed,
                    candles_elapsed=excluded.candles_elapsed,
                    exit_order_id=COALESCE(excluded.exit_order_id, challenge1_v2_position_exit_state.exit_order_id),
                    sl_tpsl_id=COALESCE(excluded.sl_tpsl_id, challenge1_v2_position_exit_state.sl_tpsl_id),
                    status=excluded.status, updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
            """, (
                position_id, client_order_id, instrument_id, side,
                safe["entry_price"], safe["initial_stop_loss"], safe["original_size"],
                safe["remaining_size"], int(tp1), bars, exit_order_id, sl_tpsl_id,
                status, now, json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str),
            ))
        return safe

    def get_v2_position_exit_state(self, position_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM challenge1_v2_position_exit_state WHERE position_id=?",
                (str(position_id),),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["payload"] = json.loads(result.pop("payload_json"))
        except (TypeError, ValueError):
            raise BloFinDemoExecutionBlocked("Persisted V2 exit state JSON is corrupt.")
        result["tp1_executed"] = bool(result["tp1_executed"])
        return result

    def list_v2_position_exit_states(self, *, active_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        query = (
            "SELECT position_id FROM challenge1_v2_position_exit_state "
            + ("WHERE status != 'CLOSED' " if active_only else "")
            + "ORDER BY updated_at DESC LIMIT ?"
        )
        with self._lock:
            ids = self._db.execute(query, (limit,)).fetchall()
        return [
            state for row in ids
            if (state := self.get_v2_position_exit_state(str(row["position_id"]))) is not None
        ]

    def equity_guard(self, equity_usdt: float, *, now: int | None = None) -> dict[str, float]:
        equity = _finite(equity_usdt, "equity_usdt", minimum=0.0000001)
        sampled_at = int(time.time()) if now is None else int(now)
        utc_day = time.strftime("%Y-%m-%d", time.gmtime(sampled_at))
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO blofin_demo_equity_snapshots(equity_usdt, sampled_at, utc_day) VALUES (?, ?, ?)",
                (equity, sampled_at, utc_day),
            )
            peak_row = self._db.execute(
                "SELECT MAX(equity_usdt) FROM blofin_demo_equity_snapshots"
            ).fetchone()
            day_row = self._db.execute(
                "SELECT equity_usdt FROM blofin_demo_equity_snapshots WHERE utc_day=? ORDER BY sampled_at ASC, sequence ASC LIMIT 1",
                (utc_day,),
            ).fetchone()
        peak = max(equity, float(peak_row[0] or equity))
        day_open = float(day_row[0] or equity)
        return {
            "equity_usdt": equity,
            "day_open_equity_usdt": day_open,
            "peak_equity_usdt": peak,
            "daily_loss_pct": max(0.0, (day_open - equity) / day_open),
            "drawdown_pct": max(0.0, (peak - equity) / peak),
        }

    def has_any_orders(self) -> bool:
        """Return whether this Demo service has ever reserved an order."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM blofin_demo_orders LIMIT 1"
            ).fetchone()
        return row is not None

    def verify_durable_round_trip(self, *, connections: int = 4) -> dict[str, Any]:
        """Verify Turso write/read visibility and serialized writes across independent connections."""
        if self.backend != "TURSO_CLOUD_SQLITE":
            return {
                "verified": False,
                "reason": "DURABLE_TURSO_NOT_CONFIGURED",
                "orders_submitted": False,
            }
        if isinstance(connections, bool) or not isinstance(connections, int) or not 2 <= connections <= 8:
            raise ValueError("connections must be between 2 and 8.")

        probe_id = "runtime-probe-" + uuid.uuid4().hex
        payload = uuid.uuid4().hex
        created_at = int(time.time())
        setup = None
        try:
            setup = connect_database()
            setup.execute("""
                CREATE TABLE IF NOT EXISTS blofin_demo_storage_probes (
                    probe_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)
            setup.commit()
        finally:
            if setup is not None:
                setup.close()

        barrier = threading.Barrier(connections)

        def write_once(_: int) -> str:
            connection = connect_database()
            try:
                barrier.wait(timeout=15)
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT payload FROM blofin_demo_storage_probes WHERE probe_id=?",
                    (probe_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO blofin_demo_storage_probes(probe_id,payload,created_at) VALUES (?,?,?)",
                        (probe_id, payload, created_at),
                    )
                    connection.commit()
                    return "inserted"
                connection.commit()
                return "observed"
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise
            finally:
                connection.close()

        inserted = 0
        try:
            with ThreadPoolExecutor(max_workers=connections) as pool:
                outcomes = list(pool.map(write_once, range(connections)))
            inserted = outcomes.count("inserted")
            verifier = connect_database()
            try:
                rows = verifier.execute(
                    "SELECT payload FROM blofin_demo_storage_probes WHERE probe_id=?",
                    (probe_id,),
                ).fetchall()
                read_back = len(rows) == 1 and str(rows[0]["payload"]) == payload
                if inserted != 1 or not read_back:
                    raise RuntimeError("Turso multi-connection probe did not produce one durable row.")
                verifier.execute("BEGIN IMMEDIATE")
                verifier.execute(
                    "DELETE FROM blofin_demo_storage_probes WHERE probe_id=?",
                    (probe_id,),
                )
                verifier.commit()
            finally:
                verifier.close()
            return {
                "verified": True,
                "connections": connections,
                "writers_won": inserted,
                "read_back": True,
                "cleanup": True,
                "orders_submitted": False,
            }
        except Exception as exc:
            cleanup = None
            try:
                cleanup = connect_database()
                cleanup.execute("BEGIN IMMEDIATE")
                cleanup.execute(
                    "DELETE FROM blofin_demo_storage_probes WHERE probe_id=?",
                    (probe_id,),
                )
                cleanup.commit()
            except Exception:
                try:
                    if cleanup is not None:
                        cleanup.rollback()
                except Exception:
                    pass
            finally:
                if cleanup is not None:
                    cleanup.close()
            return {
                "verified": False,
                "connections": connections,
                "writers_won": inserted,
                "error_type": type(exc).__name__,
                "cleanup": False,
                "orders_submitted": False,
            }

    def save_autopilot_state(self, state: Mapping[str, Any]) -> None:
        """Persist the latest operational AutoPilot snapshot in the configured durable store."""
        if not isinstance(state, Mapping):
            raise TypeError("AutoPilot state must be a mapping.")
        state_json = json.dumps(
            dict(state), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        now = int(time.time())
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO blofin_demo_autopilot_state(state_key, state_json, updated_at)
                VALUES ('default', ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at""",
                (state_json, now),
            )

    def load_autopilot_state(self) -> dict[str, Any] | None:
        """Load the last operational AutoPilot snapshot, if one was saved."""
        with self._lock:
            row = self._db.execute(
                "SELECT state_json FROM blofin_demo_autopilot_state WHERE state_key='default'"
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["state_json"])
        except (TypeError, ValueError, KeyError):
            return None
        return payload if isinstance(payload, dict) else None

    def has_signal(self, signal_id: str) -> bool:
        """Return whether this exact completed-candle signal has ever been reserved."""
        if not isinstance(signal_id, str) or not signal_id.strip():
            return False
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM blofin_demo_orders WHERE signal_id=? LIMIT 1",
                (signal_id.strip(),),
            ).fetchone()
        return row is not None

    def trade_budget_status(self, max_orders_per_utc_day: int | None = None, *, now: int | None = None) -> dict[str, Any]:
        """Report Demo order history without imposing a quota unless explicitly configured."""
        sampled_at = int(time.time()) if now is None else int(now)
        utc_day_start = sampled_at - (sampled_at % 86400)
        with self._lock:
            today_row = self._db.execute(
                "SELECT COUNT(*) FROM blofin_demo_orders WHERE created_at>=?",
                (utc_day_start,),
            ).fetchone()
            total_row = self._db.execute(
                "SELECT COUNT(*) FROM blofin_demo_orders"
            ).fetchone()
            latest_row = self._db.execute(
                "SELECT created_at, status FROM blofin_demo_orders ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            unresolved_row = self._db.execute(
                "SELECT COUNT(*) FROM blofin_demo_orders "
                "WHERE status IN ('UNKNOWN','RESERVED','LEVERAGE_SET','PENDING_RECONCILE',"
                "'ACCEPTED','PARTIALLY_FILLED','CRITICAL_UNPROTECTED')"
            ).fetchone()
            active_exposure_rows = self._db.execute(
                """SELECT reserved_risk_usdt, reserved_margin_usdt
                   FROM blofin_demo_orders
                   WHERE status IN (
                       'RESERVED','LEVERAGE_SET','PENDING_RECONCILE','UNKNOWN',
                       'ACCEPTED','PARTIALLY_FILLED','FILLED','CRITICAL_UNPROTECTED'
                   )"""
            ).fetchall()
        orders_today = int(today_row[0] or 0)
        orders_total = int(total_row[0] or 0)
        daily_limit = None if max_orders_per_utc_day is None else int(max_orders_per_utc_day)
        latest_created_at = int(latest_row["created_at"]) if latest_row is not None else None
        latest_status = str(latest_row["status"]) if latest_row is not None else None
        age = max(0, sampled_at - latest_created_at) if latest_created_at is not None else None
        aggregate_risk = Decimal("0")
        aggregate_margin = Decimal("0")
        aggregate_exposure_complete = True
        for row in active_exposure_rows:
            try:
                row_risk = _decimal(row["reserved_risk_usdt"], "reserved_risk")
                row_margin = _decimal(row["reserved_margin_usdt"], "reserved_margin")
                if row_risk <= 0 or row_margin <= 0:
                    raise ValueError("Non-positive reservation.")
                aggregate_risk += row_risk
                aggregate_margin += row_margin
            except Exception:
                aggregate_exposure_complete = False
                break
        return {
            "orders_today": orders_today,
            "orders_total": orders_total,
            "max_orders_per_utc_day": daily_limit,
            "daily_capacity_available": daily_limit is None or orders_today < daily_limit,
            "latest_order_age_seconds": age,
            "latest_order_status": latest_status,
            "unresolved_orders": int(unresolved_row[0] or 0),
            "aggregate_reserved_risk_usdt": _format_decimal(aggregate_risk) if aggregate_exposure_complete else None,
            "aggregate_reserved_margin_usdt": _format_decimal(aggregate_margin) if aggregate_exposure_complete else None,
            "aggregate_exposure_complete": aggregate_exposure_complete,
            "active_reservations": len(active_exposure_rows),
            "utc_day_start": utc_day_start,
        }

    def latest_order(self) -> dict[str, Any] | None:
        """Return the newest reserved order without exposing account credentials."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM blofin_demo_orders ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_order_for_instrument(self, instrument_id: str) -> dict[str, Any] | None:
        """Return the newest local order for one exact contract, never another symbol."""
        normalized = str(instrument_id or "").strip().upper()
        if not normalized:
            raise ValueError("instrument_id is required.")
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM blofin_demo_orders
                   WHERE UPPER(instrument_id)=? ORDER BY created_at DESC LIMIT 1""",
                (normalized,),
            ).fetchone()
        return dict(row) if row is not None else None

    def recent_events(self, client_order_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return append-only lifecycle events for the selected Demo order."""
        bounded = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._db.execute(
                """SELECT sequence, event_type, occurred_at, payload_json
                FROM blofin_demo_execution_events
                WHERE client_order_id=? ORDER BY sequence DESC LIMIT ?""",
                (client_order_id, bounded),
            ).fetchall()
        events = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                payload = {}
            events.append({
                "sequence": row["sequence"], "event_type": row["event_type"],
                "occurred_at": row["occurred_at"], "payload": payload,
            })
        return events

    def recent_all_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent append-only lifecycle events across every local Demo order."""
        bounded = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._db.execute(
                """SELECT sequence, client_order_id, event_type, occurred_at, payload_json
                FROM blofin_demo_execution_events ORDER BY sequence DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
        events = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                payload = {}
            events.append({
                "sequence": row["sequence"],
                "client_order_id": row["client_order_id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "payload": payload,
            })
        return events

    def list_unresolved_orders(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """List every order whose exchange outcome still requires read-only reconciliation."""
        if isinstance(limit, bool):
            raise ValueError("limit must be an integer between 1 and 100.")
        try:
            bounded = int(limit)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("limit must be an integer between 1 and 100.") from exc
        if bounded != limit or not 1 <= bounded <= 100:
            raise ValueError("limit must be an integer between 1 and 100.")
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM blofin_demo_orders
                   WHERE status IN (
                       'UNKNOWN','RESERVED','LEVERAGE_SET','PENDING_RECONCILE',
                       'ACCEPTED','PARTIALLY_FILLED','CRITICAL_UNPROTECTED'
                   )
                   ORDER BY created_at ASC, client_order_id ASC
                   LIMIT ?""",
                (bounded,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM blofin_demo_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_order_for_experience(self, experience_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM blofin_demo_orders WHERE experience_id=? ORDER BY created_at DESC LIMIT 1",
                (experience_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def has_final_experience(self, experience_id: str) -> bool:
        """Return whether this closed-candle decision already has an immutable outcome."""
        experience = self.learning_memory.get_experience(experience_id)
        return any(event.get("event_type") == "OUTCOME_LABELLED" for event in experience["events"])

    def record_closed_position_outcome(
        self, *, instrument_id: str, history_rows: Any,
        partial_rows: Any = None,
        client_order_id: str | None = None,
        position_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record a final Demo outcome only when the exact local order/position identity is supplied."""
        instrument = str(instrument_id or "").strip().upper()
        client_order_key = str(client_order_id or "").strip()
        requested_position_id = str(position_id or "").strip()
        if not instrument:
            return {"status": "INVALID_INSTRUMENT_ID"}
        if not client_order_key and not requested_position_id:
            return {
                "status": "IDENTITY_REQUIRED",
                "reason_code": "CLIENT_ORDER_ID_OR_POSITION_ID_REQUIRED",
            }

        with self._lock:
            if client_order_key:
                order_row = self._db.execute(
                    "SELECT * FROM blofin_demo_orders WHERE client_order_id=? AND instrument_id=? "
                    "AND experience_id IS NOT NULL "
                    "AND status IN ('ACCEPTED','PARTIALLY_FILLED','FILLED','CLOSED')",
                    (client_order_key, instrument),
                ).fetchone()
            else:
                matched_orders = self._db.execute(
                    "SELECT * FROM blofin_demo_orders WHERE position_id=? AND instrument_id=? "
                    "AND experience_id IS NOT NULL "
                    "AND status IN ('ACCEPTED','PARTIALLY_FILLED','FILLED','CLOSED') "
                    "ORDER BY created_at DESC LIMIT 2",
                    (requested_position_id, instrument),
                ).fetchall()
                if len(matched_orders) > 1:
                    return {
                        "status": "AMBIGUOUS_LOCAL_POSITION_ID",
                        "position_id": requested_position_id,
                        "match_count_at_least": 2,
                    }
                order_row = matched_orders[0] if matched_orders else None

            if order_row is None:
                return {
                    "status": "LOCAL_ORDER_NOT_FOUND",
                    "instrument_id": instrument,
                    "client_order_id": client_order_key or None,
                    "position_id": requested_position_id or None,
                }
            order = dict(order_row)
            saved_position_id = str(order.get("position_id") or "").strip()
            if requested_position_id and saved_position_id != requested_position_id:
                return {
                    "status": "POSITION_ID_MISMATCH",
                    "experience_id": order.get("experience_id"),
                    "client_order_id": order.get("client_order_id"),
                    "expected_position_id": saved_position_id or None,
                    "received_position_id": requested_position_id,
                }
            if order.get("outcome_recorded_at") is not None:
                return {
                    "status": "ALREADY_RECORDED",
                    "experience_id": order.get("experience_id"),
                    "position_id": saved_position_id or None,
                    "client_order_id": order.get("client_order_id"),
                    "order_id": order.get("exchange_order_id"),
                }

        position_id = str(order.get("position_id") or "").strip()
        if not position_id:
            return {
                "status": "WAITING_FOR_EXCHANGE_POSITION_LINK",
                "experience_id": order.get("experience_id"),
                "client_order_id": order.get("client_order_id"),
            }
        expected_side = "buy" if str(order.get("side") or "").upper() == "LONG" else "sell"
        try:
            order_created_ms = int(order.get("created_at") or 0) * 1000
            reference_entry = float(order.get("entry_price"))
            expected_quantity = float(order.get("quantity"))
        except (TypeError, ValueError, OverflowError):
            return {"status": "WAITING_FOR_VALID_LOCAL_ORDER"}
        if not all(math.isfinite(value) and value > 0 for value in (
            reference_entry, expected_quantity
        )):
            return {"status": "WAITING_FOR_VALID_LOCAL_ORDER"}

        # The final close record alone may represent only the last partial fill.
        # Sum partial-close and full-close history for this exact exchange positionId.
        combined_rows = []
        for state, source_rows in (
            ("PARTIALLY_CLOSED", partial_rows or []),
            ("CLOSED", history_rows or []),
        ):
            for row in source_rows:
                if not isinstance(row, Mapping):
                    continue
                if (
                    str(row.get("positionId") or "") != position_id
                    or str(row.get("instId") or "").upper() != instrument
                    or str(row.get("side") or "").lower() != expected_side
                ):
                    continue
                try:
                    opened_ms = int(row.get("createTime") or 0)
                    open_price = float(row.get("openAveragePrice"))
                    close_quantity = float(row.get("closePositions"))
                    total_quantity = float(row.get("maxPositions"))
                    pnl = float(row.get("realizedPnl"))
                    history_id = str(row.get("historyId") or "").strip()
                except (TypeError, ValueError, OverflowError):
                    continue
                raw_fee = row.get("fee")
                try:
                    fee = float(raw_fee) if raw_fee is not None and str(raw_fee).strip() else None
                except (TypeError, ValueError, OverflowError):
                    fee = None
                if fee is not None and not math.isfinite(fee):
                    fee = None
                values = (open_price, close_quantity, total_quantity, pnl)
                if not all(math.isfinite(value) for value in values):
                    continue
                if (
                    opened_ms < order_created_ms - 60_000
                    or open_price <= 0
                    or abs(open_price - reference_entry) / reference_entry > 0.005
                    or close_quantity <= 0
                    or total_quantity + 1e-9 < close_quantity
                    or not history_id
                ):
                    continue
                combined_rows.append({
                    "history_id": history_id,
                    "state": state,
                    "close_quantity": close_quantity,
                    "realized_pnl": pnl,
                    "trading_fee": fee,
                    "position_row": dict(row),
                })
        if not any(item["state"] == "CLOSED" for item in combined_rows):
            return {
                "status": "WAITING_FOR_FINAL_CLOSED_POSITION",
                "experience_id": order.get("experience_id"),
                "client_order_id": order.get("client_order_id"),
                "position_id": position_id,
                "matched_partial_rows": len(combined_rows),
            }
        quantity_tolerance = max(1e-9, abs(expected_quantity) * 1e-6)
        total_closed = sum(item["close_quantity"] for item in combined_rows)
        if abs(total_closed - expected_quantity) > quantity_tolerance:
            return {
                "status": "WAITING_FOR_FULL_POSITION_SIZE_RECONCILIATION",
                "experience_id": order.get("experience_id"),
                "client_order_id": order.get("client_order_id"),
                "position_id": position_id,
                "expected_quantity": expected_quantity,
                "observed_closed_quantity": total_closed,
            }
        missing_fee_history_ids = sorted({
            item["history_id"] for item in combined_rows
            if item["trading_fee"] is None
        })
        if missing_fee_history_ids:
            return {
                "status": "WAITING_FOR_EXCHANGE_FEE_DATA",
                "experience_id": order.get("experience_id"),
                "client_order_id": order.get("client_order_id"),
                "position_id": position_id,
                "missing_fee_history_ids": missing_fee_history_ids,
                "message": "Realized PnL after fees is not calculated when any matched exchange fee is missing.",
            }
        history_ids = sorted({item["history_id"] for item in combined_rows})
        realized_pnl = sum(item["realized_pnl"] for item in combined_rows)
        trading_fees = sum(float(item["trading_fee"]) for item in combined_rows)
        net_before_funding = realized_pnl - trading_fees
        if not math.isfinite(net_before_funding):
            raise ValueError("Closed-position PnL after reported trading fees is not finite.")
        result = (
            "PROFIT" if net_before_funding > 1e-8
            else "LOSS" if net_before_funding < -1e-8
            else "BREAKEVEN"
        )
        evidence = {
            "source": "BLOFIN_DEMO_POSITION_ID_LINKED_HISTORY",
            "instrument_id": instrument,
            "position_id": position_id,
            "client_order_id": str(order.get("client_order_id") or ""),
            "history_ids": history_ids,
            "closed_history_ids": [
                item["history_id"] for item in combined_rows if item["state"] == "CLOSED"
            ],
            "partial_history_ids": [
                item["history_id"] for item in combined_rows if item["state"] == "PARTIALLY_CLOSED"
            ],
            "original_order_id": order.get("exchange_order_id"),
            "expected_quantity": expected_quantity,
            "observed_closed_quantity": total_closed,
            "exchange_realized_pnl_before_trading_fees": realized_pnl,
            "exchange_trading_fees": trading_fees,
            "realized_pnl_after_reported_trading_fees": net_before_funding,
            "funding_fees_included": False,
            "funding_fee_attribution": "NOT_YET_RECONCILED",
            "decision_correctness": "UNKNOWN_PENDING_INDEPENDENT_REVIEW",
            "mark_to_market_pnl_used": False,
            "orders_submitted": True,
            "trade_executed": True,
        }
        experience_id = str(order["experience_id"])
        now = int(time.time())
        with self._lock, self._db:
            latest = self._db.execute(
                "SELECT outcome_recorded_at FROM blofin_demo_orders WHERE client_order_id=?",
                (order["client_order_id"],),
            ).fetchone()
            if latest is None or latest["outcome_recorded_at"] is not None:
                return {
                    "status": "ALREADY_RECORDED",
                    "experience_id": experience_id,
                    "position_id": position_id,
                    "client_order_id": order.get("client_order_id"),
                }
            saved = self.learning_memory.record_outcome(
                experience_id=experience_id,
                event_id=experience_id + "-outcome",
                decision_correctness="UNKNOWN",
                realized_result=result,
                realized_net_pnl=net_before_funding,
                pnl_currency="USDT",
                final=True,
                evidence=evidence,
            )
            self._db.execute(
                "UPDATE blofin_demo_orders SET status='CLOSED', outcome_recorded_at=?, "
                "position_history_id=?, updated_at=? WHERE client_order_id=?",
                (now, ",".join(history_ids), now, order["client_order_id"]),
            )
            self._append_event(order["client_order_id"], "CLOSED", {
                "experience_id": experience_id,
                "position_id": position_id,
                "history_ids": history_ids,
            })
            self._append_event(order["client_order_id"], "OUTCOME_RECORDED", {
                "experience_id": experience_id,
                "position_id": position_id,
                "history_ids": history_ids,
                "realized_result": result,
                "realized_pnl_after_reported_trading_fees": net_before_funding,
                "funding_fee_attribution": "NOT_YET_RECONCILED",
            })
        return {
            "status": "RECORDED",
            "experience_id": experience_id,
            "position_id": position_id,
            "client_order_id": str(order.get("client_order_id") or ""),
            "order_id": str(order.get("exchange_order_id") or ""),
            "history_ids": history_ids,
            "realized_result": result,
            "realized_pnl_after_reported_trading_fees": net_before_funding,
            "funding_fee_attribution": "NOT_YET_RECONCILED",
            "learning_memory_status": saved.get("event_type"),
        }

    def orders_pending_learning_outcome(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM blofin_demo_orders
                   WHERE experience_id IS NOT NULL AND experience_id != ''
                     AND position_id IS NOT NULL AND position_id != ''
                     AND outcome_recorded_at IS NULL
                     AND status IN ('ACCEPTED','PARTIALLY_FILLED','FILLED')
                   ORDER BY updated_at ASC LIMIT ?""",
                (bounded,),
            ).fetchall()
        return [dict(row) for row in rows]


    def pending_learning_outcome_count(self) -> int:
        """Count executed Demo experiences still awaiting exchange-finalized outcome data."""
        with self._lock:
            row = self._db.execute(
                """SELECT COUNT(*) FROM blofin_demo_orders
                   WHERE experience_id IS NOT NULL AND experience_id != ''
                     AND outcome_recorded_at IS NULL
                     AND status IN ('ACCEPTED','PARTIALLY_FILLED','FILLED')"""
            ).fetchone()
        return int(row[0] or 0)

    def attach_position_id(self, experience_id: str, position_id: str) -> bool:
        experience_key = str(experience_id or "").strip()
        position_key = str(position_id or "").strip()
        if not experience_key or not position_key:
            return False
        with self._lock, self._db:
            order = self.get_order_for_experience(experience_key)
            if order is None:
                return False
            existing = str(order.get("position_id") or "")
            if existing and existing != position_key:
                raise BloFinDemoExecutionBlocked(
                    "A different exchange position is already linked to this learning experience."
                )
            self._db.execute(
                "UPDATE blofin_demo_orders SET position_id=?, updated_at=? WHERE client_order_id=?",
                (position_key, int(time.time()), order["client_order_id"]),
            )
            self._append_event(
                str(order["client_order_id"]), "POSITION_LINKED",
                {"position_id": position_key},
            )
        return True

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _equity_from_balance(balance: Any) -> float:
    if isinstance(balance, list) and balance and isinstance(balance[0], Mapping):
        balance = balance[0]
    if not isinstance(balance, Mapping):
        raise BloFinDemoExecutionBlocked("Futures balance response is unavailable.")
    for key in ("totalEquity", "equity", "accountEquity"):
        if balance.get(key) not in (None, ""):
            return _finite(balance[key], "futures_equity", minimum=0.0000001)
    raise BloFinDemoExecutionBlocked("Futures equity field was not present in the API response.")


def _active_position_count(positions: Any) -> int:
    if not isinstance(positions, list):
        raise BloFinDemoExecutionBlocked("Positions response is not a list.")
    count = 0
    for row in positions:
        if not isinstance(row, Mapping):
            raise BloFinDemoExecutionBlocked("Position row is not interpretable.")
        raw = next((row[k] for k in ("positions", "pos", "positionSize", "positionAmt") if k in row), None)
        if raw is None:
            raise BloFinDemoExecutionBlocked("Position size field is missing; refusing to guess.")
        try:
            size = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BloFinDemoExecutionBlocked("Position size field is invalid.") from exc
        if not math.isfinite(size):
            raise BloFinDemoExecutionBlocked("Position size field is non-finite.")
        if abs(size) > 1e-12:
            count += 1
    return count


def _active_order_count(orders: Any) -> int:
    if not isinstance(orders, list):
        raise BloFinDemoExecutionBlocked("Pending-orders response is not a list.")
    return len(orders)


class BloFinDemoExecutor:
    """Conservative Demo executor; it does not run automatically from dashboard scans."""

    def __init__(
        self,
        *,
        client: BloFinDemoTradeClient | None = None,
        store: BloFinDemoEventStore | None = None,
        policy: BloFinDemoRiskPolicy | None = None,
        trading_enabled: bool | None = None,
    ) -> None:
        self.client = client or BloFinDemoTradeClient()
        self.store = store or BloFinDemoEventStore()
        self.policy = policy or BloFinDemoRiskPolicy()
        configured = os.getenv("BLOFIN_DEMO_TRADING_ENABLED", "").strip().lower() == "true"
        self.trading_enabled = configured if trading_enabled is None else bool(trading_enabled)

    @staticmethod
    def _response_rows(value: Any) -> list[Mapping[str, Any]]:
        if isinstance(value, Mapping):
            if isinstance(value.get("data"), list):
                value = value["data"]
            elif isinstance(value.get("data"), Mapping):
                value = [value["data"]]
            else:
                value = [value]
        if not isinstance(value, list):
            return []
        return [row for row in value if isinstance(row, Mapping)]

    @staticmethod
    def _same_decimal(left: Any, right: Any, field: str) -> bool:
        try:
            return _decimal(left, field) == _decimal(right, field)
        except Exception:
            return False

    def _verify_filled_position_and_protection(
        self, *, order: Mapping[str, Any], detail: Mapping[str, Any],
        filled_size: Decimal, client_order_id: str,
    ) -> dict[str, Any]:
        """Read-only exact-position and TP/SL verification; never repairs by writing."""
        inst_id = str(order.get("instrument_id") or "").upper()
        side = str(order.get("side") or "").upper()
        expected_position_side = str(order.get("position_side") or "net").lower()
        expected_tp = str(order.get("take_profit") or "")
        tp_required = bool(order.get("exchange_tp_required", 1))
        expected_sl = str(order.get("stop_loss") or "")
        detail_position_id = str(detail.get("positionId") or "").strip()
        try:
            position_rows = self._response_rows(self.client.get_positions(inst_id=inst_id))
        except Exception as exc:
            return {"verified": False, "reason": "POSITION_READ_FAILED",
                    "error_type": type(exc).__name__, "position_id": None}
        matching_positions = []
        for row in position_rows:
            if str(row.get("instId") or "").upper() != inst_id:
                continue
            if str(row.get("positionSide") or "").lower() != expected_position_side:
                continue
            position_id = str(row.get("positionId") or "").strip()
            if not position_id or position_id == "0":
                continue
            if detail_position_id and position_id != detail_position_id:
                continue
            try:
                signed_size = _decimal(row.get("positions"), "position_size")
            except Exception:
                continue
            if signed_size == 0 or abs(signed_size) != filled_size:
                continue
            if expected_position_side == "net":
                if (side == "LONG" and signed_size <= 0) or (side == "SHORT" and signed_size >= 0):
                    continue
            elif expected_position_side != side.lower():
                continue
            matching_positions.append(position_id)
        if len(matching_positions) != 1:
            return {"verified": False, "reason": "EXACT_POSITION_ID_NOT_PROVEN",
                    "position_id": None, "matching_position_count": len(matching_positions)}
        position_id = matching_positions[0]
        close_side = "sell" if side == "LONG" else "buy"
        try:
            tpsl_rows = self._response_rows(self.client.get_open_tpsl_orders(
                inst_id=inst_id, limit=100,
            ))
        except Exception as exc:
            return {"verified": False, "reason": "TPSL_READ_FAILED",
                    "error_type": type(exc).__name__, "position_id": position_id}
        tp_verified = False
        sl_verified = False
        matched_rows = 0
        for row in tpsl_rows:
            if str(row.get("instId") or "").upper() != inst_id:
                continue
            if str(row.get("positionSide") or "").lower() != expected_position_side:
                continue
            if str(row.get("marginMode") or "").lower() != "isolated":
                continue
            if str(row.get("side") or "").lower() != close_side:
                continue
            if str(row.get("state") or "").lower() != "live":
                continue
remove client order id requirement            raw_sizes = [v for v in (row.get("size"), row.get("actualSize")) if v not in (None, "")]
            if not raw_sizes or any(not self._same_decimal(v, filled_size, "tpsl_size") for v in raw_sizes):
                continue
            row_tp, row_sl = row.get("tpTriggerPrice"), row.get("slTriggerPrice")
            category, generic = str(row.get("orderCategory") or "").lower(), row.get("triggerPrice")
            tp_type = str(row.get("tpTriggerPriceType") or row.get("triggerPriceType") or "").lower()
            sl_type = str(row.get("slTriggerPriceType") or row.get("triggerPriceType") or "").lower()
            if row_tp not in (None, ""):
                if self._same_decimal(row_tp, expected_tp, "tp_trigger") and tp_type == "mark":
                    tp_verified = True
            elif category == "tp" and generic not in (None, ""):
                if self._same_decimal(generic, expected_tp, "tp_trigger") and tp_type == "mark":
                    tp_verified = True
            if row_sl not in (None, ""):
                if self._same_decimal(row_sl, expected_sl, "sl_trigger") and sl_type == "mark":
                    sl_verified = True
            elif category == "sl" and generic not in (None, ""):
                if self._same_decimal(generic, expected_sl, "sl_trigger") and sl_type == "mark":
                    sl_verified = True
            if tp_verified or sl_verified:
                matched_rows += 1
        return {
            "verified": sl_verified and (tp_verified if tp_required else True),
            "reason": ("VERIFIED" if sl_verified and (tp_verified if tp_required else True)
                       else "TP_OR_SL_MISSING_OR_MISMATCHED" if tp_required else "SL_MISSING_OR_MISMATCHED"),
            "position_id": position_id, "tp_verified": tp_verified,
            "tp_required": tp_required, "sl_verified": sl_verified, "matched_tpsl_rows": matched_rows,
            "filled_size": _format_decimal(filled_size),
        }

    def reconcile_unknown_order(self, *, inst_id: str, client_order_id: str) -> dict[str, Any]:
        """Read-only order reconciliation, including fill, exact position and both protective triggers."""
        order = self.store.get_order(client_order_id)
        if order is None or str(order.get("instrument_id") or "").upper() != inst_id.upper():
            raise BloFinDemoExecutionBlocked("No matching locally reserved order exists.")
        if order.get("status") in {"REJECTED", "CANCELED", "CLOSED"}:
            return {"status": order["status"], "client_order_id": client_order_id}
        try:
            detail_value = self.client.get_order_detail(inst_id, client_order_id=client_order_id)
        except Exception as exc:
            self.store.update_order(client_order_id, "PENDING_RECONCILE")
            return {"status": "PENDING_RECONCILE", "client_order_id": client_order_id,
                    "reconciliation": "ORDER_DETAIL_READ_FAILED",
                    "error_type": type(exc).__name__, "orders_submitted": False}
        detail_rows = self._response_rows(detail_value)
        detail = next((row for row in detail_rows if str(row.get("clientOrderId") or "") == client_order_id), None)
        if detail is None:
            self.store.update_order(client_order_id, "PENDING_RECONCILE")
            return {"status": "PENDING_RECONCILE", "client_order_id": client_order_id,
                    "reconciliation": "NO_EXACT_ORDER_DETAIL_MATCH", "orders_submitted": False}
        exchange_order_id = str(detail.get("orderId") or "").strip()
        state = str(detail.get("state") or "").lower()
        expected_side = "buy" if str(order.get("side") or "").upper() == "LONG" else "sell"
        if (not exchange_order_id
            or str(detail.get("instId") or "").upper() != inst_id.upper()
            or str(detail.get("side") or "").lower() != expected_side):
            self.store.update_order(client_order_id, "PENDING_RECONCILE")
            return {"status": "PENDING_RECONCILE", "client_order_id": client_order_id,
                    "reconciliation": "ORDER_IDENTITY_MISMATCH", "orders_submitted": False}
        known_exchange_id = str(order.get("exchange_order_id") or "").strip()
        if known_exchange_id and known_exchange_id != exchange_order_id:
            self.store.update_order(client_order_id, "PENDING_RECONCILE")
            return {"status": "PENDING_RECONCILE", "client_order_id": client_order_id,
                    "reconciliation": "EXCHANGE_ORDER_ID_MISMATCH", "orders_submitted": False}
        try:
            requested_size = _decimal(order.get("quantity"), "requested_size")
            raw_filled = detail.get("filledSize")
            if raw_filled in (None, ""):
                raise ValueError("filledSize is missing")
            filled_size = _decimal(raw_filled, "filledSize")
        except Exception:
            self.store.update_order(client_order_id, "PENDING_RECONCILE", exchange_order_id=exchange_order_id)
            return {"status": "PENDING_RECONCILE", "client_order_id": client_order_id,
                    "exchange_order_id": exchange_order_id,
                    "reconciliation": "FILL_QUANTITY_UNAVAILABLE", "orders_submitted": False}
        if filled_size < 0 or requested_size <= 0 or filled_size > requested_size:
            self.store.update_order(client_order_id, "PENDING_RECONCILE", exchange_order_id=exchange_order_id)
            return {"status": "PENDING_RECONCILE", "client_order_id": client_order_id,
                    "exchange_order_id": exchange_order_id,
                    "reconciliation": "INVALID_FILL_QUANTITY", "orders_submitted": False}
        if filled_size == 0:
            if state in {"order_failed", "rejected", "failed"}:
                mapped = "REJECTED"
            elif state == "canceled":
                mapped = "CANCELED"
            elif state == "live":
                mapped = "ACCEPTED"
            else:
                mapped = "PENDING_RECONCILE"
            self.store.update_order(client_order_id, mapped, exchange_order_id=exchange_order_id, filled_size="0")
            return {"status": mapped, "environment": "BLOFIN_DEMO",
                    "client_order_id": client_order_id, "exchange_order_id": exchange_order_id,
                    "filled_size": "0", "reconciliation": "READ_ONLY", "orders_submitted": False}
        verification = self._verify_filled_position_and_protection(
            order=order, detail=detail, filled_size=filled_size,
            client_order_id=client_order_id,
        )
        if not verification.get("verified"):
            reason = str(verification.get("reason") or "POST_FILL_VERIFICATION_FAILED")
            self.store.update_order(
                client_order_id, "CRITICAL_UNPROTECTED",
                exchange_order_id=exchange_order_id, position_id=verification.get("position_id"),
                filled_size=_format_decimal(filled_size), critical_reason=reason,
            )
            return {"status": "CRITICAL_UNPROTECTED", "environment": "BLOFIN_DEMO",
                    "client_order_id": client_order_id, "exchange_order_id": exchange_order_id,
                    "filled_size": _format_decimal(filled_size), "position_id": verification.get("position_id"),
                    "protection": verification, "reconciliation": "READ_ONLY_FAIL_CLOSED",
                    "new_entries_blocked": True, "orders_submitted": False}
        mapped = "FILLED" if filled_size == requested_size else "PARTIALLY_FILLED"
        self.store.update_order(
            client_order_id, mapped, exchange_order_id=exchange_order_id,
            position_id=verification["position_id"], filled_size=_format_decimal(filled_size),
            critical_reason="",
        )
        return {"status": mapped, "environment": "BLOFIN_DEMO",
                "client_order_id": client_order_id, "exchange_order_id": exchange_order_id,
                "filled_size": _format_decimal(filled_size), "requested_size": _format_decimal(requested_size),
                "position_id": verification["position_id"], "protection": verification,
                "reconciliation": "READ_ONLY_VERIFIED", "orders_submitted": False}

    def reconcile_unresolved_orders(self, *, limit: int = 50) -> dict[str, Any]:
        """Read-only sweep of every locally unresolved order; never creates or retries orders."""
        unresolved = self.store.list_unresolved_orders(limit=limit)
        results: list[dict[str, Any]] = []
        for order in unresolved:
            client_order_id = str(order.get("client_order_id") or "")
            inst_id = str(order.get("instrument_id") or "").upper()
            if not client_order_id or not inst_id:
                results.append({
                    "status": "UNKNOWN",
                    "client_order_id": client_order_id or None,
                    "reconciliation": "INVALID_LOCAL_ORDER_IDENTITY",
                })
                continue
            try:
                result = self.reconcile_unknown_order(
                    inst_id=inst_id,
                    client_order_id=client_order_id,
                )
            except Exception as exc:
                result = {
                    "status": "UNKNOWN",
                    "client_order_id": client_order_id,
                    "reconciliation": "READ_FAILED",
                    "error_type": type(exc).__name__,
                }
            results.append(result)

        remaining = self.store.trade_budget_status().get("unresolved_orders")
        try:
            remaining_count = int(remaining or 0)
        except (TypeError, ValueError, OverflowError):
            remaining_count = -1
        return {
            "status": "RECONCILED" if remaining_count == 0 else "UNRESOLVED",
            "reconciliation": "READ_ONLY",
            "attempted": len(unresolved),
            "remaining_unresolved": remaining_count,
            "orders_submitted": False,
            "results": results,
        }

    def recover_unresolved_orders_from_journal(self, *, limit: int = 50) -> dict[str, Any]:
        """Startup hook: reconcile unresolved durable journal entries using GET-only calls."""
        return self.reconcile_unresolved_orders(limit=limit)

    def execute(
        self,
        signal: Any,
        *,
        inst_id: str,
        leverage: int = 1,
        allow_demo_orders: bool = False,
        minimum_size_only: bool = False,
        cancel_check: Any | None = None,
        submission_lock: Any | None = None,
        single_shot: bool | None = None,
    ) -> dict[str, Any]:
        single_shot_enabled = (
            single_shot is True
            or os.getenv("SINGLE_SHOT_DEMO_TEST", "").strip().lower() == "true"
        )
        if not allow_demo_orders or not self.trading_enabled:
            raise BloFinDemoExecutionBlocked("Demo order execution is disabled by the two-key safety gate.")
        if self.client.base_url != DEMO_REST_URL:
            raise BloFinDemoExecutionBlocked("Only the pinned BloFin Demo host is permitted.")
        if self.store.backend != "TURSO_CLOUD_SQLITE":
            raise BloFinDemoExecutionBlocked("Durable Turso storage is required before Demo trading.")
        record = _analysis_record(signal)
        execution_profile = str(record.get("execution_profile") or "").strip().upper()
        is_challenge1 = execution_profile in {"CHALLENGE_1", "CHALLENGE_1_V2"}
        is_challenge1_v2 = execution_profile == "CHALLENGE_1_V2"
        if is_challenge1:
            quorum_ready, quorum_reason = _challenge1_two_reviewer_approval_ready(record)
            if not quorum_ready:
                raise BloFinDemoExecutionBlocked(
                    "Challenge 1 two-reviewer AI gate blocked Demo entry: " + quorum_reason
                )
        # Challenge 1 has no daily order-count quota: the completed-5m candle key
        # is the idempotency boundary. Keep daily loss/drawdown and exposure guards.
        daily_order_limit = None if is_challenge1 else self.policy.max_orders_per_utc_day
        budget = self.store.trade_budget_status(daily_order_limit)
        if (
            not is_challenge1
            and daily_order_limit is not None
            and budget["orders_today"] >= daily_order_limit
        ):
            raise BloFinDemoExecutionBlocked("The configured daily Demo order limit has been reached.")
        if budget["unresolved_orders"] > 0:
            raise BloFinDemoExecutionBlocked("An earlier Demo order has an unresolved outcome; reconcile it before another entry.")
        v2_autopilot_enabled = (
            is_challenge1_v2
            and os.getenv("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED", "").strip().lower() == "true"
        )
        single_shot_status = self.store.single_shot_lock_status()
        if single_shot_status.get("locked") and not v2_autopilot_enabled:
            raise BloFinDemoExecutionBlocked(
                "Single-shot Demo lifetime order allowance is already consumed; manual reset is required."
            )
        # A pending realized-outcome label is an audit/learning task, not proof of
        # current exchange exposure. Do not let an incomplete history record freeze
        # every market indefinitely. The fresh account-wide positions and open-orders
        # checks below remain the hard entry blockers; unresolved order submissions
        # are still blocked above and must be reconciled before another entry.
        latest_age = budget.get("latest_order_age_seconds")
        if (
            not is_challenge1
            and self.policy.min_seconds_between_orders > 0
            and latest_age is not None
            and latest_age < self.policy.min_seconds_between_orders
        ):
            raise BloFinDemoExecutionBlocked("The minimum cooldown between Demo orders has not elapsed.")
        if not all((
            self.client.api_key, self.client.api_secret, self.client.passphrase
        )):
            raise BloFinDemoExecutionBlocked("Separate BLOFIN_DEMO_TRADE_* credentials are required.")
        try:
            self.client.verify_trade_permission()
        except BloFinDemoExecutionBlocked:
            raise
        except Exception as exc:
            raise BloFinDemoExecutionBlocked(
                "BloFin Trade permission could not be verified; no order was sent."
            ) from exc
        if int(leverage) > self.policy.max_leverage or int(leverage) < 1:
            raise BloFinDemoExecutionBlocked("Leverage is outside the 1x-3x Demo test range.")

        clock = self.client.check_clock_skew(inst_id=inst_id)
        if abs(int(clock.get("offset_ms", 10**12))) >= 60_000:
            raise BloFinDemoExecutionBlocked("Exchange/local clock skew is 60 seconds or more.")

        ticker_rows = self.client.get_tickers(inst_id=inst_id)
        if not isinstance(ticker_rows, list) or not ticker_rows or not isinstance(ticker_rows[0], Mapping):
            raise BloFinDemoExecutionBlocked("Fresh contract ticker is unavailable.")
        last_price = _finite(ticker_rows[0].get("last"), "last_price", minimum=0.0000001)
        try:
            best_bid = _decimal(ticker_rows[0].get("bidPrice"), "best_bid")
            best_ask = _decimal(ticker_rows[0].get("askPrice"), "best_ask")
        except Exception as exc:
            raise BloFinDemoExecutionBlocked("BloFin ticker has no valid best bid/ask; cost gate cannot run.") from exc
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            raise BloFinDemoExecutionBlocked("BloFin best bid/ask quote is invalid; no order was sent.")
        market_quote = {"bidPrice": str(best_bid), "askPrice": str(best_ask)}
        signal_entry = _finite(
            record.get("entry_price_usd", record.get("entry_price", record.get("entry"))),
            "entry_price",
            minimum=0.0000001,
        )
        deviation = abs(last_price - signal_entry) / signal_entry
        if deviation > self.policy.max_entry_deviation_pct:
            raise BloFinDemoExecutionBlocked("Market price moved too far from the signal entry reference.")

        mode = self.client.get_position_mode()
        position_mode = mode.get("positionMode")
        if position_mode != "net_mode":
            raise BloFinDemoExecutionBlocked(
                "Demo order execution requires explicit One-Way net_mode; Hedge mode is rejected."
            )
        instrument = self.client.get_tradable_instrument(inst_id)
        balance = self.client.get_futures_balance(product_type="USDT-FUTURES")
        equity = _equity_from_balance(balance)
        guard = self.store.equity_guard(equity)
        if guard["daily_loss_pct"] >= self.policy.daily_loss_limit_pct:
            raise BloFinDemoExecutionBlocked("Daily equity-loss limit has been reached.")
        if guard["drawdown_pct"] >= self.policy.max_drawdown_pct:
            raise BloFinDemoExecutionBlocked("Maximum account drawdown limit has been reached.")

        positions = self.client.get_positions()
        # Account-wide pending orders can fill on another symbol and consume the
        # concurrency/margin budget, so the safety check is intentionally global.
        pending = self.client.get_open_orders()
        effective_position_limit = (
            4 if v2_autopilot_enabled else
            1 if single_shot_enabled else self.policy.max_concurrent_positions
        )
        if v2_autopilot_enabled:
            allowed, capacity_reason = v2_entry_capacity(
                self._response_rows(positions), self._response_rows(pending),
                candidate_symbol=inst_id, max_concurrent_positions=4,
            )
            if not allowed:
                raise BloFinDemoExecutionBlocked("V2 entry capacity blocked: " + capacity_reason)
        if _active_position_count(positions) >= effective_position_limit:
            raise BloFinDemoExecutionBlocked("Maximum concurrent-position limit has been reached.")
        if _active_order_count(pending) > 0:
            raise BloFinDemoExecutionBlocked("Pending orders exist; reconcile them before a new entry.")

        plan = build_demo_order_plan(
            record, instrument, inst_id=inst_id, equity_usdt=equity,
            leverage=int(leverage), policy=self.policy, market_quote=market_quote,
        )
        if is_challenge1:
            experience_id = str(record.get("learning_experience_id") or "").strip()
            if not experience_id:
                if is_challenge1_v2:
                    experience_id = self.store.record_challenge1_v2_decision(
                        instrument_id=inst_id, challenge_record=record,
                    )
                else:
                    # The legacy 5m path retains its own immutable evidence schema.
                    experience_id = self.store.record_challenge1_decision(
                        instrument_id=inst_id, challenge_record=record,
                    )
                record["learning_experience_id"] = experience_id
            plan["experience_id"] = experience_id
        else:
            plan["experience_id"] = str(record.get("learning_experience_id") or "") or None
        if minimum_size_only:
            lot_size = _decimal(instrument.get("lotSize"), "lotSize")
            min_size = _decimal(instrument.get("minSize"), "minSize")
            quantity = (min_size / lot_size).to_integral_value(rounding=ROUND_CEILING) * lot_size
            max_market_size = instrument.get("maxMarketSize")
            if max_market_size not in (None, "") and quantity > _decimal(max_market_size, "maxMarketSize"):
                raise BloFinDemoExecutionBlocked("The contract minimum exceeds its maximum market-order size.")
            contract_value = _decimal(instrument.get("contractValue"), "contractValue")
            entry = _decimal(plan["entry_reference_price"], "entry_reference_price")
            stop = _decimal(plan["stop_loss"], "stop_loss")
            quantity_risk = abs(entry - stop) * contract_value * quantity
            quantity_notional = entry * contract_value * quantity
            quantity_margin = quantity_notional / Decimal(int(leverage))
            if quantity_risk > _decimal(plan["risk_budget_usdt"], "risk_budget_usdt"):
                raise BloFinDemoExecutionBlocked("The minimum contract size exceeds the configured per-trade risk budget.")
            if quantity_margin > Decimal(str(equity)) * Decimal(str(self.policy.max_margin_usage_pct)):
                raise BloFinDemoExecutionBlocked("The minimum contract size exceeds the permitted isolated-margin budget.")
            plan["quantity"] = _format_decimal(quantity)
            plan["risk_per_contract_usdt"] = float(abs(entry - stop) * contract_value)
            plan["notional_usdt"] = float(quantity_notional)
            plan["estimated_initial_margin_usdt"] = float(quantity_margin)
            plan["reserved_risk_usdt"] = _format_decimal(quantity_risk)
            plan["reserved_margin_usdt"] = _format_decimal(quantity_margin)
            if (
                quantity_risk > _decimal(plan["aggregate_risk_limit_usdt"], "aggregate_risk_limit_usdt")
                or quantity_margin > _decimal(plan["aggregate_margin_limit_usdt"], "aggregate_margin_limit_usdt")
            ):
                raise BloFinDemoExecutionBlocked(
                    "Minimum-size exposure exceeds the aggregate risk or margin budget."
                )
            plan["minimum_size_only"] = True
        plan = apply_demo_execution_cost_gate(
            plan, instrument, market_quote, policy=self.policy,
        )
        # CHALLENGE_1_V2 manages TP1 in manage_open_positions(); only the full-size
        # protective SL is exchange-attached. Keep the strategy target in-memory for
        # cost validation, but omit it from the durable exchange-protection record.
        exchange_take_profit = None if is_challenge1_v2 else plan["take_profit"]
        plan["exchange_tp_required"] = not is_challenge1_v2
        position_side = "net" if position_mode == "net_mode" else plan["position_side_long_short"]
        plan["position_side"] = position_side
        if not self.store.reserve_order(
            plan,
            max_concurrent_positions=effective_position_limit,
            external_open_positions=_active_position_count(positions),
            external_pending_orders=_active_order_count(pending),
            single_shot=single_shot_enabled,
            v2_autopilot=v2_autopilot_enabled,
            max_lifetime_orders=1,
        ):
            raise BloFinDemoExecutionBlocked(
                "Duplicate signal, unresolved legacy exposure, external exposure, or aggregate risk/margin budget is already reserved; entry blocked."
            )

        exchange_side = "buy" if plan["side"] == "LONG" else "sell"
        try:
            self.client.set_futures_leverage(
                inst_id, int(leverage), margin_mode="isolated",
                position_side=None if position_mode == "net_mode" else position_side,
            )
            self.store.update_order(plan["client_order_id"], "LEVERAGE_SET")
        except Exception as exc:
            self.store.update_order(plan["client_order_id"], "REJECTED")
            raise BloFinDemoExecutionBlocked("Leverage setup failed; no entry order was submitted.") from exc

        def submit_reserved_order() -> dict[str, Any]:
            # The final cancellation check runs after all slow preflight calls and
            # immediately before the one exchange write. The caller may hold a
            # shared lock so stop requests can distinguish an in-flight write.
            if cancel_check is not None and bool(cancel_check()):
                self.store.update_order(plan["client_order_id"], "REJECTED")
                raise BloFinDemoExecutionBlocked(
                    "Automated entry was stopped before the BloFin Demo order was submitted."
                )
            self.store.update_order(plan["client_order_id"], "PENDING_RECONCILE")
            try:
                return self.client.place_futures_order(
                    inst_id=inst_id,
                    side=exchange_side,
                    position_side=position_side,
                    size=plan["quantity"],
                    client_order_id=plan["client_order_id"],
                    take_profit=exchange_take_profit,
                    stop_loss=plan["stop_loss"],
                    margin_mode="isolated",
                )
            except BloFinDemoOrderRejected as exc:
                self.store.update_order(plan["client_order_id"], "REJECTED")
                raise BloFinDemoExecutionBlocked(
                    "BloFin explicitly rejected the Demo order; no retry was sent."
                ) from exc
            except Exception as exc:
                self.store.update_order(plan["client_order_id"], "PENDING_RECONCILE")
                raise BloFinDemoExecutionUnknown(
                    "Order outcome is unknown after POST. Do not retry; reconcile by clientOrderId."
                ) from exc

        if submission_lock is None:
            response = submit_reserved_order()
        else:
            with submission_lock:
                response = submit_reserved_order()

        data = response.get("data")
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], Mapping) else {}
        if not isinstance(data, Mapping):
            self.store.update_order(plan["client_order_id"], "PENDING_RECONCILE")
            raise BloFinDemoExecutionUnknown("BloFin response had no usable receipt; reconcile before retry.")
        order_id = data.get("orderId")
        if not order_id:
            self.store.update_order(plan["client_order_id"], "PENDING_RECONCILE")
            raise BloFinDemoExecutionUnknown("Order ID is missing; reconcile before retrying.")
        self.store.update_order(
            plan["client_order_id"], "PENDING_RECONCILE", exchange_order_id=str(order_id),
        )
        reconciliation = self.reconcile_unknown_order(
            inst_id=inst_id, client_order_id=plan["client_order_id"],
        )
        if reconciliation["status"] == "PENDING_RECONCILE":
            raise BloFinDemoExecutionUnknown(
                "Post-submit exchange state is not conclusive; journal remains PENDING_RECONCILE."
            )
        return {
            **reconciliation,
            "instrument_id": inst_id,
            "side": plan["side"],
            "quantity_contracts": plan["quantity"],
            "leverage": int(leverage),
            "stop_loss": plan["stop_loss"],
            "take_profit": plan["take_profit"],
            "risk_budget_usdt": plan["risk_budget_usdt"],
            "notional_usdt": plan["notional_usdt"],
            "entry_reference_price": plan["entry_reference_price"],
            "net_reward_risk": plan.get("net_reward_risk"),
            "execution_cost_estimate": plan.get("execution_cost_estimate"),
            "net_break_even_price": plan.get("net_break_even_price"),
            "client_order_id": plan["client_order_id"],
            "entry_price": plan["entry_reference_price"],
            "position_id": reconciliation.get("position_id"),
            "filled_size": reconciliation.get("filled_size"),
            "position_mode": position_mode,
            "orders_enabled": True,
        }
