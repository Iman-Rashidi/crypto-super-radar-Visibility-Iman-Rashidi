"""Independent multi-provider AI supervision for guarded paper/demo entries.

No provider can approve a new entry alone. At least two independent providers
must return a model-verified, schema-validated approval. Any valid veto wins.
Only free-tier model IDs/endpoints are configured here; keep provider accounts
on their Free tiers and never attach billing for this zero-cost configuration.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from . import ai_second_opinion as _ai

MIN_INDEPENDENT_APPROVALS = 2
MAX_PROVIDER_CALLS_PER_UTC_DAY = 30
MAX_PROVIDER_CALLS_PER_MINUTE = 10
_REQUEST_LOCK = threading.Lock()
_DAILY_CALLS: dict[str, int] = {}
_MINUTE_CALLS: dict[str, list[float]] = {}

_PROVIDER_DEFINITIONS = (
    {
        "id": "openrouter",
        "label": "NVIDIA Nemotron 3 Ultra via OpenRouter",
        "model": _ai.DEFAULT_FREE_MODEL,
        "url": _ai.OPENROUTER_URL,
        "env": "OPENROUTER_API_KEY",
        "aliases": ("CSR_OPENROUTER_API_KEY",),
        "tier": "OpenRouter Free / :free model only",
    },
    {
        "id": "gemini",
        "label": "Google Gemini 3.8 Flash",
        "model": "gemini-3.8-flash",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "env": "GEMINI_API_KEY",
        "aliases": (),
        "tier": "Google AI Studio Free tier only; do not link billing",
    },
    {
        "id": "groq",
        "label": "Groq GPT-OSS 120B",
        "model": "openai/gpt-oss-120b",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "env": "GROQ_API_KEY",
        "aliases": (),
        "tier": "Groq Free plan only; do not upgrade to Developer",
    },
)



def _demo_ai_review_approved(review: Any) -> bool:
    """Fail closed unless a real AI review explicitly approves Demo entry."""
    if not isinstance(review, dict):
        return False
    return (
        str(review.get("status") or "").strip().upper() == "APPROVED"
        and review.get("paper_entry_eligible") is True
        and review.get("request_attempted") is True
        and str(review.get("response_source") or "").upper() == "LIVE_API"
        and review.get("response_validated") is True
        and review.get("model_verified") is True
        and bool(str(review.get("model") or "").strip())
        and bool(str(review.get("returned_model") or "").strip())
    )

def _run_two_reviewer_challenge1_supervisor_reviews(
    analysis: Any, asset: Any, *, signal_id: str | None = None,
    primary_reviewer: Callable[..., dict[str, Any]] | None = None,
    second_reviewer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run two independent AI reviews and return a fail-closed Demo entry quorum."""
    signal_value = getattr(analysis, "signal", "UNKNOWN")
    expected_side = str(getattr(signal_value, "value", signal_value) or "UNKNOWN").upper()
    metrics = getattr(analysis, "metrics", {})
    metrics = metrics if isinstance(metrics, dict) else {}
    execution_profile = str(metrics.get("execution_profile") or "").upper()
    if execution_profile == "CHALLENGE_1_V2":
        evidence = metrics.get("challenge1_v2_evidence")
    else:
        evidence = metrics.get("challenge1_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    wave = evidence.get("elliott_wave")
    wave = wave if isinstance(wave, dict) else {}
    expected_wave_phase = str(wave.get("phase") or "").upper()
    expected_wave_direction = str(wave.get("pattern") or "").upper()

    def unavailable(reviewer_id: str, error_type: str) -> dict[str, Any]:
        return {
            "reviewer_id": reviewer_id, "status": "ERROR",
            "request_attempted": None, "response_source": "UNKNOWN",
            "response_validated": False, "model_verified": False,
            "paper_entry_eligible": False, "error_type": error_type,
        }

    def verified(review: dict[str, Any]) -> bool:
        return bool(
            review.get("request_attempted") is True
            and str(review.get("response_source") or "").upper() == "LIVE_API"
            and review.get("response_validated") is True
            and review.get("model_verified") is True
            and str(review.get("model") or "").strip()
            and str(review.get("returned_model") or "").strip()
        )

    # Separate credentials/models are validated inside each adapter. Calls run
    # concurrently; neither reviewer receives the other's response.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="challenge1-review",
    ) as pool:
        primary_future = pool.submit(
            (primary_reviewer or _ai.run_free_ai_second_opinion), analysis, asset, bypass_cache=True,
        )
        second_future = pool.submit((second_reviewer or _ai.run_independent_ai_supervisor), analysis, asset)
        try:
            primary = primary_future.result()
        except Exception as exc:
            primary = unavailable("nemotron", type(exc).__name__)
        try:
            second = second_future.result()
        except Exception as exc:
            second = unavailable("second_ai", type(exc).__name__)

    if not isinstance(primary, dict):
        primary = unavailable("nemotron", "InvalidReviewPayload")
    if not isinstance(second, dict):
        second = unavailable("second_ai", "InvalidReviewPayload")
    primary, second = dict(primary), dict(second)
    primary.setdefault("reviewer_id", "nemotron")
    second.setdefault("reviewer_id", "second_ai")

    reviewed_at_utc = datetime.now(timezone.utc).isoformat()
    reviewed_signal_id = str(signal_id or "").strip()
    for reviewer in (primary, second):
        reviewer["reviewed_signal_id"] = reviewed_signal_id
        reviewer["reviewed_at_utc"] = reviewed_at_utc

    primary_verified, second_verified = verified(primary), verified(second)
    primary_wave_match = (
        primary.get("elliott_wave_phase") == expected_wave_phase
        and primary.get("elliott_wave_direction") == expected_wave_direction
    )
    second_wave_match = (
        second.get("elliott_wave_phase") == expected_wave_phase
        and second.get("elliott_wave_direction") == expected_wave_direction
    )
    primary_side_match = (
        primary.get("expected_side") == expected_side
        and primary.get("trader_proposal") == expected_side
    )
    second_side_match = (
        second.get("expected_side") == expected_side
        and second.get("trader_proposal") == expected_side
    )
    providers_independent = (
        bool(str(primary.get("provider") or "").strip())
        and bool(str(second.get("provider") or "").strip())
        and str(primary.get("provider")).strip().casefold()
            != str(second.get("provider")).strip().casefold()
    )
    models_independent = (
        bool(str(primary.get("model") or "").strip())
        and bool(str(second.get("model") or "").strip())
        and str(primary.get("model")).strip().casefold()
            != str(second.get("model")).strip().casefold()
    )
    primary_approved = (
        _demo_ai_review_approved(primary)
        and primary_verified and primary_wave_match and primary_side_match
    )
    second_approved = (
        _demo_ai_review_approved(second)
        and second_verified and second_wave_match and second_side_match
    )
    independent_pair = providers_independent and models_independent
    two_approvals = (
        expected_side in {"LONG", "SHORT"}
        and expected_wave_phase in {
            "WAVES_1_AND_2_CONFIRMED", "WAVE_3_CONFIRMED",
            "WAVE_4_CONFIRMED", "FIVE_WAVE_IMPULSE_CONFIRMED",
        }
        and expected_wave_direction == ("BULLISH" if expected_side == "LONG" else "BEARISH")
        and primary_approved and second_approved and independent_pair
    )

    primary_status = str(primary.get("status") or "UNKNOWN").upper()
    second_status = str(second.get("status") or "UNKNOWN").upper()

    def transient_provider_failure(review: dict[str, Any]) -> bool:
        # Failover is deliberately limited to transport/server faults. A bad,
        # incomplete, unverified, rate-limited, or unconfigured review is not
        # converted into approval.
        http_status = review.get("http_status")
        if isinstance(http_status, int) and 500 <= http_status <= 599:
            return True
        error_type = str(review.get("error_type") or "").casefold()
        return any(token in error_type for token in (
            "timeout", "connectionerror", "connecttimeout", "readtimeout",
            "requestexception", "networkerror",
        ))

    demo_failover_enabled = (
        str(os.getenv("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED") or "").strip().lower() == "true"
        and str(os.getenv("BLOFIN_DEMO_TRADING_ENABLED") or "").strip().lower() == "true"
    )
    explicit_veto = primary_status in {"VETO", "REJECT"} or second_status in {"VETO", "REJECT"}
    one_verified_approval = (
        expected_side in {"LONG", "SHORT"}
        and expected_wave_phase in {
            "WAVES_1_AND_2_CONFIRMED", "WAVE_3_CONFIRMED",
            "WAVE_4_CONFIRMED", "FIVE_WAVE_IMPULSE_CONFIRMED",
        }
        and expected_wave_direction == ("BULLISH" if expected_side == "LONG" else "BEARISH")
        and (
            (primary_approved and transient_provider_failure(second))
            or (second_approved and transient_provider_failure(primary))
        )
    )
    demo_single_reviewer_failover = (
        demo_failover_enabled and not explicit_veto and one_verified_approval
    )
    if two_approvals:
        consensus_status = "APPROVED_BY_BOTH"
        decision = "AUTHORIZE_DEMO_EXECUTION"
        status_fa = "هر دو ناظر مستقل، الگو و موج الیوت یکسان را تأیید کردند؛ کنترل‌های Executor همچنان اجباری است."
    elif explicit_veto:
        consensus_status = "VETO"
        decision = "BLOCK_DEMO_ENTRY"
        status_fa = "حداقل یک ناظر مستقل ورود را رد کرد؛ هیچ سفارش Demo ارسال نمی‌شود."
    elif demo_single_reviewer_failover:
        consensus_status = "APPROVED_WITH_TRANSIENT_PROVIDER_FAILURE"
        decision = "AUTHORIZE_DEMO_EXECUTION_WITH_ONE_VERIFIED_REVIEWER"
        status_fa = "یک ناظر معتبر ورود را تأیید کرد و ناظر دوم خطای موقت شبکه/سرور داشت؛ فقط در BloFin Demo و با عبور از تمام کنترل‌های Executor مجاز است."
    elif primary_verified and second_verified and (
        primary_status != second_status
        or primary.get("trader_proposal") != second.get("trader_proposal")
        or primary_wave_match != second_wave_match
    ):
        consensus_status = "DISAGREEMENT"
        decision = "BLOCK_DEMO_ENTRY"
        status_fa = "دو ناظر به نتیجه یکسان نرسیدند؛ ورود مسدود شد."
    elif "NOT_CONFIGURED" in {primary_status, second_status}:
        consensus_status = "NOT_CONFIGURED"
        decision = "BLOCK_DEMO_ENTRY"
        status_fa = "هر دو ناظر مستقل با تنظیمات معتبر و مجزا در دسترس نیستند؛ ورود مسدود شد."
    else:
        consensus_status = "UNKNOWN"
        decision = "BLOCK_DEMO_ENTRY"
        status_fa = "دو تأیید مستقل، معتبر و هم‌جهت دریافت نشد؛ ورود مسدود شد."

    for reviewer in (primary, second):
        reviewer["execution_role"] = "ENTRY_APPROVAL_GATE"
        reviewer["entry_gate_enforced"] = True
    reviews = [
        {"reviewer_id": "nemotron", "actor": "Nemotron Supervisor", "review": primary},
        {"reviewer_id": "second_ai", "actor": "Second AI Supervisor", "review": second},
    ]
    consensus = {
        "status": consensus_status,
        "primary_status": primary_status,
        "second_status": second_status,
        "primary_verified": primary_verified,
        "second_verified": second_verified,
        "primary_approved": primary_approved,
        "second_approved": second_approved,
        "providers_independent": providers_independent,
        "models_independent": models_independent,
        "expected_side": expected_side,
        "elliott_wave_phase": expected_wave_phase,
        "elliott_wave_direction": expected_wave_direction,
        "two_reviewer_approval": two_approvals,
        "single_reviewer_demo_failover": demo_single_reviewer_failover,
        "verified_approval_count": int(primary_approved) + int(second_approved),
        "transient_provider_failure_count": int(transient_provider_failure(primary)) + int(transient_provider_failure(second)),
        "veto_provider_count": int(primary_status in {"VETO", "REJECT"}) + int(second_status in {"VETO", "REJECT"}),
        "decision": decision,
        "entry_gate_enforced": True,
        "reviewed_signal_id": reviewed_signal_id,
        "reviewed_at_utc": reviewed_at_utc,
        "status_fa": status_fa,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    ai_status = "APPROVED_BY_BOTH" if two_approvals else consensus_status
    return {
        "approved": two_approvals or demo_single_reviewer_failover,
        "status": ("APPROVED_WITH_TRANSIENT_PROVIDER_FAILURE" if demo_single_reviewer_failover else ai_status),
        "ai_status": ("APPROVED_WITH_TRANSIENT_PROVIDER_FAILURE" if demo_single_reviewer_failover else ai_status),
        "ai_review": primary, "second_ai_review": second,
        "ai_reviews": reviews, "ai_consensus": consensus, "reason_fa": status_fa,
    }


def run_challenge1_v2_supervisor_reviews(
    analysis: Any, asset: Any, *, signal_id: str | None = None,
    primary_reviewer: Callable[..., dict[str, Any]] | None = None,
    second_reviewer: Callable[..., dict[str, Any]] | None = None,
    single_reviewer: bool = True,
) -> dict[str, Any]:
    """Run one independent Groq review; fail closed on any missing or invalid evidence.

    The former Nemotron/OpenRouter reviewer is retired from the Challenge 1 V2
    entry path. Groq inherits the complete review responsibility. Deterministic
    strategy, Elliott-wave, evidence, sizing, and executor risk gates remain
    mandatory and are not delegated to the language model.
    """
    del primary_reviewer  # Compatibility-only injection; Nemotron is no longer called.
    if single_reviewer is not True:
        raise ValueError("Challenge 1 V2 is configured for the Groq-only reviewer path")
    signal_value = getattr(analysis, "signal", "UNKNOWN")
    expected_side = str(getattr(signal_value, "value", signal_value) or "UNKNOWN").upper()
    metrics = getattr(analysis, "metrics", {})
    metrics = metrics if isinstance(metrics, dict) else {}
    execution_profile = str(metrics.get("execution_profile") or "").upper()
    evidence = (
        metrics.get("challenge1_v2_evidence")
        if execution_profile == "CHALLENGE_1_V2"
        else metrics.get("challenge1_evidence")
    )
    evidence = evidence if isinstance(evidence, dict) else {}
    wave = evidence.get("elliott_wave")
    wave = wave if isinstance(wave, dict) else {}
    expected_wave_phase = str(wave.get("phase") or "").upper()
    expected_wave_direction = str(wave.get("pattern") or "").upper()

    def unavailable(error_type: str) -> dict[str, Any]:
        return {
            "reviewer_id": "groq", "provider": "Groq",
            "status": "ERROR", "request_attempted": None,
            "response_source": "UNKNOWN", "response_validated": False,
            "model_verified": False, "paper_entry_eligible": False,
            "error_type": error_type,
        }

    try:
        groq = (second_reviewer or _ai.run_independent_ai_supervisor)(analysis, asset)
    except Exception as exc:
        groq = unavailable(type(exc).__name__)
    if not isinstance(groq, dict):
        groq = unavailable("InvalidReviewPayload")
    groq = dict(groq)
    groq["reviewer_id"] = "groq"
    groq["reviewed_signal_id"] = str(signal_id or "").strip()
    reviewed_at_utc = datetime.now(timezone.utc).isoformat()
    groq["reviewed_at_utc"] = reviewed_at_utc

    provider_name = str(groq.get("provider") or "").strip().casefold().replace("-", "").replace("_", "").replace(" ", "")
    allowed_groq_models = {
        "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b",
    }
    configured_model = str(groq.get("model") or "").strip()
    returned_model = str(groq.get("returned_model") or "").strip()
    verified = bool(
        provider_name in {"groq", "groqcloud"}
        and configured_model in allowed_groq_models
        and returned_model == configured_model
        and groq.get("request_attempted") is True
        and str(groq.get("response_source") or "").upper() == "LIVE_API"
        and groq.get("response_validated") is True
        and groq.get("model_verified") is True
        and str(groq.get("model") or "").strip()
        and str(groq.get("returned_model") or "").strip()
    )
    wave_match = (
        groq.get("elliott_wave_phase") == expected_wave_phase
        and groq.get("elliott_wave_direction") == expected_wave_direction
    )
    side_match = (
        groq.get("expected_side") == expected_side
        and groq.get("trader_proposal") == expected_side
    )
    provider_value = str(groq.get("provider") or "").strip().casefold().replace("-", "").replace("_", "").replace(" ", "")
    provider_id = str(groq.get("provider_id") or "").strip().casefold()
    provider_is_groq = provider_value in {"groq", "groqcloud"} or provider_id in {"groq", "groqcloud"}
    allowed_groq_models = {
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
    }
    configured_model = str(groq.get("model") or "").strip()
    returned_model = str(groq.get("returned_model") or "").strip()
    model_is_groq_allowlisted = (
        configured_model in allowed_groq_models
        and returned_model.split(":", 1)[0] == configured_model.split(":", 1)[0]
    )
    approved = (
        expected_side in {"LONG", "SHORT"}
        and expected_wave_phase in {
            "WAVES_1_AND_2_CONFIRMED", "WAVE_3_CONFIRMED",
            "WAVE_4_CONFIRMED", "FIVE_WAVE_IMPULSE_CONFIRMED",
        }
        and expected_wave_direction == ("BULLISH" if expected_side == "LONG" else "BEARISH")
        and provider_is_groq and model_is_groq_allowlisted
        and verified and wave_match and side_match
        and _demo_ai_review_approved(groq)
    )
    reviewer_status = str(groq.get("status") or "UNKNOWN").upper()
    if reviewer_status in {"VETO", "REJECT"}:
        consensus_status = "VETO"
        decision = "BLOCK_DEMO_ENTRY"
        status_fa = "ناظر Groq با شواهد معتبر ورود را رد کرد؛ هیچ سفارش Demo ارسال نمی‌شود."
    elif approved:
        consensus_status = "APPROVED_BY_GROQ"
        decision = "AUTHORIZE_DEMO_EXECUTION_WITH_ONE_REVIEWER"
        status_fa = "ناظر Groq پاسخ زنده، مدل و شواهد موج و جهت را تأیید کرد؛ تمام کنترل‌های قطعی Executor همچنان اجباری هستند."
    elif reviewer_status in {"NOT_CONFIGURED", "SECOND_AI_NOT_CONFIGURED"}:
        consensus_status = "NOT_CONFIGURED"
        decision = "BLOCK_DEMO_ENTRY"
        status_fa = "ناظر Groq پیکربندی معتبر ندارد؛ ورود مسدود است."
    else:
        consensus_status = "UNKNOWN"
        decision = "BLOCK_DEMO_ENTRY"
        status_fa = "تأیید زنده و معتبر Groq با شواهد هم‌جهت دریافت نشد؛ ورود مسدود است."

    groq["execution_role"] = "SOLE_ENTRY_APPROVAL_REVIEWER"
    groq["entry_gate_enforced"] = True
    groq["observation_only"] = False
    groq["execution_effect"] = "APPROVAL_GATE_ONLY"
    reviews = [{"reviewer_id": "groq", "actor": "Groq Supervisor", "review": groq}]
    consensus = {
        "status": consensus_status,
        "reviewer_id": "groq",
        "reviewer_status": reviewer_status,
        "reviewer_verified": verified,
        "provider_is_groq": provider_is_groq,
        "model_is_groq_allowlisted": model_is_groq_allowlisted,
        "reviewer_approved": approved,
        "primary_status": reviewer_status,
        "second_status": "DISABLED",
        "primary_verified": verified,
        "second_verified": False,
        "primary_approved": approved,
        "second_approved": False,
        "providers_independent": False,
        "models_independent": False,
        "expected_side": expected_side,
        "elliott_wave_phase": expected_wave_phase,
        "elliott_wave_direction": expected_wave_direction,
        "two_reviewer_approval": False,
        "single_reviewer_demo_failover": False,
        "single_groq_reviewer_approval": approved,
        "single_reviewer_approval": approved,
        "verified_approval_count": int(approved),
        "transient_provider_failure_count": 0,
        "veto_provider_count": int(reviewer_status in {"VETO", "REJECT"}),
        "decision": decision,
        "entry_gate_enforced": True,
        "reviewed_signal_id": str(signal_id or "").strip(),
        "reviewed_at_utc": reviewed_at_utc,
        "status_fa": status_fa,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "approved": approved,
        "status": consensus_status,
        "ai_status": consensus_status,
        "ai_review": groq,
        "second_ai_review": None,
        "ai_reviews": reviews,
        "ai_consensus": consensus,
        "reason_fa": status_fa,
    }

def _configured_providers() -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    for definition in _PROVIDER_DEFINITIONS:
        key = (os.getenv(definition["env"]) or "").strip()
        if not key:
            for alias in definition["aliases"]:
                key = (os.getenv(alias) or "").strip()
                if key:
                    break
        if key:
            providers.append({**definition, "key": key})
    return providers


def ai_supervisor_status() -> dict[str, Any]:
    """Return non-secret configuration status; never return key material."""
    configured_ids = {item["id"] for item in _configured_providers()}
    states = []
    for definition in _PROVIDER_DEFINITIONS:
        ready = definition["id"] in configured_ids
        states.append({
            "id": definition["id"],
            "provider": definition["label"],
            "model": definition["model"],
            "configured": ready,
            "status": "CONFIGURED" if ready else "MISSING_API_KEY",
            "key_variable": definition["env"],
            "pricing_guard": definition["tier"],
        })
    count = len(configured_ids)
    ready = count >= MIN_INDEPENDENT_APPROVALS
    return {
        "provider": "Independent Multi-Provider AI Supervisor",
        "model": "NVIDIA Nemotron + Gemini 3.8 Flash + Groq GPT-OSS 120B",
        "status": "READY" if ready else "INSUFFICIENT_PROVIDERS",
        "status_fa": (
            f"{count} ارائه‌دهنده تنظیم شده؛ حداقل دو مدل مستقل برای صدور تأیید لازم است."
            if ready else
            f"فقط {count} ارائه‌دهنده تنظیم شده است؛ حداقل دو کلید رایگان مستقل لازم است. تا آن زمان ورود جدید مسدود می‌ماند."
        ),
        "configured": ready,
        "configured_provider_count": count,
        "available_provider_count": len(_PROVIDER_DEFINITIONS),
        "required_independent_approvals": MIN_INDEPENDENT_APPROVALS,
        "providers": states,
        "secret_sent_to_model": False,
        "orders_submitted": False,
        "paper_entry_eligible": False,
    }


def _reserve_provider_budget(provider_id: str) -> bool:
    day = datetime.now(timezone.utc).date().isoformat()
    now = time.monotonic()
    with _REQUEST_LOCK:
        minute_calls = [
            stamp for stamp in _MINUTE_CALLS.get(provider_id, [])
            if stamp > now - 60.0
        ]
        _MINUTE_CALLS[provider_id] = minute_calls
        if len(minute_calls) >= MAX_PROVIDER_CALLS_PER_MINUTE:
            return False
        key = provider_id + ":" + day
        count = int(_DAILY_CALLS.get(key, 0))
        if count >= MAX_PROVIDER_CALLS_PER_UTC_DAY:
            return False
        _DAILY_CALLS[key] = count + 1
        minute_calls.append(now)
        _MINUTE_CALLS[provider_id] = minute_calls
        return True


def _failure(provider: dict[str, Any], status: str, *,
             http_status: int | None = None, latency_ms: int = 0,
             error_type: str | None = None) -> dict[str, Any]:
    result = {
        "provider": provider["label"],
        "provider_id": provider["id"],
        "model": provider["model"],
        "status": status,
        "model_verified": False,
        "response_validated": False,
        "paper_entry_eligible": False,
        "cost_usd": 0.0,
        "latency_ms": latency_ms,
    }
    if http_status is not None:
        result["http_status"] = http_status
    if error_type:
        result["error_type"] = error_type
    return result


def _message_text_candidates(message: Any) -> list[str]:
    """Extract parseable text/JSON candidates from OpenAI-compatible message shapes.

    Content is preferred. Reasoning is only a last-resort candidate and still
    must contain a valid JSON review object before it can be accepted.
    """
    if not isinstance(message, dict):
        return []

    candidates: list[str] = []

    def add_value(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text and text not in candidates:
                candidates.append(text)
        elif isinstance(value, dict):
            try:
                rendered = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                return
            if rendered not in candidates:
                candidates.append(rendered)

    content = message.get("content")
    if isinstance(content, str):
        add_value(content)
    elif isinstance(content, dict):
        add_value(content)
    elif isinstance(content, list):
        text_parts: list[str] = []
        json_parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                part_type = str(part.get("type") or "").lower()
                if isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                elif isinstance(part.get("content"), str):
                    text_parts.append(part["content"])
                elif part_type in {"json", "output_json"}:
                    value = part.get("json", part.get("value"))
                    if isinstance(value, dict):
                        json_parts.append(json.dumps(value, ensure_ascii=False))
        joined = "\n".join(item.strip() for item in text_parts if item.strip()).strip()
        if joined:
            add_value(joined)
        for item in json_parts:
            add_value(item)

    # Some OpenRouter reasoning models return an empty/null content field but
    # include a structured final object in a reasoning-compatible field. These
    # are fallback candidates only; plain reasoning prose is never an approval.
    add_value(message.get("output_text"))
    add_value(message.get("reasoning"))
    reasoning_details = message.get("reasoning_details")
    if isinstance(reasoning_details, list):
        detail_text = "\n".join(
            item.get("text", "") for item in reasoning_details
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ).strip()
        add_value(detail_text)
    return candidates


def _run_provider(provider: dict[str, Any], evidence: dict[str, Any], *,
                  expected_side: str, market_type: str,
                  http_post: Callable[..., Any] | None = None) -> dict[str, Any]:
    if not _reserve_provider_budget(provider["id"]):
        return _failure(provider, "RATE_LIMITED")
    payload = {
        "model": provider["model"],
        "messages": _ai._prompt_messages(evidence),
        "temperature": 0.1,
        "max_tokens": 1200,
    }
    headers = {
        "Authorization": "Bearer " + provider["key"],
        "Content-Type": "application/json",
    }
    if provider["id"] == "openrouter":
        headers["HTTP-Referer"] = os.getenv(
            "RENDER_EXTERNAL_URL", "https://crypto-super-radar.onrender.com"
        )
        headers["X-Title"] = "CRYPTO SUPER RADAR AI Supervisor"
    post = http_post or requests.post
    started = time.monotonic()
    http_status = None
    latency_ms = 0
    try:
        response = post(
            provider["url"], headers=headers, json=payload,
            timeout=(8, 35), allow_redirects=False,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        http_status = int(getattr(response, "status_code", 0) or 0)
        if http_status != 200:
            status = "RATE_LIMITED" if http_status in {402, 429} else "UNKNOWN"
            return _failure(provider, status, http_status=http_status, latency_ms=latency_ms)
        body = response.json()
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Missing AI response choices")
        message = choices[0].get("message")
        candidates = _message_text_candidates(message)
        parsed = None
        parse_errors = []
        for candidate in candidates:
            if not candidate or len(candidate) > _ai.MAX_RESPONSE_CHARS:
                continue
            try:
                parsed = _ai._extract_json(candidate)
                break
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                parse_errors.append(type(exc).__name__)
        if parsed is None:
            raise ValueError(
                "AI response content contains no valid JSON review object"
                + (":" + ",".join(parse_errors[:3]) if parse_errors else "")
            )
        returned_model = body.get("model") if isinstance(body, dict) else None
        if not isinstance(returned_model, str) or not returned_model.strip():
            return _failure(
                provider, "MODEL_UNCONFIRMED",
                http_status=http_status, latency_ms=latency_ms,
            )
        if returned_model.strip().split(":", 1)[0] != provider["model"].split(":", 1)[0]:
            result = _failure(
                provider, "MODEL_MISMATCH",
                http_status=http_status, latency_ms=latency_ms,
            )
            result["returned_model"] = returned_model[:160]
            return result
        review = _ai._validated_review(
            parsed, provider["model"], str(evidence.get("asset") or "unknown"),
            expected_side=expected_side, market_type=market_type,
        )
        usage = body.get("usage") if isinstance(body, dict) else None
        total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        if isinstance(total_tokens, bool) or not isinstance(total_tokens, int) or total_tokens < 0:
            total_tokens = None
        review.update({
            "provider": provider["label"],
            "provider_id": provider["id"],
            "model": provider["model"],
            "returned_model": returned_model[:160],
            "model_verified": True,
            "response_validated": True,
            "response_id": (str(body.get("id"))[:200] if isinstance(body, dict) and body.get("id") is not None else None),
            "confidence": (parsed.get("confidence", parsed.get("confidence_score")) if isinstance(parsed, dict) and isinstance(parsed.get("confidence", parsed.get("confidence_score")), (int, float)) and not isinstance(parsed.get("confidence", parsed.get("confidence_score")), bool) else None),
            "http_status": http_status,
            "latency_ms": latency_ms,
            "total_tokens": total_tokens,
            "cost_usd": 0.0,
        })
        return review
    except Exception as exc:
        return _failure(
            provider, "UNKNOWN", http_status=http_status, latency_ms=latency_ms,
            error_type=type(exc).__name__,
        )


def run_ai_supervisor(result: Any, asset: Any = None, *,
                      http_post: Callable[..., Any] | None = None,
                      bypass_cache: bool = True) -> dict[str, Any]:
    """Require a two-provider independent approval; any valid veto blocks entry."""
    del bypass_cache  # Provider reviews are always fresh; no approval cache is used.
    configs = _configured_providers()
    if len(configs) < MIN_INDEPENDENT_APPROVALS:
        status = ai_supervisor_status()
        return {
            "provider": status["provider"],
            "model": status["model"],
            "status": "PROVIDERS_INSUFFICIENT",
            "status_fa": status["status_fa"],
            "configured_provider_count": len(configs),
            "required_approvals": MIN_INDEPENDENT_APPROVALS,
            "provider_reviews": [],
            "paper_entry_eligible": False,
            "orders_submitted": False,
            "trade_executed": False,
            "cost_usd": 0.0,
        }

    asset_name = str(getattr(asset, "raw", asset) or getattr(result, "asset", "unknown"))
    market_type = (
        "perpetual" if getattr(asset, "market_type", None) == "perp"
        or asset_name.lower().startswith("perp:") else "spot"
    )
    expected_side = str(
        getattr(getattr(result, "signal", None), "value", getattr(result, "signal", "UNKNOWN"))
    ).upper().replace(" ", "_")
    if expected_side not in {"LONG", "SHORT"}:
        return {
            "provider": "Independent Multi-Provider AI Supervisor",
            "model": "NVIDIA Nemotron + Gemini 3.8 Flash + Groq GPT-OSS 120B",
            "status": "VETO",
            "status_fa": "سیگنال جهت‌دار معتبر برای بازبینی ورود وجود ندارد.",
            "paper_entry_eligible": False,
            "orders_submitted": False,
            "trade_executed": False,
            "cost_usd": 0.0,
        }

    native_review = _ai.build_native_agent_council_review(result)
    if native_review.get("status") != "APPROVED":
        return {
            "provider": "Independent Multi-Provider AI Supervisor",
            "model": "NVIDIA Nemotron + Gemini 3.8 Flash + Groq GPT-OSS 120B",
            "status": "VETO" if native_review.get("status") == "VETO" else "UNKNOWN",
            "status_fa": "شورای شواهد بومی رادار تأیید کامل نداده است؛ مدل‌های خارجی فراخوانی نشدند.",
            "native_council": native_review,
            "paper_entry_eligible": False,
            "orders_submitted": False,
            "trade_executed": False,
            "cost_usd": 0.0,
        }

    evidence = _ai._build_review_input(result, asset)
    if not isinstance(evidence, dict):
        return {
            "provider": "Independent Multi-Provider AI Supervisor",
            "model": "NVIDIA Nemotron + Gemini 3.8 Flash + Groq GPT-OSS 120B",
            "status": "UNKNOWN",
            "status_fa": "ساخت شواهد پاک‌سازی‌شده برای ناظر ناموفق بود.",
            "paper_entry_eligible": False,
            "orders_submitted": False,
            "trade_executed": False,
            "cost_usd": 0.0,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(configs)) as pool:
        futures = [
            pool.submit(
                _run_provider, provider, evidence,
                expected_side=expected_side, market_type=market_type,
                http_post=http_post,
            )
            for provider in configs
        ]
        reviews = [future.result() for future in futures]

    vetoes = [review for review in reviews if review.get("status") == "VETO"]
    approvals = [
        review for review in reviews
        if review.get("status") == "APPROVED"
        and review.get("model_verified") is True
        and review.get("response_validated") is True
        and review.get("paper_entry_eligible") is True
    ]
    if vetoes:
        final_status = "VETO"
        status_fa = "حداقل یک ناظر مستقل با شواهد معتبر ورود را وتو کرد؛ سفارش ممنوع است."
    elif len(approvals) >= MIN_INDEPENDENT_APPROVALS:
        final_status = "APPROVED"
        status_fa = f"{len(approvals)} ناظر مستقل از {len(configs)} ارائه‌دهنده تأیید معتبر دادند."
    else:
        final_status = "UNKNOWN"
        status_fa = (
            f"فقط {len(approvals)} تأیید معتبر دریافت شد؛ حداقل "
            f"{MIN_INDEPENDENT_APPROVALS} تأیید مستقل لازم است. ورود مسدود است."
        )

    compact_reviews = []
    for review in reviews:
        compact_reviews.append({
            key: review.get(key) for key in (
                "provider", "provider_id", "model", "returned_model", "status",
                "final_rating", "trader_proposal", "risk_manager_verdict",
                "portfolio_manager_verdict", "risk_flags", "rationale_fa",
                "http_status", "latency_ms", "total_tokens",
                "model_verified", "response_validated", "response_id", "confidence",
            ) if key in review
        })
    rationale_parts = [
        f"{review.get('provider')}: {review.get('rationale_fa') or review.get('status')}"
        for review in reviews
    ]
    latency_values = [
        int(review.get("latency_ms") or 0) for review in reviews
        if isinstance(review.get("latency_ms"), (int, float))
    ]
    token_values = [
        int(review.get("total_tokens") or 0) for review in reviews
        if isinstance(review.get("total_tokens"), int)
    ]
    return {
        "provider": "Independent Multi-Provider AI Supervisor",
        "model": " | ".join(item["model"] for item in configs),
        "returned_model": " | ".join(
            str(review.get("returned_model")) for review in approvals
            if review.get("returned_model")
        ),
        "status": final_status,
        "status_fa": status_fa,
        "final_rating": "MULTI_PROVIDER_QUORUM" if final_status == "APPROVED" else final_status,
        "rationale_fa": "؛ ".join(rationale_parts)[:3000],
        "provider_reviews": compact_reviews,
        "configured_provider_count": len(configs),
        "successful_provider_count": sum(
            review.get("response_validated") is True for review in reviews
        ),
        "approved_provider_count": len(approvals),
        "veto_provider_count": len(vetoes),
        "required_approvals": MIN_INDEPENDENT_APPROVALS,
        "decision_rule": "TWO_INDEPENDENT_APPROVALS_REQUIRED; ANY_VALID_VETO_BLOCKS",
        "model_verified": len(approvals) >= MIN_INDEPENDENT_APPROVALS,
        "response_validated": len(approvals) >= MIN_INDEPENDENT_APPROVALS,
        "latency_ms": max(latency_values) if latency_values else 0,
        "total_tokens": sum(token_values),
        "cost_usd": 0.0,
        "paper_entry_eligible": final_status == "APPROVED",
        "orders_submitted": False,
        "trade_executed": False,
    }
