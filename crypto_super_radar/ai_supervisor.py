"""Independent multi-provider AI supervision for guarded paper/demo entries.

No provider can approve a new entry alone. At least two independent providers
must return a model-verified, schema-validated approval. Any valid veto wins.
Only free-tier model IDs/endpoints are configured here; keep provider accounts
on their Free tiers and never attach billing for this zero-cost configuration.
"""
from __future__ import annotations

import concurrent.futures
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
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "\\n".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        if not isinstance(content, str) or len(content) > _ai.MAX_RESPONSE_CHARS:
            raise ValueError("AI response content is invalid or too large")
        parsed = _ai._extract_json(content)
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
                "model_verified", "response_validated",
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
