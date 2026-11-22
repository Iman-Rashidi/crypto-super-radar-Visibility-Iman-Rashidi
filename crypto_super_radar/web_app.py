"""Dependency-light Persian web dashboard for on-demand research scans."""
from __future__ import annotations

import html
import hashlib
import hmac
import copy
import concurrent.futures
from decimal import Decimal, InvalidOperation
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .integrated_engine import WhaleAwareConfirmationEngine
from .indicators import observed_price_ranges
from .models import AssetSpec, AnalysisResult, GateResult, GateStatus, SafetyStatus, Signal
from .paper_trading import PAPER_ASSETS, PaperTradingBot
from .futures_paper_trading import FUTURES_PAPER_ASSETS, FuturesPaperTradingBot
from .market_intelligence import search_dex_tokens
from .gem_hunter_discovery import discover_gem_candidates
from .gem_hunter_evaluator import evaluate_discovery_candidates
from .gem_hunter_technical import enrich_discovery_candidates_with_technicals
from .gem_hunter_pool_trades import enrich_discovery_candidates_with_pool_trades
from .gem_hunter_market_validation import reconcile_discovery_candidates
from .gem_hunter_security import screen_discovery_candidates
from .gem_hunter_qualification import CRITICAL_GATES, FILTER_IDS, SCHEMA_VERSION, public_buy_candidates
from .gem_hunter_v2 import ingest_candidates as gem_hunter_v2_ingest_candidates, public_snapshot as gem_hunter_v2_public_snapshot
from .gem_hunter_v3 import process_gem_hunter_v3_candidates
from .providers import _ohlcv_frame
from .feed_health import build_feed_health_report
from .global_market_data import GlobalMarketBoard
from .live_market_radar import BYBIT_INTERVAL, INTERVAL_MS, build_live_market_radar_payload
from .blofin_demo_readonly import BloFinDemoAuthError, BloFinDemoError, BloFinDemoReadOnly, BloFinDemoResponseError, DEMO_READ_CREDENTIAL_ENV, DEMO_TRADE_CREDENTIAL_ENV, DEMO_REST_URL, _resolve_env_credential
from .blofin_demo_executor import (
    BloFinDemoEventStore, BloFinDemoExecutionBlocked, BloFinDemoExecutionUnknown,
    BloFinDemoExecutor, BloFinDemoRiskPolicy, BloFinDemoTradeClient,
    _active_order_count, _active_position_count, _equity_from_balance,
    build_demo_order_plan, _challenge1_two_reviewer_approval_ready,
)
from .blofin_demo_smoke_test import BloFinDemoSmokeTestRunner
from .challenge1_strategy import analyze_blofin_challenge1, DEFAULT_CHALLENGE1_INSTRUMENTS
from .challenge1_v2 import challenge1_v2_execution_evidence_ready
from .challenge1_v2_orchestration import (
    Challenge1V2DataBlocked, analyze_challenge1_v2_market, get_fresh_execution_quote,
)
from .database import connect_database, connect_turso_database, storage_backend
from .challenge1_audit import Challenge1AuditRecorder
from .ai_second_opinion import ai_connection_status, second_ai_connection_status, run_openrouter_connection_diagnostic, run_free_ai_second_opinion, run_independent_ai_supervisor
from .notion_event_sync import NotionEventOutbox
from .notion_activity_journal import NotionActivityJournal
from .challenge1_position_observers import (
    schedule_position_observer_review,
    record_position_monitor_cycle,
    record_position_observer_skip,
)
from .ai_supervisor import (
    ai_supervisor_status, run_ai_supervisor, run_challenge1_v2_supervisor_reviews,
)
from .execution_diagnostics import build_execution_error_diagnostic

ENGINE = WhaleAwareConfirmationEngine()
STORE = ENGINE.whale_store
PAPER_BOT = PaperTradingBot(ENGINE)
FUTURES_PAPER_BOT = FuturesPaperTradingBot(ENGINE)
GLOBAL_MARKET_BOARD = GlobalMarketBoard(ENGINE.data.http)
JOBS: dict[str, dict] = {}
ACTIVE_SCAN_KEYS: dict[str, str] = {}
JOB_FENCE_COUNTER = 0
JOB_LEASE_SECONDS = 1800
LOCK = threading.RLock()
SCAN_RUNNING = False
SCAN_QUEUE = queue.Queue(maxsize=50)
SCAN_WORKER_STARTED = False

BLOFIN_STATUS_CACHE = {"expires_at": 0.0, "payload": None}
BLOFIN_INSTRUMENT_CACHE = {"expires_at": 0.0, "payload": None}
BLOFIN_STATUS_LOCK = threading.Lock()
BLOFIN_EXECUTION_GUARD = threading.Lock()
BLOFIN_ORDER_SUBMISSION_LOCK = threading.Lock()
BLOFIN_POSITION_MANAGER_START_LOCK = threading.Lock()
BLOFIN_POSITION_MANAGER_THREAD = None
BLOFIN_POSITION_MANAGER_STATE = {
    "status": "STARTING",
    "last_check_at_utc": None,
    "last_action": None,
    "last_message_fa": "مدیر مستقل خروج سود در حال آماده‌سازی است.",
    "readiness_status": None,
    "readiness_blockers": [],
    "trade_permission_status": "UNKNOWN",
    "credential_source": None,
    "trading_enabled": False,
    "dashboard_auth_configured": False,
    "durable_storage": False,
    "event_store_readable": False,
}
BLOFIN_EXECUTION_JOBS: dict[str, dict] = {}
BLOFIN_AUTOPILOT_LOCK = threading.Lock()
BLOFIN_AUTOPILOT_START_LOCK = threading.Lock()
BLOFIN_STARTUP_RECONCILE_LOCK = threading.Lock()
BLOFIN_STARTUP_RECONCILE_DONE = False
BLOFIN_STARTUP_RECONCILE_THREAD = None
BLOFIN_AUTOPILOT_THREAD = None
# A dashboard operator may start/stop AutoPilot for this process without editing
# Render environment variables. None means use the configured Render value.
BLOFIN_AUTOPILOT_RUNTIME_OVERRIDE: bool | None = None


def _blofin_demo_autopilot_enabled() -> bool:
    with LOCK:
        override = BLOFIN_AUTOPILOT_RUNTIME_OVERRIDE
    if override is not None:
        return bool(override)
    return os.getenv("BLOFIN_DEMO_AUTOPILOT_ENABLED", "").strip().lower() == "true"


def _set_blofin_demo_autopilot_runtime_override(enabled: bool | None) -> None:
    global BLOFIN_AUTOPILOT_RUNTIME_OVERRIDE
    with LOCK:
        BLOFIN_AUTOPILOT_RUNTIME_OVERRIDE = enabled


BLOFIN_AUTOPILOT_STATE = {
    "enabled": False,
    "status": "DISABLED",
    "runtime_role": "WEB",
    "environment": "BLOFIN_DEMO",
    "market": "BTC-USDT",
    "instrument_id": "BTC-USDT",
    "execution_profile": "CHALLENGE_1",
    "interval_seconds": 300,
    "last_cycle_started_at_utc": None,
    "last_cycle_finished_at_utc": None,
    "last_stage": None,
    "last_message_fa": "اتوپایلوت هنوز فعال نشده است.",
    "last_result_status": None,
    "cycles_started": 0,
    "persistence_status": "NOT_INITIALIZED",
    "restored_from_storage": False,
}

def _persist_blofin_autopilot_state() -> bool:
    """Persist the operational snapshot only when durable Turso storage is configured."""
    store = None
    try:
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            raise RuntimeError("Durable Turso storage is not configured.")
        store = BloFinDemoEventStore()
        with LOCK:
            snapshot = copy.deepcopy(BLOFIN_AUTOPILOT_STATE)
        snapshot["persistence_status"] = "DURABLE"
        store.save_autopilot_state(snapshot)
        with LOCK:
            BLOFIN_AUTOPILOT_STATE["persistence_status"] = "DURABLE"
            BLOFIN_AUTOPILOT_STATE.pop("persistence_error_type", None)
        return True
    except Exception as exc:
        with LOCK:
            BLOFIN_AUTOPILOT_STATE["persistence_status"] = "UNAVAILABLE"
            BLOFIN_AUTOPILOT_STATE["persistence_error_type"] = type(exc).__name__
        return False
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


def _restore_blofin_autopilot_state() -> bool:
    """Restore the last durable operational snapshot without claiming a worker is running."""
    store = None
    try:
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            raise RuntimeError("Durable Turso storage is not configured.")
        store = BloFinDemoEventStore()
        saved = store.load_autopilot_state()
        with LOCK:
            if isinstance(saved, dict):
                previous_enabled = bool(saved.get("enabled"))
                previous_status = str(saved.get("status") or "UNKNOWN")
                BLOFIN_AUTOPILOT_STATE.update(saved)
                BLOFIN_AUTOPILOT_STATE["enabled"] = False
                BLOFIN_AUTOPILOT_STATE["persistence_status"] = "DURABLE"
                BLOFIN_AUTOPILOT_STATE["restored_from_storage"] = True
                if previous_enabled and previous_status not in {"DISABLED", "ERROR"}:
                    BLOFIN_AUTOPILOT_STATE["status"] = "RECOVERED"
                    BLOFIN_AUTOPILOT_STATE["last_message_fa"] = (
                        "آخرین وضعیت پایدار بازیابی شد؛ اجرای چرخه جدید فقط با فعال‌بودن تنظیم Demo آغاز می‌شود."
                    )
                snapshot = copy.deepcopy(BLOFIN_AUTOPILOT_STATE)
            else:
                BLOFIN_AUTOPILOT_STATE["persistence_status"] = "DURABLE"
                BLOFIN_AUTOPILOT_STATE["restored_from_storage"] = False
                snapshot = copy.deepcopy(BLOFIN_AUTOPILOT_STATE)
        snapshot["persistence_status"] = "DURABLE"
        store.save_autopilot_state(snapshot)
        return True
    except Exception as exc:
        with LOCK:
            BLOFIN_AUTOPILOT_STATE["persistence_status"] = "UNAVAILABLE"
            BLOFIN_AUTOPILOT_STATE["persistence_error_type"] = type(exc).__name__
        return False
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


AI_DIAGNOSTIC_LOCK = threading.Lock()
AI_DIAGNOSTIC_STATE = {"day": "", "count": 0, "last_started": 0.0}
AI_DIAGNOSTIC_MAX_PER_UTC_DAY = 3
AI_DIAGNOSTIC_MIN_INTERVAL_SECONDS = 300


def _run_ai_diagnostic_limited() -> dict:
    """Cap public diagnostic requests; this function never starts a bot cycle."""
    configured = ai_connection_status()
    if not configured.get("configured"):
        return {
            "provider": "OpenRouter", "model": configured.get("model"),
            "status": "MISSING_API_KEY",
            "status_fa": "کلید API در محیط Render پیدا نشد؛ هیچ درخواست خارجی ارسال نشد.",
            "probe": "NOT_RUN", "paper_entry_eligible": False, "orders_submitted": False,
        }

    now = time.monotonic()
    day = datetime.now(timezone.utc).date().isoformat()
    with AI_DIAGNOSTIC_LOCK:
        if AI_DIAGNOSTIC_STATE.get("day") != day:
            AI_DIAGNOSTIC_STATE.update(day=day, count=0, last_started=0.0)
        elapsed = now - float(AI_DIAGNOSTIC_STATE.get("last_started") or 0.0)
        if elapsed < AI_DIAGNOSTIC_MIN_INTERVAL_SECONDS:
            return {
                "provider": "OpenRouter", "model": configured.get("model"),
                "status": "DIAGNOSTIC_COOLDOWN",
                "status_fa": "برای جلوگیری از مصرف سهمیه، کمی بعد دوباره آزمایش کن.",
                "retry_after_seconds": max(1, int(AI_DIAGNOSTIC_MIN_INTERVAL_SECONDS - elapsed)),
                "paper_entry_eligible": False, "orders_submitted": False,
            }
        if int(AI_DIAGNOSTIC_STATE.get("count") or 0) >= AI_DIAGNOSTIC_MAX_PER_UTC_DAY:
            return {
                "provider": "OpenRouter", "model": configured.get("model"),
                "status": "DAILY_DIAGNOSTIC_LIMIT",
                "status_fa": "سقف سه آزمایش تشخیصی امروز پر شده است.",
                "paper_entry_eligible": False, "orders_submitted": False,
            }
        AI_DIAGNOSTIC_STATE["last_started"] = now
        AI_DIAGNOSTIC_STATE["count"] = int(AI_DIAGNOSTIC_STATE.get("count") or 0) + 1

    result = run_openrouter_connection_diagnostic()
    result["paper_entry_eligible"] = False
    result["orders_submitted"] = False
    return result


AI_DRY_RUN_LOCK = threading.Lock()
AI_DRY_RUN_STATE = {"day": "", "count": 0, "last_started": 0.0}
AI_DRY_RUN_MAX_PER_UTC_DAY = 2
AI_DRY_RUN_MIN_INTERVAL_SECONDS = 300


def _ai_dry_run_http_status(payload: dict) -> int:
    """Map dry-run outcomes to HTTP semantics without disguising missing config as 502."""
    if payload.get("test_passed") is True:
        return 200
    status = str(payload.get("status") or "").upper()
    if status in {"RATE_LIMITED", "DRY_RUN_COOLDOWN", "DAILY_DRY_RUN_LIMIT"}:
        return 429
    if status in {"MISSING_API_KEY", "PROVIDERS_INSUFFICIENT", "NOT_CONFIGURED"}:
        return 503
    return 502


def _ai_dry_run_safe_diagnostic(payload: dict) -> dict:
    """Return provider failure metadata safe for logs; never include keys or raw prompts."""
    reviewers = payload.get("ai_reviews")
    safe_reviewers = []
    if isinstance(reviewers, list):
        for item in reviewers[:2]:
            if not isinstance(item, dict):
                continue
            review = item.get("review") if isinstance(item.get("review"), dict) else item
            safe_reviewers.append({
                key: review.get(key)
                for key in (
                    "reviewer_id", "status", "provider", "model", "returned_model",
                    "request_attempted", "response_source", "response_validated",
                    "model_verified", "http_status", "latency_ms",
                    "skip_reason_code", "error_type", "error_code", "parser_diagnostic",
                )
                if review.get(key) is not None
            })
    consensus = payload.get("ai_consensus")
    consensus = consensus if isinstance(consensus, dict) else {}
    safe_consensus = {
        key: consensus.get(key)
        for key in (
            "status", "decision", "primary_status", "second_status",
            "primary_verified", "second_verified", "providers_independent",
            "models_independent",
        )
        if consensus.get(key) is not None
    }
    return {
        "status": str(payload.get("status") or "UNKNOWN"),
        "test_passed": payload.get("test_passed") is True,
        "consensus": safe_consensus,
        "reviewers": safe_reviewers,
        "orders_submitted": payload.get("orders_submitted") is True,
        "trade_executed": payload.get("trade_executed") is True,
    }


def _run_ai_synthetic_dry_run_limited() -> dict:
    """Call the real configured LLM on synthetic evidence, with no bot or ledger access."""
    primary_config = ai_connection_status()
    second_config = second_ai_connection_status()
    if not primary_config.get("configured") or not second_config.get("configured"):
        primary_state = {
            "reviewer_id": "nemotron",
            "provider": primary_config.get("provider"),
            "model": primary_config.get("model"),
            "status": primary_config.get("status"),
            "request_attempted": False,
            "response_source": "NONE",
            "response_validated": False,
            "model_verified": False,
            "paper_entry_eligible": False,
        }
        second_state = {
            "reviewer_id": "second_ai",
            "provider": second_config.get("provider"),
            "model": second_config.get("model"),
            "status": second_config.get("status"),
            "configuration_issues": list(second_config.get("configuration_issues") or []),
            "request_attempted": False,
            "response_source": "NONE",
            "response_validated": False,
            "model_verified": False,
            "paper_entry_eligible": False,
        }
        return {
            "provider": "Challenge 1 Independent Reviewer Pair",
            "model": "Nemotron + independently configured second reviewer",
            "status": "PROVIDERS_INSUFFICIENT",
            "status_fa": "هر دو ناظر اختصاصی Challenge 1 آماده نیستند؛ هیچ درخواست مدل ارسال نشد.",
            "ai_reviews": [
                {"reviewer_id": "nemotron", "actor": "Nemotron Supervisor", "review": primary_state},
                {"reviewer_id": "second_ai", "actor": "Second AI Supervisor", "review": second_state},
            ],
            "ai_consensus": {"status": "NOT_CONFIGURED", "decision": "BLOCK_DEMO_ENTRY"},
            "test_passed": False,
            "paper_entry_eligible": False,
            "orders_submitted": False,
            "trade_executed": False,
        }

    now = time.monotonic()
    day = datetime.now(timezone.utc).date().isoformat()
    with AI_DRY_RUN_LOCK:
        if AI_DRY_RUN_STATE.get("day") != day:
            AI_DRY_RUN_STATE.update(day=day, count=0, last_started=0.0)
        last_started = float(AI_DRY_RUN_STATE.get("last_started") or 0.0)
        elapsed = now - last_started if last_started > 0 else AI_DRY_RUN_MIN_INTERVAL_SECONDS
        if elapsed < AI_DRY_RUN_MIN_INTERVAL_SECONDS:
            return {
                "provider": "OpenRouter", "model": primary_config.get("model"),
                "status": "DRY_RUN_COOLDOWN",
                "status_fa": "برای جلوگیری از مصرف سهمیه، فاصله پنج‌دقیقه‌ای تست رعایت شود.",
                "retry_after_seconds": max(1, int(AI_DRY_RUN_MIN_INTERVAL_SECONDS - elapsed)),
                "test_passed": False, "paper_entry_eligible": False,
                "orders_submitted": False, "trade_executed": False,
            }
        if int(AI_DRY_RUN_STATE.get("count") or 0) >= AI_DRY_RUN_MAX_PER_UTC_DAY:
            return {
                "provider": "OpenRouter", "model": primary_config.get("model"),
                "status": "DAILY_DRY_RUN_LIMIT",
                "status_fa": "سقف دو تست یکپارچگی امروز تکمیل شده است.",
                "test_passed": False, "paper_entry_eligible": False,
                "orders_submitted": False, "trade_executed": False,
            }
        AI_DRY_RUN_STATE["last_started"] = now
        AI_DRY_RUN_STATE["count"] = int(AI_DRY_RUN_STATE.get("count") or 0) + 1

    # This fixture deliberately satisfies the actual 5m candle/pattern/wave
    # evidence contract. It is synthetic and cannot be submitted as a market order.
    asset = AssetSpec.parse("perp:BTCUSDT")
    candles_5m = [
        {"open_time_utc": "2026-11-18T09:50:00+00:00", "open": 99.0, "high": 100.0, "low": 98.0, "close": 99.2, "volume": 80.0},
        {"open_time_utc": "2026-11-18T09:55:00+00:00", "open": 99.2, "high": 100.2, "low": 98.8, "close": 99.0, "volume": 90.0},
        {"open_time_utc": "2026-11-18T10:00:00+00:00", "open": 99.0, "high": 102.0, "low": 98.7, "close": 101.0, "volume": 160.0},
    ]
    signal_bar = candles_5m[-1]["open_time_utc"]
    signal_id = f"challenge1-v4:BTC-USDT:5m:{signal_bar}"
    chosen_pattern = {
        "name_en": "Bullish Engulfing",
        "direction": "BULLISH",
        "score": 13,
        "category": "CANDLESTICK",
        "timeframe": "5m",
    }
    elliott_wave = {
        "status": "CANDIDATE",
        "pattern": "BULLISH",
        "phase": "WAVES_1_AND_2_CONFIRMED",
        "wave_count": 2,
        "next_wave": 3,
        "confirmed_pivot_count": 3,
        "pivot_reversal_threshold_pct": 0.15,
        "invalidation_price": 98.0,
        "pivots": [
            {"index": 1, "kind": "LOW", "price": 98.0, "timestamp": "2026-11-18T09:35:00+00:00", "confirmed_at": "2026-11-18T09:40:00+00:00"},
            {"index": 2, "kind": "HIGH", "price": 100.0, "timestamp": "2026-11-18T09:45:00+00:00", "confirmed_at": "2026-11-18T09:50:00+00:00"},
            {"index": 3, "kind": "LOW", "price": 99.0, "timestamp": "2026-11-18T09:55:00+00:00", "confirmed_at": "2026-11-18T10:00:00+00:00"},
        ],
        "reason": "Synthetic directional Elliott wave prefix for reviewer integration testing.",
    }
    evidence = {
        "decision_engine_version": "CHALLENGE_1_LOCAL_RULES_V5_5M_ELLIOTT_AWARE",
        "completed_candles_only": True,
        "setup_timeframe": "5m",
        "higher_timeframes_used": False,
        "chosen_pattern": dict(chosen_pattern),
        "detected_patterns": [dict(chosen_pattern)],
        "confirmation_candle": dict(candles_5m[-1]),
        "candles_5m": candles_5m,
        "chart_structure": {},
        "elliott_wave": elliott_wave,
        "wave_alignment": "ALIGNED",
        "volume_confirmation": {
            "confirmed": True, "volume_ratio": 1.5,
            "close_location": 0.8, "minimum_volume_ratio": 1.2,
        },
        "decision": "LONG",
        "legacy_12_gates_used_for_entry": False,
        "external_ai_required": True,
        "tinyfish_required": False,
    }
    record = {
        "asset": "perp:BTCUSDT",
        "signal": "LONG",
        "execution_profile": "CHALLENGE_1",
        "entry_price_usd": 101.0,
        "stop_loss_usd": 99.0,
        "take_profit_usd": 105.0,
        "inst_id": "BTC-USDT",
        "instrument_id": "BTC-USDT",
        "signal_bar_open_utc": signal_bar,
        "signal_id": signal_id,
        "execution": {"status": "PASS", "direction": "LONG", "signal_id": signal_id},
        "challenge1_evidence": evidence,
    }
    result = SimpleNamespace(
        asset=asset.raw,
        signal=Signal.LONG,
        to_record=lambda: record,
        confirmation_score=82.0,
        estimated_risk_score=20.0,
        opportunity_rank=80.0,
        entry_price=101.0,
        stop_loss=99.0,
        take_profit=105.0,
        expected_holding_days=2,
        metrics={"execution_profile": "CHALLENGE_1", "challenge1_evidence": evidence},
        gates=[],
    )
    quorum = _run_challenge1_supervisor_reviews(result, asset)
    consensus = quorum.get("ai_consensus") if isinstance(quorum.get("ai_consensus"), dict) else {}
    payload = {
        "provider": "Challenge 1 Independent Reviewer Pair",
        "model": "Nemotron + independently configured second reviewer",
        "status": quorum.get("status", "UNKNOWN"),
        "status_fa": quorum.get("reason_fa") or "بررسی دو ناظر کامل نشد.",
        "ai_review": quorum.get("ai_review"),
        "second_ai_review": quorum.get("second_ai_review"),
        "ai_reviews": quorum.get("ai_reviews", []),
        "ai_consensus": consensus,
        "test_mode": "SYNTHETIC_CHALLENGE1_DUAL_REVIEWER_DRY_RUN",
        "input_data": "SYNTHETIC_NOT_LIVE_MARKET",
        "synthetic_asset": "perp:BTCUSDT",
        "synthetic_setup": {
            "timeframe": "5m",
            "pattern": chosen_pattern["name_en"],
            "elliott_wave_phase": elliott_wave["phase"],
            "elliott_wave_direction": elliott_wave["pattern"],
            "signal": "LONG",
            "orders_enabled": False,
        },
        "test_passed": bool(
            quorum.get("approved") is True
            and consensus.get("status") == "APPROVED_BY_BOTH"
            and consensus.get("primary_verified") is True
            and consensus.get("second_verified") is True
            and consensus.get("providers_independent") is True
            and consensus.get("models_independent") is True
        ),
        "ai_approval_for_synthetic_scenario": quorum.get("approved") is True,
        "paper_entry_eligible": False,
        "orders_submitted": False,
        "trade_executed": False,
    }
    return payload


def _blofin_api_key_permission(info: dict | None) -> str:
    """Translate BloFin's readOnly flag independently from this route's GET-only scope."""
    if not isinstance(info, dict):
        return "UNKNOWN"
    value = info.get("readOnly")
    if isinstance(value, bool):
        return "UNKNOWN"
    if value in (1, "1"):
        return "READ_ONLY"
    if value in (0, "0"):
        return "READ_WRITE"
    return "UNKNOWN"


def _blofin_demo_connection_status() -> dict:
    """Return a rate-limited, non-sensitive read-only BloFin Demo connectivity result."""
    with BLOFIN_STATUS_LOCK:
        now = time.monotonic()
        cached = BLOFIN_STATUS_CACHE.get("payload")
        if isinstance(cached, dict) and BLOFIN_STATUS_CACHE.get("expires_at", 0.0) > now:
            return copy.deepcopy(cached)

        # Use the same standard + legacy credential aliases as the Demo adapter.
        credential_name_groups = tuple(DEMO_READ_CREDENTIAL_ENV.values())
        credential_names = tuple(
            name for name_group in credential_name_groups for name in name_group
        )
        configured = all(
            any(bool(os.getenv(name, "").strip()) for name in name_group)
            for name_group in credential_name_groups
        )
        stage = "ENVIRONMENT_CHECK"
        market_data = "NOT_CHECKED"
        private_api = "NOT_CHECKED"
        api_key_permission = "UNKNOWN"
        if not configured:
            payload = {
                "status": "NOT_CONFIGURED",
                "environment": "BLOFIN_DEMO",
                "permission_mode": "READ_ONLY_REQUESTS",
                "market_data": market_data,
                "private_api": private_api,
                "orders_enabled": False,
                "message_fa": "سه متغیر API Key، Secret Key و Passphrase باید در Environment همین سرویس تنظیم شوند.",
            }
            cache_seconds = 15
        else:
            try:
                client = BloFinDemoReadOnly(timeout=8.0)

                stage = "PRIVATE_API_KEY_PERMISSION"
                api_key_permission = _blofin_api_key_permission(client.get_api_key_info())

                stage = "PUBLIC_MARKET_TICKER"
                tickers = client.get_tickers(inst_id="BTC-USDT")
                if not isinstance(tickers, list) or not tickers or not isinstance(tickers[0], dict):
                    raise BloFinDemoResponseError("BloFin Demo ticker response is unavailable.")
                market_data = "PASS"

                stage = "PRIVATE_FUTURES_BALANCE"
                private_api = "IN_PROGRESS"
                balance = client.get_futures_balance(product_type="USDT-FUTURES")
                if not isinstance(balance, dict):
                    raise BloFinDemoResponseError("BloFin Demo futures balance response is invalid.")

                stage = "PRIVATE_POSITIONS"
                positions = client.get_positions()
                if not isinstance(positions, list):
                    raise BloFinDemoResponseError("BloFin Demo positions response is invalid.")

                stage = "PRIVATE_OPEN_ORDERS"
                orders = client.get_open_orders()
                if not isinstance(orders, list):
                    raise BloFinDemoResponseError("BloFin Demo open-orders response is invalid.")

                private_api = "PASS"
                payload = {
                    "status": "CONNECTED_READ_ONLY",
                    "environment": "BLOFIN_DEMO",
                    "permission_mode": "READ_ONLY_REQUESTS",
                    "api_key_permission": api_key_permission,
                    "market_data": market_data,
                    "private_api": private_api,
                    "orders_enabled": False,
                    "message_fa": "اتصال Demo برقرار است. این مسیر فقط درخواست GET می‌فرستد؛ مجوز واقعی کلید در api_key_permission گزارش می‌شود و از این مسیر سفارشی ارسال نمی‌شود.",
                }
                cache_seconds = 60
            except BloFinDemoAuthError as exc:
                failed_credential = next(
                    (name for name in credential_names if name in str(exc)),
                    None,
                )
                if failed_credential:
                    message_fa = (
                        f"مقدار {failed_credential} دارای فاصله یا کاراکتر غیرمجاز است؛ "
                        "همان مقدار را از BloFin دوباره کپی و در Render جایگزین کن."
                    )
                else:
                    message_fa = "BloFin احراز هویت API را رد کرد؛ API Key، Secret، Passphrase و مجوز READ را بررسی کن."
                payload = {
                    "status": "AUTH_FAILED",
                    "environment": "BLOFIN_DEMO",
                    "permission_mode": "READ_ONLY_REQUESTS",
                    "market_data": market_data,
                    "private_api": "FAIL" if stage.startswith("PRIVATE_") else "NOT_CHECKED",
                    "orders_enabled": False,
                    "failed_stage": stage,
                    "failed_credential": failed_credential,
                    "message_fa": message_fa,
                }
                cache_seconds = 20
            except BloFinDemoError as exc:
                cause = getattr(exc, "__cause__", None)
                response = getattr(cause, "response", None)
                http_status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
                if http_status == 403:
                    message_fa = "BloFin درخواست را با HTTP 403 رد کرده است؛ محدودیت Firewall یا IP بررسی شود و از تکرار سریع درخواست خودداری کن."
                elif http_status == 429:
                    message_fa = "BloFin محدودیت تعداد درخواست اعمال کرده است؛ پیش از آزمایش بعدی مکث کن."
                else:
                    message_fa = "درخواست به BloFin کامل نشد؛ مرحله و نوع خطای فنی برای تشخیص دقیق ثبت شده است."
                payload = {
                    "status": "CONNECTION_FAILED",
                    "environment": "BLOFIN_DEMO",
                    "permission_mode": "READ_ONLY_REQUESTS",
                    "api_key_permission": api_key_permission,
                    "market_data": "FAIL" if stage == "PUBLIC_MARKET_TICKER" else market_data,
                    "private_api": "FAIL" if stage.startswith("PRIVATE_") else private_api,
                    "orders_enabled": False,
                    "failed_stage": stage,
                    "error_type": type(exc).__name__,
                    "cause_type": type(cause).__name__ if cause is not None else None,
                    "http_status": http_status,
                    "message_fa": message_fa,
                }
                cache_seconds = 20
            except Exception as exc:
                payload = {
                    "status": "UNKNOWN",
                    "environment": "BLOFIN_DEMO",
                    "permission_mode": "READ_ONLY_REQUESTS",
                    "market_data": market_data,
                    "private_api": private_api,
                    "orders_enabled": False,
                    "failed_stage": stage,
                    "error_type": type(exc).__name__,
                    "message_fa": "آزمون اتصال با خطای پیش‌بینی‌نشده متوقف شد؛ اطلاعات محرمانه در پاسخ عمومی نمایش داده نمی‌شود.",
                }
                cache_seconds = 20

        payload.setdefault("api_key_permission", api_key_permission)
        payload.setdefault("request_scope", "READ_ONLY_GET_REQUESTS")
        BLOFIN_STATUS_CACHE["payload"] = copy.deepcopy(payload)
        BLOFIN_STATUS_CACHE["expires_at"] = time.monotonic() + cache_seconds
        return copy.deepcopy(payload)


def _blofin_dashboard_token_valid(auth_header: str, expected_token: str) -> bool:
    """Validate the separate dashboard token without logging or exposing it."""
    if not expected_token or not isinstance(auth_header, str):
        return False
    scheme, separator, supplied = auth_header.partition(" ")
    return bool(
        separator
        and scheme.lower() == "bearer"
        and supplied
        and hmac.compare_digest(supplied, expected_token)
    )


V2_AUTOPILOT_RUN_LOCK = threading.Lock()
V2_AUTOPILOT_RUN_STATE = {
    "status": "IDLE", "job_id": None, "started_at_utc": None,
    "finished_at_utc": None, "result": None, "error_type": None,
}


def _run_challenge1_v2_dashboard_cycle(job_id: str) -> None:
    """Run the same Turso-backed V2 run_cycle used by the scheduled worker."""
    connection = None
    store = None
    try:
        from .challenge1_v2_autopilot import build_v2_demo_risk_policy, preflight_errors, run_cycle
        from .blofin_demo_readonly import DEMO_REST_URL
        if os.getenv("BLOFIN_DEMO_AUTOPILOT_ENABLED", "").strip().lower() != "false":
            raise BloFinDemoExecutionBlocked("Legacy AutoPilot must remain explicitly false.")
        errors = preflight_errors()
        if errors:
            raise BloFinDemoExecutionBlocked("; ".join(errors))
        if not _enabled_v2_env("BLOFIN_DEMO_TRADING_ENABLED"):
            raise BloFinDemoExecutionBlocked("BLOFIN_DEMO_TRADING_ENABLED is not true.")
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            raise BloFinDemoExecutionBlocked("Durable Turso storage is required.")
        if os.getenv("BLOFIN_DEMO_AUTOPILOT_ROLE", "WEB").strip().upper() != "WEB":
            raise BloFinDemoExecutionBlocked("This Render process is not the WEB execution role.")
        client = BloFinDemoTradeClient(timeout=10.0)
        if str(getattr(client, "base_url", "")).rstrip("/") != DEMO_REST_URL.rstrip("/"):
            raise BloFinDemoExecutionBlocked("BloFin endpoint is not the fixed Demo REST host.")
        store = BloFinDemoEventStore()
        executor = BloFinDemoExecutor(
            client=client, store=store, policy=build_v2_demo_risk_policy(),
            trading_enabled=True,
        )
        executor.recover_unresolved_orders_from_journal()
        connection = connect_database()
        with LOCK:
            V2_AUTOPILOT_RUN_STATE.update({
                "status": "RUNNING", "job_id": job_id,
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "finished_at_utc": None, "result": None, "error_type": None,
            })
        result = run_cycle(client, connection, executor=executor, environ=os.environ)
        with LOCK:
            V2_AUTOPILOT_RUN_STATE.update({
                "status": result.get("status", "UNKNOWN"), "result": result,
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as exc:
        with LOCK:
            V2_AUTOPILOT_RUN_STATE.update({
                "status": "BLOCKED_OR_ERROR", "error_type": type(exc).__name__,
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "result": {"status": "BLOCKED_OR_ERROR", "orders_submitted": False},
            })
        print("CSR_V2_DASHBOARD_CYCLE " + json.dumps({
            "job_id": job_id, "status": "BLOCKED_OR_ERROR",
            "error_type": type(exc).__name__,
        }, sort_keys=True), flush=True)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        V2_AUTOPILOT_RUN_LOCK.release()


def _enabled_v2_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() == "true"


def _blofin_v2_cycle_snapshot() -> dict:
    """Return a secret-free recent V2 cycle and open-exit-state snapshot from Turso."""
    connection = None
    store = None
    try:
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            raise RuntimeError("DURABLE_TURSO_NOT_CONFIGURED")
        connection = connect_database()
        connection.execute("""CREATE TABLE IF NOT EXISTS challenge1_v2_autopilot_cycles (
            cycle_id TEXT NOT NULL, symbol TEXT NOT NULL, started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT NOT NULL, environment TEXT NOT NULL, mode TEXT NOT NULL,
            status TEXT NOT NULL, signal TEXT, reason TEXT, orders_submitted INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL, PRIMARY KEY (cycle_id, symbol))""")
        rows = connection.execute(
            "SELECT cycle_id,symbol,started_at_utc,finished_at_utc,mode,status,signal,reason,orders_submitted,payload_json "
            "FROM challenge1_v2_autopilot_cycles ORDER BY finished_at_utc DESC LIMIT 25"
        ).fetchall()
        cycles = []
        for row in rows:
            item = dict(row)
            try:
                payload = json.loads(item.pop("payload_json") or "{}")
            except (TypeError, ValueError):
                payload = {}
            reviews = payload.get("ai_reviews") if isinstance(payload.get("ai_reviews"), list) else []
            votes = []
            for entry in reviews[:2]:
                if not isinstance(entry, dict):
                    continue
                review = entry.get("review") if isinstance(entry.get("review"), dict) else entry
                votes.append({
                    "reviewer_id": entry.get("reviewer_id") or review.get("reviewer_id"),
                    "provider": review.get("provider") or entry.get("provider"),
                    "status": review.get("status") or entry.get("status") or "UNKNOWN",
                    "opinion": (
                        review.get("decision") or review.get("verdict")
                        or review.get("final_rating") or review.get("proposal")
                        or review.get("recommendation") or review.get("status")
                        or entry.get("status") or "UNKNOWN"
                    ),
                    "reason_fa": str(review.get("reason_fa") or review.get("status_fa") or "")[:320],
                    "verified": review.get("model_verified") is True and review.get("response_validated") is True,
                    "returned_model": review.get("returned_model") or review.get("model") or entry.get("model"),
                })
            item["ai_votes"] = votes
            item["ai_consensus"] = payload.get("ai_consensus") or {}
            item["exit_manager"] = payload.get("exit_manager_summary") or []
            cycles.append(item)
        store = BloFinDemoEventStore()
        raw_positions = store.list_v2_position_exit_states(active_only=True, limit=100)
        positions = []
        for wrapped in raw_positions:
            state = dict(wrapped.get("payload") or wrapped)
            positions.append({
                "position_id": state.get("position_id"),
                "instrument_id": state.get("instrument_id"),
                "side": state.get("side"),
                "status": state.get("status"),
                "tp1_executed": state.get("tp1_executed"),
                "candles_elapsed": state.get("candles_elapsed"),
                "remaining_size": state.get("remaining_size"),
            })
        with LOCK:
            run_state = copy.deepcopy(V2_AUTOPILOT_RUN_STATE)
        return {
            "status": "OK", "environment": "BLOFIN_DEMO",
            "run_state": run_state, "cycles": cycles, "open_exit_positions": positions,
        }
    except Exception as exc:
        return {
            "status": "UNAVAILABLE", "environment": "BLOFIN_DEMO",
            "error_type": type(exc).__name__, "cycles": [], "open_exit_positions": [],
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if store is not None:
            try:
                store.close()
            except Exception:
                pass



def _verify_staging_github_oidc_token(authorization: str) -> dict:
    """Verify a short-lived GitHub Actions OIDC token for this Staging workflow only."""
    prefix = "Bearer "
    if not isinstance(authorization, str) or not authorization.startswith(prefix):
        raise PermissionError("OIDC_BEARER_REQUIRED")
    token = authorization[len(prefix):].strip()
    if not token or len(token) > 12_000:
        raise PermissionError("OIDC_TOKEN_INVALID")
    try:
        import jwt
        client = jwt.PyJWKClient(
            "https://token.actions.githubusercontent.com/.well-known/jwks",
            timeout=8,
        )
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience="csr-staging-cycle-sync",
            issuer="https://token.actions.githubusercontent.com",
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except Exception as exc:
        raise PermissionError("OIDC_TOKEN_VERIFICATION_FAILED") from exc
    expected_repo = "Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi"
    expected_ref = "refs/heads/staging/challenge1-integration-2026-11-16"
    expected_workflow = (
        expected_repo + "/.github/workflows/challenge1-v2-demo-autopilot.yml@" + expected_ref
    )
    if (
        claims.get("repository") != expected_repo
        or claims.get("ref") != expected_ref
        or claims.get("workflow_ref") != expected_workflow
        or claims.get("event_name") not in {"push", "workflow_dispatch"}
    ):
        raise PermissionError("OIDC_WORKFLOW_SCOPE_MISMATCH")
    return {
        "repository": expected_repo,
        "ref": expected_ref,
        "workflow_ref": expected_workflow,
        "run_id": str(claims.get("run_id") or "")[:40],
    }


def _publish_blofin_v2_cycle_report(report: dict) -> dict:
    """Persist a verified, read-only Staging Demo cycle report into this app's Turso DB."""
    if not isinstance(report, dict):
        raise ValueError("CYCLE_REPORT_INVALID")
    if report.get("environment") != "BLOFIN_DEMO":
        raise ValueError("CYCLE_ENVIRONMENT_NOT_DEMO")
    cycle_id = str(report.get("cycle_id") or "").strip()
    if not re.fullmatch(r"[a-fA-F0-9]{16,64}", cycle_id):
        raise ValueError("CYCLE_ID_INVALID")
    symbols = report.get("symbols")
    expected = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT"]
    if not isinstance(symbols, list) or [str(row.get("symbol") or "") for row in symbols if isinstance(row, dict)] != expected:
        raise ValueError("CYCLE_SYMBOL_SET_INVALID")
    if len(symbols) != 5 or any(not isinstance(row, dict) for row in symbols):
        raise ValueError("CYCLE_SYMBOL_ROWS_INVALID")
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("DURABLE_TURSO_NOT_CONFIGURED")

    connection = connect_database()
    finished_at = datetime.now(timezone.utc).isoformat()
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS challenge1_v2_autopilot_cycles (
            cycle_id TEXT NOT NULL, symbol TEXT NOT NULL, started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT NOT NULL, environment TEXT NOT NULL, mode TEXT NOT NULL,
            status TEXT NOT NULL, signal TEXT, reason TEXT, orders_submitted INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL, PRIMARY KEY (cycle_id, symbol))""")
        for row in symbols:
            proof = row.get("market_data_proof") if isinstance(row.get("market_data_proof"), dict) else {}
            started_at = str(proof.get("asof_utc") or report.get("started_at_utc") or finished_at)
            payload = {
                "ai_reviews": report.get("ai_reviews") if isinstance(report.get("ai_reviews"), list) else [],
                "ai_consensus": report.get("ai_consensus") if isinstance(report.get("ai_consensus"), dict) else {},
                "ai_reviews_performed": report.get("ai_reviews_performed") is True,
                "market_data_proof": proof,
                "regime": row.get("regime") if isinstance(row.get("regime"), dict) else {},
                "entry_skipped_reason": row.get("entry_skipped_reason"),
                "cycle_status": report.get("status"),
                "source": "GITHUB_ACTIONS_OIDC_VERIFIED_STAGING_WORKER",
            }
            connection.execute(
                """INSERT OR REPLACE INTO challenge1_v2_autopilot_cycles
                (cycle_id,symbol,started_at_utc,finished_at_utc,environment,mode,status,signal,reason,orders_submitted,payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, str(row.get("symbol")), started_at, finished_at,
                    "BLOFIN_DEMO", str(row.get("mode") or report.get("mode") or "DEMO_AUTOPILOT"),
                    str(row.get("status") or "UNKNOWN"), str(row.get("signal") or "NO_TRADE"),
                    str(row.get("reason") or "")[:500],
                    1 if row.get("orders_submitted") is True or report.get("orders_submitted") is True else 0,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                ),
            )
        if hasattr(connection, "commit"):
            connection.commit()
        return {
            "status": "PUBLISHED", "environment": "BLOFIN_DEMO",
            "cycle_id": cycle_id, "rows_written": 5,
            "finished_at_utc": finished_at,
        }
    finally:
        connection.close()


def _blofin_v2_public_cycle_snapshot() -> dict:
    """Read and expose only the latest five public market rows from Turso."""
    connection = None
    try:
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            raise RuntimeError("DURABLE_TURSO_NOT_CONFIGURED")
        connection = connect_database()
        connection.execute("""CREATE TABLE IF NOT EXISTS challenge1_v2_autopilot_cycles (
            cycle_id TEXT NOT NULL, symbol TEXT NOT NULL, started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT NOT NULL, environment TEXT NOT NULL, mode TEXT NOT NULL,
            status TEXT NOT NULL, signal TEXT, reason TEXT, orders_submitted INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL, PRIMARY KEY (cycle_id, symbol))""")
        rows = connection.execute(
            "SELECT cycle_id,symbol,started_at_utc,finished_at_utc,mode,status,signal,reason,orders_submitted,payload_json "
            "FROM challenge1_v2_autopilot_cycles ORDER BY finished_at_utc DESC LIMIT 25"
        ).fetchall()
        decoded = []
        for row in rows:
            item = dict(row)
            try:
                payload = json.loads(item.pop("payload_json") or "{}")
            except (TypeError, ValueError):
                payload = {}
            reviews = payload.get("ai_reviews") if isinstance(payload.get("ai_reviews"), list) else []
            votes = []
            for entry in reviews[:2]:
                if not isinstance(entry, dict):
                    continue
                review = entry.get("review") if isinstance(entry.get("review"), dict) else entry
                votes.append({
                    "reviewer_id": entry.get("reviewer_id") or review.get("reviewer_id"),
                    "provider": review.get("provider") or entry.get("provider"),
                    "status": review.get("status") or entry.get("status") or "UNKNOWN",
                    "opinion": (
                        review.get("decision") or review.get("verdict")
                        or review.get("final_rating") or review.get("proposal")
                        or review.get("recommendation") or review.get("status")
                        or entry.get("status") or "UNKNOWN"
                    ),
                    "reason_fa": str(review.get("reason_fa") or review.get("status_fa") or "")[:320],
                    "verified": review.get("model_verified") is True and review.get("response_validated") is True,
                    "returned_model": review.get("returned_model") or review.get("model") or entry.get("model"),
                })
            item["ai_votes"] = votes
            item["ai_consensus"] = payload.get("ai_consensus") if isinstance(payload.get("ai_consensus"), dict) else {}
            decoded.append(item)

        latest_cycle_id = decoded[0].get("cycle_id") if decoded else None
        latest_rows = [
            row for row in decoded
            if latest_cycle_id and row.get("cycle_id") == latest_cycle_id
        ]
        symbol_order = {"BTC-USDT": 0, "ETH-USDT": 1, "SOL-USDT": 2, "XRP-USDT": 3, "LINK-USDT": 4}
        latest_rows.sort(key=lambda row: symbol_order.get(str(row.get("symbol") or ""), 99))
        public_rows = []
        for row in latest_rows[:5]:
            public_rows.append({
                "cycle_id": row.get("cycle_id"),
                "symbol": row.get("symbol"),
                "started_at_utc": row.get("started_at_utc"),
                "finished_at_utc": row.get("finished_at_utc"),
                "mode": row.get("mode"),
                "status": row.get("status"),
                "signal": row.get("signal"),
                "reason": str(row.get("reason") or "")[:500],
                "orders_submitted": bool(row.get("orders_submitted")),
                "ai_votes": row.get("ai_votes") if isinstance(row.get("ai_votes"), list) else [],
                "ai_consensus": row.get("ai_consensus") if isinstance(row.get("ai_consensus"), dict) else {},
            })
        with LOCK:
            state = copy.deepcopy(V2_AUTOPILOT_RUN_STATE)
        return {
            "status": "OK", "environment": "BLOFIN_DEMO",
            "latest_cycle_id": latest_cycle_id,
            "scan_time_utc": (
                public_rows[0].get("finished_at_utc") or public_rows[0].get("started_at_utc")
            ) if public_rows else None,
            "cycles": public_rows,
            "run_state": {
                "status": state.get("status") or "UNKNOWN",
                "job_id": state.get("job_id"),
                "started_at_utc": state.get("started_at_utc"),
                "finished_at_utc": state.get("finished_at_utc"),
            },
        }
    except Exception as exc:
        return {
            "status": "UNAVAILABLE", "environment": "BLOFIN_DEMO",
            "error_type": type(exc).__name__,
            "cycles": [], "latest_cycle_id": None,
            "run_state": {"status": "UNKNOWN"},
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _blofin_demo_account_snapshot() -> dict:
    """Fetch a deliberately whitelisted read-only account snapshot from BloFin Demo."""
    try:
        client = BloFinDemoReadOnly(timeout=8.0)
        api_key_permission = _blofin_api_key_permission(client.get_api_key_info())
        balance = client.get_futures_balance(product_type="USDT-FUTURES")
        positions = client.get_positions()
        open_orders = client.get_open_orders()
        if not isinstance(balance, dict) or not isinstance(positions, list) or not isinstance(open_orders, list):
            raise BloFinDemoResponseError("BloFin account response has an unexpected shape.")

        def pick(row: dict, allowed: tuple[str, ...]) -> dict:
            return {key: row[key] for key in allowed if key in row}

        details = balance.get("details")
        details = details if isinstance(details, list) else []
        position_fields = (
            "positionId", "instId", "marginMode", "positionSide", "leverage", "positions",
            "availablePositions", "averagePrice", "markPrice", "liquidationPrice",
            "unrealizedPnl", "unrealizedPnlRatio", "initialMargin",
            "maintenanceMargin", "createTime", "updateTime",
        )
        order_fields = (
            "instId", "side", "positionSide", "orderType", "price", "size",
            "filledSize", "filledAmount", "state", "createTime", "updateTime",
        )
        balance_fields = (
            "currency", "equity", "balance", "available", "availableEquity",
            "frozen", "orderFrozen", "equityUsd", "isolatedUnrealizedPnl", "bonus",
        )
        return {
            "status": "CONNECTED_READ_ONLY",
            "environment": "BLOFIN_DEMO",
            "permission_mode": "READ_ONLY_REQUESTS",
            "api_key_permission": api_key_permission,
            "request_scope": "READ_ONLY_GET_REQUESTS",
            "orders_enabled": False,
            "balance": {
                "ts": balance.get("ts"),
                "totalEquity": balance.get("totalEquity"),
                "isolatedEquity": balance.get("isolatedEquity"),
                "details": [
                    pick(row, balance_fields) for row in details if isinstance(row, dict)
                ],
            },
            "positions": [
                pick(row, position_fields) for row in positions if isinstance(row, dict)
            ],
            "open_orders": [
                pick(row, order_fields) for row in open_orders if isinstance(row, dict)
            ],
            "message_fa": "اطلاعات حساب Demo فقط خوانده شده است؛ هیچ سفارش یا تغییری ارسال نشده است.",
        }
    except BloFinDemoAuthError:
        return {
            "status": "AUTH_FAILED",
            "environment": "BLOFIN_DEMO",
            "permission_mode": "READ_ONLY_REQUESTS",
            "orders_enabled": False,
            "message_fa": "BloFin احراز هویت درخواست خواندن حساب را رد کرد.",
        }
    except BloFinDemoError as exc:
        return {
            "status": "READ_FAILED",
            "environment": "BLOFIN_DEMO",
            "permission_mode": "READ_ONLY_REQUESTS",
            "orders_enabled": False,
            "error_type": type(exc).__name__,
            "message_fa": "خواندن اطلاعات حساب از BloFin Demo کامل نشد.",
        }
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "environment": "BLOFIN_DEMO",
            "permission_mode": "READ_ONLY_REQUESTS",
            "orders_enabled": False,
            "error_type": type(exc).__name__,
            "message_fa": "خواندن حساب با خطای پیش‌بینی‌نشده متوقف شد.",
        }


COMMON_ASSET_NAMES = {
    "BTC": ("Bitcoin", "بیت‌کوین"), "ETH": ("Ethereum", "اتریوم"),
    "XRP": ("XRP / Ripple", "ریپل"), "SOL": ("Solana", "سولانا"),
    "BNB": ("BNB", "بی‌ان‌بی"), "DOGE": ("Dogecoin", "دوج‌کوین"),
    "ADA": ("Cardano", "کاردانو"), "AVAX": ("Avalanche", "آوالانچ"),
    "LINK": ("Chainlink", "چین‌لینک"), "DOT": ("Polkadot", "پولکادات"),
    "LTC": ("Litecoin", "لایت‌کوین"), "TRX": ("TRON", "ترون"),
    "TON": ("Gram (formerly Toncoin)", "گرام (تون‌کوین سابق)"),
    "GRAM": ("Gram (formerly Toncoin)", "گرام (تون‌کوین سابق)"), "PEPE": ("Pepe", "پپه"),
    "SHIB": ("Shiba Inu", "شیبا اینو"),
}
ASSET_NAME_ALIASES = {
    "BITCOIN": "BTCUSDT", "ETHEREUM": "ETHUSDT", "RIPPLE": "XRPUSDT",
    "SOLANA": "SOLUSDT", "DOGECOIN": "DOGEUSDT", "CARDANO": "ADAUSDT",
    "AVALANCHE": "AVAXUSDT", "CHAINLINK": "LINKUSDT", "POLKADOT": "DOTUSDT",
    "LITECOIN": "LTCUSDT", "TRON": "TRXUSDT", "TON": "GRAMUSDT", "TONCOIN": "GRAMUSDT", "GRAM": "GRAMUSDT",
}
TIMEFRAME_WINDOWS = (("1h", 1), ("6h", 6), ("12h", 12), ("1d", 24), ("1w", 168), ("1mo", 720))

# Small, explicitly curated public spot board; it is not a market-cap ranking.
CRYPTO_MARKET_UNIVERSE = (
    ("BTCUSDT", "Bitcoin", "بیت‌کوین"),
    ("ETHUSDT", "Ethereum", "اتریوم"),
    ("SOLUSDT", "Solana", "سولانا"),
    ("BNBUSDT", "BNB", "بی‌ان‌بی"),
    ("XRPUSDT", "XRP", "ریپل"),
    ("DOGEUSDT", "Dogecoin", "دوج‌کوین"),
    ("ADAUSDT", "Cardano", "کاردانو"),
    ("AVAXUSDT", "Avalanche", "آوالانچ"),
    ("LINKUSDT", "Chainlink", "چین‌لینک"),
    ("DOTUSDT", "Polkadot", "پولکادات"),
    ("LTCUSDT", "Litecoin", "لایت‌کوین"),
    ("TRXUSDT", "TRON", "ترون"),
    ("GRAMUSDT", "Gram (formerly Toncoin)", "گرام (تون‌کوین سابق)"),
    ("PEPEUSDT", "Pepe", "پپه"),
    ("SHIBUSDT", "Shiba Inu", "شیبا اینو"),
    ("BCHUSDT", "Bitcoin Cash", "بیت‌کوین کش"),
    ("SUIUSDT", "Sui", "سویی"),
    ("HBARUSDT", "Hedera", "هدرا"),
    ("NEARUSDT", "NEAR Protocol", "نیر"),
    ("UNIUSDT", "Uniswap", "یونی‌سواپ"),
)
CRYPTO_MARKET_CACHE_TTL_SECONDS = 60
CRYPTO_MARKET_CACHE = {"expires_at": 0.0, "payload": None, "refreshing": False, "last_error": None}
CRYPTO_MARKET_CACHE_LOCK = threading.Lock()

LIVE_MARKET_RADAR_CACHE: dict[tuple[str, str], dict] = {}
LIVE_MARKET_RADAR_CACHE_TTL_SECONDS = 8
LIVE_MARKET_RADAR_CACHE_LOCK = threading.RLock()
GEM_HUNTER_CACHE_TTL_SECONDS = 60
GEM_HUNTER_CACHE = {"expires_at": 0.0, "payload": None}
GEM_HUNTER_CACHE_LOCK = threading.Lock()
GEM_HUNTER_RESULT_CACHE_TTL_SECONDS = 90
GEM_HUNTER_RESULT_CACHE = {
    "expires_at": 0.0, "payload": None, "refreshing": False, "last_error": None,
}
GEM_HUNTER_RESULT_CACHE_LOCK = threading.Lock()


def _gem_hunter_discovery_payload(limit: int = 20, *, include_diagnostics: bool = False, progress_callback=None) -> dict:
    """Return strict BUYs and optionally publish discovery-only progress before validation."""
    limit = max(1, min(int(limit), 50))
    with GEM_HUNTER_CACHE_LOCK:
        now = time.monotonic()
        cached = GEM_HUNTER_CACHE.get("payload")
        if cached is None or GEM_HUNTER_CACHE.get("expires_at", 0.0) <= now:
            cached = discover_gem_candidates(getattr(ENGINE.data, "http", None), limit=50)
            GEM_HUNTER_CACHE["payload"] = copy.deepcopy(cached)
            # Do not hammer rate-limited feeds after PARTIAL results.
            ttl = GEM_HUNTER_CACHE_TTL_SECONDS if cached.get("status") == "PASS" else 90
            GEM_HUNTER_CACHE["expires_at"] = time.monotonic() + ttl
        result = copy.deepcopy(cached)

    # Discovery records are deliberately never exposed by this public endpoint.
    # Count evaluation records without assuming that discovery itself is a
    # qualification engine or that an incomplete record is a completed evaluation.
    raw_candidates = result.get("candidates")
    raw_candidates = raw_candidates if isinstance(raw_candidates, list) else []
    if callable(progress_callback):
        progress_candidates = []
        for item in raw_candidates[:limit]:
            if not isinstance(item, dict) or item.get("trade_authorization") == "BUY":
                continue
            progress_item = copy.deepcopy(item)
            progress_item.setdefault("trade_authorization", "NO_TRADE")
            progress_candidates.append(progress_item)
        progress_payload = _gem_hunter_pending_payload(refreshing=True)
        progress_payload.update({
            "status": result.get("status", "UNKNOWN"),
            "generated_at_utc": result.get("generated_at_utc"),
            "networks": copy.deepcopy(result.get("networks") or ["base", "solana", "ethereum"]),
            "source_count": result.get("source_count", 0),
            "sources": copy.deepcopy(result.get("sources") or {}),
            "discovered_candidate_count": len(raw_candidates),
            "diagnostic_candidates": progress_candidates,
            "progress_stage": "DISCOVERY_COMPLETE",
            "diagnostic_freshness": "CURRENT_SCAN_DISCOVERY",
            "diagnostic_fa": (
                f"کشف اولیه پایان یافت؛ {len(raw_candidates)} نامزد پیدا شد. "
                "تطبیق مستقل بازار و غربال امنیت هنوز در حال اجراست؛ این فهرست سیگنال BUY نیست."
            ),
        })
        progress_payload["public_policy"]["discovery_candidates_exposed"] = True
        progress_payload["public_policy"]["unknown_or_incomplete_candidates_hidden"] = True
        progress_callback(progress_payload)
    # Discovery and qualification are separate stages. Attach an explicit,
    # evidence-bounded first-stage record to every raw discovery candidate.
    # This evaluator deliberately leaves unavailable security/holder/whale/
    # slippage evidence UNKNOWN and can never authorize a BUY.
    # Validate the exact discovered token/pool against independent market sources
    # before constructing qualification gates. The bounded shortlist and cache
    # prevent an unbounded burst against public APIs; unchecked rows remain explicit.
    raw_candidates = reconcile_discovery_candidates(
        raw_candidates, getattr(ENGINE.data, "http", None),
    )
    raw_candidates = screen_discovery_candidates(
        raw_candidates, ENGINE.data,
    )
    # Fetch a bounded sample of exact-pool OHLCV and BTC/ETH daily benchmarks.
    # The technical module caches complete evidence and rotates one candidate
    # per scan to respect the free public GeckoTerminal request budget.
    raw_candidates = enrich_discovery_candidates_with_technicals(
        raw_candidates, ENGINE.data, limit=1,
    )
    # Collect a separate bounded sample of actual swaps for the same validated Pool.
    raw_candidates = enrich_discovery_candidates_with_pool_trades(
        raw_candidates, ENGINE.data, limit=1,
    )
    raw_candidates = evaluate_discovery_candidates(raw_candidates)
    # V3 adds research-only scores, persistent lifecycle snapshots and optional
    # deduplicated Telegram alerts. It never creates a Futures/spot order.
    raw_candidates = process_gem_hunter_v3_candidates(raw_candidates, notify=True)
    discovered_count = len(raw_candidates)
    evaluation_records = [
        item.get("qualification")
        for item in raw_candidates
        if isinstance(item, dict) and isinstance(item.get("qualification"), dict)
    ]
    complete_evaluation_count = 0
    for qualification in evaluation_records:
        filters = qualification.get("filters")
        critical = qualification.get("critical_gates")
        metrics = qualification.get("market_metrics")
        sources = qualification.get("market_data_sources")
        whale_evidence = qualification.get("whale_evidence")
        complete = (
            qualification.get("schema_version") == SCHEMA_VERSION
            and qualification.get("decision") in {"BUY", "NO_TRADE", "REJECT"}
            and qualification.get("market_type") == "SPOT"
            and isinstance(filters, dict)
            and set(filters) == set(FILTER_IDS)
            and all(value in {"PASS", "FAIL", "UNKNOWN"} for value in filters.values())
            and isinstance(critical, dict)
            and all(gate in critical and critical[gate] in {"PASS", "FAIL", "UNKNOWN"}
                    for gate in CRITICAL_GATES)
            and isinstance(metrics, dict)
            and bool(qualification.get("data_as_of_utc"))
            and isinstance(sources, list)
            and len({str(value).strip() for value in sources if str(value).strip()}) >= 2
            and isinstance(whale_evidence, dict)
        )
        if complete:
            complete_evaluation_count += 1

    reconciliation_counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0, "NOT_CHECKED_BUDGET": 0}
    for item in raw_candidates:
        status = (item.get("market_reconciliation") or {}).get("status")
        if status in reconciliation_counts:
            reconciliation_counts[status] += 1
        else:
            reconciliation_counts["UNKNOWN"] += 1
    security_counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0, "NOT_CHECKED_BUDGET": 0}
    for item in raw_candidates:
        status = (item.get("security_evidence") or {}).get("status")
        if status in security_counts:
            security_counts[status] += 1
        else:
            security_counts["UNKNOWN"] += 1
    evaluation_record_count = len(evaluation_records)
    unassessed_count = max(0, discovered_count - evaluation_record_count)
    incomplete_evaluation_count = max(0, evaluation_record_count - complete_evaluation_count)
    qualified = public_buy_candidates(raw_candidates, limit=limit)

    if qualified:
        signal_status = "BUY_FOUND"
        diagnostic_fa = f"{len(qualified)} فرصت BUY با شواهد کامل تأیید شد."
        engine_status = "COMPLETE"
    elif discovered_count == 0:
        if result.get("status") == "PASS":
            signal_status = "NO_DISCOVERY_CANDIDATES"
            diagnostic_fa = "منابع کشف پاسخ موفق داده‌اند، اما در این دریافت هیچ نامزدی برنگشته است."
        else:
            signal_status = "DISCOVERY_FEED_DEGRADED"
            diagnostic_fa = "دادهٔ منابع کشف کامل نیست؛ صفر بودن خروجی را نمی‌توان به معنی نبود فرصت بازار دانست."
        engine_status = "NO_CANDIDATES"
    elif evaluation_record_count == 0:
        signal_status = "EVALUATION_NOT_CONFIGURED"
        diagnostic_fa = (
            f"{discovered_count} نامزد کشف شد، اما برای هیچ‌کدام پروندهٔ ارزیابی ۱۲ فیلتر و ۴ گیت ساخته نشده است؛ "
            "بنابراین هنوز نمی‌توان گفت این کوین‌ها رد شده‌اند یا واجد BUY هستند."
        )
        engine_status = "NOT_WIRED"
    elif complete_evaluation_count < discovered_count:
        signal_status = "EVALUATION_INCOMPLETE"
        diagnostic_fa = (
            f"{discovered_count} نامزد کشف شد؛ {complete_evaluation_count} پرونده با ساختار ارزیابی کامل، "
            f"{incomplete_evaluation_count} پروندهٔ ناقص و {unassessed_count} نامزدِ ارزیابی‌نشده وجود دارد. "
            "تا تکمیل شواهد، هیچ سیگنال BUY صادر نمی‌شود."
        )
        engine_status = "INCOMPLETE"
    else:
        signal_status = "NO_QUALIFIED_BUY"
        diagnostic_fa = (
            f"ساختار پروندهٔ ارزیابی برای هر {complete_evaluation_count} نامزد کامل است، اما شواهد یا شروط هیچ‌کدام BUY را تأیید نمی‌کند."
        )
        engine_status = "COMPLETE"

    result.pop("trade_authorization", None)
    result.pop("available_candidate_count", None)
    result["candidates"] = qualified
    result["gem_hunter_v3_tier1_count"] = sum(1 for item in raw_candidates if item.get("gem_hunter_v3_status") == "TIER_1_VERIFIED_GEM")
    result["gem_hunter_v3_tier2_count"] = sum(1 for item in raw_candidates if item.get("gem_hunter_v3_status") == "TIER_2_EARLY_ALPHA")
    result["gem_hunter_v3_review_count"] = sum(1 for item in raw_candidates if item.get("gem_hunter_v3_status") == "REVIEW_REQUIRED")
    result["gem_hunter_v3_blocked_count"] = sum(1 for item in raw_candidates if str(item.get("gem_hunter_v3_status") or "").startswith("BLOCKED_"))
    result["gem_hunter_v3_qualified_count"] = result["gem_hunter_v3_tier1_count"] + result["gem_hunter_v3_tier2_count"]
    result["candidate_count"] = len(qualified)
    result["discovered_candidate_count"] = discovered_count
    result["evaluation_record_count"] = evaluation_record_count
    result["market_reconciliation_counts"] = {
        "checked": reconciliation_counts["PASS"] + reconciliation_counts["FAIL"] + reconciliation_counts["UNKNOWN"],
        **reconciliation_counts,
    }
    result["security_screening_counts"] = security_counts
    result["complete_evaluation_count"] = complete_evaluation_count
    result["incomplete_evaluation_count"] = incomplete_evaluation_count
    result["unassessed_candidate_count"] = unassessed_count
    result["qualified_buy_count"] = len(qualified)
    result["qualification_engine_status"] = engine_status
    result["signal_status"] = signal_status
    result["diagnostic_fa"] = diagnostic_fa
    if include_diagnostics:
        # Separate research-only report; this never upgrades a candidate to BUY.
        result["diagnostic_candidates"] = copy.deepcopy(raw_candidates[:limit])
        result["gem_hunter_v3_candidates"] = copy.deepcopy(raw_candidates[:limit])
    result["progress_stage"] = "COMPLETE"
    result["diagnostic_freshness"] = "CURRENT_SCAN"
    result["public_policy"] = {
        "qualification_schema": SCHEMA_VERSION,
        "minimum_passed_filters": 9,
        "total_filters": len(FILTER_IDS),
        "minimum_opportunity_score": 80,
        "critical_gates": list(CRITICAL_GATES),
        "unknown_or_incomplete_candidates_hidden": True,
        "discovery_candidates_exposed": bool(include_diagnostics),
        "aggregate_pipeline_diagnostics_exposed": True,
        "diagnostic_candidates_are_trade_signals": False,
        "evaluation_completeness_semantics": "STRUCTURAL_RECORD_ONLY; UNKNOWN evidence never qualifies as BUY",
    }
    return result


def _gem_hunter_pending_payload(*, refreshing: bool, error: str | None = None) -> dict:
    """Return an explicit non-tradeable state while the provider pipeline runs."""
    failed = bool(error)
    return {
        "status": "UNKNOWN" if failed else "LOADING",
        "generated_at_utc": None,
        "networks": ["base", "ethereum", "solana"],
        "trade_authorization": "NO_TRADE",
        "source_count": 0,
        "sources": {},
        "candidate_count": 0,
        "candidates": [],
        "discovered_candidate_count": 0,
        "evaluation_record_count": 0,
        "market_reconciliation_counts": {
            "checked": 0, "PASS": 0, "FAIL": 0, "UNKNOWN": 0, "NOT_CHECKED_BUDGET": 0,
        },
        "security_screening_counts": {
            "PASS": 0, "FAIL": 0, "UNKNOWN": 0, "NOT_CHECKED_BUDGET": 0,
        },
        "complete_evaluation_count": 0,
        "incomplete_evaluation_count": 0,
        "unassessed_candidate_count": 0,
        "qualified_buy_count": 0,
        "qualification_engine_status": "UNKNOWN" if failed else ("REFRESHING" if refreshing else "LOADING"),
        "signal_status": "DISCOVERY_FEED_DEGRADED" if failed else "REFRESHING",
        "progress_stage": "FAILED" if failed else "WAITING_FOR_DISCOVERY",
        "diagnostic_freshness": "NONE",
        "diagnostic_candidates": [],
        "diagnostic_fa": (
            "به‌روزرسانی Gem Hunter کامل نشد؛ داده‌ها UNKNOWN می‌مانند و هیچ سیگنال BUY صادر نمی‌شود."
            if failed else
            "اسکن کشف و اعتبارسنجی در پس‌زمینه در حال اجراست؛ برای جلوگیری از Timeout، پاسخ اولیه فوراً برمی‌گردد."
        ),
        "public_policy": {
            "qualification_schema": SCHEMA_VERSION,
            "minimum_passed_filters": 9,
            "total_filters": len(FILTER_IDS),
            "minimum_opportunity_score": 80,
            "critical_gates": list(CRITICAL_GATES),
            "unknown_or_incomplete_candidates_hidden": True,
            "discovery_candidates_exposed": False,
            "aggregate_pipeline_diagnostics_exposed": True,
            "diagnostic_candidates_are_trade_signals": False,
            "evaluation_completeness_semantics": "STRUCTURAL_RECORD_ONLY; UNKNOWN evidence never qualifies as BUY",
        },
        "refreshing": refreshing,
        "last_error": error,
    }


def _publish_gem_hunter_progress(progress_payload: dict) -> None:
    """Publish current-scan discovery rows without ever publishing a BUY decision."""
    payload = copy.deepcopy(progress_payload)
    payload["refreshing"] = True
    payload["stale"] = False
    payload["candidates"] = []
    payload["candidate_count"] = 0
    payload["qualified_buy_count"] = 0
    payload["signal_status"] = "REFRESHING"
    payload["qualification_engine_status"] = "REFRESHING"
    payload["diagnostic_freshness"] = "CURRENT_SCAN_DISCOVERY"
    with GEM_HUNTER_RESULT_CACHE_LOCK:
        if not GEM_HUNTER_RESULT_CACHE.get("refreshing"):
            return
        GEM_HUNTER_RESULT_CACHE["payload"] = payload
        GEM_HUNTER_RESULT_CACHE["expires_at"] = time.monotonic() + GEM_HUNTER_RESULT_CACHE_TTL_SECONDS
        GEM_HUNTER_RESULT_CACHE["last_error"] = None


def _refresh_gem_hunter_result() -> None:
    """Run slow public-provider checks off the HTTP request thread."""
    try:
        payload = _gem_hunter_discovery_payload(
            limit=50,
            include_diagnostics=True,
            progress_callback=_publish_gem_hunter_progress,
        )
        payload["refreshing"] = False
        payload["stale"] = False
        payload["progress_stage"] = "COMPLETE"
        payload["diagnostic_freshness"] = "CURRENT_SCAN"
        payload["last_error"] = None
        expires_at = time.monotonic() + GEM_HUNTER_RESULT_CACHE_TTL_SECONDS
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)[:160]
        payload = _gem_hunter_pending_payload(refreshing=False, error=error)
        with GEM_HUNTER_RESULT_CACHE_LOCK:
            previous = GEM_HUNTER_RESULT_CACHE.get("payload") or {}
        if previous.get("progress_stage") == "DISCOVERY_COMPLETE":
            prior_diagnostics = [
                copy.deepcopy(item)
                for item in previous.get("diagnostic_candidates", [])
                if isinstance(item, dict) and item.get("trade_authorization") != "BUY"
            ]
            payload["diagnostic_candidates"] = prior_diagnostics
            payload["discovered_candidate_count"] = len(prior_diagnostics)
            payload["progress_stage"] = "FAILED_AFTER_DISCOVERY"
            payload["diagnostic_freshness"] = "CURRENT_SCAN_INCOMPLETE"
        expires_at = time.monotonic() + GEM_HUNTER_RESULT_CACHE_TTL_SECONDS

    with GEM_HUNTER_RESULT_CACHE_LOCK:
        GEM_HUNTER_RESULT_CACHE["payload"] = copy.deepcopy(payload)
        GEM_HUNTER_RESULT_CACHE["expires_at"] = expires_at
        GEM_HUNTER_RESULT_CACHE["refreshing"] = False
        GEM_HUNTER_RESULT_CACHE["last_error"] = payload.get("last_error")


def _start_gem_hunter_refresh() -> None:
    """Start at most one bounded Gem Hunter refresh worker per process."""
    with GEM_HUNTER_RESULT_CACHE_LOCK:
        if GEM_HUNTER_RESULT_CACHE.get("refreshing"):
            return
        GEM_HUNTER_RESULT_CACHE["refreshing"] = True
        GEM_HUNTER_RESULT_CACHE["last_error"] = None
        previous = GEM_HUNTER_RESULT_CACHE.get("payload")
        if isinstance(previous, dict):
            previous["progress_stage"] = "STALE_LAST_COMPLETED_SCAN"

    worker = threading.Thread(
        target=_refresh_gem_hunter_result,
        daemon=True,
        name="gem-hunter-refresh",
    )
    try:
        worker.start()
    except Exception as exc:
        with GEM_HUNTER_RESULT_CACHE_LOCK:
            GEM_HUNTER_RESULT_CACHE["refreshing"] = False
            GEM_HUNTER_RESULT_CACHE["last_error"] = type(exc).__name__


def _gem_hunter_api_payload(limit: int = 20, *, include_diagnostics: bool = False, force_refresh: bool = False) -> dict:
    """Return a cached Gem Hunter result immediately and optionally refresh it in a worker."""
    limit = max(1, min(int(limit), 50))
    now = time.monotonic()
    with GEM_HUNTER_RESULT_CACHE_LOCK:
        cached = copy.deepcopy(GEM_HUNTER_RESULT_CACHE.get("payload"))
        expires_at = GEM_HUNTER_RESULT_CACHE.get("expires_at", 0.0)
        refreshing = GEM_HUNTER_RESULT_CACHE.get("refreshing", False)

    if (force_refresh or cached is None or expires_at <= now) and not refreshing:
        _start_gem_hunter_refresh()

    with GEM_HUNTER_RESULT_CACHE_LOCK:
        cached = copy.deepcopy(GEM_HUNTER_RESULT_CACHE.get("payload"))
        expires_at = GEM_HUNTER_RESULT_CACHE.get("expires_at", 0.0)
        refreshing = GEM_HUNTER_RESULT_CACHE.get("refreshing", False)
        last_error = GEM_HUNTER_RESULT_CACHE.get("last_error")

    if cached is None:
        cached = _gem_hunter_pending_payload(refreshing=refreshing, error=last_error)
    elif refreshing:
        # A current-scan discovery snapshot is safe to show as research-only;
        # old BUY decisions are always removed while validation is running.
        cached["refreshing"] = True
        cached["candidates"] = []
        cached["candidate_count"] = 0
        cached["qualified_buy_count"] = 0
        cached["signal_status"] = "REFRESHING"
        cached["qualification_engine_status"] = "REFRESHING"
        if cached.get("progress_stage") == "DISCOVERY_COMPLETE":
            cached["status"] = cached.get("status") or "LOADING"
            cached["stale"] = False
            cached["diagnostic_freshness"] = "CURRENT_SCAN_DISCOVERY"
            cached["diagnostic_fa"] = (
                f"{int(cached.get('discovered_candidate_count') or 0)} نامزد در اسکن جاری پیدا شد؛ "
                "اعتبارسنجی بازار و امنیت ادامه دارد. فعلاً هیچ BUY تأیید نشده است."
            )
        else:
            cached["status"] = "STALE"
            cached["stale"] = True
            cached["diagnostic_candidates"] = [
                item for item in (cached.get("diagnostic_candidates") or [])
                if isinstance(item, dict) and item.get("trade_authorization") != "BUY"
            ]
            cached["diagnostic_freshness"] = (
                "STALE_LAST_COMPLETED_SCAN" if cached["diagnostic_candidates"] else "NONE"
            )
            cached["diagnostic_fa"] = (
                "گزارش قبلی منقضی است؛ نامزدهای آخرین اسکن فقط با برچسب دادهٔ قدیمی نمایش داده می‌شوند. "
                "اعتبارسنجی تازه در پس‌زمینه ادامه دارد و هیچ BUY قدیمی استفاده نمی‌شود."
            )
    elif expires_at <= time.monotonic():
        # If a worker could not be started, report UNKNOWN rather than pretending
        # that a refresh is active or exposing expired evidence.
        cached = _gem_hunter_pending_payload(
            refreshing=False, error=last_error or "Gem Hunter refresh unavailable",
        )
    else:
        cached["refreshing"] = False
        cached["stale"] = False

    cached["candidates"] = (cached.get("candidates") or [])[:limit]
    cached["candidate_count"] = len(cached["candidates"])
    if include_diagnostics:
        cached["diagnostic_candidates"] = (cached.get("diagnostic_candidates") or [])[:limit]
        cached["public_policy"] = dict(cached.get("public_policy") or {})
        cached["public_policy"]["discovery_candidates_exposed"] = True
    else:
        cached.pop("diagnostic_candidates", None)
        cached.pop("gem_hunter_v3_candidates", None)
        cached["public_policy"] = dict(cached.get("public_policy") or {})
        cached["public_policy"]["discovery_candidates_exposed"] = False
    return cached


def _radar_panel(radar_id: str, heading: str, note: str) -> str:
    """Render the shared radar UI used by every market-discovery surface."""
    safe_id = html.escape(radar_id, quote=True)
    safe_heading = html.escape(heading)
    safe_note = html.escape(note)
    return f"""
    <section class="crypto-radar" id="{safe_id}" data-radar-id="{safe_id}" data-radar-state="idle">
      <div class="radar-heading">
        <div><strong>{safe_heading}</strong><p>{safe_note}</p></div>
        <span class="radar-state" data-radar-state-label>آمادهٔ اسکن</span>
      </div>
      <div class="radar-layout">
        <div class="radar-scope" role="img" aria-label="رادار چرخان شناسایی رمزارزها">
          <div class="radar-grid"></div>
          <div class="radar-sweep"></div>
          <svg class="radar-points" viewBox="0 0 320 320" aria-hidden="true"></svg>
          <div class="radar-center"><span>CSR</span></div>
        </div>
        <div class="radar-readout">
          <div class="radar-readout-title"><span class="radar-led"></span><strong>خلاصهٔ شناسایی</strong><small data-radar-total>۰ دارایی</small></div>
          <div class="radar-counts">
            <div class="radar-count radar-buy"><span>BUY · خرید</span><strong data-radar-count="BUY">۰</strong></div>
            <div class="radar-count radar-sell"><span>SELL · فروش</span><strong data-radar-count="SELL">۰</strong></div>
            <div class="radar-count radar-neutral"><span>خنثی / بدون معامله</span><strong data-radar-count="NEUTRAL">۰</strong></div>
            <div class="radar-count radar-unknown"><span>نامشخص / در انتظار</span><strong data-radar-count="UNKNOWN">۰</strong></div>
          </div>
          <p class="radar-caption" data-radar-caption>هنوز داده‌ای برای نمایش روی رادار دریافت نشده است.</p>
          <div class="radar-legend">
            <span><i class="radar-legend-dot radar-buy-dot"></i>خرید</span>
            <span><i class="radar-legend-dot radar-sell-dot"></i>فروش</span>
            <span><i class="radar-legend-dot radar-neutral-dot"></i>خنثی</span>
            <span><i class="radar-legend-dot radar-unknown-dot"></i>نامشخص</span>
          </div>
        </div>
      </div>
    </section>
    """


def _radar_css() -> str:
    return """
    .crypto-radar{margin:14px 0 18px;padding:16px;border:1px solid #245064;border-radius:16px;background:linear-gradient(135deg,#081a25,#0b1d2b 58%,#0a1725);box-shadow:inset 0 0 26px #0c4b4b18}
    .radar-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}
    .radar-heading strong{font-size:1rem;color:#d9fff6}
    .radar-heading p{margin:3px 0 0;color:var(--muted);font-size:.82rem}
    .radar-state{flex:0 0 auto;padding:5px 9px;border:1px solid #2c5964;border-radius:999px;background:#0c2b35;color:#9ff3dd;font-size:.78rem;font-weight:700}
    .radar-layout{display:grid;grid-template-columns:minmax(190px,310px) minmax(0,1fr);gap:18px;align-items:center}
    .radar-scope{position:relative;width:min(100%,310px);aspect-ratio:1;margin:auto;overflow:hidden;border:1px solid #24635e;border-radius:50%;background:radial-gradient(circle at center,#123d3b 0,#0b282e 45%,#07161f 74%,#06121b 100%);box-shadow:0 0 25px #27d7a326,inset 0 0 38px #2bbd9e18}
    .radar-grid{position:absolute;inset:0;border-radius:50%;background:repeating-radial-gradient(circle at center,transparent 0 19%,#5ad9b52b 19.4% 19.8%,transparent 20.2% 39%,#5ad9b526 39.4% 39.8%,transparent 40.2% 59%,#5ad9b522 59.4% 59.8%,transparent 60.2% 79%,#5ad9b51e 79.4% 79.8%,transparent 80.2%),linear-gradient(90deg,transparent 49.7%,#5ad9b52b 49.9%,#5ad9b52b 50.1%,transparent 50.3%),linear-gradient(0deg,transparent 49.7%,#5ad9b52b 49.9%,#5ad9b52b 50.1%,transparent 50.3%)}
    .radar-sweep{position:absolute;inset:0;border-radius:50%;background:conic-gradient(from 0deg,transparent 0deg 298deg,#2be0a006 322deg,#36e9ad24 345deg,#42ffb46b 359deg,#42ffb400 360deg);animation:crypto-radar-spin 5.2s linear infinite;mix-blend-mode:screen}
    .radar-scope:after{content:"";position:absolute;z-index:1;left:50%;top:50%;width:8px;height:8px;transform:translate(-50%,-50%);border-radius:50%;background:#9affd7;box-shadow:0 0 10px 4px #53d6a0a8}
    .radar-points{position:absolute;z-index:2;inset:0;width:100%;height:100%;overflow:visible}
    .radar-dot{stroke:#05131a;stroke-width:1.8;opacity:.95;animation:crypto-radar-blip 2.4s ease-in-out infinite}
    .radar-dot-buy{fill:#53d6a0;filter:drop-shadow(0 0 5px #53d6a0)}
    .radar-dot-sell{fill:#ff7b85;filter:drop-shadow(0 0 5px #ff7b85)}
    .radar-dot-neutral{fill:#72a7ff;filter:drop-shadow(0 0 4px #72a7ff)}
    .radar-dot-unknown{fill:#ffc66d;filter:drop-shadow(0 0 4px #ffc66d)}
    .radar-center{position:absolute;z-index:3;inset:0;display:grid;place-items:center;pointer-events:none}
    .radar-center span{margin-top:18px;color:#9ff3dd;font-size:.57rem;font-weight:800;letter-spacing:.15em;text-shadow:0 0 9px #53d6a0}
    .radar-readout{min-width:0}
    .radar-readout-title{display:flex;align-items:center;gap:8px;margin-bottom:10px}
    .radar-readout-title strong{font-size:.94rem}
    .radar-readout-title small{margin-right:auto;color:var(--muted)}
    .radar-led{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}
    .radar-counts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
    .radar-count{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px;border:1px solid #263e50;border-radius:11px;background:#091824;min-width:0}
    .radar-count span{font-size:.78rem;color:var(--muted)}
    .radar-count strong{font-size:1.1rem;font-variant-numeric:tabular-nums}
    .radar-buy strong{color:var(--green)} .radar-sell strong{color:var(--red)} .radar-neutral strong{color:var(--blue)} .radar-unknown strong{color:var(--amber)}
    .radar-caption{min-height:44px;margin:12px 0 8px;color:#c2d4e1;font-size:.84rem;overflow-wrap:anywhere}
    .radar-legend{display:flex;flex-wrap:wrap;gap:12px;color:var(--muted);font-size:.76rem}
    .radar-legend span{display:inline-flex;align-items:center;gap:5px}
    .radar-legend-dot{width:7px;height:7px;border-radius:50%;display:inline-block}
    .radar-buy-dot{background:var(--green)} .radar-sell-dot{background:var(--red)} .radar-neutral-dot{background:var(--blue)} .radar-unknown-dot{background:var(--amber)}
    .crypto-radar[data-radar-state="scanning"] .radar-state{color:#baf8e5;border-color:#39836f;background:#103b35}
    .crypto-radar[data-radar-state="error"] .radar-state{color:#ffb9bd;border-color:#75424a;background:#351c27}
    .crypto-radar[data-radar-state="error"] .radar-led{background:var(--red);box-shadow:0 0 10px var(--red)}
    @keyframes crypto-radar-spin{to{transform:rotate(360deg)}}
    @keyframes crypto-radar-blip{0%,100%{opacity:.7}50%{opacity:1}}
    @media(max-width:720px){.radar-layout{grid-template-columns:minmax(0,1fr)}.radar-scope{width:min(100%,270px)}.radar-readout{width:100%}}
    @media(prefers-reduced-motion:reduce){.radar-sweep,.radar-dot{animation:none!important}}
    """


def _radar_runtime() -> str:
    return r"""<script>
    (() => {
      const labels = {BUY:'خرید', SELL:'فروش', NEUTRAL:'خنثی / بدون معامله', UNKNOWN:'نامشخص / در انتظار'};
      const stateLabels = {idle:'آمادهٔ اسکن', scanning:'رادار در حال اسکن', complete:'آخرین نتایج شناسایی شد', error:'خطا در دریافت داده'};
      const svgNS = 'http://www.w3.org/2000/svg';
      const signalOf = item => {
        const explicit = String(item.trade_authorization || item.radar_signal || item.signal || item.action || item.recommendation || '').toUpperCase().replace(/-/g, '_');
        if (explicit === 'BUY' || explicit === 'LONG') return 'BUY';
        if (explicit === 'SELL' || explicit === 'SHORT') return 'SELL';
        if (['NO_TRADE','NO TRADE','HOLD','NEUTRAL'].includes(explicit)) return 'NEUTRAL';
        const status = String(item.status || item.candidate_status || '').toUpperCase();
        if (['PASS','VERIFIED','DISCOVERED'].includes(status)) return 'NEUTRAL';
        return 'UNKNOWN';
      };
      const pointLabel = item => String(item.symbol || item.token_symbol || item.name || item.token_name || item.asset || 'رمزارز').slice(0, 48);
      function update(id, items, options = {}) {
        const root = document.getElementById(id);
        if (!root) return;
        const safeItems = Array.isArray(items) ? items.filter(item => item && typeof item === 'object').slice(0, 48) : [];
        root._radarItems = safeItems;
        const svg = root.querySelector('.radar-points');
        if (!svg) return;
        svg.replaceChildren();
        const counts = {BUY:0, SELL:0, NEUTRAL:0, UNKNOWN:0};
        safeItems.forEach((item, index) => {
          const signal = signalOf(item);
          counts[signal] += 1;
          let hash = 0;
          const label = pointLabel(item);
          for (let i = 0; i < label.length; i += 1) hash = (hash * 31 + label.charCodeAt(i)) >>> 0;
          const angle = ((index * 137.507764 + (hash % 360)) % 360) * Math.PI / 180;
          const radius = 37 + ((index * 29 + (hash % 83)) % 91);
          const cx = 160 + Math.cos(angle) * radius;
          const cy = 160 + Math.sin(angle) * radius;
          const dot = document.createElementNS(svgNS, 'circle');
          dot.setAttribute('cx', cx.toFixed(2));
          dot.setAttribute('cy', cy.toFixed(2));
          dot.setAttribute('r', signal === 'BUY' || signal === 'SELL' ? '5.2' : '4.2');
          dot.setAttribute('class', 'radar-dot radar-dot-' + signal.toLowerCase());
          const title = document.createElementNS(svgNS, 'title');
          title.textContent = label + ' · ' + labels[signal];
          dot.appendChild(title);
          svg.appendChild(dot);
        });
        Object.entries(counts).forEach(([key, value]) => {
          const output = root.querySelector('[data-radar-count="' + key + '"]');
          if (output) output.textContent = String(value);
        });
        const total = root.querySelector('[data-radar-total]');
        if (total) total.textContent = safeItems.length.toLocaleString('fa-IR') + ' دارایی';
        const state = options.state || 'complete';
        root.dataset.radarState = state;
        const stateOutput = root.querySelector('[data-radar-state-label]');
        if (stateOutput) stateOutput.textContent = options.stateText || stateLabels[state] || stateLabels.idle;
        const caption = root.querySelector('[data-radar-caption]');
        if (caption) caption.textContent = options.caption || (safeItems.length
          ? 'نقاط بر اساس آخرین دادهٔ دریافتی رسم شده‌اند؛ رنگ‌ها وضعیت همان منبع را نشان می‌دهند.'
          : 'هنوز داده‌ای برای نمایش روی رادار دریافت نشده است.');
      }
      function scanning(id, message) {
        const root = document.getElementById(id);
        update(id, root && root._radarItems || [], {state:'scanning', caption:message || 'در حال دریافت و بررسی داده‌های بازار...'});
      }
      function error(id, message) {
        const root = document.getElementById(id);
        update(id, root && root._radarItems || [], {state:'error', stateText:'خطا در اسکن', caption:message || 'داده از منبع اسکن دریافت نشد؛ سیگنالی تولید نشده است.'});
      }
      window.CryptoRadar = {update, scanning, error};
    })();
    </script>"""

def _page(body: str, title: str = "CRYPTO SUPER RADAR") -> bytes:
    body = (
        body.replace("<!--RADAR_GEM_HUNTER-->", _radar_panel(
            "radar-gem-hunter", "رادار کشف توکن‌های تازه",
            "نقاط زندهٔ مرحلهٔ کشف و ارزیابی؛ BUY فقط پس از تأیید کامل موتور نمایش داده می‌شود."
        ))
        .replace("<!--RADAR_MARKET-->", _radar_panel(
            "radar-market", "رادار بازار رمزارزها",
            "آبی یعنی دادهٔ قیمت از منابع مستقل تأیید شده؛ این بخش به‌تنهایی سیگنال خرید یا فروش صادر نمی‌کند."
        ))
        .replace("<!--RADAR_DEX_SEARCH-->", _radar_panel(
            "radar-dex-search", "رادار جست‌وجوی DEX",
            "توکن‌های پیدا‌شده روی رادار به‌عنوان نامزد شناسایی نمایش داده می‌شوند، نه سیگنال معاملاتی."
        ))
        .replace("<!--RADAR_ASSET_SCAN-->", _radar_panel(
            "radar-asset-scan", "رادار اسکن اختصاصی دارایی",
            "وضعیت نهایی فقط بعد از دریافت پاسخ کامل موتور تحلیل روی رادار ثبت می‌شود."
        ))
    )
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{color-scheme:dark;--bg:#07111f;--surface:#0e1b2c;--surface-raised:#14263c;--line:#24374f;--text:#edf4fb;--muted:#98abc0;--teal:#54ded0;--blue:#72a7ff;--green:#53d6a0;--amber:#ffc66d;--red:#ff7b85;--radius:18px}}
*{{box-sizing:border-box}}
body{{font-family:"Segoe UI",Tahoma,Arial,sans-serif;background:radial-gradient(ellipse at 82% -10%,#14334a 0,transparent 34%),var(--bg);color:var(--text);margin:0;padding:32px 20px 48px;line-height:1.65}}
main{{max-width:1120px;margin:auto}}
.app-header{{display:flex;align-items:center;gap:16px;margin:0 0 22px;padding:8px 2px 18px;border-bottom:1px solid var(--line)}}
.brand-mark{{display:grid;place-items:center;width:54px;height:54px;border:1px solid #2a5263;border-radius:17px;background:linear-gradient(145deg,#153445,#102338);font-size:27px;box-shadow:0 10px 30px #0003}}
.brand-copy{{min-width:0;flex:1}}
.brand-copy h1{{margin:1px 0 0;font-size:clamp(1.3rem,3vw,1.85rem);letter-spacing:.035em;line-height:1.25}}
.brand-copy p{{margin:5px 0 0}}
.eyebrow{{color:var(--teal);font-size:.78rem;font-weight:700;letter-spacing:.04em}}
.mode-pill{{display:inline-flex;align-items:center;gap:8px;padding:7px 11px;border:1px solid #2e5360;border-radius:999px;background:#102a34;color:#a8f2e6;font-size:.82rem;white-space:nowrap}}
.mode-dot{{width:8px;height:8px;border-radius:50%;background:var(--teal);box-shadow:0 0 12px #54ded080}}
.card{{background:linear-gradient(145deg,#101e30,#0d1929);border:1px solid var(--line);border-radius:var(--radius);padding:22px;margin:16px 0;box-shadow:0 14px 34px #0002}}
.card h2{{margin:0 0 6px;font-size:1.25rem;letter-spacing:-.01em}}
.card h3{{margin-top:22px}}
.muted{{color:var(--muted)}}
input,button,select{{font:inherit;padding:12px 14px;border-radius:11px;border:1px solid #334a63;background:#091522;color:var(--text);width:100%;box-sizing:border-box}}
input:focus{{outline:2px solid #54ded080;border-color:var(--teal)}}

.market-loading-mascot{{display:flex;align-items:center;justify-content:center;gap:14px;min-height:112px;padding:12px;color:#f5d9a8;font-weight:700}}
.market-loading-rabbit{{width:104px;height:82px;flex:0 0 104px;overflow:visible}}
.market-loading-rabbit .rabbit-body{{transform-box:fill-box;transform-origin:50% 80%;animation:rabbit-dig-body .38s ease-in-out infinite alternate}}
.market-loading-rabbit .rabbit-ear{{transform-box:fill-box;transform-origin:50% 100%;animation:rabbit-ear-flop .3s ease-in-out infinite alternate}}
.market-loading-rabbit .rabbit-paw{{transform-box:fill-box;transform-origin:50% 10%;animation:rabbit-dig-paw .2s ease-in-out infinite alternate}}
.market-loading-rabbit .rabbit-dirt{{transform-box:fill-box;transform-origin:center;animation:rabbit-dirt-pop .42s ease-in-out infinite}}
.market-loading-rabbit .rabbit-carrot{{transform-box:fill-box;transform-origin:50% 100%;animation:rabbit-carrot-peek 1.25s ease-out infinite}}
.market-loading-carrot{{display:inline-block;font-size:25px;animation:rabbit-carrot-bounce .65s ease-in-out infinite alternate}}
@keyframes rabbit-dig-body{{from{{transform:translateY(1px) rotate(-2deg)}}to{{transform:translateY(-2px) rotate(2deg)}}}}
@keyframes rabbit-ear-flop{{from{{transform:rotate(-7deg)}}to{{transform:rotate(9deg)}}}}
@keyframes rabbit-dig-paw{{from{{transform:rotate(-28deg) translateY(1px)}}to{{transform:rotate(24deg) translateY(4px)}}}}
@keyframes rabbit-dirt-pop{{0%,100%{{opacity:.2;transform:translateY(2px) scale(.7)}}50%{{opacity:1;transform:translateY(-5px) scale(1.1)}}}}
@keyframes rabbit-carrot-peek{{0%,35%{{transform:translateY(17px) rotate(-12deg);opacity:0}}65%,100%{{transform:translateY(0) rotate(8deg);opacity:1}}}}
@keyframes rabbit-carrot-bounce{{from{{transform:translateY(2px) rotate(-8deg)}}to{{transform:translateY(-4px) rotate(8deg)}}}}
@media(prefers-reduced-motion:reduce){{.market-loading-rabbit *,.market-loading-carrot{{animation:none!important}}}}

.market-search-activity{{display:none;align-items:center;gap:10px;margin:10px 2px 0;padding:8px 11px;border:1px solid #24556a;border-radius:14px;background:linear-gradient(100deg,#0c2634,#102c39);color:#9feee4;font-size:.84rem;font-weight:650;overflow:hidden}}
.market-search-activity.is-searching,.market-search-activity.is-found{{display:flex}}
.market-search-dog{{display:block;flex:0 0 82px;width:82px;height:58px;overflow:visible}}
.market-search-spin{{animation:shiba-sniff-body .48s ease-in-out infinite alternate}}
.market-search-dog .shiba-body{{transform-box:fill-box;transform-origin:center;animation:shiba-sniff-body .48s ease-in-out infinite alternate}}
.market-search-dog .shiba-head{{transform-box:fill-box;transform-origin:72% 78%;animation:shiba-sniff-head .34s ease-in-out infinite alternate}}
.market-search-dog .shiba-tail{{transform-box:fill-box;transform-origin:10% 90%;animation:shiba-tail-wag .22s ease-in-out infinite alternate}}
.market-search-dog .shiba-leg-front{{transform-box:fill-box;transform-origin:50% 0;animation:shiba-paw .3s ease-in-out infinite alternate}}
.market-search-dog .shiba-leg-back{{transform-box:fill-box;transform-origin:50% 0;animation:shiba-paw .3s ease-in-out .12s infinite alternate}}
.market-search-treasure{{font-size:25px;opacity:0;transform:scale(.35) rotate(-18deg);transition:opacity .22s ease,transform .3s cubic-bezier(.2,1.5,.5,1)}}
.market-search-activity.is-found .market-search-treasure{{opacity:1;transform:scale(1) rotate(8deg);animation:shiba-treasure-pop .55s ease-in-out infinite alternate}}
.market-search-activity.is-found .market-search-dog .shiba-body{{animation:shiba-found-bounce .38s ease-in-out infinite alternate}}
.market-search-activity.is-found .market-search-dog .shiba-head{{animation:shiba-found-look .6s ease-in-out infinite alternate}}
.market-search-activity.is-found .market-search-dog .shiba-tail{{animation:shiba-tail-wag .1s ease-in-out infinite alternate}}
.market-search-found-copy{{display:none}}
.market-search-activity.is-found .market-search-searching-copy{{display:none}}
.market-search-activity.is-found .market-search-found-copy{{display:inline}}
@keyframes shiba-sniff-body{{from{{transform:translateY(1px)}}to{{transform:translateY(-2px)}}}}
@keyframes shiba-sniff-head{{from{{transform:rotate(5deg) translateY(2px)}}to{{transform:rotate(-8deg) translateY(5px)}}}}
@keyframes shiba-tail-wag{{from{{transform:rotate(-20deg)}}to{{transform:rotate(24deg)}}}}
@keyframes shiba-paw{{from{{transform:rotate(-9deg) translateY(0)}}to{{transform:rotate(10deg) translateY(2px)}}}}
@keyframes shiba-found-bounce{{from{{transform:translateY(0) scale(1)}}to{{transform:translateY(-5px) scale(1.025)}}}}
@keyframes shiba-found-look{{from{{transform:rotate(-12deg)}}to{{transform:rotate(7deg)}}}}
@keyframes shiba-treasure-pop{{from{{filter:drop-shadow(0 0 2px #ffc857)}}to{{filter:drop-shadow(0 0 10px #ffc857)}}}}
@media(prefers-reduced-motion:reduce){{.market-search-dog *,.market-search-spin,.market-search-treasure{{animation:none!important;transition:none!important}}}}
label{{display:block;margin-top:12px;font-size:.94rem;font-weight:600}}
button{{background:linear-gradient(135deg,#3179e8,#2862c7);border:0;margin-top:12px;cursor:pointer;font-weight:700;transition:filter .15s ease,transform .15s ease}}
button:hover{{filter:brightness(1.12);transform:translateY(-1px)}}
button:focus-visible{{outline:3px solid #54ded080;outline-offset:2px}}
button:disabled{{opacity:.45;cursor:not-allowed;transform:none}}
button.market-scan-button{{width:auto;min-width:132px;margin:0;padding:8px 12px;font-size:.88rem;border:1px solid #315b74;background:#123344;color:#b9f5ed}}
button.market-scan-button:hover{{background:#174355}}
#crypto-market-status{{padding:9px 12px;border:1px solid #1f3448;border-radius:10px;background:#0a1725;font-size:.88rem}}
table{{width:100%;border-collapse:collapse;font-size:.93rem}}
th{{color:var(--muted);font-size:.79rem;font-weight:650;background:#0a1725;white-space:nowrap}}
td,th{{padding:12px 10px;border-bottom:1px solid #203149;text-align:right;vertical-align:middle}}
tbody tr:hover{{background:#ffffff06}}
.numeric{{direction:ltr;text-align:left;font-variant-numeric:tabular-nums;white-space:nowrap}}
.status{{font-weight:bold}} .status-pass{{color:var(--green)}} .status-unknown{{color:var(--amber)}}
code{{direction:ltr;display:inline-block}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#08101d;padding:12px;border-radius:10px;direction:ltr;text-align:left}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.analysis-links-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:16px}}
.analysis-link-card{{display:flex;flex-direction:column;gap:8px;padding:18px;border:1px solid #2b4660;border-radius:15px;background:linear-gradient(145deg,#12283a,#0b1a2a);min-width:0}}
.analysis-link-card h3{{margin:0;font-size:1.08rem}}
.analysis-link-card p{{margin:0;color:var(--muted);font-size:.9rem}}
.analysis-link{{display:inline-flex;align-items:center;justify-content:center;gap:8px;margin-top:6px;padding:11px 14px;border:1px solid #37627b;border-radius:11px;background:#123344;color:#c8fff4;text-decoration:none;font-weight:700}}
.analysis-link:hover{{background:#19465a}}
.exchange-links-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:16px}}
.exchange-card{{display:flex;align-items:center;gap:13px;min-width:0;padding:16px;border:1px solid #2b4660;border-radius:15px;background:linear-gradient(145deg,#12283a,#0b1a2a);text-decoration:none;color:var(--text);transition:border-color .15s ease,transform .15s ease}}
.exchange-card:hover{{border-color:var(--teal);transform:translateY(-2px)}}
.exchange-logo{{width:42px;height:42px;flex:0 0 42px;object-fit:contain;padding:7px;border-radius:12px;background:#f4f7fb}}
.exchange-copy{{display:flex;flex-direction:column;min-width:0;gap:2px}}
.exchange-copy strong{{font-size:1rem}}
.exchange-copy span{{font-size:.8rem;color:var(--muted)}}
.gem-report-list{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,430px),1fr));gap:13px;margin-top:16px}}
.gem-candidate{{border:1px solid #2b4660;border-radius:14px;padding:15px;background:#0a1725;min-width:0}}
.gem-candidate h3{{margin:0 0 4px;font-size:1.05rem}}
.gem-candidate .candidate-meta{{color:var(--muted);font-size:.82rem;overflow-wrap:anywhere}}
.gem-facts{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:12px 0}}
.gem-fact{{padding:8px 10px;border:1px solid #20364d;border-radius:10px;background:#0c1b2c;min-width:0}}
.gem-fact span{{display:block;color:var(--muted);font-size:.76rem}}
.gem-fact strong{{display:block;direction:ltr;text-align:right;overflow-wrap:anywhere;font-size:.9rem}}
.gem-status-pass{{color:var(--green)}} .gem-status-fail{{color:var(--red)}} .gem-status-unknown{{color:var(--amber)}}
.gem-detail-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}
.gem-detail{{padding:8px 10px;border:1px solid #20364d;border-radius:9px;font-size:.82rem;min-width:0}}
.gem-detail strong{{display:block}}
.gem-links{{display:flex;flex-wrap:wrap;gap:12px;margin:10px 0}}
.gem-links a,.watch-entry a{{color:#9feee4;overflow-wrap:anywhere}}
.gem-add-watch{{width:auto;margin:8px 0 0;padding:9px 12px;background:linear-gradient(135deg,#167b70,#155e65)}}
.watchlist-form{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:14px 0}}
.watchlist-form label{{margin:0}}
.watchlist-form .wide{{grid-column:1/-1}}
.watchlist-form textarea{{width:100%;min-height:76px;padding:11px 13px;border:1px solid #334a63;border-radius:11px;background:#091522;color:var(--text);font:inherit}}
.watchlist-entries{{display:grid;gap:11px;margin-top:14px}}
.watch-entry{{padding:13px;border:1px solid #2b4660;border-radius:13px;background:#0a1725;min-width:0}}
.watch-entry h3{{margin:0 0 4px;font-size:1rem}}
.watch-entry label{{font-size:.82rem;margin-top:8px}}
.watch-entry input,.watch-entry textarea{{margin-top:4px}}
.watch-entry textarea{{width:100%;min-height:58px;padding:9px;border:1px solid #334a63;border-radius:9px;background:#091522;color:var(--text);font:inherit}}
.watch-entry-actions{{display:flex;gap:8px;flex-wrap:wrap}}
.watch-entry-actions button{{width:auto;padding:7px 11px;font-size:.82rem}}
.watch-delete{{background:#6c2934}}
@media(max-width:680px){{.exchange-links-grid{{grid-template-columns:1fr}}.watchlist-form{{grid-template-columns:1fr}}.watchlist-form .wide{{grid-column:auto}}.gem-facts,.gem-detail-grid{{grid-template-columns:1fr}}}}

@media(max-width:680px){{.analysis-links-grid{{grid-template-columns:1fr}}}}
.metric{{background:#0a1725;border:1px solid #263a53;border-radius:13px;padding:14px}}
.metric strong{{display:block;font-size:1.2rem;margin-top:6px;direction:ltr;text-align:right;font-variant-numeric:tabular-nums}}
canvas{{width:100%;height:300px;background:#08101d;border-radius:12px}}
.calc-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
@media(max-width:680px){{body{{padding:20px 12px 32px}}.app-header{{gap:11px;align-items:flex-start}}.brand-mark{{width:44px;height:44px;border-radius:14px;font-size:23px}}.mode-pill{{font-size:.72rem;padding:5px 8px}}.card{{padding:17px 14px;border-radius:15px}}table{{font-size:.84rem}}td,th{{padding:10px 8px}}button.market-scan-button{{min-width:116px;padding:8px;font-size:.8rem}}}}
{_radar_css()}
</style></head><body><main>{_radar_runtime()}{body}<script>
(function(){{
  const source=document.getElementById('crypto-market-search-activity');
  if(!source)return;
  const selector='input[type="search"], input[data-search], input[id*="search"], input[id*="query"], input[placeholder*="جست"]';
  const attach=input=>{{
    if(input.id==='crypto-market-search'||input.dataset.shibaAttached==='1')return;
    input.dataset.shibaAttached='1';
    const panel=source.cloneNode(true);
    panel.removeAttribute('id');
    panel.classList.remove('is-searching','is-found');
    panel.classList.add('global-search-activity');
    panel.querySelector('.market-search-searching-copy').textContent='شیبا داره اینجا رو می‌گرده... 🐾';
    panel.querySelector('.market-search-found-copy').textContent='پیداش کرد! گنج اینجاست! 🎉';
    const svg=panel.querySelector('svg');
    if(svg)svg.setAttribute('aria-label','شیبا در حال جست‌وجوی نتیجه');
    const host=input.parentElement;
    if(host&&host.tagName==='LABEL')input.insertAdjacentElement('afterend',panel);
    else if(host)host.insertAdjacentElement('afterend',panel);
    let timer=null;
    const show=()=>{{
      const term=(input.value||'').trim();
      if(!term){{panel.classList.remove('is-searching','is-found');return;}}
      panel.classList.add('is-searching');panel.classList.remove('is-found');
      if(timer)clearTimeout(timer);
      timer=setTimeout(()=>{{
        panel.classList.remove('is-searching');
        const id=input.id==='dex-token-query'?'dex-search-results':input.dataset.results;
        const resultBox=id?document.getElementById(id):null;
        if(term.length>=2&&resultBox&&resultBox.children.length)panel.classList.add('is-found');
      }},500);
    }};
    input.addEventListener('input',show);
    input.addEventListener('search',show);
    input.addEventListener('keydown',event=>{{if(event.key==='Enter')show();}});
    show();
  }};
  document.querySelectorAll(selector).forEach(attach);
  new MutationObserver(records=>records.forEach(record=>record.addedNodes.forEach(node=>{{
    if(node.nodeType!==1)return;
    if(node.matches&&node.matches(selector))attach(node);
    node.querySelectorAll&&node.querySelectorAll(selector).forEach(attach);
  }}))).observe(document.body,{{childList:true,subtree:true}});
}})();
</script></main></body></html>""".encode("utf-8")


def _parse_scan_asset(value: str) -> AssetSpec:
    """Accept simple coin names/tickers as USDT spot pairs; keep explicit DEX contracts."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("نام ارز یا نماد را وارد کن؛ نمونه: XRP یا BTCUSDT.")
    if ":" in raw:
        return AssetSpec.parse(raw)
    compact = re.sub(r"[\s/_-]+", "", raw).upper()
    if not compact.isalnum() or len(compact) > 24:
        raise ValueError("نماد ارز معتبر نیست. نمونه: XRP، XRPUSDT یا eth:آدرس‌قرارداد.")
    if compact in ASSET_NAME_ALIASES:
        compact = ASSET_NAME_ALIASES[compact]
    else:
        quote_suffixes = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "BTC", "ETH", "EUR")
        # A bare ticker such as BTC or ETH is a base asset, not a quote suffix.
        has_quote = any(
            len(compact) > len(quote) and compact.endswith(quote)
            for quote in quote_suffixes
        )
        if not has_quote:
            compact += "USDT"
    return AssetSpec.parse("cex:" + compact)


def _coin_display(asset: AssetSpec, market_intelligence: dict) -> dict:
    dex = (market_intelligence or {}).get("dex_screener") or {}
    raw_symbol = (asset.symbol or "").upper()
    for quote in ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "EUR", "BTC", "ETH"):
        if raw_symbol.endswith(quote) and len(raw_symbol) > len(quote):
            raw_symbol = raw_symbol[:-len(quote)]
            break
    symbol = dex.get("token_symbol") or raw_symbol or asset.address or asset.raw
    names = COMMON_ASSET_NAMES.get(str(symbol).upper(), (str(dex.get("token_name") or symbol), str(dex.get("token_name") or symbol)))
    name_en = dex.get("token_name") or names[0]
    name_fa = names[1]
    if asset.market_type == "dex" and dex.get("token_name"):
        name_en = dex["token_name"]
        name_fa = dex["token_name"]
    return {"symbol": symbol, "name_en": name_en, "name_fa": name_fa}


def _timeframe_ranges(asset: AssetSpec, snapshot=None) -> dict:
    """Fetch actual 1h candles and derive truthful rolling high/low windows."""
    empty = {
        label: {"status": "UNKNOWN", "high": None, "low": None, "bar_count": 0,
                "expected_bars": hours, "coverage_pct": 0.0, "age_hours": None}
        for label, hours in TIMEFRAME_WINDOWS
    }
    try:
        if asset.market_type == "cex":
            frame = ENGINE.data.binance.candles(asset.symbol, interval="1h", limit=750)
            source = "Binance Spot 1h candles"
        elif asset.market_type == "perp":
            provider = ENGINE.data.perpetual
            rows = provider.http.get(
                f"{provider.BINANCE}/fapi/v1/klines",
                {"symbol": asset.symbol, "interval": "1h", "limit": 750},
            )
            frame = _ohlcv_frame(rows)
            source = "Binance USD-M perpetual 1h candles"
        elif asset.market_type == "dex":
            snapshot = snapshot or getattr(ENGINE.data, "snapshots", {}).get(asset.raw)
            if snapshot is None or not snapshot.pool_address:
                raise ValueError("No validated DEX pool is available for timeframe candles.")
            frame = ENGINE.data.gecko.candles(
                asset.chain, snapshot.pool_address, token_address=asset.address,
                timeframe="hour", aggregate=1, limit=750,
            )
            source = "GeckoTerminal pool 1h candles"
        else:
            raise ValueError("Unsupported asset market type.")
        if frame is None or frame.empty:
            raise ValueError("The provider returned no 1h candles.")
        if getattr(frame.index, "tz", None) is None:
            frame = frame.copy()
            frame.index = frame.index.tz_localize(timezone.utc)
        # Exclude the still-forming current hour so high/low values use closed candles only.
        closed_before = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        frame = frame.loc[frame.index < closed_before]
        if frame.empty:
            raise ValueError("No completed 1h candles are available yet.")
        required = {"high", "low"}
        if not required.issubset(frame.columns):
            raise ValueError("The provider returned an incomplete OHLC schema.")
        latest = frame.index.max()
        if hasattr(latest, "to_pydatetime"):
            latest = latest.to_pydatetime()
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0)
        output = {}
        for label, hours in TIMEFRAME_WINDOWS:
            subset = frame.tail(min(hours, len(frame)))
            count = len(subset)
            coverage = min(100.0, count / float(hours) * 100.0)
            status = "KNOWN" if coverage >= 95.0 else "PARTIAL"
            output[label] = {
                "status": status,
                "high": float(subset["high"].max()) if count else None,
                "low": float(subset["low"].min()) if count else None,
                "bar_count": count,
                "expected_bars": hours,
                "coverage_pct": round(coverage, 2),
                "age_hours": round(age_hours, 2),
                "source": source,
                "window_hours": hours,
                "note": None if status == "KNOWN" else f"فقط {coverage:.1f}% از کندل‌های این بازه دریافت شده است.",
            }
        return output
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:160]}"
        return {label: {**row, "reason": reason} for label, row in empty.items()}


def _enforce_daily_candle_freshness(result, range_evidence: dict, timeframe_ranges: dict) -> None:
    """Fail closed when the daily analysis candles exceed their configured age limit.

    The separately fetched 1h range table is descriptive and must never make stale
    daily candles appear fresh for the engine's daily trend/backtest calculations.
    """
    thresholds = getattr(getattr(ENGINE, "config", None), "thresholds", None)
    max_age = float(getattr(thresholds, "max_candle_age_hours", 36.0))
    raw_age = range_evidence.get("candle_age_hours")
    try:
        age = float(raw_age)
        age_valid = math.isfinite(age) and age >= -1.0
    except (TypeError, ValueError, OverflowError):
        age = None
        age_valid = False
    stale = not age_valid or age > max_age
    if not stale:
        return

    hourly = (timeframe_ranges.get("1h") or {}).get("age_hours")
    if age is None:
        detail_fa = "سن آخرین کندل روزانه قابل تعیین نیست."
        detail_en = "Latest daily candle age is unavailable."
    else:
        detail_fa = f"سن کندل روزانه {age:.1f} ساعت است و از حد مجاز {max_age:.1f} ساعت بیشتر است."
        detail_en = f"Daily candle age {age:.1f}h exceeds the {max_age:.1f}h limit."
    if hourly is not None:
        detail_fa += f" دادهٔ ۱ساعته جداگانه {float(hourly):.1f} ساعت سن دارد و جایگزین تازگی دادهٔ روزانه نیست."
        detail_en += f" Separately fetched 1h candles are {float(hourly):.1f}h old and do not refresh daily analysis data."

    data_gate = next((gate for gate in (result.gates or []) if gate.gate_id == 1), None)
    if data_gate is not None:
        data_gate.status = GateStatus.UNKNOWN
        data_gate.reason_fa = detail_fa
        data_gate.reason_en = detail_en
        evidence = dict(data_gate.evidence or {})
        evidence.update({
            "daily_candle_age_hours": age,
            "daily_candle_max_age_hours": max_age,
            "hourly_candle_age_hours": hourly,
            "daily_freshness_status": "UNKNOWN",
        })
        data_gate.evidence = evidence
    range_evidence["status"] = "UNKNOWN"
    range_evidence["reason"] = detail_en
    # A stale daily data gate must also invalidate the engine's directional signal.
    result.signal = Signal.NO_TRADE
    result.summary_fa = "معامله مسدود شد: سن کندل روزانه از حد مجاز تازگی داده بیشتر است."


def _enforce_volume_cross_validation(result) -> None:
    """A missing independent volume comparison cannot be described as validated data."""
    data_gate = next((gate for gate in (result.gates or []) if gate.gate_id == 1), None)
    if data_gate is None:
        return
    evidence = dict(data_gate.evidence or {})
    discrepancy = evidence.get("volume_discrepancy_pct")
    try:
        volume_valid = math.isfinite(float(discrepancy)) and float(discrepancy) >= 0.0
    except (TypeError, ValueError, OverflowError):
        volume_valid = False
    if volume_valid:
        return
    prior_fa = data_gate.reason_fa if data_gate.status == GateStatus.UNKNOWN else ""
    prior_en = data_gate.reason_en if data_gate.status == GateStatus.UNKNOWN else ""
    volume_reason_fa = (
        "اختلاف قیمت بین منابع قابل بررسی است، اما اختلاف حجم مستقل محاسبه نشده؛ "
        "بنابراین اعتبارسنجی کامل حجم تأیید نشده است."
    )
    volume_reason_en = (
        "Price comparison is available, but independent volume discrepancy is missing; "
        "full volume validation remains UNKNOWN."
    )
    data_gate.status = GateStatus.UNKNOWN
    data_gate.reason_fa = (prior_fa + " همچنین " + volume_reason_fa) if prior_fa else volume_reason_fa
    data_gate.reason_en = (prior_en + " Also, " + volume_reason_en) if prior_en else volume_reason_en
    evidence["volume_cross_validation_status"] = "UNKNOWN"
    data_gate.evidence = evidence
    result.signal = Signal.NO_TRADE
    result.summary_fa = "معامله مسدود شد: اعتبارسنجی مستقل حجم کامل نیست."


def _build_user_diagnosis(result, asset: AssetSpec, timeframe_ranges: dict, market_overview: dict) -> dict:
    """Turn engine output into a plain-language, gate-backed decision summary."""
    mi = result.metrics.get("market_intelligence") or {}
    coin = _coin_display(asset, mi)
    gates = list(result.gates or [])
    failed = [g for g in gates if getattr(g.status, "value", g.status) == "FAIL"]
    unknown = [g for g in gates if getattr(g.status, "value", g.status) == "UNKNOWN"]
    signal = getattr(result.signal, "value", str(result.signal))
    direction = "BUY" if signal == "LONG" else "SELL" if signal == "SHORT" else "NEUTRAL"
    analysis_incomplete = bool(unknown) or market_overview.get("current_price") is None
    if analysis_incomplete:
        direction = "ANALYSIS_INCOMPLETE"
    thresholds = getattr(getattr(ENGINE, "config", None), "thresholds", None)
    min_confirmation = float(getattr(thresholds, "min_confirmation_score", 75.0))
    max_risk = float(getattr(thresholds, "max_estimated_risk_score", 45.0))
    confirmation = result.confirmation_score
    risk = result.estimated_risk_score
    all_pass = len(gates) >= 12 and all(
        getattr(g.status, "value", g.status) == "PASS" for g in gates
    )
    score_pass = confirmation is not None and float(confirmation) >= min_confirmation
    risk_pass = risk is not None and float(risk) < max_risk
    tradable = direction in {"BUY", "SELL"} and all_pass and score_pass and risk_pass
    buy_allowed = direction == "BUY" and tradable

    gate_reasons = []
    for gate in failed + unknown:
        status = getattr(gate.status, "value", gate.status)
        gate_reasons.append({
            "id": gate.gate_id,
            "name": gate.name_fa,
            "status": status,
            "reason": gate.reason_fa,
        })
    if not score_pass:
        score_status = "NOT_COMPUTABLE" if confirmation is None else "FAIL"
        gate_reasons.append({
            "id": None, "name": "امتیاز نهایی",
            "status": score_status,
            "reason": f"امتیاز تأیید {confirmation if confirmation is not None else 'قابل محاسبه نیست'} است؛ حداقل لازم {min_confirmation:g} است.",
        })
    if not risk_pass:
        risk_status = "NOT_COMPUTABLE" if risk is None else "FAIL"
        gate_reasons.append({
            "id": None, "name": "ریسک معامله",
            "status": risk_status,
            "reason": f"ریسک {risk if risk is not None else 'قابل محاسبه نیست'} است؛ سقف مجاز کمتر از {max_risk:g} است.",
        })
    if direction == "SELL" and tradable:
        gate_reasons.append({
            "id": None, "name": "جهت بازار",
            "status": "PASS",
            "reason": "جهت تأییدشدهٔ موتور فروش است؛ بنابراین خرید تازه توصیه نمی‌شود و فقط کاهش موقعیت موجود قابل بررسی است.",
        })
    elif direction == "NEUTRAL" and not gate_reasons:
        gate_reasons.append({
            "id": None, "name": "جهت بازار",
            "status": "UNKNOWN",
            "reason": result.summary_fa or "جهت خرید یا فروش مشخص نشده است؛ فعلاً صبر کن.",
        })
    elif analysis_incomplete and not gate_reasons:
        gate_reasons.append({
            "id": None, "name": "کفایت داده",
            "status": "UNKNOWN",
            "reason": "قیمت معتبر یا شواهد کافی برای تکمیل تحلیل در دسترس نیست.",
        })
    elif not gate_reasons:
        gate_reasons.append({
            "id": None, "name": "وضعیت فعلی",
            "status": "PASS",
            "reason": "همهٔ فیلترهای اجباری و حدود امتیاز تأیید و ریسک رعایت شده‌اند.",
        })

    if buy_allowed:
        short_action = "خرید کوتاه‌مدت قابل بررسی است؛ ورود فقط با حدضرر و اندازه موقعیت کنترل‌شده."
        long_action = "خرید پله‌ای و نگهداری بلندمدت قابل بررسی است؛ این اسکن ارزش بنیادی یا تضمین سود را تأیید نمی‌کند."
        final_title = "خرید قابل بررسی است"
    elif analysis_incomplete:
        short_action = "تحلیل ناقص است؛ تا دریافت قیمت و شواهد معتبر، هیچ تصمیم معاملاتی صادر نمی‌شود."
        long_action = "دادهٔ تاریخی یا Snapshot معتبر کافی نیست؛ ارزیابی بلندمدت نیز متوقف است."
        final_title = "تحلیل ناقص است؛ تصمیم‌گیری متوقف شد"
    elif direction == "SELL" and tradable:
        short_action = "فروش یا کاهش موقعیت کوتاه‌مدت قابل بررسی است؛ در بازار Spot این به معنی فروش دارایی موجود است، نه فروش استقراضی."
        long_action = "برای خرید و نگهداری بلندمدت فعلاً صبر کن؛ روند و داده‌های فعلی از خرید تازه پشتیبانی نمی‌کنند."
        final_title = "خرید توصیه نمی‌شود؛ فشار فروش تأیید شده است"
    else:
        short_action = "فعلاً صبر؛ خرید یا فروش فوری تأیید نشده است."
        long_action = "فعلاً خرید بلندمدت و نگهداری جدید توصیه نمی‌شود؛ ابتدا روند و آزمون‌های آماری باید تأیید شوند."
        final_title = "فعلاً خرید توصیه نمی‌شود"

    short_reasons = []
    for gate_id in (1, 2, 3, 4, 5, 7, 10):
        gate = next((g for g in gates if g.gate_id == gate_id), None)
        if gate is not None:
            status = getattr(gate.status, "value", gate.status)
            short_reasons.append(f"{gate.name_fa}: {gate.reason_fa} (وضعیت: {status})")
    if not short_reasons:
        short_reasons = [result.summary_fa]

    long_reasons = []
    for gate_id in (2, 3, 6, 8, 12):
        gate = next((g for g in gates if g.gate_id == gate_id), None)
        if gate is not None:
            status = getattr(gate.status, "value", gate.status)
            long_reasons.append(f"{gate.name_fa}: {gate.reason_fa} (وضعیت: {status})")
    month = timeframe_ranges.get("1mo") or {}
    week = timeframe_ranges.get("1w") or {}
    if month.get("status") != "KNOWN":
        long_reasons.append("دادهٔ یک‌ماهه کامل نیست؛ کف و سقف این بازه با پوشش ناقص نمایش داده می‌شود.")
    if week.get("status") != "KNOWN":
        long_reasons.append("دادهٔ یک‌هفته کامل نیست؛ برای تصمیم بلندمدت به تاریخچهٔ بیشتری نیاز است.")
    if not long_reasons:
        long_reasons = ["روند، ریسک و اعتبارسنجی زمانی بررسی شده‌اند."]

    age_hours = market_overview.get("candle_age_hours")
    hourly_age = (timeframe_ranges.get("1h") or {}).get("age_hours")
    max_daily_age = float(getattr(thresholds, "max_candle_age_hours", 36.0))
    if age_hours is None:
        candle_age_text = "سن کندل روزانه در دسترس نیست؛ اعتبار تحلیل مبتنی بر کندل روزانه UNKNOWN است."
    elif float(age_hours) > max_daily_age:
        candle_age_text = (
            f"کندل روزانه {float(age_hours):.1f} ساعت سن دارد و از حد مجاز {max_daily_age:.1f} ساعت عبور کرده است؛ "
            "بنابراین تحلیل روزانه معتبر تلقی نمی‌شود. "
            + (f"کندل ۱ساعته جداگانه {float(hourly_age):.1f} ساعت سن دارد، اما جایگزین دادهٔ روزانه نیست." if hourly_age is not None else "دادهٔ ۱ساعته نیز در دسترس نیست.")
        )
    else:
        candle_age_text = (
            f"سن کندل روزانه {float(age_hours):.1f} ساعت است (حد مجاز {max_daily_age:.1f} ساعت). "
            + (f"سن دادهٔ ۱ساعتهٔ مستقل {float(hourly_age):.1f} ساعت است." if hourly_age is not None else "دادهٔ ۱ساعته در دسترس نیست.")
        )
    return {
        "coin_name": coin["name_fa"],
        "coin_name_en": coin["name_en"],
        "symbol": coin["symbol"],
        "overall_signal": direction,
        "analysis_status": "INCOMPLETE" if analysis_incomplete else "COMPLETE",
        "overall_signal_fa": {"BUY": "خرید", "SELL": "فروش", "NEUTRAL": "خنثی",
                              "ANALYSIS_INCOMPLETE": "تحلیل ناقص · داده معتبر کافی نیست"}[direction],
        "short_term": {"action_fa": short_action, "reasons": short_reasons},
        "long_term": {"action_fa": long_action, "reasons": long_reasons},
        "final_recommendation": final_title,
        "buy_recommended": buy_allowed,
        "score_status": "NOT_COMPUTABLE" if confirmation is None else ("PASS" if score_pass else "FAIL"),
        "risk_status": "NOT_COMPUTABLE" if risk is None else ("PASS" if risk_pass else "FAIL"),
        "final_reasons": [row["reason"] for row in gate_reasons],
        "blockers": gate_reasons,
        "candle_age_text": candle_age_text,
        "explanation": "نتیجه فقط بر پایهٔ فیلترهای همین اسکن است؛ در وضعیت خنثی یا با فیلتر ردشده، سیستم ورود را تأیید نمی‌کند. این خروجی سفارش واقعی ارسال نمی‌کند.",
    }


def _run_scan(job_id: str, asset: AssetSpec, headline: str, social_post: str) -> None:
    global SCAN_RUNNING
    try:
        result = ENGINE.analyze(
            asset,
            headlines=[headline] if headline else [],
            social_posts=[social_post] if social_post else [],
        )
        candle_frame = getattr(ENGINE.data, "candle_frames", {}).get(asset.raw)
        range_evidence = observed_price_ranges(candle_frame)
        trend_gate = next((gate for gate in result.gates if gate.gate_id == 3), None)
        snapshot = getattr(ENGINE.data, "snapshots", {}).get(asset.raw)
        timeframe_ranges = _timeframe_ranges(asset, snapshot)
        _enforce_daily_candle_freshness(result, range_evidence, timeframe_ranges)
        _enforce_volume_cross_validation(result)
        market_intelligence = result.metrics.get("market_intelligence", {}) or {}
        coin_display = _coin_display(asset, market_intelligence)
        regime_gate = next((gate for gate in result.gates if gate.gate_id == 2), None)
        candle_records = []
        if candle_frame is not None and not candle_frame.empty:
            required_ohlc = {"open", "high", "low", "close"}
            if required_ohlc.issubset(candle_frame.columns):
                for timestamp, row in candle_frame.tail(120).iterrows():
                    values = [float(row[key]) for key in ("open", "high", "low", "close")]
                    if all(value > 0 for value in values):
                        candle_records.append({
                            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
                            "open": values[0], "high": values[1], "low": values[2], "close": values[3],
                        })
        payload = {
            "safety": {
                "read_only": True,
                "public_data_only": True,
                "wallet_credentials_used": False,
                "private_exchange_credentials_used": False,
                "orders_submitted": False,
                "transactions_signed": False,
            },
            "asset": asset.raw,
            "coin_name": coin_display["name_fa"],
            "coin_name_en": coin_display["name_en"],
            "symbol": coin_display["symbol"],
            "timeframe_ranges": timeframe_ranges,
            "diagnosis": _build_user_diagnosis(result, asset, timeframe_ranges, range_evidence),
            "market_overview": {
                "status": range_evidence.get("status", "UNKNOWN"),
                "reason": range_evidence.get("reason"),
                "current_price": range_evidence.get("current_price"),
                "candle_age_hours": range_evidence.get("candle_age_hours"),
                "ranges": range_evidence.get("windows", {}),
                "candles": candle_records,
                "chart_pattern": result.metrics.get("chart_pattern"),
                "asset_regime": (trend_gate.evidence or {}).get("asset_regime") if trend_gate else None,
                "benchmark_regime": (regime_gate.evidence or {}).get("regime") if regime_gate else None,
            },
            "signal": result.signal.value,
            "confirmation_score": result.confirmation_score,
            "estimated_risk_score": result.estimated_risk_score,
            "risk_score": result.metrics.get("risk_score"),
            "opportunity_rank": result.opportunity_rank,
            "confidence_level_pct": result.metrics.get("confidence_level_pct"),
            "evidence_confidence_pct": result.metrics.get("evidence_confidence_pct"),
            "summary_fa": result.summary_fa,
            "market_intelligence": result.metrics.get("market_intelligence", {}),
            "gates": [
                {"id": gate.gate_id, "name": gate.name_fa, "name_en": gate.name_en,
                 "status": gate.status.value, "reason": gate.reason_fa,
                 "reason_en": gate.reason_en, "evidence": gate.evidence}
                for gate in result.gates
            ],
            "metrics": {
                key: result.metrics.get(key) for key in (
                    "whale_status", "whale_bias", "whale_history_count",
                    "whale_recent_history_bias", "whale_alignment", "sentiment_alignment",
                    "wallet_graph_status", "wallet_graph_event_count",
                    "wallet_graph_unique_wallet_count", "wallet_graph_suspicious_pair_count",
                    "wallet_graph_coverage", "wallet_event_persistence_status",
                    "market_sentiment_score", "fear_greed_value", "fear_greed_attribution",
                    "walk_forward_status", "walk_forward_fold_count", "risk_score",
                    "opportunity_rank", "confidence_level_pct", "evidence_confidence_pct",
                    "elliott_wave",
                )
            },
        }
        with LOCK:
            JOBS[job_id] = {"status": "completed", "result": payload}
    except Exception as exc:
        with LOCK:
            JOBS[job_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with LOCK:
            SCAN_RUNNING = False




_PAPER_CYCLE_STATUS_FA = {
    "NO_TRADE": "معامله‌ای انجام نشد",
    "OPENED": "موقعیت آزمایشی باز شد",
    "CLOSED": "موقعیت آزمایشی بسته شد",
    "HOLDING": "موقعیت آزمایشی همچنان باز است",
    "BLOCKED": "چرخه مسدود شد",
    "SKIPPED": "دارایی از چرخه کنار گذاشته شد",
    "UNKNOWN": "وضعیت نامشخص",
    "RISK_HALT": "ورود به‌دلیل محدودیت ریسک متوقف شد",
    "AI_BLOCKED": "ورود به‌دلیل نبود تأیید هوش مصنوعی رایگان متوقف شد",
}
_PAPER_REASON_FA = {
    "Paper bot currently accepts only BTC, ETH and SOL spot symbols.":
        "این ربات فعلاً فقط بازار اسپات بیت‌کوین، اتریوم و سولانا را پشتیبانی می‌کند.",
    "One paper position is already open; manage it before scanning another asset.":
        "یک موقعیت آزمایشی باز وجود دارد؛ پیش از بررسی دارایی دیگر، ابتدا همان موقعیت مدیریت شود.",
    "Fresh price unavailable; open paper position was left unchanged.":
        "قیمت تازه در دسترس نبود؛ موقعیت آزمایشی باز بدون تغییر باقی ماند.",
    "Fresh cross-validated price is invalid; position was left unchanged.":
        "قیمت تازه و تأییدشده معتبر نیست؛ موقعیت باز بدون تغییر باقی ماند.",
    "Market scan failed; paper state was not changed ({type(exc).__name__}).":
        "تحلیل بازار با خطا روبه‌رو شد؛ وضعیت حساب آزمایشی تغییر نکرد.",
    "A fresh cross-validated public spot price is unavailable.":
        "قیمت عمومی و تازهٔ بازار اسپات که با منابع مستقل تطبیق داده شده باشد، در دسترس نیست.",
    "Every mandatory signal gate must PASS before paper entry.":
        "برای ورود آزمایشی، تمام دروازه‌های اجباری سیگنال باید تأیید شوند؛ دست‌کم یک شرط هنوز تأیید نشده است.",
    "Paper spot bot opens LONG signals only; short and neutral outputs are skipped.":
        "ربات اسپات فقط موقعیت خرید (Long) باز می‌کند؛ سیگنال‌های فروش یا خنثی نادیده گرفته می‌شوند.",
    "Score, risk or stop/target plan did not pass paper safety limits.":
        "امتیاز تأیید، سطح ریسک یا برنامهٔ حد ضرر و حد سود از حدود ایمنی حساب آزمایشی عبور نکرد.",
    "Spot entry, stop-loss and take-profit levels are not ordered safely.":
        "ترتیب قیمت ورود، حد ضرر و حد سود برای اجرای ایمن معامله مناسب نیست.",
    "A timestamped completed candle is required to prevent duplicate entries.":
        "برای جلوگیری از ورود تکراری، وجود یک کندل تکمیل‌شده با زمان ثبت معتبر الزامی است.",
    "This completed-candle signal was already paper-traded.":
        "سیگنال مربوط به این کندل قبلاً در حساب آزمایشی اجرا شده است.",
    "Paper entries paused by risk circuit breaker.":
        "مدار قطع‌کنندهٔ ریسک، ورودهای جدید حساب آزمایشی را موقتاً متوقف کرده است.",
    "A valid approval from the configured free AI reviewer is mandatory for every new paper entry.":
        "برای هر ورود آزمایشی جدید، دریافت تأیید از هوش مصنوعی رایگان الزامی است؛ اتصال یا پاسخ معتبر در دسترس نیست.",
    "Paper account balance is invalid.":
        "موجودی حساب آزمایشی معتبر نیست.",
    "Net reward/risk is invalid after costs.":
        "نسبت بازده به ریسک پس از محاسبهٔ هزینه‌ها معتبر نیست.",
    "Net reward/risk after fees and slippage is below the minimum.":
        "نسبت بازده به ریسک خالص پس از کارمزد و لغزش قیمت، کمتر از حداقل مجاز است.",
    "A valid maximum holding period is required for paper entry.":
        "برای ورود آزمایشی باید حداکثر مدت نگهداری معتبر مشخص شده باشد.",
    "Position sizing returned no safe quantity.":
        "محاسبهٔ اندازهٔ موقعیت، مقدار امن و قابل اجرا تولید نکرد.",
    "Paper ledger rejected duplicate, concurrent or unaffordable entry.":
        "دفترکل حساب آزمایشی به‌دلیل تکراری بودن، اجرای هم‌زمان یا کافی نبودن موجودی، ورود را نپذیرفت.",
}
_PAPER_ENUM_FA = {
    "INSUFFICIENT_SAMPLE": "تعداد نمونهٔ آماری کافی نیست",
    "NO_DATA": "داده‌ای ثبت نشده است",
    "INCOMPLETE": "ناقص",
    "COMPLETE": "کامل",
    "NOT_APPLICABLE": "قابل اعمال نیست",
    "UNKNOWN": "نامشخص",
    "APPROVED": "تأیید",
    "VETO": "رد",
    "RATE_LIMITED": "سهمیه یا محدودیت سرویس پر شده است",
    "CONFIGURED": "کلید ثبت شده",
    "MISSING_API_KEY": "کلید API ثبت نشده",
    "PASS": "تأیید",
    "FAIL": "رد",
    "PARTIAL": "جزئی",
    "KNOWN": "موجود و معتبر",
    "LONG": "خرید (Long)",
    "SHORT": "فروش (Short)",
    "TAKE_PROFIT": "رسیدن به حد سود",
    "STOP_LOSS": "فعال شدن حد ضرر",
    "TRAILING_STOP": "فعال شدن حد ضرر شناور",
    "TIME_EXIT": "خروج بر اساس محدودیت زمانی",
}
def _paper_reason_fa(reason: Any) -> str:
    if not isinstance(reason, str) or not reason.strip():
        return "دلیل تکمیلی برای این وضعیت ثبت نشده است."
    if reason in _PAPER_REASON_FA:
        return _PAPER_REASON_FA[reason]
    if reason.startswith("Market scan failed"):
        return "تحلیل بازار با خطا روبه‌رو شد؛ وضعیت حساب آزمایشی تغییر نکرد."
    return "جزئیات علت در گزارش فنی موجود است؛ برای جلوگیری از ترجمهٔ نادرست، متن ناشناخته نمایش داده نمی‌شود."


def _paper_enum_fa(value: Any) -> str:
    if value is None:
        return "ثبت نشده"
    normalized = str(getattr(value, "value", value)).upper()
    return _PAPER_ENUM_FA.get(normalized, normalized if normalized.isdigit() else "نامشخص")


def _paper_tick_report_fa(cycle: dict, paper_state: dict, safety: dict) -> dict:
    """Build a Persian, user-facing report without changing trading decisions or raw API fields."""
    cycle = cycle if isinstance(cycle, dict) else {}
    paper_state = paper_state if isinstance(paper_state, dict) else {}
    safety = safety if isinstance(safety, dict) else {}

    def row(label: str, value: Any, unit: Optional[str] = None) -> dict:
        return {"label": label, "value": value, "unit": unit}

    status = str(cycle.get("status") or "UNKNOWN").upper()
    asset = str(cycle.get("asset") or "نامشخص").split(":")[-1]
    reason = cycle.get("reason")
    cycle_rows = [
        row("وضعیت چرخه", _PAPER_CYCLE_STATUS_FA.get(status, "وضعیت نامشخص")),
        row("دارایی بررسی‌شده", asset),
    ]
    if reason:
        cycle_rows.append(row("دلیل تصمیم", _paper_reason_fa(reason)))
    ai_review = cycle.get("ai_review")
    if isinstance(ai_review, dict):
        cycle_rows.append(row(
            "وضعیت بررسی هوش مصنوعی",
            ai_review.get("status_fa") or _paper_enum_fa(ai_review.get("status")),
        ))
        if ai_review.get("model"):
            cycle_rows.append(row("مدل AI رایگان", ai_review.get("model")))
        if ai_review.get("final_rating"):
            cycle_rows.append(row("نظر نهایی AI", ai_review.get("final_rating")))
        if ai_review.get("rationale_fa"):
            cycle_rows.append(row("دلیل نظر AI", ai_review.get("rationale_fa")))
        for role, view in (ai_review.get("specialist_views") or {}).items():
            if isinstance(view, dict):
                cycle_rows.append(row(
                    "تحلیل " + str(role),
                    str(view.get("view") or "UNKNOWN") + " · " + str(view.get("reason_fa") or ""),
                ))
    ai_connection = paper_state.get("ai_connection") or {}
    if ai_connection:
        cycle_rows.append(row(
            "اتصال هوش مصنوعی رایگان",
            ai_connection.get("status_fa") or _paper_enum_fa(ai_connection.get("status")),
        ))
    for key, label, unit in (
        ("side", "جهت موقعیت", None),
        ("quantity", "مقدار دارایی", None),
        ("entry_price", "قیمت ورود", "USD"),
        ("exit_price", "قیمت خروج", "USD"),
        ("mark_price", "قیمت فعلی ثبت‌شده", "USD"),
        ("active_stop_loss", "حد ضرر فعال", "USD"),
        ("stop_loss", "حد ضرر", "USD"),
        ("take_profit", "حد سود", "USD"),
        ("risk_budget_usd", "بودجهٔ ریسک", "USD"),
        ("confirmation_score", "امتیاز تأیید", None),
        ("net_reward_risk", "نسبت بازده به ریسک خالص", None),
        ("unrealized_pnl_usd", "سود/زیان تحقق‌نیافته", "USD"),
        ("max_holding_days", "حداکثر مدت نگهداری", "روز"),
        ("fee_usd", "کارمزد ثبت‌شده", "USD"),
    ):
        if key in cycle and cycle.get(key) is not None:
            value = cycle.get(key)
            if key == "side":
                value = _paper_enum_fa(value)
            cycle_rows.append(row(label, value, unit))

    trade = cycle.get("trade")
    if isinstance(trade, dict):
        for key, label, unit in (
            ("gross_pnl_usd", "سود/زیان ناخالص معاملهٔ بسته‌شده", "USD"),
            ("net_pnl_usd", "سود/زیان خالص معاملهٔ بسته‌شده", "USD"),
            ("exit_price", "قیمت خروج", "USD"),
            ("exit_reason", "علت خروج", None),
        ):
            if trade.get(key) is not None:
                value = _paper_enum_fa(trade[key]) if key == "exit_reason" else trade[key]
                cycle_rows.append(row(label, value, unit))

    safety_rows = [
        row("حالت فقط‌خواندنی", "فعال" if safety.get("read_only") else "غیرفعال"),
        row("حالت فقط حساب آزمایشی", "فعال" if safety.get("paper_only") else "غیرفعال"),
        row("استفاده از اعتبارنامهٔ کیف‌پول", "استفاده نشده" if not safety.get("wallet_credentials_used") else "استفاده شده"),
        row("استفاده از اعتبارنامهٔ خصوصی صرافی", "استفاده نشده" if not safety.get("private_exchange_credentials_used") else "استفاده شده"),
        row("ارسال سفارش واقعی", "انجام نشده" if not safety.get("orders_submitted") else "انجام شده"),
        row("امضای تراکنش بلاک‌چین", "انجام نشده" if not safety.get("transactions_signed") else "انجام شده"),
    ]

    account = paper_state.get("account") or {}
    account_rows = []
    for key, label, unit in (
        ("initial_equity_usd", "سرمایهٔ اولیهٔ حساب", "USD"),
        ("cash_balance_usd", "موجودی نقدی", "USD"),
        ("equity_usd", "ارزش کل حساب", "USD"),
        ("realized_pnl_usd", "سود/زیان تحقق‌یافته", "USD"),
        ("unrealized_pnl_usd", "سود/زیان تحقق‌نیافته", "USD"),
        ("total_return_pct", "بازده کل", "%"),
    ):
        if key in account:
            account_rows.append(row(label, account.get(key), unit))

    position = paper_state.get("open_position")
    if isinstance(position, dict):
        position_rows = [
            row("دارایی", str(position.get("asset") or "").split(":")[-1]),
            row("مقدار", position.get("quantity")),
            row("قیمت ورود", position.get("entry_price"), "USD"),
            row("قیمت مبنا", position.get("mark_price"), "USD"),
            row("حد ضرر", position.get("stop_loss"), "USD"),
            row("حد سود", position.get("take_profit"), "USD"),
            row("سود/زیان تحقق‌نیافته", paper_state.get("account", {}).get("unrealized_pnl_usd"), "USD"),
        ]
        position_note = None
    else:
        position_rows = []
        position_note = "در حال حاضر هیچ موقعیت بازی در حساب آزمایشی وجود ندارد."

    recent_trades = []
    for item in paper_state.get("recent_trades") or []:
        if not isinstance(item, dict):
            continue
        trade_rows = []
        for key, label, unit in (
            ("side", "جهت", None),
            ("quantity", "مقدار", None),
            ("entry_price", "قیمت ورود", "USD"),
            ("exit_price", "قیمت خروج", "USD"),
            ("gross_pnl", "سود/زیان ناخالص", "USD"),
            ("net_pnl", "سود/زیان خالص", "USD"),
            ("entry_fee", "کارمزد ورود", "USD"),
            ("exit_fee", "کارمزد خروج", "USD"),
        ):
            if key in item:
                value = _paper_enum_fa(item[key]) if key == "side" else item.get(key)
                trade_rows.append(row(label, value, unit))
        if item.get("exit_reason"):
            trade_rows.append(row("علت خروج", _paper_enum_fa(item["exit_reason"])))
        recent_trades.append({
            "title": str(item.get("asset") or "دارایی نامشخص").split(":")[-1],
            "rows": trade_rows,
        })

    performance = paper_state.get("performance") or {}
    performance_rows = [
        row("وضعیت ارزیابی عملکرد", _paper_enum_fa(performance.get("status"))),
        row("تعداد معاملات معتبر بسته‌شده", performance.get("sample_size")),
        row("حداقل نمونهٔ لازم", performance.get("minimum_sample_size")),
        row("اجازهٔ ادعای عملکرد آماری", "مجاز" if performance.get("performance_claim_allowed") else "مجاز نیست"),
    ]
    for prefix, label in (
        ("fee", "کارمزد"),
        ("slippage", "لغزش قیمت"),
        ("spread", "اسپرد"),
        ("funding", "هزینهٔ تأمین مالی"),
    ):
        status_key = prefix + "_data_status"
        if status_key in performance:
            performance_rows.append(row("وضعیت دادهٔ " + label, _paper_enum_fa(performance.get(status_key))))
        coverage_key = prefix + "_coverage_pct"
        if coverage_key in performance:
            performance_rows.append(row("پوشش دادهٔ " + label, performance.get(coverage_key), "%"))
        total_key = "total_recorded_" + ("fees" if prefix == "fee" else "slippage" if prefix == "slippage" else "spread" if prefix == "spread" else "funding") + "_usd"
        if total_key in performance:
            performance_rows.append(row("مجموع " + label + " ثبت‌شده", performance.get(total_key), "USD"))
    if "execution_cost_attribution_status" in performance:
        performance_rows.append(row("وضعیت انتساب هزینه‌های اجرا", _paper_enum_fa(performance.get("execution_cost_attribution_status"))))
    if performance.get("reason"):
        performance_rows.append(row("توضیح ارزیابی عملکرد", "هنوز معاملهٔ آزمایشیِ بسته‌شده و معتبری ثبت نشده است." if performance["reason"] == "No valid closed paper trades are recorded yet." else "اطلاعات تکمیلی در دسترس نیست."))

    risk = paper_state.get("risk_controls") or cycle.get("risk_controls") or {}
    risk_rows = []
    if risk:
        risk_rows = [
            row("اجازهٔ ورود جدید", "مجاز" if risk.get("entry_allowed") else "متوقف"),
            row("زیان تحقق‌یافتهٔ روزانه", risk.get("daily_realized_loss_pct"), "%"),
            row("بیشترین افت ارزش حساب", risk.get("max_equity_drawdown_pct"), "%"),
            row("تعداد زیان‌های پیاپی", risk.get("consecutive_losses")),
            row("زمان باقی‌ماندهٔ توقف", risk.get("cooldown_hours_remaining"), "ساعت"),
            row("علت‌های توقف", "، ".join(_PAPER_ENUM_FA.get(str(x).upper(), "محدودیت ریسک") for x in risk.get("halt_reasons", [])) or "محدودیتی ثبت نشده است"),
        ]

    notes = [
        "لغزش قیمت ورود و خروج، در صورت وجود مظنهٔ تازه، از بهترین قیمت خرید/فروش مشاهده‌شده محاسبه می‌شود؛ در غیر این صورت قیمت مرجعِ تطبیق‌یافته مبناست. این هزینه در قیمت اجرای شبیه‌سازی‌شده لحاظ شده و دوباره از سود/زیان کسر نمی‌شود.",
        "هزینهٔ اسپرد فقط وقتی ثبت می‌شود که مظنهٔ خرید و فروشِ تازه و زمان‌دار موجود باشد؛ نبود پوشش کامل به‌عنوان دادهٔ ناقص گزارش می‌شود.",
        "این چرخه فقط PAPER_ONLY است و هیچ سفارش واقعی به صرافی ارسال نمی‌کند.",
        "هزینهٔ تأمین مالی برای این دفترکلِ اسپات کاربرد ندارد؛ در بک‌تست قراردادهای دائمی، تاریخچهٔ کامل Funding لازم است.",
    ]

    return {
        "title": "گزارش فارسی چرخهٔ ربات آزمایشی",
        "cycle": cycle_rows,
        "safety": safety_rows,
        "account": account_rows,
        "open_position": {"rows": position_rows, "note": position_note},
        "recent_trades": recent_trades,
        "recent_trades_note": None if recent_trades else "هنوز معاملهٔ آزمایشیِ بسته‌شده‌ای در دفترکل ثبت نشده است.",
        "performance": performance_rows,
        "risk_controls": risk_rows,
        "execution_cost_notes": notes,
    }


def _paper_tick_page_script(job_id: str) -> str:
    return r"""
<script>
const job='__JOB_ID__';
const root=document.getElementById('result');
function el(tag,text){const node=document.createElement(tag);node.textContent=text===null||text===undefined?'ثبت نشده':String(text);return node;}
function valueText(row){
  const value=row.value;
  let rendered;
  if(value===null||value===undefined) rendered='ثبت نشده';
  else if(typeof value==='number') rendered=new Intl.NumberFormat('fa-IR',{maximumFractionDigits:6}).format(value);
  else rendered=String(value);
  return row.unit ? rendered+' '+row.unit : rendered;
}
function addRows(parent,title,rows,note){
  const section=document.createElement('section');section.className='card';
  section.appendChild(el('h3',title));
  if(note) section.appendChild(el('p',note));
  if(Array.isArray(rows)&&rows.length){
    const table=document.createElement('table'),body=document.createElement('tbody');
    rows.forEach(row=>{
      const tr=document.createElement('tr'),label=document.createElement('th'),value=document.createElement('td');
      label.textContent=row.label||'جزئیات';value.textContent=valueText(row);
      tr.append(label,value);body.appendChild(tr);
    });
    table.appendChild(body);section.appendChild(table);
  } else if(!note) section.appendChild(el('p','داده‌ای برای نمایش ثبت نشده است.'));
  parent.appendChild(section);
}
function renderPaperBotReport(result){
  const report=result&&result.report_fa;
  root.replaceChildren();
  if(!report){root.appendChild(el('p','گزارش فارسی از سرور دریافت نشد؛ صفحه را تازه‌سازی کنید.'));return;}
  root.appendChild(el('h2',report.title||'گزارش چرخهٔ ربات آزمایشی'));
  addRows(root,'نتیجهٔ چرخه',report.cycle);
  addRows(root,'وضعیت ایمنی',report.safety);
  addRows(root,'وضعیت حساب آزمایشی',report.account);
  addRows(root,'موقعیت باز',report.open_position&&report.open_position.rows,report.open_position&&report.open_position.note);
  if(report.recent_trades&&report.recent_trades.length){
    report.recent_trades.forEach((trade,index)=>addRows(root,'معاملهٔ ثبت‌شدهٔ '+(index+1)+' · '+trade.title,trade.rows));
  } else addRows(root,'سوابق معاملات',[],report.recent_trades_note||'معامله‌ای ثبت نشده است.');
  addRows(root,'ارزیابی عملکرد آماری',report.performance);
  if(report.risk_controls&&report.risk_controls.length)addRows(root,'کنترل‌های ریسک',report.risk_controls);
  if(report.execution_cost_notes&&report.execution_cost_notes.length){
    const section=document.createElement('section');section.className='card';
    section.appendChild(el('h3','توضیح هزینه‌های اجرا'));
    const list=document.createElement('ul');
    report.execution_cost_notes.forEach(note=>list.appendChild(el('li',note)));
    section.appendChild(list);root.appendChild(section);
  }
  const safety=document.createElement('p');safety.className='muted';
  safety.textContent='این گزارش صرفاً مربوط به حساب آزمایشی است؛ هیچ سفارش واقعی ارسال نشده و هیچ تراکنشی امضا نشده است.';
  root.appendChild(safety);
}
let pollAttempts=0;
const pollStarted=Date.now();
async function poll(){
  try{
    const response=await fetch('/jobs/'+job,{cache:'no-store'});
    if(!response.ok)throw new Error('HTTP '+response.status);
    const jobData=await response.json();
    pollAttempts=0;
    const states={queued:'در صف اجرا',running:'در حال اجرا',completed:'تکمیل شد',failed:'ناموفق'};
    document.getElementById('state').textContent=states[jobData.status]||'در حال پردازش';
    if(jobData.status==='completed'){renderPaperBotReport(jobData.result);return;}
    if(jobData.status==='failed'){root.replaceChildren(el('p','اجرای چرخه ناموفق بود. خطای فنی: '+(jobData.error||'نامشخص')));return;}
    if(Date.now()-pollStarted>=1800000){document.getElementById('state').textContent='مهلت پیگیری پایان یافت؛ شناسهٔ چرخه: '+job;return;}
  }catch(error){
    pollAttempts+=1;
    if(Date.now()-pollStarted>=1800000){document.getElementById('state').textContent='مهلت بازیابی ارتباط پایان یافت؛ شناسهٔ چرخه: '+job;return;}
    const delay=Math.min(30000,1500*Math.pow(2,Math.min(pollAttempts,4)));
    document.getElementById('state').textContent='ارتباط موقتاً قطع شد؛ همان چرخه به‌طور خودکار دوباره بررسی می‌شود ('+Math.ceil(delay/1000)+' ثانیهٔ دیگر).';
    setTimeout(poll,delay);return;
  }
  setTimeout(poll,2500);
}
poll();
</script>
""".replace("__JOB_ID__", job_id)


def _run_paper_tick(job_id: str, asset: AssetSpec) -> None:
    global SCAN_RUNNING
    try:
        result = PAPER_BOT.run_once(asset)
        paper_state = PAPER_BOT.state()
        safety = {
            "read_only": True,
            "paper_only": True,
            "wallet_credentials_used": False,
            "private_exchange_credentials_used": False,
            "orders_submitted": False,
            "transactions_signed": False,
        }
        cycle = dict(result) if isinstance(result, dict) else {"status": "UNKNOWN"}
        cycle["status_fa"] = _PAPER_CYCLE_STATUS_FA.get(
            str(cycle.get("status") or "UNKNOWN").upper(), "وضعیت نامشخص"
        )
        cycle["reason_fa"] = _paper_reason_fa(cycle.get("reason")) if cycle.get("reason") else None
        payload = {
            "safety": safety,
            "cycle": cycle,
            "paper_state": paper_state,
            "report_fa": _paper_tick_report_fa(cycle, paper_state, safety),
        }
        with LOCK:
            JOBS[job_id] = {"status": "completed", "result": payload}
    except Exception as exc:
        with LOCK:
            JOBS[job_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with LOCK:
            SCAN_RUNNING = False


def _live_market_radar_payload(symbol: str, interval: str, *, force_refresh: bool = False) -> dict:
    """Fetch live spot candles from two independent venues and cache briefly."""
    normalized_symbol = str(symbol or "").strip().upper()
    raw_interval = str(interval or "").strip()
    normalized_interval = raw_interval if raw_interval == "1M" else raw_interval.lower()
    allowed_symbols = {item[0] for item in CRYPTO_MARKET_UNIVERSE}
    if normalized_symbol not in allowed_symbols:
        raise ValueError("نماد برای رادار زنده در فهرست بازار منتخب نیست.")
    if normalized_interval not in INTERVAL_MS:
        raise ValueError("بازهٔ کندل پشتیبانی نمی‌شود.")
    key = (normalized_symbol, normalized_interval)
    now = time.monotonic()
    with LIVE_MARKET_RADAR_CACHE_LOCK:
        cached = LIVE_MARKET_RADAR_CACHE.get(key)
        if (
            not force_refresh and cached is not None
            and now - float(cached.get("_cached_at", 0.0)) < (2 if normalized_interval == "1s" else LIVE_MARKET_RADAR_CACHE_TTL_SECONDS)
        ):
            return copy.deepcopy({k: v for k, v in cached.items() if k != "_cached_at"})
    # Fetch independent feeds concurrently. A slow secondary endpoint must not
    # serialize candle, trade-flow, and order-book round trips on the chart path.
    binance_provider = ENGINE.data.binance
    bybit_provider = ENGINE.data.bybit
    bybit_frame = None
    bybit_error = None
    aggregate_trades = None
    order_book = None
    flow_errors = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="live-radar-feed"
    ) as executor:
        binance_future = executor.submit(
            binance_provider.candles,
            normalized_symbol, interval=normalized_interval, limit=300,
        )
        bybit_future = None
        if normalized_interval in BYBIT_INTERVAL:
            bybit_future = executor.submit(
                bybit_provider.candles,
                normalized_symbol, interval=BYBIT_INTERVAL[normalized_interval], limit=300,
            )
        else:
            bybit_error = "Bybit Spot does not provide 1-second candles; independent candle validation is unavailable on this interval."

        trades_future = executor.submit(
            binance_provider.http.get,
            f"{binance_provider.BASE}/api/v3/aggTrades",
            {"symbol": normalized_symbol, "limit": 500},
        )
        depth_future = executor.submit(
            binance_provider.http.get,
            f"{binance_provider.BASE}/api/v3/depth",
            {"symbol": normalized_symbol, "limit": 20},
        )

        # Binance candles are mandatory for the chart. Other feeds fail independently.
        binance_frame = binance_future.result()
        if bybit_future is not None:
            try:
                bybit_frame = bybit_future.result()
            except Exception as exc:
                bybit_error = f"{type(exc).__name__}: {str(exc)[:160]}"

        try:
            aggregate_trades = trades_future.result()
            if not isinstance(aggregate_trades, list):
                raise ValueError("Binance aggregate-trade response was not a list")
        except Exception as exc:
            flow_errors.append(f"aggTrades: {type(exc).__name__}: {str(exc)[:100]}")

        try:
            order_book = depth_future.result()
            if not isinstance(order_book, dict) or not isinstance(order_book.get("bids"), list) or not isinstance(order_book.get("asks"), list):
                raise ValueError("Binance depth response schema was incomplete")
        except Exception as exc:
            flow_errors.append(f"depth: {type(exc).__name__}: {str(exc)[:100]}")

    payload = build_live_market_radar_payload(
        normalized_symbol,
        normalized_interval,
        binance_frame,
        bybit_frame,
        bybit_error=bybit_error,
        aggregate_trades=aggregate_trades,
        order_book=order_book,
        market_flow_error="; ".join(flow_errors) if flow_errors else None,
    )
    cached_payload = {**copy.deepcopy(payload), "_cached_at": time.monotonic()}
    with LIVE_MARKET_RADAR_CACHE_LOCK:
        LIVE_MARKET_RADAR_CACHE[key] = cached_payload
        while len(LIVE_MARKET_RADAR_CACHE) > 60:
            oldest = next(iter(LIVE_MARKET_RADAR_CACHE))
            LIVE_MARKET_RADAR_CACHE.pop(oldest, None)
    return payload


def _market_board_payload(items: list[dict], *, refreshing: bool) -> dict:
    total = len(CRYPTO_MARKET_UNIVERSE)
    complete = len(items) >= total
    passed_items = [
        item for item in items[:total]
        if isinstance(item, dict) and item.get("status") == "PASS"
    ]
    passed_count = len(passed_items)
    observed_times = []
    for item in passed_items:
        raw = item.get("received_at_utc")
        if isinstance(raw, str) and raw.strip():
            try:
                stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                observed_times.append(stamp.astimezone(timezone.utc))
            except ValueError:
                continue
    # The board's age reflects its oldest successful quote, not the last fetch attempt.
    oldest_observation = min(observed_times) if observed_times else None
    if complete:
        status = (
            "PASS" if passed_count == total and len(observed_times) == total
            else "PARTIAL" if passed_count else "UNKNOWN"
        )
    else:
        status = "PARTIAL" if passed_count else "LOADING"
    # Report only venues that actually contributed to validated rows. This
    # keeps a successful OKX fallback from being mislabeled as Bybit data.
    sources = list(dict.fromkeys(
        provider
        for item in items
        if isinstance(item, dict) and item.get("status") == "PASS"
        for provider in (item.get("providers") or [])
        if isinstance(provider, str) and provider.strip()
    ))
    if not sources:
        sources = ["Binance Spot", "Bybit Spot"]
    return {
        "status": status,
        "freshness": (
            "FRESH" if status == "PASS"
            else "PARTIAL" if passed_count else "UNKNOWN"
        ),
        "updated_at_utc": oldest_observation.isoformat() if oldest_observation else None,
        "completed_at_utc": datetime.now(timezone.utc).isoformat() if complete else None,
        "cache_ttl_seconds": CRYPTO_MARKET_CACHE_TTL_SECONDS,
        "sources": sources,
        "items": copy.deepcopy(items),
        "completed_items": min(len(items), total),
        "total_items": total,
        "refreshing": refreshing,
    }


def _refresh_crypto_market_board() -> None:
    """Refresh public prices away from the request thread, publishing bounded progress."""
    items: list[dict] = []
    try:
        for symbol, name_en, name_fa in CRYPTO_MARKET_UNIVERSE:
            try:
                asset = AssetSpec.parse("cex:" + symbol)
                snapshot = ENGINE.data.snapshot(asset)
                item = {
                    "symbol": symbol,
                    "name_en": name_en,
                    "name_fa": name_fa,
                    "status": "PASS",
                    "price_usd": snapshot.price_usd,
                    "volume_24h_usd": snapshot.volume_24h_usd,
                    "price_discrepancy_pct": snapshot.price_discrepancy_pct,
                    "providers": [snapshot.provider_a, snapshot.provider_b],
                    "received_at_utc": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as exc:
                item = {
                    "symbol": symbol,
                    "name_en": name_en,
                    "name_fa": name_fa,
                    "status": "UNKNOWN",
                    "reason": type(exc).__name__ + ": " + str(exc)[:180],
                }
            items.append(item)
            with CRYPTO_MARKET_CACHE_LOCK:
                CRYPTO_MARKET_CACHE["payload"] = _market_board_payload(items, refreshing=True)
                CRYPTO_MARKET_CACHE["expires_at"] = 0.0

        with CRYPTO_MARKET_CACHE_LOCK:
            CRYPTO_MARKET_CACHE["payload"] = _market_board_payload(items, refreshing=False)
            CRYPTO_MARKET_CACHE["expires_at"] = time.monotonic() + CRYPTO_MARKET_CACHE_TTL_SECONDS
            CRYPTO_MARKET_CACHE["last_error"] = None
    except Exception as exc:
        with CRYPTO_MARKET_CACHE_LOCK:
            CRYPTO_MARKET_CACHE["last_error"] = type(exc).__name__ + ": " + str(exc)[:180]
            if CRYPTO_MARKET_CACHE.get("payload") is None:
                CRYPTO_MARKET_CACHE["payload"] = {
                    "status": "UNKNOWN", "freshness": "UNKNOWN", "updated_at_utc": None,
                    "cache_ttl_seconds": CRYPTO_MARKET_CACHE_TTL_SECONDS,
                    "sources": ["Binance Spot", "Bybit Spot"], "items": [],
                    "completed_items": 0, "total_items": len(CRYPTO_MARKET_UNIVERSE),
                    "refreshing": False, "error": CRYPTO_MARKET_CACHE["last_error"],
                }
    finally:
        with CRYPTO_MARKET_CACHE_LOCK:
            CRYPTO_MARKET_CACHE["refreshing"] = False
            current = CRYPTO_MARKET_CACHE.get("payload")
            if current is not None:
                current["refreshing"] = False


def _start_market_board_refresh() -> None:
    with CRYPTO_MARKET_CACHE_LOCK:
        if CRYPTO_MARKET_CACHE["refreshing"]:
            return
        CRYPTO_MARKET_CACHE["refreshing"] = True
        CRYPTO_MARKET_CACHE["last_error"] = None
    worker = threading.Thread(
        target=_refresh_crypto_market_board,
        daemon=True,
        name="crypto-market-board-refresh",
    )
    try:
        worker.start()
    except Exception:
        with CRYPTO_MARKET_CACHE_LOCK:
            CRYPTO_MARKET_CACHE["refreshing"] = False
        raise


def _crypto_market_board() -> dict:
    """Return cached public prices immediately; refresh an expired board in one worker."""
    now = time.monotonic()
    with CRYPTO_MARKET_CACHE_LOCK:
        cached = CRYPTO_MARKET_CACHE.get("payload")
        expires_at = CRYPTO_MARKET_CACHE.get("expires_at", 0.0)
        refreshing = CRYPTO_MARKET_CACHE.get("refreshing", False)

    if (cached is None or expires_at <= now) and not refreshing:
        _start_market_board_refresh()

    with CRYPTO_MARKET_CACHE_LOCK:
        cached = copy.deepcopy(CRYPTO_MARKET_CACHE.get("payload"))
        expires_at = CRYPTO_MARKET_CACHE.get("expires_at", 0.0)
        refreshing = CRYPTO_MARKET_CACHE.get("refreshing", False)
        last_error = CRYPTO_MARKET_CACHE.get("last_error")

    if cached is None:
        return {
            "status": "LOADING", "freshness": "LOADING", "updated_at_utc": None,
            "cache_ttl_seconds": CRYPTO_MARKET_CACHE_TTL_SECONDS,
            "sources": ["Binance Spot", "Bybit Spot"], "items": [],
            "completed_items": 0, "total_items": len(CRYPTO_MARKET_UNIVERSE),
            "refreshing": refreshing,
        }

    cached["refreshing"] = refreshing
    if refreshing:
        # The last item may be published just before the worker commits the final
        # cache expiry. Keep the board in LOADING until refreshing is cleared.
        cached["status"] = "LOADING"
        cached["freshness"] = "PARTIAL" if cached.get("items") else "LOADING"
        return cached

    if expires_at <= time.monotonic():
        cached["status"] = "STALE"
        cached["freshness"] = "STALE"
        for item in cached.get("items", []):
            previous = item.get("status")
            item["status"] = "UNKNOWN"
            item["previous_status"] = previous
            item["reason"] = "Cached quote expired; awaiting fresh cross-validation."
    if last_error:
        cached["refresh_error"] = last_error
    return cached

def _futures_paper_cycle_report(cycle: dict) -> dict:
    """Attach Persian decision text without changing the underlying trading decision."""
    result = dict(cycle) if isinstance(cycle, dict) else {}
    status = str(result.get("status") or "UNKNOWN").upper()
    direction = str(result.get("direction") or "NO_TRADE").upper()
    reason_code = str(result.get("reason_code") or "")
    reason = str(result.get("reason") or "").strip()

    status_labels = {
        "OPENED": "موقعیت آزمایشی باز شد",
        "CLOSED": "موقعیت آزمایشی بسته شد",
        "HOLDING": "موقعیت آزمایشی باز نگه داشته شد",
        "HOLDING_FUNDING_INCOMPLETE": "موقعیت باز است؛ دادهٔ تسویهٔ Funding ناقص است",
        "NO_TRADE": "معامله‌ای انجام نشد",
        "RISK_HALT": "ورود به‌دلیل محدودیت ریسک متوقف شد",
        "AI_BLOCKED": "تأیید هوش مصنوعی دریافت نشد؛ ورود متوقف شد",
        "BLOCKED": "چرخه مسدود شد",
        "SKIPPED": "چرخه اجرا نشد",
        "UNKNOWN": "نتیجه نامشخص؛ چرخه به‌صورت ایمن متوقف شد",
    }
    direction_labels = {
        "LONG": "خرید (LONG)",
        "SHORT": "فروش استقراضی (SHORT)",
        "NO_TRADE": "بدون معامله",
        "NONE": "بدون معامله",
    }
    decision_labels = {
        "OPENED": "ثبت موقعیت آزمایشی",
        "CLOSED": "بستن موقعیت آزمایشی",
        "HOLDING": "مدیریت موقعیت باز",
        "HOLDING_FUNDING_INCOMPLETE": "مدیریت موقعیت باز با هشدار Funding",
        "NO_TRADE": "عدم ورود",
        "RISK_HALT": "توقف به‌علت ترمز ریسک",
        "AI_BLOCKED": "توقف به‌علت عدم تأیید AI",
        "BLOCKED": "توقف ایمنی",
        "SKIPPED": "عدم اجرا",
        "UNKNOWN": "توقف ایمن؛ داده یا وضعیت لازم تأیید نشد",
    }

    if reason_code == "PERPETUAL_MARKET_DATA_UNAVAILABLE":
        reason_fa = (
            "دادهٔ لحظه‌ای قرارداد دائمی از منابع مستقل دریافت یا اعتبارسنجی نشد. "
            "برای حفظ ایمنی، ورود به معامله مسدود ماند و هیچ تغییری در حساب آزمایشی ایجاد نشد."
        )
    elif status == "RISK_HALT":
        reason_fa = "یکی از محدودیت‌های زیان روزانه، افت سرمایه، تعداد زیان‌های پیاپی یا دورهٔ توقف فعال است."
    elif status == "NO_TRADE":
        lowered_reason = reason.casefold()
        if "all 12 unique mandatory gates" in lowered_reason:
            reason_fa = "هر ۱۲ فیلتر اجباری با شناسهٔ یکتا باید PASS باشند؛ دست‌کم یک فیلتر هنوز تأیید نشده است."
        elif "radar did not confirm long or short" in lowered_reason:
            reason_fa = "رادار جهت معتبر خرید یا فروش استقراضی را تأیید نکرد؛ شرایط خنثی یا نامشخص معامله نمی‌شوند."
        elif "confirmation score, risk score" in lowered_reason:
            reason_fa = "امتیاز تأیید، امتیاز ریسک یا حد ضرر و حد سود ساختاری از حد مجاز عبور نکرد."
        elif "timestamped completed perpetual candle" in lowered_reason:
            reason_fa = "کندل تکمیل‌شده و زمان‌دار قرارداد فیوچرز برای جلوگیری از ثبت معاملهٔ تکراری موجود نیست."
        elif "already paper-traded" in lowered_reason:
            reason_fa = "این کندل قبلاً در حساب Paper معامله شده است؛ از ثبت تکراری جلوگیری شد."
        elif "net reward/risk" in lowered_reason:
            reason_fa = "نسبت سود به زیان خالص پس از کارمزد و لغزش قیمت به حداقل تعیین‌شده نرسید."
        elif "historical funding" in lowered_reason or "funding is invalid" in lowered_reason:
            reason_fa = "دادهٔ معتبر Funding یا فاصلهٔ تسویه تأیید نشد؛ ورود متوقف ماند."
        elif "expected holding period" in lowered_reason:
            reason_fa = "مدت نگهداری مورد انتظار معتبر نیست؛ اندازه‌گذاری و ارزیابی معامله انجام نشد."
        else:
            reason_fa = "همهٔ شرایط ایمنی و ورود تأیید نشد؛ موقعیت جدیدی ساخته نشد. جزئیات فنی در بخش دادهٔ خام موجود است."
    elif status == "AI_BLOCKED":
        reason_fa = "پاسخ معتبر و تأییدشده از مرحلهٔ هوش مصنوعی دریافت نشد؛ ورود مجاز نشد."
    elif status == "UNKNOWN":
        reason_fa = "یکی از داده‌ها یا مراحل ضروری قابل تأیید نبود؛ سیستم معامله را متوقف کرد."
    else:
        reason_fa = reason or status_labels.get(status, "وضعیت چرخه ثبت شد.")

    result.update({
        "status_fa": status_labels.get(status, "وضعیت نامشخص"),
        "decision_fa": decision_labels.get(status, "نیازمند بررسی"),
        "direction_fa": direction_labels.get(direction, "بدون معامله"),
        "reason_fa": reason_fa,
        "entry_blocked": status in {"UNKNOWN", "NO_TRADE", "RISK_HALT", "AI_BLOCKED", "BLOCKED", "SKIPPED"},
        "paper_only": True,
        "orders_submitted": False,
    })
    return result


def _run_futures_paper_tick(job_id: str, asset: AssetSpec) -> None:
    """Run one futures-only paper cycle and publish Persian reporting plus raw evidence."""
    global SCAN_RUNNING
    try:
        cycle = _futures_paper_cycle_report(FUTURES_PAPER_BOT.run_once(asset))
        state = FUTURES_PAPER_BOT.state()
        account = state.get("account") or {}
        risk = state.get("risk_controls") or {}
        ai = state.get("ai_connection") or {}
        ai_was_reviewed = isinstance(cycle.get("ai_review"), dict)
        report_fa = {
            "title": "گزارش چرخهٔ فیوچرز آزمایشی",
            "asset": cycle.get("asset") or asset.raw,
            "status": cycle.get("status_fa"),
            "decision": cycle.get("decision_fa"),
            "direction": cycle.get("direction_fa"),
            "reason": cycle.get("reason_fa"),
            "technical_detail": cycle.get("technical_detail"),
            "technical_error_type": cycle.get("technical_error_type"),
            "balance_usd": account.get("balance_usd"),
            "equity_usd": account.get("equity_usd"),
            "open_position": state.get("open_position"),
            "recent_trade_count": len(state.get("recent_trades") or []),
            "orders_submitted": False,
            "account_type_fa": "موجودی نمایشی حساب Paper است و پول واقعی نیست.",
            "risk_circuit_breaker_fa": risk.get(
                "risk_circuit_breaker_status_fa",
                "وضعیت ترمز ریسک حساب در دسترس نیست؛ مجوز نهایی ورود تأیید نشده است.",
            ),
            "risk_entry_flag_scope_fa": risk.get(
                "entry_allowed_scope_fa",
                "فیلد entry_allowed فقط وضعیت ترمز ریسک حساب را نشان می‌دهد، نه مجوز نهایی ورود.",
            ),
            "ai_status": ai.get("status"),
            "ai_status_fa": (
                "تنظیمات AI با وضعیت " + str(ai.get("status") or "نامشخص") + " ثبت شده‌اند؛ اما Nemotron در این چرخه فراخوانی نشد، چون چرخه پیش از مرحلهٔ AI متوقف شد."
                if not ai_was_reviewed else
                "مرحلهٔ AI در این چرخه اجرا شد؛ نتیجهٔ آن در جزئیات فنی ثبت شده است."
            ),
            "liquidation_model_status": state.get("liquidation_model_status"),
        }
        payload = {
            "safety": {
                "read_only": True,
                "paper_only": True,
                "private_exchange_credentials_used": False,
                "orders_submitted": False,
                "transactions_signed": False,
                "leverage": 1.0,
                "liquidation_model_status": state.get("liquidation_model_status"),
            },
            "cycle": cycle,
            "report_fa": report_fa,
            "futures_state": state,
        }
        with LOCK:
            JOBS[job_id] = {"status": "completed", "result": payload}
    except Exception as exc:
        with LOCK:
            JOBS[job_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with LOCK:
            SCAN_RUNNING = False


def _scan_queue_worker():
    """Run stateful scan and paper-bot jobs serially with lease and fencing checks."""
    global SCAN_RUNNING
    while True:
        task = SCAN_QUEUE.get()
        job_id = task["job_id"]
        try:
            with LOCK:
                SCAN_RUNNING = True
                if job_id in JOBS:
                    current = JOBS[job_id]
                    expired = False
                    lease_text = current.get("valid_until_utc")
                    if lease_text:
                        try:
                            expired = datetime.now(timezone.utc) >= datetime.fromisoformat(lease_text)
                        except ValueError:
                            expired = True
                    if current.get("fencing_token") != task.get("fencing_token"):
                        current["status"] = "superseded"
                    elif expired:
                        current["status"] = "expired"
                        current["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
                    else:
                        current["status"] = "running"
                        current["worker_started_at_utc"] = datetime.now(timezone.utc).isoformat()
            with LOCK:
                current = JOBS.get(job_id) or {}
                should_run = (
                    current.get("status") == "running"
                    and current.get("fencing_token") == task.get("fencing_token")
                )
            if should_run:
                if task["route"] == "/paper-bot/tick":
                    _run_paper_tick(job_id, task["asset"])
                elif task["route"] == "/futures-paper-bot/tick":
                    _run_futures_paper_tick(job_id, task["asset"])
                else:
                    _run_scan(job_id, task["asset"], task["headline"], task["social_post"])
        except Exception as exc:
            with LOCK:
                JOBS[job_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            with LOCK:
                SCAN_RUNNING = False
                key = task.get("idempotency_key")
                if key and ACTIVE_SCAN_KEYS.get(key) == job_id:
                    ACTIVE_SCAN_KEYS.pop(key, None)
            SCAN_QUEUE.task_done()


def _ensure_scan_worker():
    global SCAN_WORKER_STARTED
    with LOCK:
        if SCAN_WORKER_STARTED:
            return
        threading.Thread(target=_scan_queue_worker, daemon=True, name="crypto-scan-queue").start()
        SCAN_WORKER_STARTED = True



def _blofin_demo_execution_readiness() -> dict:
    """Expose Demo execution prerequisites; READY requires verified read/write API permission."""
    credential_fields = ("api_key", "api_secret", "passphrase")
    selected_credential_sources = tuple(
        _resolve_env_credential(DEMO_TRADE_CREDENTIAL_ENV[field])[1]
        for field in credential_fields
    )
    dedicated_source = tuple(
        DEMO_TRADE_CREDENTIAL_ENV[field][0] for field in credential_fields
    )
    unified_source = tuple(
        DEMO_TRADE_CREDENTIAL_ENV[field][1] for field in credential_fields
    )
    legacy_demo_source = tuple(
        DEMO_TRADE_CREDENTIAL_ENV[field][2] for field in credential_fields
    )
    # Report the source selected by the same precedence as the actual client.
    credential_source = {
        dedicated_source: "DEDICATED_TRADE_KEY",
        unified_source: "UNIFIED_ACCOUNT_KEY",
        legacy_demo_source: "EXISTING_DEMO_KEY",
    }.get(selected_credential_sources, "MIXED_OR_INCOMPLETE")
    trade_client = BloFinDemoTradeClient(timeout=8.0)
    trade_credentials_configured = all(bool(value) for value in (
        trade_client.api_key, trade_client.api_secret, trade_client.passphrase
    ))
    trade_permission_status = "NOT_CHECKED"
    trade_permission_verified = False
    if trade_credentials_configured:
        try:
            trade_client.verify_trade_permission()
            trade_permission_status = "READ_WRITE_CONFIRMED"
            trade_permission_verified = True
        except BloFinDemoExecutionBlocked as exc:
            trade_permission_status = "READ_ONLY" if "readOnly=1" in str(exc) else "BLOCKED"
        except Exception:
            trade_permission_status = "CHECK_FAILED"

    trading_enabled = os.getenv("BLOFIN_DEMO_TRADING_ENABLED", "").strip().lower() == "true"
    autopilot_enabled = _blofin_demo_autopilot_enabled()
    dashboard_auth_configured = bool(os.getenv("BLOFIN_DASHBOARD_TOKEN", "").strip())
    budget = None
    single_shot_lock = None
    try:
        backend = storage_backend()
    except Exception:
        backend = "INVALID"
    durable_storage = backend == "TURSO_CLOUD_SQLITE"
    if backend in {"TURSO_CLOUD_SQLITE", "LOCAL_SQLITE"}:
        try:
            store = BloFinDemoEventStore()
            try:
                budget = store.trade_budget_status()
                try:
                    single_shot_lock = store.single_shot_lock_status()
                except Exception:
                    # Keep general event-store readability separate from lock observability.
                    single_shot_lock = None
            finally:
                store.close()
        except Exception:
            budget = None
    orders_today = budget.get("orders_today") if isinstance(budget, dict) else None
    orders_total = budget.get("orders_total") if isinstance(budget, dict) else None
    unresolved_orders = budget.get("unresolved_orders") if isinstance(budget, dict) else None
    event_store_readable = isinstance(budget, dict)
    blockers = []
    if not trade_credentials_configured:
        blockers.append("TRADE_CREDENTIALS_MISSING")
    elif not trade_permission_verified:
        blockers.append("TRADE_PERMISSION_NOT_VERIFIED")
    if not trading_enabled:
        blockers.append("DEMO_TRADING_DISABLED")
    if not dashboard_auth_configured:
        blockers.append("DASHBOARD_TOKEN_NOT_CONFIGURED")
    if not durable_storage:
        blockers.append("DURABLE_TURSO_NOT_CONFIGURED")
    if not event_store_readable:
        blockers.append("EVENT_STORE_UNAVAILABLE")
    ready = not blockers
    blocker_messages = {
        "TRADE_CREDENTIALS_MISSING": "کلید، Secret و Passphrase مربوط به BloFin Demo Trade کامل نیستند",
        "TRADE_PERMISSION_NOT_VERIFIED": (
            "مجوز Read/Write یا Trade کلید Demo با query-apikey تأیید نشد"
            if trade_permission_status != "READ_ONLY"
            else "کلید API فقط‌خواندنی است؛ مجوز Trade باید برای همان کلید Demo فعال باشد"
        ),
        "DEMO_TRADING_DISABLED": "کلید ایمنی BLOFIN_DEMO_TRADING_ENABLED در Render فعال نیست",
        "DASHBOARD_TOKEN_NOT_CONFIGURED": "توکن دسترسی پنل BLOFIN_DASHBOARD_TOKEN در Render تنظیم نشده است",
        "DURABLE_TURSO_NOT_CONFIGURED": "ذخیره‌سازی پایدار Turso برای ثبت و تطبیق سفارش‌ها آماده نیست",
        "EVENT_STORE_UNAVAILABLE": "دفتر رویداد معامله قابل خواندن نیست",
    }
    blockers_fa = [blocker_messages[item] for item in blockers]
    if blockers:
        message_fa = "اجرای سفارش Demo مسدود است: " + "؛ ".join(blockers_fa) + "."
    else:
        message_fa = (
            "پیش‌نیازهای BloFin Demo آماده‌اند؛ مجوز Read/Write کلید واقعی با query-apikey تأیید شد. "
            "در پروفایل Challenge 1، کندل‌های بسته‌شدهٔ ۵دقیقه‌ای، الگوهای کندلی/نموداری، "
            "موج الیوت، دو تأیید مستقل AI و کنترل‌های حساب و ریسک بررسی می‌شوند."
        )
    return {
        "status": "READY" if ready else "BLOCKED",
        "blockers": blockers,
        "blockers_fa": blockers_fa,
        "environment": "BLOFIN_DEMO",
        "trade_credentials_configured": trade_credentials_configured,
        "credential_source": credential_source,
        "trade_permission_status": trade_permission_status,
        "trade_permission_verified": trade_permission_verified,
        "trade_permission_verified_each_cycle": trade_permission_verified,
        "trading_enabled": trading_enabled,
        "autopilot_enabled": autopilot_enabled,
        "durable_storage": durable_storage,
        "dashboard_auth_configured": dashboard_auth_configured,
        "order_quota_unlimited": False,
        "single_shot_lock": single_shot_lock,
        "single_shot_consumed_orders": single_shot_lock.get("consumed_orders") if isinstance(single_shot_lock, dict) else None,
        "single_shot_locked": single_shot_lock.get("locked") if isinstance(single_shot_lock, dict) else None,
        "v2_orchestration_enabled": os.getenv("BLOFIN_CHALLENGE1_V2_ORCHESTRATION_ENABLED", "").strip().lower() == "true",
        "single_shot_write_enabled": os.getenv("CHALLENGE_1_V2_SINGLE_SHOT_WRITE", "").strip().lower() == "true",
        "orders_today_utc": orders_today,
        "orders_total": orders_total,
        "max_orders_per_utc_day": None,
        "daily_capacity_available": event_store_readable,
        "event_store_readable": event_store_readable,
        "unresolved_orders": unresolved_orders,
        "latest_order_status": budget.get("latest_order_status") if isinstance(budget, dict) else None,
        "latest_order_age_seconds": budget.get("latest_order_age_seconds") if isinstance(budget, dict) else None,
        "leverage": 1,
        "margin_mode": "isolated",
        "risk_per_trade_pct": 0.25,
        "daily_loss_limit_pct": 1.0,
        "max_drawdown_pct": 5.0,
        "max_margin_usage_pct": 10.0,
        "max_concurrent_positions": 1,
        "signal_deduplication": "ONE_ORDER_PER_COMPLETED_5M_CANDLE",
        "message_fa": message_fa,
    }

def _blofin_demo_smoke_test_readiness(*, check_account_state: bool = False) -> dict:
    """Require a complete read-only BTC-USDT account check before the independent Demo test.

    Manual dashboard checks can inspect the Demo account even while Trade readiness or
    the smoke-test feature flag is blocked. This path is GET-only and never submits,
    amends, or cancels an order.
    """
    base = _blofin_demo_execution_readiness()
    enabled = os.getenv("BLOFIN_DEMO_SMOKE_TEST_ENABLED", "").strip().lower() == "true"
    blockers: list[str] = []
    open_position_count = None
    open_order_count = None
    snapshot = None
    account_check_performed = bool(
        check_account_state or (base.get("status") == "READY" and enabled)
    )

    if account_check_performed:
        try:
            snapshot = _blofin_demo_live_monitor_snapshot("BTC-USDT")
        except Exception:
            snapshot = None
            blockers.append("LIVE_MONITOR_FAILED")

        if not isinstance(snapshot, dict):
            if "LIVE_MONITOR_FAILED" not in blockers:
                blockers.append("LIVE_MONITOR_UNAVAILABLE")
        elif (
            snapshot.get("status") != "CONNECTED_READ_ONLY"
            or snapshot.get("data_completeness") != "COMPLETE"
            or snapshot.get("read_errors")
        ):
            blockers.append("LIVE_MONITOR_INCOMPLETE")
        else:
            try:
                positions = snapshot.get("positions_for_instrument")
                if not isinstance(positions, list):
                    all_positions = snapshot.get("positions")
                    positions = (
                        [
                            row for row in all_positions
                            if not isinstance(row, dict)
                            or str(row.get("instId") or "BTC-USDT").strip().upper() == "BTC-USDT"
                        ]
                        if isinstance(all_positions, list) else all_positions
                    )
                open_orders = snapshot.get("open_orders_for_instrument")
                if not isinstance(open_orders, list):
                    all_orders = snapshot.get("open_orders")
                    open_orders = (
                        [
                            row for row in all_orders
                            if not isinstance(row, dict)
                            or str(row.get("instId") or "BTC-USDT").strip().upper() == "BTC-USDT"
                        ]
                        if isinstance(all_orders, list) else all_orders
                    )
                open_position_count = _active_position_count(positions)
                open_order_count = _active_order_count(open_orders)
            except Exception:
                blockers.append("POSITION_OR_ORDER_STATE_UNVERIFIABLE")
            else:
                if open_position_count:
                    blockers.append("EXISTING_OPEN_POSITION")
                if open_order_count:
                    blockers.append("EXISTING_OPEN_ORDER")

    ready = base.get("status") == "READY" and enabled and not blockers
    account_summary_fa = ""
    if account_check_performed:
        if (
            isinstance(snapshot, dict)
            and snapshot.get("status") == "CONNECTED_READ_ONLY"
            and snapshot.get("data_completeness") == "COMPLETE"
            and open_position_count is not None
            and open_order_count is not None
        ):
            account_summary_fa = (
                f" بررسی فقط‌خواندنی BTC-USDT کامل شد: "
                f"{open_position_count} پوزیشن باز و {open_order_count} سفارش باز."
            )
        else:
            account_summary_fa = " بررسی فقط‌خواندنی حساب کامل نشد؛ تا رفع ابهام، هیچ سفارشی ارسال نمی‌شود."

    if blockers:
        message_fa = (
            "تست مستقل مسدود شد؛ وضعیت کامل و بدون ابهام حساب BTC-USDT لازم است. "
            "پوزیشن یا سفارش قبلی به‌هیچ‌وجه توسط تست بسته یا لغو نمی‌شود."
            + account_summary_fa
        )
    elif base.get("status") != "READY":
        message_fa = (
            base.get("message_fa")
            or "پیش‌نیازهای اجرای Demo کامل نیستند؛ تست مستقل مسدود می‌ماند."
        ) + account_summary_fa
    elif not enabled:
        message_fa = (
            "بررسی حساب انجام شد، اما اجرای تست مستقل هنوز غیرفعال است. "
            "فعال‌سازی تست فقط با BLOFIN_DEMO_SMOKE_TEST_ENABLED=true در Environment سرویس Demo انجام می‌شود."
            + account_summary_fa
        )
    else:
        message_fa = (
            "تست مستقل آماده است: حساب BTC-USDT در Demo بدون پوزیشن و سفارش باز تأیید شد؛ "
            "تست فقط LONG، حداقل حجم و ایزوله 1x است و فقط پوزیشن متعلق به همان تست را می‌بندد."
            + account_summary_fa
        )

    return {
        **base,
        "status": "READY" if ready else "BLOCKED",
        "smoke_test_enabled": enabled,
        "smoke_test_signal_independent": True,
        "smoke_test_instrument": "BTC-USDT",
        "smoke_test_direction": "LONG",
        "smoke_test_minimum_size_only": True,
        "smoke_test_auto_close": True,
        "smoke_test_blockers": blockers,
        "smoke_test_open_position_count": open_position_count,
        "smoke_test_open_order_count": open_order_count,
        "read_only_account_check_performed": account_check_performed,
        "read_only_account_status": (
            snapshot.get("status") if isinstance(snapshot, dict) else
            ("UNAVAILABLE" if account_check_performed else "NOT_CHECKED")
        ),
        "read_only_account_data_completeness": (
            snapshot.get("data_completeness") if isinstance(snapshot, dict) else None
        ),
        "read_only_account_instrument": "BTC-USDT",
        "read_only_account_orders_submitted": False,
        "message_fa": message_fa,
    }


def _record_challenge1_audit(
    cycle_id: str, phase: str, details: dict, *, event_key: str,
    experience_id: str | None = None, position_id: str | None = None,
    status: str = "SUCCESS", reason: str = "",
) -> dict:
    """Persist full evidence first; then queue a compact Notion projection."""
    recorder = None
    try:
        recorder = Challenge1AuditRecorder()
        saved = recorder.append_event(
            cycle_id=cycle_id, phase=phase, event_key=event_key, details=details,
            experience_id=experience_id, position_id=position_id,
        )
    finally:
        if recorder is not None:
            recorder.close()

    audit_event_id = str(saved["event_id"])
    notion_status = "NOT_ATTEMPTED"
    outbox = None
    try:
        outbox = NotionEventOutbox()
        position_phase = phase.startswith("POSITION_") or phase.startswith("EXIT_")
        event_type = "POSITION_REVIEW" if position_phase else "CHALLENGE1_DECISION_TRACE"
        symbol = str(details.get("instrument_id") or details.get("symbol") or details.get("instId") or "").upper()
        signal = str(details.get("signal") or "UNKNOWN").upper()
        if signal not in {"LONG", "SHORT", "NO_TRADE"}:
            signal = "UNKNOWN"
        decision_value = str(details.get("decision") or "").upper()
        ai_decision = (
            "APPROVE" if decision_value in {"APPROVE", "OPEN", "CLOSE", "EXIT"}
            else "REJECT" if decision_value in {"REJECT", "BLOCK"}
            else "OBSERVE_ONLY"
        )
        outbox.enqueue({
            "event_id": audit_event_id,
            "cycle_id": str(saved["cycle_id"]),
            "occurred_at_utc": saved["occurred_at_utc"],
            "event_type": event_type,
            "actor": "Robot",
            "status": status if status in {"SUCCESS", "WARNING", "FAILED", "UNKNOWN", "PENDING"} else "UNKNOWN",
            "market": "Futures",
            "symbol": symbol,
            "timeframe": "5m",
            "signal": signal,
            "ai_decision": ai_decision,
            "reviewer_consensus": str(details.get("reviewer_consensus") or ""),
            "input_data_type": "LIVE_MARKET",
            "reason": str(reason or details.get("reason_fa") or details.get("reason") or phase)[:1600],
            "experience_id": experience_id or "",
            "position_id": position_id or "",
            "details": json.dumps(
                {"phase": phase, "event_key": event_key, "evidence": details},
                ensure_ascii=False, sort_keys=True, default=str,
            )[:1750],
            "source_url": (
                os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
                + "/api/blofin/demo/audit/" + str(saved["cycle_id"])
                if os.getenv("RENDER_EXTERNAL_URL", "").strip() else ""
            ),
        })
        notion_status = "QUEUED"
    except Exception as exc:
        notion_status = "UNAVAILABLE:" + type(exc).__name__
    finally:
        if outbox is not None:
            outbox.close()

    return {
        "status": "RECORDED", "event_id": audit_event_id,
        "sequence": saved["sequence"], "cycle_id": saved["cycle_id"],
        "experience_id": saved.get("experience_id"),
        "position_id": saved.get("position_id"),
        "event_hash": saved["event_hash"],
        "inserted": saved.get("inserted", False),
        "notion_sync_status": notion_status,
    }


def _set_blofin_execution_job(job_id: str, status: str, **values) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with LOCK:
        current = BLOFIN_EXECUTION_JOBS.get(job_id, {})
        current.update({"status": status, **values})
        event = {
            "time_utc": now,
            "status": status,
            "stage": values.get("stage") or current.get("stage") or status,
            "message_fa": values.get("message_fa") or status,
        }
        events = current.setdefault("events", [])
        if not events or any(
            events[-1].get(key) != event.get(key)
            for key in ("status", "stage", "message_fa")
        ):
            events.append(event)
            current["events"] = events[-100:]
        current["updated_at_utc"] = now
        BLOFIN_EXECUTION_JOBS[job_id] = current


def _blofin_demo_live_monitor_snapshot(inst_id: str = "BTC-USDT") -> dict:
    """Build a complete, read-only Demo monitor snapshot with partial-source disclosure."""
    inst_id = str(inst_id or "BTC-USDT").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", inst_id):
        raise BloFinDemoExecutionBlocked("Only a USDT linear perpetual can be monitored.")
    client = BloFinDemoReadOnly(timeout=8.0)
    read_errors: list[dict[str, str]] = []
    try:
        store = BloFinDemoEventStore()
    except Exception as exc:
        store = None
        read_errors.append({"endpoint": "local_event_store", "error_type": type(exc).__name__})

    def read(endpoint: str, operation, default):
        try:
            return operation()
        except Exception as exc:
            # Do not leak exchange response bodies, credentials, or signed request data.
            read_errors.append({"endpoint": endpoint, "error_type": type(exc).__name__})
            return default

    def rows(value, allowed):
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            return []
        return [
            {key: row[key] for key in allowed if key in row}
            for row in value if isinstance(row, dict)
        ]

    try:
        latest = read(
            "local_latest_order",
            lambda: store.latest_order_for_instrument(inst_id) if store is not None else None,
            None,
        ) if store is not None else None
        ticker_rows = read(
            "market_ticker", lambda: client.get_tickers(inst_id=inst_id), []
        )
        mark_rows = read(
            "mark_price", lambda: client.get_mark_price(inst_id=inst_id), []
        )
        api_key_info = read("api_key_info", client.get_api_key_info, {})
        api_key_permission = _blofin_api_key_permission(api_key_info)
        balance = read(
            "futures_balance",
            lambda: client.get_futures_balance(product_type="USDT-FUTURES"),
            {},
        )
        # Concurrency limits are account-wide; the dashboard must observe the same scope.
        positions = read(
            "positions", lambda: client.get_positions(), []
        )
        open_orders = read(
            "open_orders", lambda: client.get_open_orders(), []
        )
        open_tpsl = read(
            "open_tpsl_orders",
            lambda: client.get_open_tpsl_orders(limit=100),
            [],
        )
        tpsl_history = read(
            "tpsl_orders_history",
            lambda: client.get_tpsl_orders_history(inst_id=inst_id, limit=100),
            [],
        )
        fills = read(
            "fills_history",
            lambda: client.get_fills_history(inst_id=inst_id, limit=100),
            [],
        )
        completed_orders = read(
            "orders_history",
            lambda: client.get_orders_history(inst_id=inst_id, limit=100),
            [],
        )
        closed_positions = read(
            "closed_positions_history",
            lambda: client.get_positions_history(
                inst_id=inst_id, state="closed", limit=100,
            ),
            [],
        )
        partially_closed_positions = read(
            "partially_closed_positions_history",
            lambda: client.get_positions_history(
                inst_id=inst_id, state="partially_closed", limit=100,
            ),
            [],
        )

        order_detail = None
        latest_matches_instrument = (
            isinstance(latest, dict)
            and str(latest.get("instrument_id") or "BTC-USDT").upper() == inst_id
        )
        if latest_matches_instrument and latest.get("client_order_id"):
            detail = read(
                "latest_local_order_detail",
                lambda: client.get_order_detail(
                    inst_id, client_order_id=str(latest["client_order_id"]),
                ),
                [],
            )
            if isinstance(detail, list):
                order_detail = next(
                    (row for row in detail if isinstance(row, dict)), None
                )
            elif isinstance(detail, dict):
                order_detail = detail

        ticker = (
            ticker_rows[0]
            if isinstance(ticker_rows, list) and ticker_rows
            and isinstance(ticker_rows[0], dict) else {}
        )
        mark = (
            mark_rows[0]
            if isinstance(mark_rows, list) and mark_rows
            and isinstance(mark_rows[0], dict) else {}
        )
        balance = balance if isinstance(balance, dict) else {}
        if not ticker:
            read_errors.append({"endpoint": "market_ticker", "error_type": "EmptyResponse"})
        if not mark or mark.get("markPrice") in (None, ""):
            read_errors.append({"endpoint": "mark_price", "error_type": "EmptyResponse"})
        if "totalEquity" not in balance or balance.get("totalEquity") is None:
            read_errors.append({"endpoint": "futures_balance_equity", "error_type": "MissingField"})

        position_fields = (
            "positionId", "instId", "instType", "marginMode", "positionSide",
            "positions", "availablePositions", "averagePrice", "markPrice",
            "unrealizedPnl", "unrealizedPnlRatio", "realizedPnl", "leverage",
            "liquidationPrice", "marginRatio", "initialMargin",
            "maintenanceMargin", "createTime", "updateTime",
        )
        order_fields = (
            "orderId", "clientOrderId", "instId", "instType", "marginMode",
            "positionSide", "side", "orderType", "orderCategory", "price",
            "averagePrice", "size", "filledSize", "filledAmount", "fee", "pnl",
            "state", "leverage", "reduceOnly", "createTime", "updateTime",
            "tpTriggerPrice", "tpOrderPrice", "tpTriggerPriceType",
            "slTriggerPrice", "slOrderPrice", "slTriggerPriceType",
            "attachAlgoOrders",
        )
        fill_fields = (
            "tradeId", "orderId", "instId", "positionSide", "side",
            "fillPrice", "fillSize", "fillPnl", "fee", "ts",
        )
        closed_fields = (
            "historyId", "positionId", "instId", "instType", "side",
            "positionSide", "marginMode", "closePositions", "maxPositions",
            "liquidationPositions", "openAveragePrice", "closeAveragePrice",
            "createTime", "updateTime", "leverage", "realizedPnl",
            "realizedPnlRatio", "fee",
        )
        tpsl_fields = (
            "tpslId", "clientOrderId", "instId", "marginMode", "positionSide",
            "side", "tpTriggerPrice", "tpOrderPrice", "tpTriggerPriceType",
            "slTriggerPrice", "slOrderPrice", "slTriggerPriceType", "size",
            "state", "leverage", "reduceOnly", "actualSize", "createTime",
            "updateTime", "brokerId",
        )
        balance_fields = (
            "currency", "equity", "balance", "available", "availableEquity",
            "frozen", "orderFrozen", "equityUsd", "isolatedEquity",
            "isolatedUnrealizedPnl", "bonus", "ts",
        )
        balance_details = balance.get("details")
        balance_details = [
            {key: item[key] for key in balance_fields if key in item}
            for item in balance_details
            if isinstance(balance_details, list) and isinstance(item, dict)
        ] if isinstance(balance.get("details"), list) else []

        safe_detail_fields = (
            "orderId", "clientOrderId", "instId", "side", "positionSide",
            "marginMode", "orderType", "orderCategory", "size", "filledSize",
            "filledAmount", "averagePrice", "price", "state", "createTime",
            "updateTime", "fee", "pnl", "leverage", "tpTriggerPrice",
            "tpOrderPrice", "tpTriggerPriceType", "slTriggerPrice",
            "slOrderPrice", "slTriggerPriceType", "attachAlgoOrders",
        )
        safe_detail = (
            {key: order_detail[key] for key in safe_detail_fields if key in order_detail}
            if isinstance(order_detail, dict) else None
        )
        safe_latest = None
        if latest_matches_instrument:
            allowed_local = (
                "client_order_id", "signal_id", "instrument_id", "side",
                "quantity", "entry_price", "stop_loss", "take_profit",
                "leverage", "status", "exchange_order_id", "created_at", "updated_at",
            )
            safe_latest = {key: latest[key] for key in allowed_local if key in latest}

        execution_events = read(
            "local_execution_events",
            lambda: store.recent_all_events(limit=100) if store is not None else [],
            [],
        )
        def _event_instrument(event: dict) -> str:
            payload = event.get("payload") if isinstance(event, dict) else None
            if not isinstance(payload, dict):
                return ""
            return str(payload.get("instrument_id") or payload.get("instId") or "").strip().upper()

        instrument_execution_events = [
            event for event in execution_events
            if _event_instrument(event) == inst_id
            or (
                not _event_instrument(event)
                and isinstance(safe_latest, dict)
                and event.get("client_order_id") == safe_latest.get("client_order_id")
            )
        ]
        selected_positions = [
            row for row in positions
            if isinstance(row, dict) and str(row.get("instId") or "").strip().upper() == inst_id
        ] if isinstance(positions, list) else []
        selected_open_orders = [
            row for row in open_orders
            if isinstance(row, dict) and str(row.get("instId") or "").strip().upper() == inst_id
        ] if isinstance(open_orders, list) else []
        selected_open_tpsl = [
            row for row in open_tpsl
            if isinstance(row, dict) and str(row.get("instId") or "").strip().upper() == inst_id
        ] if isinstance(open_tpsl, list) else []
        return {
            "instrument_id": inst_id,
            "position_scope": "ACCOUNT_WIDE",
            "open_order_scope": "ACCOUNT_WIDE",
            "selected_instrument_scope": inst_id,
            "status": (
                "AUTH_FAILED"
                if any(item.get("error_type") == "BloFinDemoAuthError" for item in read_errors)
                else "CONNECTED_READ_ONLY"
            ),
            "environment": "BLOFIN_DEMO",
            "permission_mode": "READ_ONLY_REQUESTS",
            "api_key_permission": api_key_permission,
            "request_scope": "READ_ONLY_GET_REQUESTS",
            "orders_enabled": False,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "refresh_interval_seconds": 5,
            "data_completeness": "COMPLETE" if not read_errors else "PARTIAL",
            "read_errors": read_errors,
            "market": {
                "instId": inst_id,
                "last": ticker.get("last"),
                "markPrice": mark.get("markPrice"),
                "indexPrice": mark.get("indexPrice"),
                "ts": ticker.get("ts"),
                "markPriceTs": mark.get("ts"),
            },
            "account": {
                "ts": balance.get("ts"),
                "totalEquity": balance.get("totalEquity"),
                "isolatedEquity": balance.get("isolatedEquity"),
                "details": balance_details,
            },
            "latest_local_order": safe_latest,
            "order_detail": safe_detail,
            "positions": rows(positions, position_fields),
            "positions_for_instrument": rows(selected_positions, position_fields),
            "open_orders": rows(open_orders, order_fields),
            "open_orders_for_instrument": rows(selected_open_orders, order_fields),
            "open_tpsl_orders_for_instrument": rows(selected_open_tpsl, tpsl_fields),
            "fills": rows(fills, fill_fields),
            "completed_orders": rows(completed_orders, order_fields),
            "closed_positions": rows(closed_positions, closed_fields),
            "partially_closed_positions": rows(partially_closed_positions, closed_fields),
            "open_tpsl_orders": rows(open_tpsl, tpsl_fields),
            "tpsl_orders_history": rows(tpsl_history, tpsl_fields),
            "execution_events": execution_events,
            "instrument_execution_events": instrument_execution_events,
        }
    finally:
        if store is not None:
            store.close()



def _blofin_elliott_wave_report_fa(wave: dict | None, direction: str = "NO_TRADE") -> dict:
    """Translate confirmed Elliott structure into a timestamped, non-probabilistic trade context."""
    source = wave if isinstance(wave, dict) else {}
    status = str(source.get("status") or "UNKNOWN").upper()
    pattern = str(source.get("pattern") or "AMBIGUOUS").upper()
    phase = str(source.get("phase") or "INSUFFICIENT_DATA").upper()
    direction = str(direction or "NO_TRADE").upper()
    digit = lambda value: str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))

    status_fa = {
        "CONFIRMED": "ساختار موج پنج‌گانه تأیید شده",
        "CANDIDATE": "شمارش ساختاری موج نامزد است",
        "AMBIGUOUS": "شمارش موج مبهم است",
        "UNKNOWN": "دادهٔ کافی برای شمارش موج موجود نیست",
    }.get(status, "وضعیت شمارش موج مشخص نیست")
    pattern_fa = {
        "BULLISH": "صعودی",
        "BEARISH": "نزولی",
        "AMBIGUOUS": "نامشخص",
    }.get(pattern, "نامشخص")
    phase_fa = {
        "INSUFFICIENT_DATA": "دادهٔ کافی برای تشخیص موج‌های تأییدشده وجود ندارد.",
        "NO_RULE_VALID_IMPULSE": "پیوت‌های تأییدشده با قواعد ساختار ایمپالس الیوت منطبق نیستند.",
        "WAVES_1_AND_2_CONFIRMED": "موج‌های ۱ و ۲ از نظر ساختاری تأیید شده‌اند؛ موج ۳ فقط سناریوی بعدی است.",
        "WAVE_3_CONFIRMED": "موج ۳ از نظر ساختاری تأیید شده است؛ موج ۴ سناریوی بعدی است.",
        "WAVE_4_CONFIRMED": "موج ۴ از نظر ساختاری تأیید شده است؛ موج ۵ سناریوی بعدی است.",
        "FIVE_WAVE_IMPULSE_CONFIRMED": "ساختار پنج‌موجی کامل تأیید شده است؛ جهت موج اصلاحی از این شمارش پیش‌بینی نمی‌شود.",
    }.get(phase, "مرحلهٔ موج به‌صورت قطعی قابل طبقه‌بندی نیست.")

    timeframe = str(source.get("timeframe") or "").upper()
    timeframe_fa = {"5M": "۵ دقیقه‌ای", "1H": "یک‌ساعته", "15M": "۱۵ دقیقه‌ای", "4H": "چهارساعته", "1D": "روزانه"}.get(timeframe, "نامشخص")
    source_code = str(source.get("source") or "").upper()
    source_fa = (
        "منبع: کندل‌های بسته‌شده و تأییدشدهٔ BloFin Demo"
        if source_code == "BLOFIN_DEMO_COMPLETED_1H_CANDLES" else
        "منبع: کندل‌های زمان‌دار و تأییدشدهٔ موتور تحلیل"
    )
    completed_candle_count = source.get("completed_candle_count")
    if completed_candle_count is not None:
        source_fa += f" · کندل‌های بسته‌شده: {digit(completed_candle_count)}"
    if source.get("last_completed_candle_at"):
        source_fa += " · آخرین کندل: " + str(source.get("last_completed_candle_at"))

    next_wave = source.get("next_wave")
    try:
        next_wave = int(next_wave) if next_wave is not None else None
    except (TypeError, ValueError, OverflowError):
        next_wave = None
    if next_wave not in {3, 4, 5}:
        next_wave = None

    selected_wave_fa = "موج بعدی انتخاب نشد؛ ساختار معتبر یا دادهٔ کافی در دسترس نیست."
    if status == "CANDIDATE" and next_wave is not None and pattern in {"BULLISH", "BEARISH"}:
        selected_wave_fa = f"سناریوی ساختاری منتخب: موج {digit(next_wave)} {pattern_fa}؛ این سناریو پیش‌بینی قطعی یا احتمال موفقیت نیست."
    elif status == "CONFIRMED" and phase == "FIVE_WAVE_IMPULSE_CONFIRMED":
        selected_wave_fa = "موج‌های ۱ تا ۵ تکمیل شده‌اند؛ موج اصلاحی بعدی بدون شواهد تازه انتخاب نمی‌شود."

    if direction in {"LONG", "SHORT"}:
        expected_pattern = "BULLISH" if direction == "LONG" else "BEARISH"
        if status in {"CANDIDATE", "CONFIRMED"} and pattern in {"BULLISH", "BEARISH"}:
            alignment = "ALIGNED" if pattern == expected_pattern else "CONFLICT"
        else:
            alignment = "UNKNOWN"
    else:
        alignment = "NO_DIRECTION"

    alignment_fa = {
        "ALIGNED": "ساختار موج با جهت سیگنال هم‌جهت است؛ این مورد به‌تنهایی مجوز ورود نیست.",
        "CONFLICT": "ساختار موج با جهت سیگنال هم‌جهت نیست؛ این تعارض باید در گزارش تصمیم دیده شود.",
        "UNKNOWN": "هم‌جهتی موج با سیگنال قابل تأیید نیست؛ شمارش موج مبهم یا داده ناقص است.",
        "NO_DIRECTION": "هنوز سیگنال LONG یا SHORT وجود ندارد تا با ساختار موج مقایسه شود.",
    }[alignment]

    pivots = []
    for item in source.get("pivots", [])[-6:] if isinstance(source.get("pivots"), list) else []:
        if not isinstance(item, dict):
            continue
        pivots.append({
            "kind_fa": "سقف تأییدشده" if str(item.get("kind", "")).upper() == "HIGH" else "کف تأییدشده",
            "price": item.get("price"),
            "timestamp_utc": item.get("timestamp"),
            "confirmed_at_utc": item.get("confirmed_at"),
        })

    return {
        "status": status,
        "status_fa": status_fa,
        "pattern": pattern,
        "pattern_fa": pattern_fa,
        "phase": phase,
        "phase_fa": phase_fa,
        "timeframe": timeframe,
        "timeframe_fa": timeframe_fa,
        "source_fa": source_fa,
        "completed_candle_count": completed_candle_count,
        "last_completed_candle_at": source.get("last_completed_candle_at"),
        "confirmed_pivot_count": source.get("confirmed_pivot_count", len(source.get("pivots", [])) if isinstance(source.get("pivots"), list) else 0),
        "wave_count": source.get("wave_count"),
        "next_wave": next_wave,
        "selected_wave_fa": selected_wave_fa,
        "trade_direction": direction,
        "direction_alignment": alignment,
        "direction_alignment_fa": alignment_fa,
        "invalidation_price": source.get("invalidation_price"),
        "invalidation_price_note_fa": (
            "شمارش معتبر الیوت تشکیل نشده است؛ بنابراین سطح ابطال قابل تعیین نیست."
            if source.get("invalidation_price") is None else None
        ),
        "pivot_reversal_threshold_pct": source.get("pivot_reversal_threshold_pct"),
        "pivot_reversal_threshold_note_fa": (
            "کندل یا پیوت معتبر کافی برای محاسبه آستانه وجود ندارد."
            if source.get("pivot_reversal_threshold_pct") is None else None
        ),
        "pivots": pivots,
        "reason_fa": phase_fa,
        "confidence_note_fa": "شماره‌گذاری موج فقط شواهد ساختاری است، نه احتمال موفقیت، سیگنال مستقل یا تضمین حرکت بعدی.",
    }


def _blofin_gate_rows_fa(gates: list) -> list:
    """Build safe Persian labels for each of the 12 mandatory execution gates."""
    rows = []
    labels = {"PASS": "تأیید شد", "FAIL": "رد شد", "UNKNOWN": "نامشخص"}
    for item in gates if isinstance(gates, list) else []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "UNKNOWN").upper()
        rows.append({
            "gate_id": item.get("gate_id"),
            "name_fa": item.get("name_fa") or f"فیلتر {item.get('gate_id', '—')}",
            "status": status,
            "status_fa": labels.get(status, "نامشخص"),
            "reason_fa": item.get("reason_fa") or "دلیل فارسی برای این فیلتر ثبت نشده است.",
        })
    return rows


def _blofin_execution_report_fa(job: dict) -> dict:
    """Return a display-safe Persian execution report while retaining source evidence."""
    current = job if isinstance(job, dict) else {}
    result = current.get("result") if isinstance(current.get("result"), dict) else {}
    analysis = current.get("analysis") if isinstance(current.get("analysis"), dict) else {}
    order = current.get("order") if isinstance(current.get("order"), dict) else {}
    fill = current.get("fill") if isinstance(current.get("fill"), dict) else {}
    context = current.get("trade_context") if isinstance(current.get("trade_context"), dict) else {}
    wave = current.get("elliott_wave") if isinstance(current.get("elliott_wave"), dict) else result.get("elliott_wave")
    is_independent_smoke_test = str(current.get("test_name") or "") == BloFinDemoSmokeTestRunner.TEST_NAME
    status = str(result.get("status") or current.get("status") or "UNKNOWN").upper()
    stage = str(current.get("stage") or status).upper()
    direction = str(analysis.get("signal") or result.get("signal") or order.get("side") or "NO_TRADE").upper()
    instrument_id = str(context.get("instrument_id") or result.get("instrument_id") or order.get("instrument_id") or "—")
    execution_profile = (
        "INDEPENDENT_DEMO_SMOKE_TEST" if is_independent_smoke_test else
        str(context.get("execution_profile") or result.get("execution_profile") or analysis.get("execution_profile") or "CHALLENGE_1")
    )

    status_labels = {
        "QUEUED": "در صف اجرا",
        "RUNNING": "در حال اجرا",
        "COMPLETED": "پایان یافت",
        "FAILED": "با خطای کنترل‌شده متوقف شد",
        "OPENED": "موقعیت آزمایشی باز شد",
        "FILLED": "سفارش پر شد",
        "PARTIALLY_FILLED": "سفارش بخشی پر شد",
        "NO_TRADE": "معامله‌ای انجام نشد",
        "AI_BLOCKED": "ورود از سوی هوش مصنوعی تأیید نشد",
        "BLOCKED": "ورود به‌دلیل کنترل ایمنی مسدود شد",
        "UNKNOWN": "نتیجه نامشخص؛ اقدام ایمن متوقف شد",
        "ORDER_OUTCOME_UNKNOWN": "نتیجه سفارش نامشخص است",
        "SKIPPED_DUPLICATE_SIGNAL": "به‌دلیل تکراری بودن کندل، سفارش ساخته نشد",
        "ACCEPTED": "سفارش پذیرفته شد؛ وضعیت پرشدن در حال بررسی است",
        "MONITORING": "معامله در حال پایش است",
    }
    stage_labels = {
        "QUEUED": "ثبت درخواست",
        "READINESS_CHECK": "بررسی پیش‌نیازهای حساب Demo",
        "UNRESOLVED_ORDER_BLOCK": "توقف به‌دلیل سفارش قبلی تعیین‌تکلیف‌نشده",
        "READ_ONLY_ORDER_RECONCILIATION": "تطبیق فقط‌خواندنی سفارش قبلی",
        "ORDER_REMAINS_UNKNOWN": "نتیجه سفارش قبلی هنوز مشخص نیست",
        "LIVE_12_GATE_ANALYSIS": "تحلیل زندهٔ ۱۲ فیلتر",
        "LIVE_EASY_MTF_ANALYSIS": "مسیر قدیمی EASY · تحلیل ۱ ساعته/۱۵ دقیقه‌ای (خارج از Challenge 1)",
        "PROFILE_GATE_REVIEW": "بررسی گیت‌های الزامی پروفایل انتخابی",
        "MTF_TRIGGER_BLOCKED": "تعارض تایم‌فریم مسیر قدیمی EASY (خارج از Challenge 1)",
        "ELLIOTT_WAVE_ANALYSIS": "تحلیل و انتخاب سناریوی موج الیوت",
        "NO_TRADE": "عدم ورود؛ شرایط کامل نیست",
        "DUPLICATE_CANDLE_SKIPPED": "جلوگیری از معامله تکراری",
        "NEMOTRON_SECOND_OPINION": "دریافت نظر دوم از Nemotron",
        "AI_BLOCKED": "توقف به‌دلیل تأیید نشدن Nemotron",
        "DEMO_ORDER_GUARDS": "کنترل نهایی اندازه، مارجین و حد ضرر/سود",
        "ORDER_SUBMITTED": "ارسال سفارش به حساب آزمایشی",
        "ENTRY_FILL_CONFIRMED": "تأیید پرشدن سفارش",
        "ORDER_PENDING": "سفارش ثبت شده؛ پرشدن هنوز تأیید نشده",
        "MONITORING": "پایش معامله و موقعیت",
        "ORDER_OUTCOME_UNKNOWN": "نتیجه ارسال نامشخص",
        "BLOCKED": "توقف ایمنی",
        "EXECUTION_ERROR": "خطای اجرای کنترل‌شده",
    }
    signal_fa = {"LONG": "خرید (LONG)", "SHORT": "فروش استقراضی (SHORT)",
                 "NO_TRADE": "بدون معامله", "UNKNOWN": "جهت نامشخص"}.get(direction, "بدون معامله")
    reason_fa = (
        result.get("reason_fa")
        or current.get("message_fa")
        or "هنوز نتیجهٔ نهایی چرخه ثبت نشده است."
    )
    pass_count = analysis.get("gate_pass_count", result.get("gate_pass_count"))
    fail_count = analysis.get("gate_fail_count", result.get("gate_fail_count"))
    unknown_count = analysis.get("gate_unknown_count", result.get("gate_unknown_count"))
    gates_fa = analysis.get("gates_fa")
    if not isinstance(gates_fa, list):
        gates_fa = _blofin_gate_rows_fa(analysis.get("gates", []))

    if is_independent_smoke_test:
        wave_report = {}
    elif isinstance(wave, dict):
        wave_report = wave if "status_fa" in wave else _blofin_elliott_wave_report_fa(wave, direction)
    else:
        wave_report = _blofin_elliott_wave_report_fa({})

    events_fa = []
    for event in current.get("events", []) if isinstance(current.get("events"), list) else []:
        if not isinstance(event, dict):
            continue
        event_stage = str(event.get("stage") or "").upper()
        events_fa.append({
            "time_utc": event.get("time_utc"),
            "status_fa": status_labels.get(str(event.get("status") or "").upper(), str(event.get("status") or "ثبت شد")),
            "stage_fa": stage_labels.get(event_stage, "مرحلهٔ پردازش"),
            "message_fa": event.get("message_fa") or "رویداد ثبت شد.",
        })

    stage_fa = stage_labels.get(stage, "مرحلهٔ پردازش")
    if execution_profile == "CHALLENGE_1":
        stage_fa = "Challenge 1 · " + stage_fa
    elif execution_profile == "EASY":
        stage_fa = "موتور Challenge 1 · " + stage_fa
    elif execution_profile == "STANDARD":
        stage_fa = "ربات استاندارد · " + stage_fa
    elif execution_profile == "STRICT":
        stage_fa = "ربات سخت‌گیر · " + stage_fa
    fa_digits = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    count_fa = lambda value: str(value if value is not None else "—").translate(fa_digits)
    return {
        "title_fa": "گزارش فارسی اجرای معامله آزمایشی · " + instrument_id,
        "instrument_id": instrument_id,
        "execution_profile": execution_profile,
        "execution_profile_fa": (
            "تست مستقل Demo بدون سیگنال" if is_independent_smoke_test else execution_profile
        ),
        "wave_analysis_applicable": not is_independent_smoke_test,
        "wave_analysis_note_fa": (
            "این تست مستقل فقط مسیر سفارش، Fill، حفاظت TP/SL و بستن پوزیشن متعلق به خودش را می‌آزماید؛ تحلیل موج الیوت و ۱۲ فیلتر در آن اجرا نمی‌شوند."
            if is_independent_smoke_test else None
        ),
        "status_fa": status_labels.get(status, status_labels.get(str(current.get("status") or "").upper(), "وضعیت نامشخص")),
        "stage_fa": stage_fa,
        "signal_fa": signal_fa,
        "reason_fa": reason_fa,
        "gate_pass_count": pass_count,
        "gate_fail_count": fail_count,
        "gate_unknown_count": unknown_count,
        "gate_summary_fa": (
            "در تست مستقل، ۱۲ فیلتر و تحلیل موج اجرا نمی‌شوند."
            if is_independent_smoke_test else
            f"{count_fa(pass_count)} فیلتر تأیید، "
            f"{count_fa(fail_count)} فیلتر رد و "
            f"{count_fa(unknown_count)} فیلتر نامشخص."
        ),
        "gates_fa": gates_fa,
        "elliott_wave": wave_report,
        "analysis_time_utc": context.get("analysis_time_utc"),
        "last_completed_candle_time_utc": context.get("last_completed_candle_time_utc"),
        "last_candle_close": context.get("last_candle_close"),
        "decision_time_utc": current.get("updated_at_utc"),
        "submitted_at_utc": current.get("submitted_at_utc"),
        "filled_at_utc": fill.get("filled_at_utc") or result.get("filled_at_utc"),
        "entry_reference_price": analysis.get("entry_reference_price", result.get("entry_reference_price")),
        "average_fill_price": fill.get("average_fill_price", result.get("average_fill_price")),
        "stop_loss": order.get("stop_loss", result.get("stop_loss")),
        "take_profit": order.get("take_profit", result.get("take_profit")),
        "quantity_contracts": order.get("quantity_contracts", result.get("quantity_contracts")),
        "leverage": order.get("leverage", result.get("leverage", 1)),
        "exchange_order_id": order.get("exchange_order_id", result.get("exchange_order_id")),
        "events_fa": events_fa,
        "orders_submitted": result.get("orders_submitted", False),
        "trade_executed": result.get("trade_executed", False),
    }


def _blofin_parse_completed_candles(client, *, inst_id: str, bar: str, limit: int, interval_ms: int, minimum_rows: int, now_ms: int):
    """Parse only completed BloFin candles and sort oldest to newest."""
    rows = client.get_candles(inst_id, bar=bar, limit=limit)
    if not isinstance(rows, list):
        raise BloFinDemoExecutionBlocked(f"Invalid {bar} candle response.")
    indexed = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 9 or str(row[8]) != "1":
            continue
        try:
            ts = int(row[0])
            o, h, low, close = (float(row[i]) for i in (1, 2, 3, 4))
        except (TypeError, ValueError, OverflowError):
            continue
        values = (o, h, low, close)
        if ts <= 0 or any(not math.isfinite(v) or v <= 0 for v in values):
            continue
        if h < max(o, low, close) or low > min(o, h, close):
            continue
        close_ms = ts + interval_ms
        age = now_ms - close_ms
        if age < -60_000:
            continue
        indexed[ts] = {"timestamp_ms": ts, "close_time_ms": close_ms,
                       "age_ms": max(0, age), "open": o, "high": h,
                       "low": low, "close": close}
    result = [indexed[k] for k in sorted(indexed)]
    if len(result) < minimum_rows:
        raise BloFinDemoExecutionBlocked(f"Insufficient completed {bar} candles.")
    return result


def _blofin_ema(values: list[float], period: int) -> float:
    if period < 2 or len(values) < period:
        raise ValueError("Insufficient EMA history.")
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
    return current


def _blofin_mtf_execution_signal(
    client, *, desired_direction: str | None = None, inst_id: str = "BTC-USDT",
    now_ms: int | None = None,
) -> dict:
    """Use only completed BloFin 1H and 15m candles; no candle pattern is required."""
    requested_direction = str(desired_direction or "").upper()
    if requested_direction and requested_direction not in {"LONG", "SHORT"}:
        return {"status": "BLOCKED", "reason_fa": "سیگنال جهت‌دار LONG یا SHORT نیست."}
    if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", str(inst_id or "")):
        return {"status": "BLOCKED", "reason_fa": "قرارداد باید یک نماد خطی USDT معتبر باشد."}
    direction = requested_direction
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    ms15, ms1h = 15 * 60 * 1000, 60 * 60 * 1000
    try:
        c15 = _blofin_parse_completed_candles(
            client, inst_id=inst_id, bar="15m", limit=120,
            interval_ms=ms15, minimum_rows=30, now_ms=now,
        )
        c1h = _blofin_parse_completed_candles(
            client, inst_id=inst_id, bar="1H", limit=120,
            interval_ms=ms1h, minimum_rows=55, now_ms=now,
        )
    except (BloFinDemoError, BloFinDemoExecutionBlocked, ValueError) as exc:
        return {"status": "BLOCKED",
                "reason_fa": "تاریخچه معتبر کندل ۱۵ دقیقه‌ای یا یک‌ساعته موجود نیست.",
                "error_type": type(exc).__name__}
    last15, prev15, last1h = c15[-1], c15[-2], c1h[-1]
    if last15["age_ms"] > 2 * ms15:
        return {"status": "BLOCKED", "reason_fa": "کندل ۱۵ دقیقه‌ای قدیمی است."}
    if last1h["age_ms"] > 2 * ms1h:
        return {"status": "BLOCKED", "reason_fa": "کندل یک‌ساعته قدیمی است."}
    close15 = [x["close"] for x in c15]
    close1h = [x["close"] for x in c1h]
    ema9, ema21 = _blofin_ema(close15, 9), _blofin_ema(close15, 21)
    ema20h, ema50h = _blofin_ema(close1h, 20), _blofin_ema(close1h, 50)
    trend = ("LONG" if last1h["close"] > ema20h > ema50h else
             "SHORT" if last1h["close"] < ema20h < ema50h else "NEUTRAL")
    trigger = ("LONG" if ema9 > ema21 and last15["close"] > ema9
                        and last15["close"] > prev15["close"] else
               "SHORT" if ema9 < ema21 and last15["close"] < ema9
                         and last15["close"] < prev15["close"] else "NEUTRAL")
    if not direction:
        direction = trend if trend == trigger and trend in {"LONG", "SHORT"} else ""
    if not direction or trend != direction or trigger != direction:
        return {"status": "BLOCKED",
                "reason_fa": "روند ۱ ساعته و تریگر ۱۵ دقیقه‌ای جهت مشترک ندارند.",
                "requested_direction": requested_direction or "AUTO",
                "candidate_direction": trend if trend == trigger else "NO_TRADE",
                "hourly_direction": trend, "trigger_15m_direction": trigger}
    ranges = []
    for i in range(1, len(c15)):
        x, prev_close = c15[i], c15[i-1]["close"]
        ranges.append(max(x["high"]-x["low"], abs(x["high"]-prev_close),
                          abs(x["low"]-prev_close)))
    atr = sum(ranges[-14:]) / 14.0
    entry = last15["close"]
    if not math.isfinite(atr) or atr <= 0 or entry <= 0:
        return {"status": "BLOCKED", "reason_fa": "ATR یا قیمت ورود معتبر نیست."}
    distance = 2.0 * atr
    stop = entry - distance if direction == "LONG" else entry + distance
    target = entry + 2.0 * distance if direction == "LONG" else entry - 2.0 * distance
    if min(entry, stop, target) <= 0:
        return {"status": "BLOCKED", "reason_fa": "حد ضرر یا حد سود نامعتبر است."}
    open_utc = datetime.fromtimestamp(last15["timestamp_ms"]/1000, tz=timezone.utc).isoformat()
    close_utc = datetime.fromtimestamp(last15["close_time_ms"]/1000, tz=timezone.utc).isoformat()
    return {
        "status": "PASS", "direction": direction,
        "instrument_id": inst_id,
        "signal_id": f"{inst_id}|15m|{last15['timestamp_ms']}",
        "signal_bar_open_ms": last15["timestamp_ms"],
        "signal_bar_open_utc": open_utc, "signal_bar_close_utc": close_utc,
        "entry_price": entry, "stop_loss": stop, "take_profit": target,
        "atr_15m": atr, "ema9_15m": ema9, "ema21_15m": ema21,
        "ema20_1h": ema20h, "ema50_1h": ema50h,
        "hourly_direction": trend, "trigger_15m_direction": trigger,
        "completed_15m_candle_age_seconds": last15["age_ms"]/1000.0,
        "completed_1h_candle_age_seconds": last1h["age_ms"]/1000.0,
        "candlestick_pattern_required": False,
    }


def _blofin_easy_trader_analysis(
    client: BloFinDemoReadOnly, store: BloFinDemoEventStore, asset: AssetSpec, inst_id: str,
) -> AnalysisResult:
    """Build EASY_TRADER's transparent signal from BloFin-only exchange evidence.

    The Easy profile uses seven core checks. Research-only evidence (news,
    whale coverage and walk-forward) remains UNKNOWN/advisory rather than being
    falsely marked PASS. The executor independently repeats every account,
    order, price, margin and risk check before sending anything.
    """
    evidence: dict[str, Any] = {"instrument_id": inst_id, "execution_profile": "EASY"}
    error_types: list[str] = []
    instrument: dict[str, Any] = {}
    ticker: dict[str, Any] = {}
    mark: dict[str, Any] = {}
    account_gate = "UNKNOWN"
    account_reason = "وضعیت حساب و بودجه ریسک قابل تأیید نیست."
    liquidity_gate = "UNKNOWN"
    liquidity_reason = "داده نقدشوندگی کامل نیست."
    instrument_gate = "UNKNOWN"
    instrument_reason = "مشخصات مجاز قرارداد تأیید نشده است."
    mtf = _blofin_mtf_execution_signal(client, desired_direction=None, inst_id=inst_id)
    direction = str(mtf.get("direction") or "").upper() if mtf.get("status") == "PASS" else ""

    def safe_read(label: str, operation):
        try:
            return operation()
        except Exception as exc:
            error_types.append(type(exc).__name__)
            evidence.setdefault("read_errors", []).append(label + ":" + type(exc).__name__)
            return None

    instrument_value = safe_read("instrument", lambda: client.get_tradable_instrument(inst_id))
    if isinstance(instrument_value, dict):
        instrument = instrument_value
        instrument_gate = "PASS"
        instrument_reason = "قرارداد خطی USDT، زنده و دارای مجوز کامل API برای بازکردن موقعیت تأیید شد."
    else:
        instrument_gate = "FAIL"
        instrument_reason = "قرارداد زنده، خطی USDT و مجاز برای بازکردن موقعیت تأیید نشد."

    ticker_rows = safe_read("ticker", lambda: client.get_tickers(inst_id=inst_id))
    if isinstance(ticker_rows, list) and ticker_rows and isinstance(ticker_rows[0], dict):
        ticker = ticker_rows[0]
    mark_rows = safe_read("mark_price", lambda: client.get_mark_price(inst_id=inst_id))
    if isinstance(mark_rows, list) and mark_rows and isinstance(mark_rows[0], dict):
        mark = mark_rows[0]

    now_ms = int(time.time() * 1000)
    ticker_last = None
    mark_price = None
    spread_pct = None
    quote_volume_usdt = None
    ticker_age_seconds = None
    mark_deviation_pct = None
    try:
        ticker_last = float(ticker.get("last"))
        bid = float(ticker.get("bidPrice"))
        ask = float(ticker.get("askPrice"))
        turnover_base = float(ticker.get("volCurrency24h"))
        ticker_ts = int(ticker.get("ts"))
        mid = (ask + bid) / 2.0
        if min(ticker_last, bid, ask, turnover_base, mid) <= 0 or ask < bid:
            raise ValueError("invalid ticker values")
        spread_pct = (ask - bid) / mid * 100.0
        quote_volume_usdt = ticker_last * turnover_base
        ticker_age_seconds = max(0.0, (now_ms - ticker_ts) / 1000.0)
        mark_price = float(mark.get("markPrice"))
        if mark_price <= 0:
            raise ValueError("invalid mark price")
        mark_deviation_pct = abs(mark_price - ticker_last) / ticker_last * 100.0
        if ticker_age_seconds <= 60.0 and spread_pct <= 0.30 and quote_volume_usdt >= 250_000.0 and mark_deviation_pct <= 0.50:
            liquidity_gate = "PASS"
            liquidity_reason = "قیمت، مارک‌پرایس، تازگی، اسپرد و گردش ۲۴ ساعته قرارداد در محدوده مجازند."
        else:
            liquidity_gate = "FAIL"
            liquidity_reason = "اسپرد، حجم ۲۴ ساعته، تازگی قیمت یا اختلاف مارک‌پرایس از حد مجاز عبور کرده است."
    except (TypeError, ValueError, OverflowError, AttributeError):
        liquidity_gate = "UNKNOWN"
        liquidity_reason = "تیکر، بهترین Bid/Ask، گردش ۲۴ ساعته یا Mark Price قابل اعتبارسنجی نیست."

    try:
        balance = client.get_futures_balance(product_type="USDT-FUTURES")
        equity = _equity_from_balance(balance)
        guard = store.equity_guard(equity)
        positions = client.get_positions()
        open_orders = client.get_open_orders()
        budget = store.trade_budget_status()
        active_positions = _active_position_count(positions)
        active_orders = _active_order_count(open_orders)
        evidence.update({
            "daily_loss_pct": round(float(guard.get("daily_loss_pct", 1.0)) * 100.0, 4),
            "drawdown_pct": round(float(guard.get("drawdown_pct", 1.0)) * 100.0, 4),
            "active_positions": active_positions,
            "pending_orders": active_orders,
            "unresolved_orders": int(budget.get("unresolved_orders", 1)),
        })
        account_pass = (
            float(guard.get("daily_loss_pct", 1.0)) < 0.01
            and float(guard.get("drawdown_pct", 1.0)) < 0.05
            and active_positions == 0 and active_orders == 0
            and int(budget.get("unresolved_orders", 1)) == 0
        )
        account_gate = "PASS" if account_pass else "FAIL"
        account_reason = (
            "حساب Demo معتبر است؛ سقف زیان، افت سرمایه، پوزیشن باز، سفارش معلق و سفارش حل‌نشده بررسی شدند."
            if account_pass else
            "حساب/ریسک مجوز ورود نمی‌دهد: پوزیشن یا سفارش باز، سفارش حل‌نشده یا حد زیان/افت سرمایه فعال است."
        )
    except Exception as exc:
        error_types.append(type(exc).__name__)
        account_gate = "UNKNOWN"
        account_reason = "داده حساب یا دفترکل پایدار برای کنترل ریسک قابل تأیید نیست."

    mtf_ok = mtf.get("status") == "PASS"
    data_status = "PASS" if mtf_ok and ticker_age_seconds is not None and ticker_age_seconds <= 60.0 else "UNKNOWN"
    regime_status = "PASS" if mtf_ok and mtf.get("hourly_direction") in {"LONG", "SHORT"} else "UNKNOWN"
    trend_status = "PASS" if mtf_ok and direction in {"LONG", "SHORT"} else "FAIL"
    momentum_status = "PASS" if mtf_ok and mtf.get("trigger_15m_direction") == direction else "FAIL"
    if data_status != "PASS":
        data_reason = mtf.get("reason_fa") or "کندل تکمیل‌شده و تیکر تازه تأیید نشدند."
    else:
        data_reason = "کندل‌های ۱۵ دقیقه و یک‌ساعت تکمیل‌شده و تازه از BloFin دریافت شدند."
    if not mtf_ok:
        trend_reason = mtf.get("reason_fa") or "تطبیق روند و تریگر تایم‌فریمی تأیید نشد."
        momentum_reason = trend_reason
    else:
        trend_reason = "جهت روند ۱ ساعته با سیگنال ۱۵ دقیقه‌ای هم‌جهت است."
        momentum_reason = "EMA9/EMA21 و بسته‌شدن کندل ۱۵ دقیقه‌ای مومنتوم هم‌جهت را تأیید می‌کنند."

    gates = [
        GateResult(1, "Data integrity and freshness", "اعتبار و تازگی داده", GateStatus(data_status), data_reason,
                   "فقط کندل‌های کامل و تیکر تازه BloFin استفاده می‌شوند.",
                   {"ticker_age_seconds": ticker_age_seconds, "mark_deviation_pct": mark_deviation_pct}),
        GateResult(2, "Market regime", "رژیم بازار", GateStatus(regime_status),
                   "روند ۱ ساعته جهت‌دار است." if regime_status == "PASS" else mtf.get("reason_fa", "رژیم نامشخص است."),
                   "رژیم خنثی به‌تنهایی سیگنال ورود نیست.",
                   {"hourly_direction": mtf.get("hourly_direction")}),
        GateResult(3, "Trend alignment", "هم‌جهتی روند", GateStatus(trend_status), trend_reason,
                   "تأیید جهت با روند ۱ ساعته و کندل ۱۵ دقیقه‌ای؛ الگوی کندلی شاخص شرط اجباری نیست.",
                   {"direction": direction or "NO_TRADE", "ema20_1h": mtf.get("ema20_1h"),
                    "ema50_1h": mtf.get("ema50_1h")}),
        GateResult(4, "Momentum confirmation", "تأیید مومنتوم", GateStatus(momentum_status), momentum_reason,
                   "الگوی کندلی خاص لازم نیست؛ EMA و ساختار بسته‌شدن کندل بررسی می‌شوند.",
                   {"trigger_15m_direction": mtf.get("trigger_15m_direction"),
                    "ema9_15m": mtf.get("ema9_15m"), "ema21_15m": mtf.get("ema21_15m")}),
        GateResult(5, "Market liquidity and spread", "نقدشوندگی و اسپرد", GateStatus(liquidity_gate),
                   liquidity_reason, "گردش ۲۴ ساعته، بهترین Bid/Ask، اسپرد و Mark Price کنترل می‌شوند.",
                   {"spread_pct": spread_pct, "quote_volume_24h_usdt": quote_volume_usdt,
                    "mark_deviation_pct": mark_deviation_pct}),
        GateResult(6, "Tradable contract permission", "مجوز قرارداد", GateStatus(instrument_gate),
                   instrument_reason, "فقط قرارداد زنده Linear-USDT با apiPermission=full پذیرفته می‌شود.",
                   {"contract_type": instrument.get("contractType"), "settle_currency": instrument.get("settleCurrency"),
                    "api_permission": instrument.get("apiPermission"), "inst_type": instrument.get("instType")}),
        GateResult(7, "News and sentiment", "خبر و احساسات", GateStatus.UNKNOWN,
                   "در پروفایل EASY این ورودی برای صدور جهت لازم نیست؛ داده غایب به‌عنوان PASS ثبت نمی‌شود.",
                   "خبر/احساسات تکمیلی است و به‌عنوان هشدار نگه‌داری می‌شود.", {}),
        GateResult(8, "Out-of-sample validation", "اعتبارسنجی خارج از نمونه", GateStatus.UNKNOWN,
                   "Walk-forward کامل در چرخه سبک BloFin اجرا نشده است.",
                   "این گیت در Standard/Strict اجباری می‌ماند.", {}),
        GateResult(9, "Account and portfolio risk", "ریسک حساب و پرتفوی", GateStatus(account_gate),
                   account_reason, "موجودی، زیان روزانه، افت سرمایه، پوزیشن‌ها و سفارش‌های معلق تأیید می‌شوند.",
                   {k: evidence[k] for k in ("daily_loss_pct", "drawdown_pct", "active_positions", "pending_orders", "unresolved_orders") if k in evidence}),
        GateResult(10, "Whale intelligence", "هوش نهنگ‌ها", GateStatus.UNKNOWN,
                   "پوشش نهنگ برای این چرخه سبک از APIهای مستقل فراخوانی نشد.",
                   "این داده تکمیلی است و نبود آن به‌عنوان تأیید مثبت ثبت نمی‌شود.", {}),
        GateResult(11, "Full market sentiment coverage", "پوشش احساسات بازار", GateStatus.UNKNOWN,
                   "پوشش کامل News/Social/Fear & Greed در چرخه سبک اجرا نشده است.",
                   "در صورت انتخاب Standard/Strict، این ورودی بررسی می‌شود.", {}),
        GateResult(12, "Walk-forward and historical analog risk", "اعتبارسنجی زمانی و ریسک تاریخی", GateStatus.UNKNOWN,
                   "این ارزیابی آماری در مسیر سبک اجرای BloFin اجرا نشده است.",
                   "گیت‌های پژوهشی کامل همچنان در پروفایل Strict اجباری هستند.", {}),
    ]
    if mtf_ok:
        entry = float(mtf["entry_price"])
        atr = float(mtf["atr_15m"])
        atr_pct = atr / entry * 100.0
        spread_strength = abs(float(mtf["ema9_15m"]) - float(mtf["ema21_15m"])) / entry * 10_000.0
        trend_strength = abs(float(mtf["ema20_1h"]) - float(mtf["ema50_1h"])) / entry * 10_000.0
        confirmation = min(95.0, 65.0 + min(12.0, spread_strength * 2.0) + min(8.0, trend_strength * 0.5) + 5.0)
        volatility_risk = min(44.0, max(10.0, 15.0 + atr_pct * 20.0))
        signal_side = Signal(direction)
        signal_levels = (entry, float(mtf["take_profit"]), float(mtf["stop_loss"]))
        pass_count = sum(g.status == GateStatus.PASS for g in gates)
        blockers = [g.name_fa for g in gates if g.gate_id in {1, 2, 3, 4, 5, 6, 9} and g.status != GateStatus.PASS]
        allowed = not blockers and pass_count >= 7
        if not allowed:
            signal_side = Signal.NO_TRADE
            signal_levels = (None, None, None)
        metrics = {
            "execution_profile": "EASY",
            "execution_profile_label": "EASY_TRADER",
            "execution_timeframe": "15m",
            "trend_confirmation_timeframe": "1H",
            "blofin_mtf_execution": mtf,
            "quote_volume_24h_usdt": quote_volume_usdt,
            "spread_pct": spread_pct,
            "mark_deviation_pct": mark_deviation_pct,
            "risk_score_semantics": "Volatility-based preliminary proxy from 15m ATR only; exchange/account limits are checked independently by the Demo executor.",
            "confirmation_score_semantics": "Deterministic MTF evidence score, not a probability of profit.",
            "decision_blockers": blockers,
        }
        return AnalysisResult(
            asset=asset.raw, signal=signal_side,
            safety_status=SafetyStatus.LOW_RISK if volatility_risk < 35 else SafetyStatus.UNKNOWN,
            confirmation_score=confirmation, estimated_risk_score=volatility_risk,
            opportunity_rank=None, entry_price=signal_levels[0],
            take_profit=signal_levels[1], stop_loss=signal_levels[2],
            expected_holding_days=1, position_notional_usd=None,
            risk_budget_usd=None, gates=gates,
            summary_en="EASY profile: completed BloFin 1H trend and 15m trigger aligned; no candle-pattern label is required.",
            summary_fa=(
                "موتور Challenge 1: روند یک‌ساعته و تریگر ۱۵ دقیقه‌ای BloFin هم‌جهت‌اند؛ الگوی کندلی خاص شرط ورود نیست."
                if allowed else
                "موتور Challenge 1: سیگنال یا یکی از کنترل‌های ضروری BloFin تأیید نشد؛ علت در گیت‌ها مشخص است."
            ),
            metrics=metrics,
        )
    # Keep all actual observations and blockers visible even when no signal exists.
    unknown = volatility_risk = 50.0
    blockers = [mtf.get("reason_fa") or "سیگنال چندتایم‌فریمی در دسترس نیست."]
    metrics = {
        "execution_profile": "EASY",
        "execution_profile_label": "EASY_TRADER",
        "execution_timeframe": "15m",
        "trend_confirmation_timeframe": "1H",
        "blofin_mtf_execution": mtf,
        "risk_score_semantics": "Not calculated because a valid directional MTF setup was unavailable.",
        "decision_blockers": blockers,
    }
    if instrument_gate != "PASS":
        blockers.append(instrument_reason)
    if liquidity_gate != "PASS":
        blockers.append(liquidity_reason)
    if account_gate != "PASS":
        blockers.append(account_reason)
    return AnalysisResult(
        asset=asset.raw, signal=Signal.NO_TRADE, safety_status=SafetyStatus.UNKNOWN,
        confirmation_score=None, estimated_risk_score=unknown, opportunity_rank=None,
        entry_price=None, take_profit=None, stop_loss=None, expected_holding_days=None,
        position_notional_usd=None, risk_budget_usd=None, gates=gates,
        summary_en="EASY profile blocked: one or more required BloFin market or account checks are not confirmed.",
        summary_fa="موتور Challenge 1 متوقف شد: دست‌کم یکی از کنترل‌های ضروری بازار، قرارداد یا حساب BloFin تأیید نشد.",
        metrics=metrics,
    )


def _blofin_demo_instrument_options(*, force: bool = False) -> dict:
    """Return live USDT-margined linear SWAP choices from BloFin's public catalog."""
    now = time.monotonic()
    with BLOFIN_STATUS_LOCK:
        cached = BLOFIN_INSTRUMENT_CACHE.get("payload")
        if not force and cached is not None and BLOFIN_INSTRUMENT_CACHE.get("expires_at", 0.0) > now:
            return copy.deepcopy(cached)
    try:
        client = BloFinDemoReadOnly(timeout=8.0)
        rows = client.get_futures_instruments()
        if not isinstance(rows, list):
            raise BloFinDemoResponseError("BloFin instrument catalog is not a list.")
        options = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (
                row.get("state") != "live"
                or row.get("instType") != "SWAP"
                or row.get("contractType") != "linear"
                or row.get("settleCurrency") != "USDT"
                or row.get("apiPermission") != "full"
            ):
                continue
            inst_id = str(row.get("instId") or "").upper()
            if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", inst_id):
                continue
            try:
                min_size = float(row.get("minSize"))
                lot_size = float(row.get("lotSize"))
                contract_value = float(row.get("contractValue"))
                tick_size = float(row.get("tickSize"))
            except (TypeError, ValueError, OverflowError):
                continue
            if min(min_size, lot_size, contract_value, tick_size) <= 0:
                continue
            options.append({
                "inst_id": inst_id,
                "base_currency": str(row.get("baseCurrency") or inst_id.split("-", 1)[0]),
                "quote_currency": "USDT",
                "api_permission": str(row.get("apiPermission") or "UNKNOWN"),
                "min_size": min_size,
                "lot_size": lot_size,
                "contract_value": contract_value,
                "tick_size": tick_size,
            })
        options.sort(key=lambda item: (item["inst_id"] != "BTC-USDT", item["inst_id"]))
        result = {
            "status": "PASS" if options else "UNKNOWN",
            "market": "BloFin Demo USDT Perpetual (linear SWAP)",
            "instruments": options[:300],
            "count": len(options),
        }
    except Exception as exc:
        result = {
            "status": "UNKNOWN", "market": "BloFin Demo USDT Perpetual (linear SWAP)",
            "instruments": [], "count": 0, "error_type": type(exc).__name__,
            "message_fa": "فهرست زنده قراردادهای BloFin دریافت نشد؛ انتخاب بازار تا بازیابی فهرست متوقف است.",
        }
    with BLOFIN_STATUS_LOCK:
        BLOFIN_INSTRUMENT_CACHE.update({"expires_at": now + 60.0, "payload": result})
    return copy.deepcopy(result)



def _blofin_demo_analysis_storage_ready(readiness: dict) -> bool:
    """Analysis may proceed without Trade permission, but only with readable durable storage."""
    return bool(
        isinstance(readiness, dict)
        and readiness.get("durable_storage") is True
        and readiness.get("event_store_readable") is True
    )



def _blofin_v2_aggregate_exposure_preview(
    client: BloFinDemoReadOnly, *, proposed_plan: dict, equity_usdt: float,
    policy: BloFinDemoRiskPolicy,
) -> dict[str, Any]:
    """Read-only aggregate margin/risk gate for at most four Demo positions.

    Existing positions without an independently readable margin and attached
    stop trigger are UNKNOWN and block the preview. Pending entry orders also
    block because their reserved exposure cannot be safely reconstructed here.
    """
    positions = client.get_positions()
    open_orders = client.get_open_orders()
    if not isinstance(positions, list) or not isinstance(open_orders, list):
        raise BloFinDemoExecutionBlocked("Demo positions/open-orders response is incomplete.")
    if open_orders:
        raise BloFinDemoExecutionBlocked("Pending Demo entry orders exist; aggregate exposure is unresolved.")
    active = []
    for row in positions:
        if not isinstance(row, dict):
            raise BloFinDemoExecutionBlocked("Demo position row is malformed.")
        raw_size = row.get("positions")
        try:
            size = abs(Decimal(str(raw_size)))
        except Exception as exc:
            raise BloFinDemoExecutionBlocked("Demo position size is missing or invalid.") from exc
        if not size.is_finite():
            raise BloFinDemoExecutionBlocked("Demo position size is not finite.")
        if size > 0:
            active.append((row, size))
    if len(active) + 1 > policy.max_concurrent_positions:
        raise BloFinDemoExecutionBlocked("Four-position Demo concurrency cap would be exceeded.")

    aggregate_margin = Decimal("0")
    aggregate_risk = Decimal("0")
    for position, size in active:
        inst_id = str(position.get("instId") or "").strip().upper()
        position_side = str(position.get("positionSide") or "").strip().lower()
        if not inst_id or not position_side:
            raise BloFinDemoExecutionBlocked("Existing Demo position identity is incomplete.")
        try:
            margin = Decimal(str(position.get("margin")))
            average_price = Decimal(str(position.get("averagePrice")))
        except Exception as exc:
            raise BloFinDemoExecutionBlocked("Existing Demo margin/averagePrice is unavailable.") from exc
        if not margin.is_finite() or margin < 0 or not average_price.is_finite() or average_price <= 0:
            raise BloFinDemoExecutionBlocked("Existing Demo margin/averagePrice is invalid.")
        tpsl = client.get_open_tpsl_orders(inst_id=inst_id, limit=100)
        if not isinstance(tpsl, list):
            raise BloFinDemoExecutionBlocked("Existing Demo TP/SL orders cannot be verified.")
        matching_stops = []
        for item in tpsl:
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("instId") or "").upper() == inst_id
                and str(item.get("positionSide") or "").strip().lower() == position_side
            ):
                raw_stop = item.get("slTriggerPrice")
                try:
                    stop_price = Decimal(str(raw_stop))
                except Exception:
                    continue
                if stop_price.is_finite() and stop_price > 0:
                    matching_stops.append(stop_price)
        if not matching_stops:
            raise BloFinDemoExecutionBlocked("An existing Demo position has no independently verified stop-loss trigger.")
        instrument = client.get_tradable_instrument(inst_id)
        if not isinstance(instrument, dict) or instrument.get("instId") != inst_id:
            raise BloFinDemoExecutionBlocked("Existing Demo position contract metadata is unavailable.")
        try:
            contract_value = Decimal(str(instrument.get("contractValue")))
        except Exception as exc:
            raise BloFinDemoExecutionBlocked("Existing Demo contractValue is invalid.") from exc
        if not contract_value.is_finite() or contract_value <= 0:
            raise BloFinDemoExecutionBlocked("Existing Demo contractValue is invalid.")
        stop_price = matching_stops[0]
        aggregate_margin += margin
        aggregate_risk += abs(average_price - stop_price) * size * contract_value

    proposed_margin = Decimal(str(proposed_plan.get("reserved_margin_usdt")))
    proposed_risk = Decimal(str(proposed_plan.get("reserved_risk_usdt")))
    margin_limit = Decimal(str(proposed_plan.get("aggregate_margin_limit_usdt")))
    risk_limit = Decimal(str(proposed_plan.get("aggregate_risk_limit_usdt")))
    if any(not value.is_finite() or value < 0 for value in (
        proposed_margin, proposed_risk, margin_limit, risk_limit,
    )):
        raise BloFinDemoExecutionBlocked("Proposed aggregate margin/risk values are invalid.")
    total_margin = aggregate_margin + proposed_margin
    total_risk = aggregate_risk + proposed_risk
    if total_margin > margin_limit:
        raise BloFinDemoExecutionBlocked("Aggregate Demo margin would exceed the 10% policy ceiling.")
    if total_risk > risk_limit:
        raise BloFinDemoExecutionBlocked("Aggregate Demo risk would exceed the 0.5% policy ceiling.")
    return {
        "status": "PASS",
        "active_positions": len(active),
        "max_concurrent_positions": policy.max_concurrent_positions,
        "existing_margin_usdt": str(aggregate_margin),
        "proposed_margin_usdt": str(proposed_margin),
        "aggregate_margin_after_plan_usdt": str(total_margin),
        "aggregate_margin_limit_usdt": str(margin_limit),
        "existing_stop_risk_usdt": str(aggregate_risk),
        "proposed_stop_risk_usdt": str(proposed_risk),
        "aggregate_risk_after_plan_usdt": str(total_risk),
        "aggregate_risk_limit_usdt": str(risk_limit),
        "orders_submitted": False,
    }


def _run_blofin_demo_v2_readonly_job(
    job_id: str, instrument_id: str, selection_context: dict | None = None,
) -> None:
    """Run isolated V2 ingestion, dual review and order-plan preview; never write orders."""
    store = None
    try:
        instrument_id = str(instrument_id or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", instrument_id):
            raise BloFinDemoExecutionBlocked("Invalid V2 USDT perpetual instrument.")
        if os.getenv("BLOFIN_CHALLENGE1_V2_ORCHESTRATION_ENABLED", "").strip().lower() != "true":
            raise BloFinDemoExecutionBlocked("V2_ORCHESTRATION_FEATURE_FLAG_DISABLED")
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            raise BloFinDemoExecutionBlocked("V2 durable Turso storage is not configured.")
        _set_blofin_execution_job(
            job_id, "running", stage="V2_READ_ONLY_INGESTION",
            message_fa="موتور V2 فقط‌خواندنی در حال دریافت کندل‌های بسته‌شدهٔ 1H/4H/15m، BTC 15m و Funding است.",
            trade_context={"instrument_id": instrument_id, "execution_profile": "CHALLENGE_1_V2"},
        )
        store = BloFinDemoEventStore()
        budget = store.trade_budget_status()
        if not isinstance(budget, dict) or budget.get("unresolved_orders") is None:
            raise BloFinDemoExecutionBlocked("V2 event-store unresolved-order state is unavailable.")
        if int(budget.get("unresolved_orders") or 0) > 0:
            raise BloFinDemoExecutionBlocked("Unresolved Demo orders block a new V2 dry-run.")
        client = BloFinDemoReadOnly(timeout=10.0)
        record = analyze_challenge1_v2_market(client, instrument_id)
        record["execution_profile"] = "CHALLENGE_1_V2"
        record["orders_submitted"] = False
        if record.get("signal") not in {"LONG", "SHORT"}:
            _set_blofin_execution_job(
                job_id, "completed", stage="V2_NO_TRADE",
                message_fa="داده‌های V2 بررسی شدند؛ شرایط ورود معتبر نیست و هیچ سفارش یا Reviewer اجرایی ارسال نشد.",
                analysis=record,
                result={
                    "status": "NO_TRADE", "environment": "BLOFIN_DEMO",
                    "execution_profile": "CHALLENGE_1_V2", "signal": "NO_TRADE",
                    "reason": record.get("reason"), "orders_submitted": False,
                    "trade_executed": False,
                },
            )
            return

        ready, evidence_reason = challenge1_v2_execution_evidence_ready(record)
        if not ready:
            raise BloFinDemoExecutionBlocked("V2 pre-reviewer evidence gate: " + evidence_reason)
        asset = AssetSpec.parse("perp:" + instrument_id.replace("-", ""))
        analysis = SimpleNamespace(
            signal=Signal(str(record["signal"])),
            asset=asset.raw,
            metrics={
                "execution_profile": "CHALLENGE_1_V2",
                "challenge1_v2_evidence": record.get("challenge1_v2_evidence"),
                "market_data_proof": record.get("market_data_proof"),
            },
            entry_price=record.get("entry"),
            stop_loss=record.get("stop_loss"),
            take_profit=record.get("take_profit"),
            confirmation_score=record.get("confidence_score"),
            estimated_risk_score=None,
            to_record=lambda: record,
        )
        _set_blofin_execution_job(
            job_id, "running", stage="V2_DUAL_REVIEW",
            message_fa="شواهد V2 تأیید شد؛ دو ناظر مستقل فقط همین signal_id و شواهد 1H/4H/15m را بررسی می‌کنند.",
            analysis=record,
        )
        if isinstance(record.get("ai_consensus"), dict) and isinstance(record.get("ai_reviews"), list):
            consensus = record["ai_consensus"]
            reviewers = {
                "approved": consensus.get("two_reviewer_approval") is True,
                "status": consensus.get("status") or "UNKNOWN",
                "ai_review": record.get("ai_review"),
                "second_ai_review": record.get("second_ai_review"),
                "ai_reviews": record.get("ai_reviews"),
                "ai_consensus": consensus,
                "reason_fa": record.get("ai_review_reason"),
            }
        else:
            reviewers = _run_challenge1_supervisor_reviews(
                analysis, asset, signal_id=str(record.get("signal_id") or ""),
            )
        record.update({
            "ai_review": reviewers.get("ai_review"),
            "second_ai_review": reviewers.get("second_ai_review"),
            "ai_reviews": reviewers.get("ai_reviews") or [],
            "ai_consensus": reviewers.get("ai_consensus") or {},
            "orders_submitted": False,
        })
        if reviewers.get("approved") is not True:
            _set_blofin_execution_job(
                job_id, "completed", stage="V2_REVIEW_BLOCKED",
                message_fa=reviewers.get("reason_fa") or "دو تأیید مستقل و معتبر دریافت نشد؛ اجرای V2 مسدود است.",
                analysis=record,
                ai_reviews=record["ai_reviews"], ai_consensus=record["ai_consensus"],
                result={
                    "status": "BLOCKED", "environment": "BLOFIN_DEMO",
                    "execution_profile": "CHALLENGE_1_V2", "signal": "NO_TRADE",
                    "reason_fa": reviewers.get("reason_fa"), "orders_submitted": False,
                    "trade_executed": False,
                },
            )
            return
        quorum_ready, quorum_reason = _challenge1_two_reviewer_approval_ready(record)
        if not quorum_ready:
            raise BloFinDemoExecutionBlocked("V2 two-reviewer quorum gate: " + quorum_reason)

        _set_blofin_execution_job(
            job_id, "running", stage="V2_READ_ONLY_ACCOUNT_RISK",
            message_fa="در حال خواندن موجودی، موقعیت‌ها، سفارش‌های باز و حفاظت TP/SL برای کنترل ریسک تجمیعی؛ هیچ سفارش ارسال نمی‌شود.",
        )
        balance = client.get_futures_balance(product_type="USDT-FUTURES")
        equity = _equity_from_balance(balance)
        if equity is None or not math.isfinite(float(equity)) or float(equity) <= 0:
            raise BloFinDemoExecutionBlocked("Fresh positive Demo equity is unavailable.")
        policy = BloFinDemoRiskPolicy(
            max_concurrent_positions=4,
            max_margin_usage_pct=0.10,
            max_aggregate_risk_pct=0.005,
            risk_per_trade_pct=0.0025,
            max_leverage=1,
        )
        quote = get_fresh_execution_quote(client, instrument_id)
        record["market_quote_after_review"] = quote
        contract = record.get("contract_specs")
        if not isinstance(contract, dict):
            raise BloFinDemoExecutionBlocked("Exact V2 contract specifications are missing.")
        plan = build_demo_order_plan(
            record, contract, inst_id=instrument_id, equity_usdt=float(equity),
            leverage=1, policy=policy,
            market_quote={"bidPrice": quote["bidPrice"], "askPrice": quote["askPrice"]},
        )
        aggregate = _blofin_v2_aggregate_exposure_preview(
            client, proposed_plan=plan, equity_usdt=float(equity), policy=policy,
        )
        if aggregate.get("status") != "PASS":
            raise BloFinDemoExecutionBlocked("V2 aggregate margin/risk gate did not pass.")
        experience_id = store.record_challenge1_v2_decision(
            instrument_id=instrument_id, challenge_record=record,
        )
        record["learning_experience_id"] = experience_id
        record["orders_submitted"] = False

        # V2 remains read-only unless all independent, explicit Demo-only switches agree.
        write_enabled = (
            os.getenv("CHALLENGE_1_V2_SINGLE_SHOT_WRITE", "").strip().lower() == "true"
        )
        if write_enabled:
            single_shot_enabled = (
                os.getenv("SINGLE_SHOT_DEMO_TEST", "").strip().lower() == "true"
            )
            demo_trading_enabled = (
                os.getenv("BLOFIN_DEMO_TRADING_ENABLED", "").strip().lower() == "true"
            )
            autopilot_env = os.getenv("BLOFIN_DEMO_AUTOPILOT_ENABLED", "").strip().lower()
            if not single_shot_enabled:
                raise BloFinDemoExecutionBlocked(
                    "V2 single-shot Order Write requires SINGLE_SHOT_DEMO_TEST=true."
                )
            if not demo_trading_enabled:
                raise BloFinDemoExecutionBlocked(
                    "V2 single-shot Order Write requires BLOFIN_DEMO_TRADING_ENABLED=true."
                )
            if autopilot_env != "false" or _blofin_demo_autopilot_enabled():
                raise BloFinDemoExecutionBlocked(
                    "V2 single-shot Order Write requires AutoPilot explicitly set to false."
                )
            lock_status = store.single_shot_lock_status()
            if lock_status.get("locked"):
                raise BloFinDemoExecutionBlocked(
                    "The durable Single-Shot lifetime allowance has already been consumed; manual reset is required."
                )

            write_policy = BloFinDemoRiskPolicy(
                max_concurrent_positions=1,
                max_margin_usage_pct=0.10,
                max_aggregate_risk_pct=0.005,
                risk_per_trade_pct=0.0025,
                max_leverage=1,
            )
            trade_client = BloFinDemoTradeClient(timeout=10.0)
            executor = BloFinDemoExecutor(
                client=trade_client, store=store, policy=write_policy,
                trading_enabled=True,
            )
            _set_blofin_execution_job(
                job_id, "running", stage="V2_SINGLE_SHOT_EXECUTOR_PREFLIGHT",
                message_fa="همه کلیدهای مستقل Demo فعال‌اند؛ Executor با One-Way، اهرم 1x، حداقل حجم و قفل یک‌بارمصرف پیش‌بررسی می‌کند.",
                analysis=record, ai_reviews=record["ai_reviews"],
                ai_consensus=record["ai_consensus"],
                trade_context={
                    "instrument_id": instrument_id,
                    "execution_profile": "CHALLENGE_1_V2",
                    "single_shot_demo_test": True,
                    "max_lifetime_orders": 1,
                    "autopilot_enabled": False,
                },
            )
            try:
                execution = executor.execute(
                    record, inst_id=instrument_id, leverage=1,
                    allow_demo_orders=True, minimum_size_only=True,
                    single_shot=True,
                    submission_lock=BLOFIN_ORDER_SUBMISSION_LOCK,
                )
            except BloFinDemoExecutionUnknown as exc:
                _set_blofin_execution_job(
                    job_id, "completed", stage="V2_SINGLE_SHOT_PENDING_RECONCILE",
                    message_fa="نتیجه POST مبهم است؛ سهم یک‌بارمصرف مصرف شده و فقط Reconciliation خواندنی مجاز است. هیچ Retry ارسال نمی‌شود.",
                    analysis=record, ai_reviews=record["ai_reviews"],
                    ai_consensus=record["ai_consensus"],
                    result={
                        "status": "PENDING_RECONCILE",
                        "environment": "BLOFIN_DEMO",
                        "execution_profile": "CHALLENGE_1_V2",
                        "error_type": type(exc).__name__,
                        "orders_submitted": True,
                        "submission_outcome": "UNKNOWN",
                        "trade_executed": False,
                        "retry_allowed": False,
                        "single_shot_lock": store.single_shot_lock_status(),
                    },
                )
                return

            execution_status = str(execution.get("status") or "UNKNOWN").upper()
            protection = execution.get("protection") if isinstance(execution.get("protection"), dict) else {}
            critical = execution_status == "CRITICAL_UNPROTECTED"
            filled = execution_status in {"FILLED", "PARTIALLY_FILLED", "CRITICAL_UNPROTECTED"}
            if critical:
                stage = "V2_SINGLE_SHOT_CRITICAL_UNPROTECTED"
            elif execution_status in {"FILLED", "PARTIALLY_FILLED"} and protection.get("verified") is True:
                stage = "V2_SINGLE_SHOT_POST_FILL_VERIFIED"
            else:
                stage = "V2_SINGLE_SHOT_RECONCILIATION_PENDING"
            _set_blofin_execution_job(
                job_id, "completed", stage=stage,
                message_fa=(
                    "سفارش Demo فقط یک‌بار ارسال شد؛ Fill، شناسه پوزیشن و TP/SL پس از Fill از API خواندنی تطبیق داده شدند."
                    if stage == "V2_SINGLE_SHOT_POST_FILL_VERIFIED"
                    else "وضعیت اجرای تک‌معامله Demo نیازمند بررسی است؛ سفارش مجدد یا Retry خودکار انجام نمی‌شود."
                ),
                analysis=record, ai_reviews=record["ai_reviews"],
                ai_consensus=record["ai_consensus"],
                trade_context={
                    "instrument_id": instrument_id,
                    "execution_profile": "CHALLENGE_1_V2",
                    "single_shot_demo_test": True,
                    "max_lifetime_orders": 1,
                    "autopilot_enabled": False,
                    "aggregate_risk_preview": aggregate,
                },
                result={
                    **execution,
                    "status": execution_status,
                    "environment": "BLOFIN_DEMO",
                    "execution_profile": "CHALLENGE_1_V2",
                    "orders_submitted": True,
                    "trade_executed": filled,
                    "post_fill_tp_sl_verified": protection.get("verified") is True,
                    "single_shot_lock": store.single_shot_lock_status(),
                    "retry_allowed": False,
                },
            )
            return

        _set_blofin_execution_job(
            job_id, "completed", stage="V2_DRY_RUN_ORDER_PLAN_READY",
            message_fa="مسیر V2 از دریافت داده تا دو Reviewer و ساخت Order Plan عبور کرد؛ ارسال سفارش عمداً انجام نشد.",
            analysis=record,
            ai_reviews=record["ai_reviews"], ai_consensus=record["ai_consensus"],
            trade_context={
                "instrument_id": instrument_id, "execution_profile": "CHALLENGE_1_V2",
                "market_data_proof": record.get("market_data_proof"),
                "contract_specs": {
                    key: contract.get(key) for key in
                    ("instId", "contractValue", "minSize", "lotSize", "tickSize")
                },
                "aggregate_risk_preview": aggregate,
            },
            result={
                "status": "DRY_RUN_ORDER_PLAN_READY",
                "environment": "BLOFIN_DEMO",
                "execution_profile": "CHALLENGE_1_V2",
                "signal": record.get("signal"),
                "signal_id": record.get("signal_id"),
                "order_plan": plan,
                "aggregate_risk_preview": aggregate,
                "learning_experience_id": experience_id,
                "orders_submitted": False,
                "trade_executed": False,
                "execution_method": "READ_ONLY_DRY_RUN_NO_ORDER_WRITE",
            },
        )
    except Exception as exc:
        reason = (
            str(exc)[:500]
            if isinstance(exc, (BloFinDemoExecutionBlocked, Challenge1V2DataBlocked, ValueError))
            else f"V2 orchestration failed closed ({type(exc).__name__})."
        )
        _set_blofin_execution_job(
            job_id, "completed", stage="V2_BLOCKED",
            message_fa="مسیر V2 به‌دلیل داده، شواهد یا کنترل ریسک نامعتبر متوقف شد: " + reason,
            result={
                "status": "BLOCKED", "environment": "BLOFIN_DEMO",
                "execution_profile": "CHALLENGE_1_V2",
                "reason_fa": reason, "error_type": type(exc).__name__,
                "orders_submitted": False, "trade_executed": False,
            },
        )
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


def _run_blofin_demo_execution_job(
    job_id: str, instrument_id: str = "BTC-USDT",
    execution_profile: str = "CHALLENGE_1",
    selection_context: dict | None = None,
) -> None:
    """Analyze a selected BloFin linear USDT perpetual with the selected robot profile."""
    store = None
    submission_started = False
    learning_experience_id = None
    learning_signal = "NO_TRADE"
    failed_operation = "execution_job_start"
    try:
        instrument_id = str(instrument_id or "").strip().upper()
        execution_profile = str(execution_profile or "").strip().upper()
        if execution_profile == "CHALLENGE_1_V2":
            _run_blofin_demo_v2_readonly_job(job_id, instrument_id, selection_context)
            return
        if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", instrument_id):
            raise BloFinDemoExecutionBlocked("Invalid BloFin USDT perpetual instrument.")
        if execution_profile != "CHALLENGE_1":
            raise BloFinDemoExecutionBlocked("Invalid execution profile.")
        if not isinstance(selection_context, dict):
            with LOCK:
                queued_job = copy.deepcopy(BLOFIN_EXECUTION_JOBS.get(job_id) or {})
            stored_selection = queued_job.get("selection_context")
            selection_context = (
                dict(stored_selection) if isinstance(stored_selection, dict) else {
                    "selection_method": "NOT_PROVIDED",
                    "selection_reason": "No caller supplied asset-selection evidence; the robot must not invent a ranking.",
                }
            )
        else:
            selection_context = dict(selection_context)
        _set_blofin_execution_job(
            job_id, "running", stage="ANALYSIS_STORAGE_CHECK",
            message_fa="در حال بررسی فقط حافظه پایدار؛ مجوز سفارش در مرحله جداگانه بررسی می‌شود.",
        )
        failed_operation = "execution_readiness.preflight"
        readiness = _blofin_demo_execution_readiness()
        if not _blofin_demo_analysis_storage_ready(readiness):
            reason_fa = (
                readiness.get("message_fa")
                or "حافظه پایدار Turso یا دفتر رویداد قابل خواندن نیست؛ تحلیل بدون ثبت پایدار آغاز نمی‌شود."
            )
            _set_blofin_execution_job(
                job_id, "completed", stage="PERSISTENCE_BLOCKED",
                message_fa="تحلیل به‌دلیل آماده نبودن حافظه پایدار شروع نشد: " + str(reason_fa),
                result={
                    "status": "PERSISTENCE_BLOCKED", "environment": "BLOFIN_DEMO",
                    "reason_fa": str(reason_fa),
                    "orders_submitted": False, "trade_executed": False,
                    "analysis_started": False,
                },
            )
            return

        failed_operation = "event_store.initialize"
        store = BloFinDemoEventStore()
        analysis_time_utc = datetime.now(timezone.utc).isoformat()
        _record_challenge1_audit(
            job_id, "ASSET_SELECTED",
            {
                "instrument_id": instrument_id, "execution_profile": execution_profile,
                "selection_context": selection_context,
                "analysis_started_at_utc": analysis_time_utc, "environment": "BLOFIN_DEMO",
            },
            event_key="asset-selected",
            reason=str(selection_context.get("selection_reason") or "Instrument selected for Challenge 1 analysis."),
        )
        pending_entry_block_reason = None
        # Reconcile previously filled positions before evaluating or entering a new market.
        # This prevents a fresh order from overtaking an exchange-close event that has not
        # yet reached the durable learning journal.
        failed_operation = "event_store.orders_pending_learning_outcome"
        pending_learning_orders = store.orders_pending_learning_outcome(limit=50)
        if pending_learning_orders:
            outcome_client = BloFinDemoReadOnly(timeout=8.0)
            for pending_order in pending_learning_orders:
                outcome_instrument = str(pending_order.get("instrument_id") or "").upper()
                if not outcome_instrument:
                    continue
                try:
                    closed_history = outcome_client.get_positions_history(
                        inst_id=outcome_instrument, state="closed", limit=100,
                    )
                    partial_history = outcome_client.get_positions_history(
                        inst_id=outcome_instrument, state="partially_closed", limit=100,
                    )
                    failed_operation = "event_store.record_closed_position_outcome"
                    store.record_closed_position_outcome(
                        instrument_id=outcome_instrument,
                        history_rows=closed_history if isinstance(closed_history, list) else [],
                        partial_rows=partial_history if isinstance(partial_history, list) else [],
                        client_order_id=str(pending_order.get("client_order_id") or ""),
                        position_id=str(pending_order.get("position_id") or ""),
                    )
                except Exception:
                    # Keep the experience pending; the entry guard prevents new exposure.
                    pass
        failed_operation = "event_store.trade_budget_status"
        prior_budget = store.trade_budget_status()
        if prior_budget.get("unresolved_orders", 0) > 0:
            _set_blofin_execution_job(
                job_id, "running", stage="READ_ONLY_ORDER_RECONCILIATION",
                message_fa=(
                    "تمام سفارش‌های unresolved به‌ترتیب قدیمی‌ترین، فقط با درخواست READ بررسی می‌شوند؛ "
                    "هیچ سفارش تازه یا retry ارسال نمی‌شود."
                ),
            )
            reconciler = BloFinDemoExecutor(
                client=BloFinDemoTradeClient(timeout=10.0),
                store=store,
                policy=BloFinDemoRiskPolicy(max_leverage=1),
                trading_enabled=False,
            )
            try:
                failed_operation = "executor.reconcile_unresolved_orders_read_only"
                reconciliation = reconciler.reconcile_unresolved_orders(limit=50)
            except Exception as exc:
                reconciliation = {
                    "status": "UNRESOLVED",
                    "reconciliation": "READ_ONLY",
                    "error_type": type(exc).__name__,
                    "orders_submitted": False,
                }
            refreshed_budget = store.trade_budget_status()
            if (
                reconciliation.get("status") != "RECONCILED"
                or refreshed_budget.get("unresolved_orders", 0) > 0
            ):
                pending_entry_block_reason = (
                    "حداقل یک سفارش یا پوزیشن قبلی هنوز به‌طور کامل و مستقل در BloFin Demo تطبیق نشده است؛ "
                    "ورود جدید همچنان مسدود است."
                )

        profile_rules = {
            "CHALLENGE_1": {"required": set(), "min_pass": 0, "min_score": 0.0},
            "EASY": {"required": {1, 2, 3, 4, 5, 6, 9}, "min_pass": 7, "min_score": 55.0},
            "STANDARD": {"required": {1, 2, 3, 4, 5, 6, 8, 9}, "min_pass": 9, "min_score": 65.0},
            "STRICT": {"required": set(range(1, 13)), "min_pass": 12, "min_score": 75.0},
        }
        profile_rule = profile_rules[execution_profile]
        asset = AssetSpec.parse("perp:" + instrument_id.replace("-", ""))
        read_client = BloFinDemoReadOnly(timeout=10.0)
        if execution_profile == "CHALLENGE_1":
            _set_blofin_execution_job(
                job_id, "running", stage="CHALLENGE_1_PATTERN_ANALYSIS",
                message_fa=f"موتور Challenge 1 در حال بررسی کندل‌های بسته‌شده و الگوهای {instrument_id} است؛ بدون TinyFish و بدون مدل پولی.",
                trade_context={"asset": asset.raw, "instrument_id": instrument_id, "execution_profile": execution_profile},
            )
            try:
                _record_challenge1_audit(
                    job_id, "ANALYSIS_STARTED",
                    {
                        "instrument_id": instrument_id, "execution_profile": execution_profile,
                        "selection_context": selection_context, "timeframe": "5m",
                        "required_input": "CLOSED_CANDLES_ONLY", "environment": "BLOFIN_DEMO",
                    },
                    event_key="analysis-started",
                    reason="The robot is collecting closed 5-minute candles before its own signal decision.",
                )
                failed_operation = "challenge1.analyze_closed_5m_candles"
                challenge_record = analyze_blofin_challenge1(
                    read_client, instrument_id,
                    audit_callback=lambda phase, details, key: _record_challenge1_audit(
                        job_id, phase, details, event_key=key,
                        reason=str(details.get("reason_fa") or phase),
                    ),
                )
                # Persist every closed-candle decision before any execution branch.
                # The stable candle identity deduplicates repeated five-minute polls.
                failed_operation = "learning_memory.record_challenge1_decision"
                learning_experience_id = store.record_challenge1_decision(
                    instrument_id=instrument_id,
                    challenge_record=challenge_record,
                )
                learning_signal = str(challenge_record.get("signal") or "NO_TRADE").strip().upper()
                challenge_record["learning_experience_id"] = learning_experience_id
                _record_challenge1_audit(
                    job_id, "STRATEGY_DECISION",
                    {
                        "instrument_id": instrument_id, "signal": challenge_record.get("signal"),
                        "decision_class": challenge_record.get("decision_class"),
                        "reason_fa": challenge_record.get("reason_fa"),
                        "entry_price": challenge_record.get("entry_price_usd"),
                        "stop_loss": challenge_record.get("stop_loss_usd"),
                        "take_profit": challenge_record.get("take_profit_usd"),
                        "signal_bar_open_utc": challenge_record.get("signal_bar_open_utc"),
                        "challenge1_evidence": challenge_record.get("challenge1_evidence", {}),
                        "execution": challenge_record.get("execution", {}),
                        "selection_context": selection_context,
                    },
                    event_key="strategy-decision", experience_id=learning_experience_id,
                    reason=str(challenge_record.get("reason_fa") or "Challenge 1 strategy decision recorded."),
                )
                failed_operation = "learning_memory.check_final_experience"
                if store.has_final_experience(learning_experience_id):
                    _set_blofin_execution_job(
                        job_id, "completed", stage="DUPLICATE_CANDLE_FINALIZED",
                        message_fa="برای این کندل نتیجهٔ نهایی قبلاً ثبت شده؛ از ورود تکراری جلوگیری شد.",
                        result={
                            "status": "SKIPPED_DUPLICATE_EXPERIENCE",
                            "environment": "BLOFIN_DEMO",
                            "learning_experience_id": learning_experience_id,
                            "orders_submitted": False,
                            "trade_executed": False,
                        },
                    )
                    return
            except ValueError as exc:
                reason_fa = (
                    f"تحلیل Challenge 1 برای {instrument_id} به‌دلیل دادهٔ کندل نامعتبر یا ناکافی متوقف شد: "
                    + str(exc)[:160]
                )
                _record_challenge1_audit(
                    job_id, "ANALYSIS_FAILED",
                    {
                        "instrument_id": instrument_id,
                        "failure_stage": "CHALLENGE_1_MARKET_DATA_ANALYSIS",
                        "error_type": type(exc).__name__, "reason_fa": str(exc)[:240],
                        "selection_context": selection_context,
                    },
                    event_key="analysis-failed", status="WARNING", reason=str(exc)[:1200],
                )
                learning_experience_id = store.record_challenge1_analysis_failure(
                    cycle_id=job_id,
                    instrument_id=instrument_id,
                    failure_stage="CHALLENGE_1_MARKET_DATA_ANALYSIS",
                    error_type=type(exc).__name__,
                    reason=str(exc),
                    timestamp_utc=analysis_time_utc,
                )
                _set_blofin_execution_job(
                    job_id, "completed", stage="DATA_INSUFFICIENT",
                    message_fa=reason_fa,
                    analysis={
                        "instrument_id": instrument_id,
                        "execution_profile": "CHALLENGE_1",
                        "signal": "NO_TRADE",
                        "decision_class": "DATA_INSUFFICIENT",
                        "data_quality": "UNKNOWN",
                        "error_type": type(exc).__name__,
                        "learning_experience_id": learning_experience_id,
                    },
                    trade_context={
                        "instrument_id": instrument_id,
                        "execution_profile": "CHALLENGE_1",
                        "analysis_attempted_at_utc": analysis_time_utc,
                    },
                    result={
                        "status": "DATA_INSUFFICIENT", "environment": "BLOFIN_DEMO",
                        "signal": "NO_TRADE", "decision_class": "DATA_INSUFFICIENT",
                        "data_quality": "UNKNOWN", "reason_fa": reason_fa,
                        "error_type": type(exc).__name__,
                        "learning_experience_id": learning_experience_id,
                        "orders_submitted": False, "trade_executed": False,
                    },
                )
                return
            mtf_signal = challenge_record.get("execution") if isinstance(challenge_record.get("execution"), dict) else {}
            last_candle_time = challenge_record.get("signal_bar_open_utc")
            last_candle_close = challenge_record.get("entry_price_usd")
            gates = [
                {
                    "gate_id": gate_id,
                    "status": "UNKNOWN",
                    "name_fa": f"فیلتر عمومی {gate_id}",
                    "reason_fa": "در موتور Challenge 1 برای صدور سیگنال استفاده نمی‌شود؛ فقط در لایه نظارت جداگانه می‌ماند.",
                    "execution_role": "OBSERVATION_ONLY",
                }
                for gate_id in range(1, 13)
            ]
            challenge_record["gates"] = gates
            metrics = {
                "execution_profile": "CHALLENGE_1",
                "execution_profile_label": "CHALLENGE 1 · CANDLE & CHART PATTERNS",
                "challenge1_evidence": challenge_record.get("challenge1_evidence", {}),
                "legacy_12_gates_used_for_entry": False,
                "external_ai_required": True,
                "tinyfish_required": False,
            }
            analysis = SimpleNamespace(
                asset=asset.raw,
                signal=(
                    Signal.LONG if str(challenge_record.get("signal") or "").upper() == "LONG"
                    else Signal.SHORT if str(challenge_record.get("signal") or "").upper() == "SHORT"
                    else Signal.NO_TRADE
                ),
                metrics=metrics,
                gates=gates,
                entry_price=challenge_record.get("entry_price_usd"),
                stop_loss=challenge_record.get("stop_loss_usd"),
                take_profit=challenge_record.get("take_profit_usd"),
                to_record=lambda: challenge_record,
            )
            if (
                str(challenge_record.get("signal") or "").upper() not in {"LONG", "SHORT"}
                or mtf_signal.get("status") != "PASS"
            ):
                reason_fa = str(challenge_record.get("reason_fa") or "الگوی معتبر Challenge 1 پیدا نشد؛ تصمیم صحیح عدم معامله است.")
                _record_challenge1_audit(
                    job_id, "FINAL_ENTRY_DECISION",
                    {
                        "instrument_id": instrument_id, "decision": "NO_TRADE",
                        "signal": challenge_record.get("signal"), "reason_fa": reason_fa,
                        "mtf_confirmation": mtf_signal,
                        "challenge1_evidence": challenge_record.get("challenge1_evidence", {}),
                        "reviewers": "NOT_CALLED: strategy produced no eligible directional setup",
                    },
                    event_key="final-entry-no-trade", experience_id=learning_experience_id,
                    status="WARNING", reason=reason_fa,
                )
                store.record_experience_not_executed(
                    experience_id=learning_experience_id,
                    instrument_id=instrument_id,
                    signal=learning_signal,
                    stage="CHALLENGE_1_SIGNAL_REJECTED",
                    reason=reason_fa,
                )
                _set_blofin_execution_job(
                    job_id, "completed", stage="NO_TRADE",
                    message_fa=reason_fa,
                    analysis={
                        "instrument_id": instrument_id,
                        "execution_profile": "CHALLENGE_1",
                        "signal": "NO_TRADE",
                        "legacy_12_gates_used_for_entry": False,
                        "challenge1_evidence": challenge_record.get("challenge1_evidence", {}),
                    },
                    trade_context={
                        "asset": asset.raw, "instrument_id": instrument_id,
                        "execution_profile": "CHALLENGE_1",
                        "execution_timeframe": "5m",
                        "higher_timeframes_used": False,
                        "completed_candles_only": True,
                    },
                    result={
                        **challenge_record,
                        "status": "NO_TRADE",
                        "environment": "BLOFIN_DEMO",
                        "orders_submitted": False,
                        "trade_executed": False,
                    },
                )
                return
        elif execution_profile == "EASY":
            _set_blofin_execution_job(
                job_id, "running", stage="LIVE_EASY_MTF_ANALYSIS",
                message_fa=f"مسیر قدیمی EASY برای {instrument_id}؛ این مسیر در اجرای Challenge 1 فعال نیست.",
                trade_context={"asset": asset.raw, "instrument_id": instrument_id, "execution_profile": execution_profile},
            )
            analysis = _blofin_easy_trader_analysis(read_client, store, asset, instrument_id)
            mtf_signal = (analysis.metrics or {}).get("blofin_mtf_execution") or {}
            last_candle_time = mtf_signal.get("signal_bar_open_utc")
            last_candle_close = mtf_signal.get("entry_price")
        else:
            _set_blofin_execution_job(
                job_id, "running", stage="LIVE_12_GATE_ANALYSIS",
                message_fa=f"دریافت داده زنده {instrument_id} و اجرای موتور {execution_profile}.",
            )
            analysis = ENGINE.analyze(asset)
            mtf_signal = {}
            frames = getattr(ENGINE.data, "candle_frames", {})
            frame = frames.get(asset.raw) if isinstance(frames, dict) else None
            last_candle_close = None
            last_candle_time = None
            if frame is not None and not getattr(frame, "empty", True):
                try:
                    last_candle_close = float(frame["close"].iloc[-1])
                    if not math.isfinite(last_candle_close):
                        last_candle_close = None
                except (KeyError, IndexError, TypeError, ValueError, OverflowError):
                    last_candle_close = None
                try:
                    last_candle_time = frame.index[-1].isoformat()
                except (IndexError, AttributeError, TypeError):
                    last_candle_time = None
        analysis.metrics["execution_profile"] = execution_profile
        analysis.metrics["execution_profile_label"] = {
            "CHALLENGE_1": "CHALLENGE 1 · CANDLE & CHART PATTERNS", "EASY": "EASY TRADER", "STANDARD": "STANDARD TRADER", "STRICT": "STRICT TRADER",
        }[execution_profile]
        record = analysis.to_record()
        record.update({
            "execution_profile": execution_profile,
            "instrument_id": instrument_id,
            "inst_id": instrument_id,
        })
        gates = record.get("gates")
        wave_report = _blofin_elliott_wave_report_fa(
            record.get("elliott_wave"), str(record.get("signal") or "NO_TRADE"),
        )
        trade_context = {
            "asset": asset.raw, "instrument_id": instrument_id,
            "execution_profile": execution_profile,
            "analysis_time_utc": analysis_time_utc,
            "last_completed_candle_time_utc": last_candle_time,
            "last_candle_close": last_candle_close,
            "execution_timeframe": "5m",
            "higher_timeframes_used": False,
            "completed_candles_only": True,
        }
        pass_count = sum(
            1 for item in gates
            if isinstance(item, dict) and str(item.get("status", "")).upper() == "PASS"
        ) if isinstance(gates, list) else 0
        fail_count = sum(
            1 for item in gates
            if isinstance(item, dict) and str(item.get("status", "")).upper() == "FAIL"
        ) if isinstance(gates, list) else 0
        unknown_count = sum(
            1 for item in gates
            if isinstance(item, dict) and str(item.get("status", "")).upper() == "UNKNOWN"
        ) if isinstance(gates, list) else 12
        direction = str(record.get("signal") or "").upper()
        gate_summary = {
            "instrument_id": instrument_id, "execution_profile": execution_profile,
            "signal": direction or "UNKNOWN",
            "gate_pass_count": pass_count, "gate_fail_count": fail_count,
            "gate_unknown_count": unknown_count,
            "required_gate_ids": sorted(profile_rule["required"]),
            "min_pass_count": profile_rule["min_pass"],
            "confirmation_score": record.get("confirmation_score"),
            "estimated_risk_score": record.get("estimated_risk_score"),
            "entry_reference_price": record.get("entry_price_usd"),
            "stop_loss": record.get("stop_loss_usd"),
            "take_profit": record.get("take_profit_usd"),
            "signal_fa": {"LONG": "خرید (LONG)", "SHORT": "فروش استقراضی (SHORT)"}.get(direction, "بدون معامله"),
            "gates_fa": _blofin_gate_rows_fa(gates),
        }
        _set_blofin_execution_job(
            job_id, "running", stage="PROFILE_GATE_REVIEW",
            message_fa=(f"ربات Challenge 1 · {instrument_id} · تأیید الگوی کندلی/نموداری؛ ۱۲ فیلتر عمومی فقط نظارتی‌اند." if execution_profile == "CHALLENGE_1" else f"ربات {execution_profile} · {instrument_id} · {pass_count}/12 گیت PASS؛ الگوی کندلی شاخص شرط اجباری نیست."),
            analysis=gate_summary, elliott_wave=wave_report, trade_context=trade_context,
        )
        statuses_by_id = {
            int(item["gate_id"]): str(item.get("status", "")).upper()
            for item in gates if isinstance(item, dict) and isinstance(item.get("gate_id"), int)
        } if isinstance(gates, list) else {}
        required_blockers = sorted(
            gate_id for gate_id in profile_rule["required"]
            if statuses_by_id.get(gate_id) != "PASS"
        )
        if (
            not isinstance(gates, list) or len(gates) != 12
            or set(statuses_by_id) != set(range(1, 13))
            or required_blockers or pass_count < profile_rule["min_pass"]
            or direction not in {"LONG", "SHORT"}
        ):
            reasons = []
            if direction not in {"LONG", "SHORT"}:
                reasons.append((mtf_signal or {}).get("reason_fa") or "سیگنال جهت‌دار LONG/SHORT ساخته نشد.")
            if required_blockers:
                reasons.append("گیت‌های ضروری این پروفایل PASS نیستند: " + ", ".join(map(str, required_blockers)))
            if pass_count < profile_rule["min_pass"]:
                reasons.append(f"حداقل {profile_rule['min_pass']} گیت PASS لازم است؛ {pass_count} مورد PASS شد.")
            if len(gates) != 12 or set(statuses_by_id) != set(range(1, 13)):
                reasons.append("گزارش ۱۲ گیت کامل و یکتا نیست.")
            reason_fa = "؛ ".join(reasons) or "شرایط ورود تکمیل نشد."
            _set_blofin_execution_job(
                job_id, "completed", stage="NO_TRADE",
                message_fa=f"ربات {execution_profile}: ورود مسدود شد؛ " + reason_fa,
                analysis=gate_summary, elliott_wave=wave_report, trade_context=trade_context,
                result={
                    "status": "NO_TRADE", "environment": "BLOFIN_DEMO",
                    **gate_summary, "elliott_wave": wave_report,
                    "trade_context": trade_context, "reason_fa": reason_fa,
                    "orders_submitted": False, "trade_executed": False,
                },
            )
            return

        if execution_profile == "EASY":
            if mtf_signal.get("status") != "PASS" or mtf_signal.get("direction") != direction:
                reason_fa = mtf_signal.get("reason_fa") or "روند ۱ ساعته و تریگر ۱۵ دقیقه‌ای تأیید نشدند."
                _set_blofin_execution_job(
                    job_id, "completed", stage="MTF_TRIGGER_BLOCKED",
                    message_fa=reason_fa, analysis=gate_summary, trade_context=trade_context,
                    result={
                        "status": "NO_TRADE", "environment": "BLOFIN_DEMO",
                        **gate_summary, "challenge1_execution": mtf_signal,
                        "reason_fa": reason_fa, "orders_submitted": False, "trade_executed": False,
                    },
                )
                return
        elif execution_profile == "CHALLENGE_1":
            if mtf_signal.get("status") != "PASS":
                reason_fa = challenge_record.get("reason_fa") or "الگوی معتبر با کندل بسته‌شده تأیید نشد."
                _set_blofin_execution_job(
                    job_id, "completed", stage="NO_TRADE",
                    message_fa=reason_fa, analysis=gate_summary,
                    trade_context={"instrument_id": instrument_id, "execution_profile": "CHALLENGE_1"},
                    result={
                        "status": "NO_TRADE", "environment": "BLOFIN_DEMO",
                        "challenge1_evidence": challenge_record.get("challenge1_evidence", {}),
                        "reason_fa": reason_fa, "orders_submitted": False, "trade_executed": False,
                    },
                )
                return
        else:
            if not last_candle_time:
                _set_blofin_execution_job(
                    job_id, "completed", stage="NO_TRADE",
                    message_fa="کندل روزانه تکمیل‌شده و زمان‌دار برای تحلیل این پروفایل موجود نیست.",
                    analysis=gate_summary, trade_context=trade_context,
                    result={
                        "status": "NO_TRADE", "environment": "BLOFIN_DEMO",
                        "reason_fa": "کندل تحلیلی تکمیل‌شده موجود نیست.",
                        "orders_submitted": False, "trade_executed": False,
                    },
                )
                return
            mtf_signal = _blofin_mtf_execution_signal(
                read_client, desired_direction=direction, inst_id=instrument_id,
            )
            if mtf_signal.get("status") != "PASS":
                reason_fa = mtf_signal.get("reason_fa") or "روند و تریگر چندتایم‌فریمی هم‌سو نیستند."
                _set_blofin_execution_job(
                    job_id, "completed", stage="MTF_TRIGGER_BLOCKED",
                    message_fa=reason_fa, analysis=gate_summary, trade_context=trade_context,
                    result={
                        "status": "NO_TRADE", "environment": "BLOFIN_DEMO",
                        **gate_summary, "mtf_confirmation": mtf_signal,
                        "reason_fa": reason_fa, "orders_submitted": False, "trade_executed": False,
                    },
                )
                return
        if pending_entry_block_reason:
            reason_fa = pending_entry_block_reason
            _record_challenge1_audit(
                job_id, "FINAL_ENTRY_DECISION",
                {
                    "instrument_id": instrument_id,
                    "decision": "BLOCK_UNRESOLVED_PRIOR_ORDER",
                    "reason_fa": reason_fa,
                    "pending_order_reconciliation": prior_budget,
                    "orders_submitted": False,
                },
                event_key="final-entry-blocked-unresolved-order",
                experience_id=learning_experience_id,
                status="WARNING",
                reason=reason_fa,
            )
            if learning_experience_id and not store.has_final_experience(learning_experience_id):
                store.record_experience_not_executed(
                    experience_id=learning_experience_id,
                    instrument_id=instrument_id,
                    signal=learning_signal,
                    stage="UNRESOLVED_ORDER_BLOCK",
                    reason=reason_fa,
                )
            _set_blofin_execution_job(
                job_id, "completed", stage="UNRESOLVED_ORDER_BLOCK",
                message_fa=reason_fa + " تحلیل ثبت شد؛ هیچ سفارش تازه‌ای ارسال نمی‌شود.",
                analysis=gate_summary, trade_context=trade_context,
                result={
                    "status": "BLOCKED", "environment": "BLOFIN_DEMO",
                    "reason_fa": reason_fa,
                    "learning_experience_id": learning_experience_id,
                    "orders_submitted": False, "trade_executed": False,
                },
            )
            return

        signal_bar = mtf_signal["signal_bar_open_utc"]
        record.update({
            "signal_id": mtf_signal["signal_id"],
            "instrument_id": instrument_id, "inst_id": instrument_id,
            "execution_profile": execution_profile,
            "entry_price_usd": mtf_signal["entry_price"],
            "stop_loss_usd": mtf_signal["stop_loss"],
            "take_profit_usd": mtf_signal["take_profit"],
        })
        analysis.entry_price = mtf_signal["entry_price"]
        analysis.stop_loss = mtf_signal["stop_loss"]
        analysis.take_profit = mtf_signal["take_profit"]
        analysis.metrics["execution_profile"] = execution_profile
        analysis.metrics["challenge1_execution"] = {
            key: value for key, value in mtf_signal.items()
            if key not in {"status", "reason_fa"}
        }
        trade_context.update({
            "last_completed_candle_time_utc": signal_bar,
            "challenge1_execution": mtf_signal,
            "execution_timeframe": "5m",
            "higher_timeframes_used": False,
            "completed_candles_only": True,
        })
        gate_summary.update({
            "entry_reference_price": mtf_signal["entry_price"],
            "stop_loss": mtf_signal["stop_loss"],
            "take_profit": mtf_signal["take_profit"],
        })
        record = analysis.to_record()
        record.update({
            "signal_id": mtf_signal["signal_id"],
            "instrument_id": instrument_id, "inst_id": instrument_id,
            "execution_profile": execution_profile,
        })
        if store.has_signal(record["signal_id"]):
            if learning_experience_id and not store.has_final_experience(learning_experience_id):
                store.record_experience_not_executed(
                    experience_id=learning_experience_id,
                    instrument_id=instrument_id,
                    signal=learning_signal,
                    stage="DUPLICATE_SIGNAL_BLOCKED",
                    reason="شناسه سیگنال قبلاً در دفتر سفارش رزرو شده است؛ سفارش تکراری ساخته نشد.",
                )
            _set_blofin_execution_job(
                job_id, "completed", stage="DUPLICATE_CANDLE_SKIPPED",
                message_fa="کندل تکمیل‌شده قبلاً بررسی معاملاتی شده است؛ برای جلوگیری از تکرار، سفارش جدید ساخته نشد.",
                elliott_wave=wave_report,
                trade_context=trade_context,
                result={
                    "status": "SKIPPED_DUPLICATE_SIGNAL", "environment": "BLOFIN_DEMO",
                    "signal_id": record["signal_id"],
                    "elliott_wave": wave_report,
                    "trade_context": trade_context,
                    "reason_fa": "کندل تکمیل‌شده قبلاً بررسی معاملاتی شده است؛ هیچ سفارش تکراری ساخته نشد.",
                    "orders_submitted": False, "trade_executed": False,
                },
            )
            return

        if execution_profile != "CHALLENGE_1":
            raise BloFinDemoExecutionBlocked("Only the Challenge 1 execution profile is supported.")

        _set_blofin_execution_job(
            job_id, "running", stage="AI_SUPERVISOR_REVIEW",
            message_fa="دو ناظر مستقل در حال بررسی هم‌زمان کندل‌ها، ساختار نمودار و موج الیوت ۵ دقیقه‌ای هستند؛ تا اجماع کامل هیچ سفارشی ارسال نمی‌شود.",
            analysis=gate_summary,
            elliott_wave=wave_report,
            trade_context=trade_context,
        )
        _record_challenge1_audit(
            job_id, "AI_REVIEW_REQUESTED",
            {
                "instrument_id": instrument_id,
                "signal": str(getattr(getattr(analysis, "signal", None), "value", getattr(analysis, "signal", "UNKNOWN"))),
                "reviewers": ["nemotron", "second_ai"],
                "review_contract": "Independent reviews run concurrently; neither receives the other's answer.",
                "execution_role": "ENTRY_APPROVAL_GATE",
            },
            event_key="ai-review-requested", experience_id=learning_experience_id,
            reason="Two independent reviewer calls are starting before any Demo order decision.",
        )
        failed_operation = "ai_supervisors.request_and_validate_quorum"
        ai_quorum = _run_challenge1_supervisor_reviews(
            analysis, asset, signal_id=str(record.get("signal_id") or ""),
        )
        _record_challenge1_audit(
            job_id, "AI_REVIEW_RESULT",
            {
                "instrument_id": instrument_id, "ai_reviews": ai_quorum.get("ai_reviews"),
                "ai_consensus": ai_quorum.get("ai_consensus"),
                "ai_status": ai_quorum.get("ai_status"),
                "approved": ai_quorum.get("approved"),
                "reason_fa": ai_quorum.get("reason_fa"),
            },
            event_key="ai-review-result", experience_id=learning_experience_id,
            status="SUCCESS" if ai_quorum.get("approved") else "WARNING",
            reason=str(ai_quorum.get("reason_fa") or "Independent reviewer results recorded."),
        )
        ai_review = ai_quorum["ai_review"]
        ai_reviews = ai_quorum["ai_reviews"]
        ai_consensus = ai_quorum["ai_consensus"]
        ai_status = ai_quorum["ai_status"]
        second_ai_review = ai_quorum["second_ai_review"]

        if not ai_quorum["approved"]:
            reason_fa = str(ai_quorum.get("reason_fa") or "اجماع دو ناظر مستقل تأیید نشد؛ ورود مسدود است.")
            _record_challenge1_audit(
                job_id, "FINAL_ENTRY_DECISION",
                {
                    "instrument_id": instrument_id, "decision": "BLOCK",
                    "reason_fa": reason_fa, "ai_reviews": ai_reviews,
                    "ai_consensus": ai_consensus,
                    "entry_price": record.get("entry_price_usd"),
                    "stop_loss": record.get("stop_loss_usd"),
                    "take_profit": record.get("take_profit_usd"),
                    "risk_policy": "NO_ORDER_WITHOUT_TWO_VERIFIED_INDEPENDENT_APPROVALS",
                },
                event_key="final-entry-blocked-by-reviewers",
                experience_id=learning_experience_id, status="WARNING", reason=reason_fa,
            )
            if learning_experience_id and not store.has_final_experience(learning_experience_id):
                store.record_experience_not_executed(
                    experience_id=learning_experience_id,
                    instrument_id=instrument_id,
                    signal=learning_signal,
                    stage="AI_SUPERVISOR_BLOCKED",
                    reason=reason_fa,
                )
            _set_blofin_execution_job(
                job_id, "completed", stage="AI_SUPERVISOR_BLOCKED",
                message_fa=reason_fa + " هیچ سفارش یا پوزیشنی ایجاد نشد.",
                ai_review=ai_review,
                ai_reviews=ai_reviews,
                ai_consensus=ai_consensus,
                ai_status=ai_status,
                analysis=gate_summary,
                elliott_wave=wave_report,
                trade_context=trade_context,
                result={
                    **challenge_record,
                    "status": "BLOCKED",
                    "environment": "BLOFIN_DEMO",
                    "learning_experience_id": learning_experience_id,
                    "ai_review": ai_review,
                    "ai_reviews": ai_reviews,
                    "ai_consensus": ai_consensus,
                    "ai_status": ai_status,
                    "elliott_wave": challenge_record.get("challenge1_evidence", {}).get("elliott_wave"),
                    "reason_fa": reason_fa,
                    "orders_submitted": False,
                    "trade_executed": False,
                },
                completed_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            _queue_blofin_completed_job_reports(job_id, instrument_id)
            return

        _set_blofin_execution_job(
            job_id, "running", stage="AI_SUPERVISOR_QUORUM_PASSED",
            message_fa="هر دو ناظر مستقل، الگوی ورود و ساختار موج الیوت را تأیید کردند؛ کنترل‌های نهایی ریسک و حساب Demo آغاز می‌شود.",
            ai_review=ai_review,
            ai_reviews=ai_reviews,
            ai_consensus=ai_consensus,
            ai_status=ai_status,
            analysis=gate_summary,
            elliott_wave=wave_report,
            trade_context=trade_context,
        )

        # Pass the independently validated quorum into the Executor. The Executor
        # revalidates it before any exchange read/write, so a direct call cannot
        # bypass the Web App entry gate.
        record.update({
            "ai_review": ai_review,
            "ai_reviews": ai_reviews,
            "ai_consensus": ai_consensus,
            "ai_status": ai_status,
            "second_ai_review": second_ai_review,
        })
        order_readiness = _blofin_demo_execution_readiness()
        if order_readiness.get("status") != "READY":
            reason_fa = (
                order_readiness.get("message_fa")
                or "پیش‌نیازهای اجرای سفارش BloFin Demo تأیید نشد."
            )
            _record_challenge1_audit(
                job_id, "FINAL_ENTRY_DECISION",
                {
                    "instrument_id": instrument_id, "decision": "BLOCK_BY_DEMO_READINESS",
                    "reason_fa": str(reason_fa),
                    "readiness": {
                        key: value for key, value in order_readiness.items()
                        if key not in {"api_key", "secret", "passphrase", "token", "authorization"}
                    },
                    "ai_consensus": ai_consensus,
                },
                event_key="final-entry-blocked-readiness",
                experience_id=learning_experience_id, status="WARNING",
                reason=str(reason_fa),
            )
            if learning_experience_id and not store.has_final_experience(learning_experience_id):
                store.record_experience_not_executed(
                    experience_id=learning_experience_id,
                    instrument_id=instrument_id,
                    signal=learning_signal,
                    stage="TRADE_READINESS_BLOCKED",
                    reason=str(reason_fa),
                )
            _set_blofin_execution_job(
                job_id, "completed", stage="TRADE_READINESS_BLOCKED",
                message_fa=(
                    str(reason_fa)
                    + " هر دو ناظر مستقل اجرا و نتیجه‌شان ثبت شد؛ هیچ سفارشی ارسال نشد."
                ),
                ai_review=ai_review,
                ai_reviews=ai_reviews,
                ai_consensus=ai_consensus,
                ai_status=ai_status,
                analysis=gate_summary,
                elliott_wave=wave_report,
                trade_context=trade_context,
                result={
                    **challenge_record,
                    "status": "BLOCKED",
                    "environment": "BLOFIN_DEMO",
                    "learning_experience_id": learning_experience_id,
                    "ai_review": ai_review,
                    "ai_reviews": ai_reviews,
                    "ai_consensus": ai_consensus,
                    "ai_status": ai_status,
                    "second_ai_review": second_ai_review,
                    "reason_fa": str(reason_fa),
                    "orders_submitted": False,
                    "trade_executed": False,
                },
                completed_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            _queue_blofin_completed_job_reports(job_id, instrument_id)
            return

        client = BloFinDemoTradeClient(timeout=10.0)
        policy = BloFinDemoRiskPolicy(
            max_leverage=1,
            min_confirmation_score=float(profile_rule["min_score"]),
            taker_fee_rate=float(os.getenv("BLOFIN_DEMO_TAKER_FEE_RATE", "0.0006")),
            estimated_slippage_bps_per_side=float(
                os.getenv("BLOFIN_DEMO_ESTIMATED_SLIPPAGE_BPS_PER_SIDE", "2.0")
            ),
        )
        trader = BloFinDemoExecutor(
            client=client, store=store, policy=policy, trading_enabled=True,
        )
        _record_challenge1_audit(
            job_id, "ORDER_SUBMISSION_AUTHORIZED",
            {
                "instrument_id": instrument_id, "decision": "APPROVE",
                "reason_fa": "Both reviewers passed, Demo readiness is READY, and executor preflight remains mandatory.",
                "order_plan": {
                    key: record.get(key) for key in (
                        "signal_id", "signal", "entry_price_usd", "stop_loss_usd",
                        "take_profit_usd", "risk_budget_usdt", "confirmation_score",
                    ) if key in record
                },
                "execution_environment": "BLOFIN_DEMO",
                "leverage": 1, "minimum_size_only": True,
            },
            event_key="order-submission-authorized", experience_id=learning_experience_id,
            reason="The exact Demo order plan passed the robot and two-reviewer gates.",
        )
        submission_started = True
        failed_operation = "blofin_demo_executor.reserve_and_submit_order"
        execution = trader.execute(
            record, inst_id=instrument_id, leverage=1,
            allow_demo_orders=True, minimum_size_only=True,
            submission_lock=BLOFIN_ORDER_SUBMISSION_LOCK,
        )
        _record_challenge1_audit(
            job_id, "ORDER_SUBMISSION_RESULT",
            {
                "instrument_id": instrument_id,
                "execution_result": {
                    key: execution.get(key) for key in (
                        "status", "side", "quantity_contracts", "entry_reference_price",
                        "exchange_order_id", "client_order_id", "stop_loss", "take_profit",
                        "risk_budget_usdt", "leverage",
                    ) if key in execution
                },
                "orders_submitted": True, "environment": "BLOFIN_DEMO",
            },
            event_key="order-submission-result", experience_id=learning_experience_id,
            status="SUCCESS",
            reason="BloFin Demo returned an order-submission response; fill remains independently unconfirmed.",
        )
        _set_blofin_execution_job(
            job_id, "running", stage="ORDER_SUBMITTED",
            message_fa="سفارش به BloFin Demo ارسال شد؛ در حال تأیید Fill، قیمت ورود و پوزیشن.",
            order={
                "exchange_order_id": execution.get("exchange_order_id"),
                "client_order_id": execution.get("client_order_id"),
                "instrument_id": execution.get("instrument_id"),
                "side": execution.get("side"),
                "quantity_contracts": execution.get("quantity_contracts"),
                "reference_entry_price": execution.get("entry_reference_price"),
                "leverage": execution.get("leverage"),
                "stop_loss": execution.get("stop_loss"),
                "take_profit": execution.get("take_profit"),
                "risk_budget_usdt": execution.get("risk_budget_usdt"),
            },
            submitted_at_utc=datetime.now(timezone.utc).isoformat(),
            analysis=gate_summary,
            elliott_wave=wave_report,
            trade_context=trade_context,
        )

        order_detail = None
        fills = []
        detail_error = None
        for attempt in range(3):
            try:
                raw_detail = client.get_order_detail(
                    instrument_id, client_order_id=execution["client_order_id"],
                )
                if isinstance(raw_detail, list):
                    order_detail = next((row for row in raw_detail if isinstance(row, dict)), None)
                elif isinstance(raw_detail, dict):
                    order_detail = raw_detail
                raw_fills = client.get_fills_history(inst_id=instrument_id, order_id=execution["exchange_order_id"], limit=100)
                fills = raw_fills if isinstance(raw_fills, list) else []
                if fills or (
                    isinstance(order_detail, dict)
                    and str(order_detail.get("state") or "").lower() in {
                        "filled", "partially_filled", "canceled", "order_failed",
                    }
                ):
                    break
            except Exception as exc:
                detail_error = type(exc).__name__
            if attempt < 2:
                time.sleep(1.5)

        detail_state = str((order_detail or {}).get("state") or "").lower()
        mapped_state = {
            "live": "ACCEPTED", "partially_filled": "PARTIALLY_FILLED",
            "filled": "FILLED", "canceled": "CANCELED", "order_failed": "REJECTED",
        }.get(detail_state)
        if mapped_state and execution.get("client_order_id"):
            store.update_order(
                execution["client_order_id"], mapped_state,
                exchange_order_id=str((order_detail or {}).get("orderId") or execution.get("exchange_order_id") or "") or None,
            )

        allowed_order_fields = (
            "orderId", "clientOrderId", "instId", "side", "positionSide",
            "orderType", "size", "filledSize", "averagePrice", "price",
            "state", "createTime", "updateTime", "fee", "pnl",
        )
        safe_detail = (
            {key: order_detail[key] for key in allowed_order_fields if key in order_detail}
            if isinstance(order_detail, dict) else None
        )
        allowed_fill_fields = (
            "tradeId", "orderId", "instId", "side", "fillPrice",
            "fillSize", "fillPnl", "fee", "ts",
        )
        safe_fills = [
            {key: row[key] for key in allowed_fill_fields if key in row}
            for row in fills if isinstance(row, dict)
        ]
        filled_size = 0.0
        for row in safe_fills:
            try:
                filled_size += float(row.get("fillSize") or 0)
            except (TypeError, ValueError, OverflowError):
                pass
        if not filled_size:
            try:
                filled_size = float((order_detail or {}).get("filledSize") or 0)
            except (TypeError, ValueError, OverflowError):
                filled_size = 0.0
        average_fill = (
            sum(float(row["fillPrice"]) * float(row["fillSize"]) for row in safe_fills
                if row.get("fillPrice") not in (None, "") and row.get("fillSize") not in (None, ""))
            / sum(float(row["fillSize"]) for row in safe_fills
                  if row.get("fillPrice") not in (None, "") and row.get("fillSize") not in (None, ""))
            if any(row.get("fillPrice") not in (None, "") and row.get("fillSize") not in (None, "") for row in safe_fills)
            else (safe_detail or {}).get("averagePrice")
        )
        trade_executed = filled_size > 0
        positions = []
        position_history = []
        try:
            rows = client.get_positions(inst_id=instrument_id)
            positions = [
                {key: row[key] for key in (
                    "positionId", "instId", "positionSide", "positions",
                    "averagePrice", "markPrice", "unrealizedPnl",
                    "leverage", "liquidationPrice", "createTime", "updateTime",
                ) if key in row}
                for row in rows if isinstance(row, dict)
            ] if isinstance(rows, list) else []
            if trade_executed and learning_experience_id:
                expected_side = "long" if execution.get("side") == "LONG" else "short"
                matching_positions = []
                for position in positions:
                    position_side = str(position.get("positionSide") or "").lower()
                    if position_side == expected_side:
                        matching_positions.append(position)
                    elif position_side == "net":
                        try:
                            signed_size = float(position.get("positions") or 0)
                        except (TypeError, ValueError, OverflowError):
                            signed_size = 0.0
                        if (expected_side == "long" and signed_size > 0) or (expected_side == "short" and signed_size < 0):
                            matching_positions.append(position)
                if len(matching_positions) == 1 and matching_positions[0].get("positionId"):
                    store.attach_position_id(
                        learning_experience_id,
                        str(matching_positions[0]["positionId"]),
                    )
        except Exception:
            pass
        try:
            rows = client.get_positions_history(inst_id=instrument_id, state="closed", limit=100)
            position_history = [
                {key: row[key] for key in (
                    "historyId", "positionId", "instId", "side", "positionSide",
                    "closePositions", "openAveragePrice", "closeAveragePrice",
                    "createTime", "updateTime", "leverage", "realizedPnl",
                    "realizedPnlRatio", "fee",
                ) if key in row}
                for row in rows if isinstance(row, dict)
            ] if isinstance(rows, list) else []
        except Exception:
            pass
        filled_at = None
        if safe_fills:
            latest_fill = max(safe_fills, key=lambda row: int(row.get("ts") or 0))
            if latest_fill.get("ts"):
                try:
                    filled_at = datetime.fromtimestamp(int(latest_fill["ts"]) / 1000, tz=timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError, OSError):
                    filled_at = None
        _record_challenge1_audit(
            job_id, "ENTRY_FILL_RECONCILIATION",
            {
                "instrument_id": instrument_id, "trade_executed": trade_executed,
                "orders_submitted": True, "average_fill_price": average_fill,
                "filled_size": filled_size, "filled_at_utc": filled_at,
                "order_detail": safe_detail, "fills": safe_fills, "positions": positions,
                "position_id": next(
                    (str(item.get("positionId")) for item in positions if item.get("positionId")),
                    None,
                ),
            },
            event_key="entry-fill-reconciliation", experience_id=learning_experience_id,
            position_id=next(
                (str(item.get("positionId")) for item in positions if item.get("positionId")),
                None,
            ),
            status="SUCCESS" if trade_executed else "WARNING",
            reason="Fill and position data were checked against BloFin Demo read-only history.",
        )
        _set_blofin_execution_job(
            job_id, "running", stage="ENTRY_FILL_CONFIRMED" if trade_executed else "ORDER_PENDING",
            message_fa=(
                "Fill ورود از تاریخچه معاملات BloFin تأیید شد."
                if trade_executed else
                "سفارش ثبت شده، اما پرشدن آن هنوز از تاریخچه Fill تأیید نشده است."
            ),
            fill={
                "filled_size": filled_size,
                "average_fill_price": average_fill,
                "filled_at_utc": filled_at,
                "fills": safe_fills,
            },
            analysis=gate_summary,
            elliott_wave=wave_report,
            trade_context=trade_context,
        )
        result = {
            **execution,
            "status": mapped_state or execution.get("status", "ACCEPTED"),
            "environment": "BLOFIN_DEMO",
            "instrument_id": instrument_id,
            "execution_profile": execution_profile,
            "analysis": gate_summary,
            "elliott_wave": wave_report,
            "trade_context": trade_context,
            "order_detail": safe_detail,
            "fills": safe_fills,
            "average_fill_price": average_fill,
            "filled_size": filled_size,
            "filled_at_utc": filled_at,
            "trade_executed": trade_executed,
            "orders_submitted": True,
            "ai_review": {
                "status": ai_status, "model": ai_review.get("model"),
                "returned_model": ai_review.get("returned_model"),
                "final_rating": ai_review.get("final_rating"),
                "latency_ms": ai_review.get("latency_ms"),
            },
            "ai_reviews": ai_reviews,
            "ai_consensus": ai_consensus,
            "ai_status": ai_status,
            "positions": positions,
            "closed_positions": position_history,
            "reason_fa": (
                "ورود آزمایشی در BloFin Demo پر شده است؛ مانیتور زنده قیمت، PnL، حد ضرر/حد سود و زمان خروج را دنبال می‌کند."
                if trade_executed else
                "سفارش به BloFin Demo ارسال شد، اما پرشدن آن هنوز تأیید نشده؛ سفارش دوباره ارسال نمی‌شود."
            ),
        }
        _set_blofin_execution_job(
            job_id, "completed", stage="MONITORING",
            message_fa=result["reason_fa"], result=result,
            completed_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    except BloFinDemoExecutionUnknown as exc:
        try:
            _record_challenge1_audit(
                job_id, "ORDER_OUTCOME_UNKNOWN",
                {
                    "instrument_id": instrument_id,
                    "failed_operation": failed_operation,
                    "error_type": type(exc).__name__,
                    "reason_fa": str(exc)[:1000],
                    "orders_submitted": "UNKNOWN",
                    "retry_allowed": False,
                },
                event_key="order-outcome-unknown",
                experience_id=learning_experience_id,
                status="WARNING",
                reason=str(exc)[:1200],
            )
        except Exception:
            pass
        _set_blofin_execution_job(
            job_id, "completed", stage="ORDER_OUTCOME_UNKNOWN",
            message_fa="نتیجه ارسال مبهم است؛ برای جلوگیری از سفارش تکراری ارسال مجدد نمی‌شود.",
            result={
                "status": "UNKNOWN", "environment": "BLOFIN_DEMO",
                "reason_fa": "نتیجه ارسال سفارش مبهم است؛ قبل از هر اقدام، order history و positions را بررسی کن.",
                "error_type": type(exc).__name__,
                "orders_submitted": "UNKNOWN", "trade_executed": False,
            },
        )
    except BloFinDemoExecutionBlocked as exc:
        try:
            _record_challenge1_audit(
                job_id, "EXECUTION_BLOCKED",
                {
                    "instrument_id": instrument_id,
                    "failed_operation": failed_operation,
                    "error_type": type(exc).__name__,
                    "reason_fa": str(exc)[:1000],
                    "orders_submitted": False,
                    "trade_executed": False,
                },
                event_key="execution-blocked",
                experience_id=learning_experience_id,
                status="WARNING",
                reason=str(exc)[:1200],
            )
        except Exception:
            pass
        if store is not None and not learning_experience_id:
            try:
                learning_experience_id = store.record_challenge1_analysis_failure(
                    cycle_id=job_id,
                    instrument_id=instrument_id,
                    failure_stage="CHALLENGE_1_EXECUTION_SETUP_BLOCKED",
                    error_type=type(exc).__name__,
                    reason=str(exc),
                    timestamp_utc=analysis_time_utc,
                )
            except Exception:
                pass
        elif store is not None and learning_experience_id:
            try:
                prior_order = store.get_order_for_experience(learning_experience_id)
                if prior_order is None or prior_order.get("status") == "REJECTED":
                    store.record_experience_not_executed(
                        experience_id=learning_experience_id,
                        instrument_id=instrument_id,
                        signal=learning_signal,
                        stage="EXECUTION_BLOCKED",
                        reason=str(exc),
                    )
            except Exception:
                pass
        _set_blofin_execution_job(
            job_id, "completed", stage="BLOCKED",
            message_fa=str(exc),
            result={
                "status": "BLOCKED", "environment": "BLOFIN_DEMO",
                "reason_fa": str(exc), "error_type": type(exc).__name__,
                "orders_submitted": False, "trade_executed": False,
            },
        )
    except Exception as exc:
        try:
            with LOCK:
                audit_job_snapshot = copy.deepcopy(BLOFIN_EXECUTION_JOBS.get(job_id) or {})
            _record_challenge1_audit(
                job_id, "CYCLE_FAILED",
                {
                    "instrument_id": instrument_id,
                    "failed_operation": failed_operation,
                    "execution_stage": audit_job_snapshot.get("stage"),
                    "error_type": type(exc).__name__,
                    "orders_submitted": "UNKNOWN" if submission_started else False,
                    "trade_executed": False,
                },
                event_key="cycle-failed",
                experience_id=learning_experience_id,
                status="FAILED",
                reason=f"Challenge 1 cycle failed at {failed_operation}: {type(exc).__name__}",
            )
        except Exception:
            pass
        with LOCK:
            failed_job_snapshot = copy.deepcopy(BLOFIN_EXECUTION_JOBS.get(job_id) or {})
        execution_stage = str(failed_job_snapshot.get("stage") or "EXECUTION_ERROR")
        try:
            database_backend = storage_backend()
        except Exception as backend_exc:
            database_backend = "UNAVAILABLE:" + type(backend_exc).__name__
        diagnostic_timestamp = datetime.now(timezone.utc).isoformat()
        diagnostic = build_execution_error_diagnostic(
            exc,
            failed_operation=failed_operation,
            instrument_id=instrument_id,
            cycle_id=job_id,
            job_id=job_id,
            execution_stage=execution_stage,
            database_backend=str(database_backend),
            timestamp_utc=diagnostic_timestamp,
        )
        # Render captures stdout as an application log. The helper redacts known
        # server secrets and credential-shaped values before this line is emitted.
        print(
            "CSR_SAFE_EXECUTION_DIAGNOSTIC "
            + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
            flush=True,
        )
        learning_memory_status = "NOT_APPLICABLE"
        if store is not None and not learning_experience_id:
            try:
                learning_experience_id = store.record_challenge1_analysis_failure(
                    cycle_id=job_id,
                    instrument_id=instrument_id,
                    failure_stage="CHALLENGE_1_CYCLE_FAILURE",
                    error_type=type(exc).__name__,
                    reason=diagnostic["error_message"],
                    timestamp_utc=diagnostic_timestamp,
                )
                learning_memory_status = "PERSISTED"
            except Exception as memory_exc:
                learning_memory_status = "FAILED:" + type(memory_exc).__name__
        elif store is not None and learning_experience_id and not submission_started:
            try:
                prior_order = store.get_order_for_experience(learning_experience_id)
                if prior_order is None or prior_order.get("status") == "REJECTED":
                    store.record_experience_not_executed(
                        experience_id=learning_experience_id,
                        instrument_id=instrument_id,
                        signal=learning_signal,
                        stage="EXECUTION_ERROR",
                        reason=diagnostic["error_type"] + ": " + diagnostic["error_message"],
                    )
                    learning_memory_status = "PERSISTED"
            except Exception as memory_exc:
                learning_memory_status = "FAILED:" + type(memory_exc).__name__
        _set_blofin_execution_job(
            job_id, "failed", stage="EXECUTION_ERROR",
            message_fa="اجرای آزمایشی با خطای کنترل‌شده متوقف شد؛ قبل از تلاش مجدد باید وضعیت سفارش در BloFin بررسی شود.",
            error={
                **diagnostic,
                "type": diagnostic["error_type"],
                "learning_memory_status": learning_memory_status,
                "learning_experience_id": learning_experience_id,
                "orders_submitted": "UNKNOWN" if submission_started else False,
                "trade_executed": False,
            },
        )
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        try:
            _queue_blofin_completed_job_reports(job_id, instrument_id)
        except Exception:
            pass


def _run_blofin_demo_smoke_test_job(job_id: str) -> None:
    """Run the dedicated signal-independent open/close test, strictly on BloFin Demo."""
    _set_blofin_execution_job(
        job_id, "running", stage="READINESS_CHECK",
        message_fa="در حال بررسی قفل مستقل تست، کلید Trade و محیط BloFin Demo.",
        test_name="تست ربات در محیط دمو صرافی",
    )
    readiness = _blofin_demo_smoke_test_readiness()
    if readiness.get("status") != "READY":
        _set_blofin_execution_job(
            job_id, "completed", stage="BLOCKED",
            message_fa=readiness.get("message_fa"),
            result={
                "status": "BLOCKED", "environment": "BLOFIN_DEMO",
                "test_name": "تست ربات در محیط دمو صرافی",
                "reason_fa": readiness.get("message_fa"),
                "orders_submitted": False, "trade_executed": False,
                "position_closed": False,
            },
        )
        return

    last_stage = {"value": "PREFLIGHT"}
    def record_event(**event: Any) -> None:
        stage = str(event.pop("stage", "RUNNING"))
        last_stage["value"] = stage
        message = str(event.pop("message_fa", stage))
        _set_blofin_execution_job(
            job_id, "running", stage=stage, message_fa=message,
            smoke_test_event=event,
        )

    try:
        runner = BloFinDemoSmokeTestRunner(
            client=BloFinDemoTradeClient(timeout=10.0),
            on_event=record_event,
        )
        result = runner.run()
        _set_blofin_execution_job(
            job_id, "completed", stage=result.get("status", "COMPLETED"),
            message_fa=(
                "تست مستقل موفق شد: ربات سفارش Demo را خودش باز کرد، Fill را دید و همان پوزیشن را بست."
                if result.get("status") == "CLOSED_VERIFIED" else
                "تست مستقل پایان یافت؛ وضعیت نهایی را در گزارش بررسی کن."
            ),
            result=result,
            completed_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    except BloFinDemoExecutionUnknown as exc:
        _set_blofin_execution_job(
            job_id, "completed", stage="ORDER_OUTCOME_UNKNOWN",
            message_fa="نتیجه ورود یا خروج نامشخص است؛ هیچ سفارش تکراری ارسال نمی‌شود. پوزیشن و تاریخچه Demo را بررسی کن.",
            result={
                "status": "UNKNOWN", "environment": "BLOFIN_DEMO",
                "test_name": "تست ربات در محیط دمو صرافی",
                "reason_fa": str(exc), "error_type": type(exc).__name__,
                "orders_submitted": "UNKNOWN", "trade_executed": "UNKNOWN",
                "position_closed": False,
            },
            completed_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    except BloFinDemoExecutionBlocked as exc:
        _set_blofin_execution_job(
            job_id, "completed", stage="BLOCKED",
            message_fa=str(exc),
            result={
                "status": "BLOCKED", "environment": "BLOFIN_DEMO",
                "test_name": "تست ربات در محیط دمو صرافی",
                "reason_fa": str(exc), "error_type": type(exc).__name__,
                "orders_submitted": False, "trade_executed": False,
                "position_closed": False,
            },
            completed_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        failed_stage = last_stage["value"]
        submission_may_have_started = failed_stage in {
            "ENTRY_SUBMITTING", "ENTRY_ACCEPTED", "ENTRY_FILLED",
            "CLOSE_SUBMITTING", "CLOSE_ACCEPTED",
        }
        _set_blofin_execution_job(
            job_id, "failed", stage="SMOKE_TEST_ERROR",
            message_fa="تست مستقل با خطای کنترل‌شده متوقف شد؛ مرحله دقیق خطا در گزارش ثبت شده است.",
            result={
                "status": "UNKNOWN" if submission_may_have_started else "PREFLIGHT_FAILED",
                "environment": "BLOFIN_DEMO",
                "test_name": "تست ربات در محیط دمو صرافی",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:240],
                "failed_stage": failed_stage,
                "orders_submitted": "UNKNOWN" if submission_may_have_started else False,
                "trade_executed": "UNKNOWN" if submission_may_have_started else False,
                "position_closed": False,
            },
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "CryptoSuperRadar/0.2"

    def _send(self, status: int, content: bytes, content_type: str = "text/html; charset=utf-8"):
        """Send a response without logging a traceback when the client disconnects."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A probe/browser may close its socket before the body is written.
            # The request is already gone; do not turn that into an app traceback.
            self.close_connection = True
            return False
        return True

    def do_HEAD(self):
        """Support Render's default HEAD health probe without returning a 501."""
        parsed = urlparse(self.path)
        if parsed.path not in {"/", "/health"}:
            status = 404
            content_type = "text/plain; charset=utf-8"
        else:
            status = 200
            content_type = "application/json" if parsed.path == "/health" else "text/html; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            health = {"status": "ok", "commit": os.getenv("RENDER_GIT_COMMIT") or None}
            return self._send(200, json.dumps(health, separators=(",", ":")).encode(), "application/json")
        if parsed.path == "/api/ai/status":
            payload = ai_supervisor_status()
            payload["status_scope"] = "OPTIONAL_MULTI_PROVIDER_CONFIGURATION_ONLY"
            payload["used_for_challenge1_entry_gate"] = False
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/ai/challenge1-status":
            payload = {
                "primary_supervisor": ai_connection_status(),
                "second_supervisor": second_ai_connection_status(),
                "two_reviewer_gate": "REQUIRES_BOTH_VERIFIED_APPROVALS",
                "required_verified_approvals": 2,
                "execution_role": "ENTRY_APPROVAL_GATE",
                "entry_gate_enforced": True,
                "status_scope": "NON_SECRET_CONFIGURATION_PREFLIGHT_ONLY",
                "connectivity_probe_performed": False,
            }
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/notion-sync/status":
            outbox = None
            activity_journal = None
            try:
                outbox = NotionEventOutbox()
                payload = outbox.status_snapshot()
                with LOCK:
                    payload["worker"] = copy.deepcopy(NOTION_EVENT_SYNC_STATE)
                try:
                    activity_journal = NotionActivityJournal()
                    payload["activity_journal"] = activity_journal.status_snapshot()
                except Exception as activity_exc:
                    payload["activity_journal"] = {
                        "status": "UNAVAILABLE",
                        "error_type": type(activity_exc).__name__,
                    }
                audit_recorder = None
                try:
                    audit_recorder = Challenge1AuditRecorder()
                    payload["challenge1_audit"] = audit_recorder.status_snapshot()
                except Exception as audit_exc:
                    payload["challenge1_audit"] = {
                        "status": "UNAVAILABLE", "error_type": type(audit_exc).__name__,
                    }
                finally:
                    if audit_recorder is not None:
                        audit_recorder.close()
                return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            except Exception as exc:
                payload = {
                    "status": "UNAVAILABLE",
                    "token_configured": bool(
                        (os.getenv("NOTION_API_TOKEN") or os.getenv("NOTION_TOKEN") or "").strip()
                    ),
                    "error_type": type(exc).__name__,
                }
                with LOCK:
                    payload["worker"] = copy.deepcopy(NOTION_EVENT_SYNC_STATE)
                return self._send(503, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            finally:
                if outbox is not None:
                    outbox.close()
                if activity_journal is not None:
                    activity_journal.close()
        if parsed.path == "/api/blofin/demo/v2-autopilot/latest-public":
            payload = _blofin_v2_public_cycle_snapshot()
            return self._send(200 if payload.get("status") == "OK" else 503,
                              json.dumps(payload, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/v2-autopilot/cycles":
            expected_token = os.getenv("BLOFIN_DASHBOARD_TOKEN", "")
            if not _blofin_dashboard_token_valid(self.headers.get("Authorization", ""), expected_token):
                return self._send(401, b'{"status":"UNAUTHORIZED"}', "application/json; charset=utf-8")
            payload = _blofin_v2_cycle_snapshot()
            return self._send(200 if payload.get("status") == "OK" else 503,
                              json.dumps(payload, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/position-manager/status":
            with LOCK:
                payload = copy.deepcopy(BLOFIN_POSITION_MANAGER_STATE)
            payload["thread_alive"] = bool(
                BLOFIN_POSITION_MANAGER_THREAD is not None
                and BLOFIN_POSITION_MANAGER_THREAD.is_alive()
            )
            return self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/status":
            payload = _blofin_demo_connection_status()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/smoke-test-readiness":
            manual_check = parse_qs(parsed.query).get("refresh", [""])[0] == "1"
            payload = _blofin_demo_smoke_test_readiness(check_account_state=manual_check)
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/execution-readiness":
            payload = _blofin_demo_execution_readiness()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/instruments":
            payload = _blofin_demo_instrument_options(
                force=parse_qs(parsed.query).get("refresh", [""])[0] == "1"
            )
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/autopilot/status":
            with LOCK:
                payload = copy.deepcopy(BLOFIN_AUTOPILOT_STATE)
            autopilot_enabled = _blofin_demo_autopilot_enabled()
            payload["enabled"] = autopilot_enabled
            if not autopilot_enabled:
                # Do not expose a stale persisted schedule as if another cycle were queued.
                payload.update({
                    "status": "DISABLED",
                    "search_continues": False,
                    "next_cycle_scheduled": False,
                    "next_cycle_at_utc": None,
                    "next_cycle_in_seconds": None,
                })
            payload["instrument_ids"] = [s.strip().upper() for s in os.getenv("BLOFIN_DEMO_AUTOPILOT_INSTRUMENTS", ",".join(DEFAULT_CHALLENGE1_INSTRUMENTS)).split(",") if s.strip()]
            payload["instrument_id"] = payload["instrument_ids"][0] if payload["instrument_ids"] else "BTC-USDT"
            configured_profile = os.getenv("BLOFIN_DEMO_AUTOPILOT_PROFILE", "CHALLENGE_1").strip().upper()
            payload["execution_profile"] = (
                configured_profile
                if configured_profile in {"CHALLENGE_1", "CHALLENGE_1_V2"}
                else "CHALLENGE_1"
            )
            payload["v2_orchestration_enabled"] = (
                os.getenv("BLOFIN_CHALLENGE1_V2_ORCHESTRATION_ENABLED", "").strip().lower() == "true"
            )
            with LOCK:
                payload["runtime_controlled_by_dashboard"] = BLOFIN_AUTOPILOT_RUNTIME_OVERRIDE is not None
            payload["readiness"] = _blofin_demo_execution_readiness()
            with LOCK:
                payload["position_manager"] = copy.deepcopy(BLOFIN_POSITION_MANAGER_STATE)
                payload["position_manager_thread_alive"] = bool(
                    BLOFIN_POSITION_MANAGER_THREAD is not None
                    and BLOFIN_POSITION_MANAGER_THREAD.is_alive()
                )
            return self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/live-monitor":
            expected_token = os.getenv("BLOFIN_DASHBOARD_TOKEN", "")
            if not _blofin_dashboard_token_valid(self.headers.get("Authorization", ""), expected_token):
                return self._send(401, b'{"status":"UNAUTHORIZED"}', "application/json; charset=utf-8")
            try:
                requested = parse_qs(parsed.query).get("instrument", ["BTC-USDT"])[0].upper()
                payload = _blofin_demo_live_monitor_snapshot(requested)
                return self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                                  "application/json; charset=utf-8")
            except BloFinDemoError as exc:
                payload = {"status": "READ_FAILED", "environment": "BLOFIN_DEMO",
                           "error_type": type(exc).__name__,
                           "message_fa": "دریافت تازه اطلاعات معاملات دمو از BloFin ممکن نشد."}
                return self._send(502, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            except Exception as exc:
                payload = {"status": "UNKNOWN", "environment": "BLOFIN_DEMO",
                           "error_type": type(exc).__name__,
                           "message_fa": "مانیتور زنده با خطای کنترل‌شده متوقف شد."}
                return self._send(502, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
        if parsed.path.startswith("/api/blofin/demo/audit/"):
            authorization = self.headers.get("Authorization", "")
            expected_token = os.getenv("BLOFIN_DASHBOARD_TOKEN", "")
            if not _blofin_dashboard_token_valid(authorization, expected_token):
                return self._send(401, b'{"status":"UNAUTHORIZED"}', "application/json; charset=utf-8")
            reference = parsed.path.rsplit("/", 1)[-1]
            recorder = None
            try:
                recorder = Challenge1AuditRecorder()
                payload = recorder.list_related(reference)
                return self._send(
                    200 if payload.get("status") != "NOT_FOUND" else 404,
                    json.dumps(payload, ensure_ascii=False, default=str).encode(),
                    "application/json; charset=utf-8",
                )
            except Exception as exc:
                payload = {
                    "status": "UNAVAILABLE", "error_type": type(exc).__name__,
                    "message_fa": "دفتر شواهد ممیزی در دسترس نیست؛ هیچ موفقیت یا تکمیل گزارشی فرض نمی‌شود.",
                }
                return self._send(503, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            finally:
                if recorder is not None:
                    recorder.close()
        if parsed.path.startswith("/api/blofin/demo/execution-jobs/"):
            authorization = self.headers.get("Authorization", "")
            dashboard_token = os.getenv("BLOFIN_DASHBOARD_TOKEN", "")
            smoke_test_token = os.getenv("BLOFIN_DEMO_SMOKE_TEST_TOKEN", "").strip()
            dashboard_authorized = _blofin_dashboard_token_valid(authorization, dashboard_token)
            smoke_test_authorized = (
                bool(smoke_test_token)
                and _blofin_dashboard_token_valid(authorization, smoke_test_token)
            )
            if not (dashboard_authorized or smoke_test_authorized):
                return self._send(401, b'{"status":"UNAUTHORIZED"}', "application/json; charset=utf-8")
            job_id = parsed.path.rsplit("/", 1)[-1]
            with LOCK:
                job = copy.deepcopy(BLOFIN_EXECUTION_JOBS.get(job_id))
            if job is None:
                return self._send(404, b'{"status":"NOT_FOUND"}', "application/json; charset=utf-8")
            if (
                smoke_test_authorized and not dashboard_authorized
                and job.get("test_name") != "تست ربات در محیط دمو صرافی"
            ):
                return self._send(403, b'{"status":"FORBIDDEN"}', "application/json; charset=utf-8")
            job["report_fa"] = _blofin_execution_report_fa(job)
            return self._send(200, json.dumps(job, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/account":
            expected_token = os.getenv("BLOFIN_DASHBOARD_TOKEN", "")
            if not expected_token:
                payload = {"status": "ACCESS_NOT_CONFIGURED", "orders_enabled": False,
                           "message_fa": "توکن دسترسی پنل حساب در Environment تنظیم نشده است."}
                return self._send(503, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            if not _blofin_dashboard_token_valid(self.headers.get("Authorization", ""), expected_token):
                payload = {"status": "UNAUTHORIZED", "orders_enabled": False,
                           "message_fa": "توکن دسترسی پنل معتبر نیست."}
                return self._send(401, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            payload = _blofin_demo_account_snapshot()
            status_code = 200 if payload.get("status") == "CONNECTED_READ_ONLY" else 502
            return self._send(status_code, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/dex-search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            payload = search_dex_tokens(getattr(ENGINE.data, "http", None), query)
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/global-markets":
            refresh = parse_qs(parsed.query).get("refresh", ["0"])[0] == "1"
            payload = GLOBAL_MARKET_BOARD.get_board(force_refresh=refresh)
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/gem-hunter/report":
            raw_limit = parse_qs(parsed.query).get("limit", ["50"])[0]
            try:
                limit = max(1, min(int(raw_limit), 50))
            except (TypeError, ValueError):
                return self._send(400, b'{"error":"limit must be an integer from 1 to 50"}', "application/json")
            force_refresh = parse_qs(parsed.query).get("refresh", ["0"])[0] == "1"
            payload = _gem_hunter_api_payload(limit=limit, include_diagnostics=True, force_refresh=force_refresh)
            payload["report_mode"] = "RESEARCH_ONLY_NOT_A_TRADE_SIGNAL"
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/gem-hunter/v2/candidates":
            payload = gem_hunter_v2_public_snapshot()
            candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
            mapped = []
            for item in candidates:
                row = dict(item)
                parts = row.get("score_parts") or {}
                security_state = "PASS" if (row.get("security") or {}).get("status") == "PASS" else "FAIL" if (row.get("security") or {}).get("status") == "FAIL" else "UNKNOWN"
                filters_v2 = {
                    "contract_security": security_state,
                    "liquidity_depth": "PASS" if (parts.get("liquidity") or 0) >= 12 else "UNKNOWN",
                    "buy_sell_momentum": "PASS" if (parts.get("buy_sell_momentum") or 0) >= 10 else "UNKNOWN",
                    "holder_distribution": "PASS" if (row.get("holders") or {}).get("status") == "AVAILABLE" else "NOT_AVAILABLE",
                    "age_activity": "PASS" if (parts.get("age_activity") or 0) >= 6 else "UNKNOWN",
                    "pool_quality": "PASS" if row.get("gecko_match") else "UNKNOWN",
                }
                row["candidate_status"] = row.get("status", "WATCH")
                row["discovery_priority_score"] = row.get("score")
                row["trade_authorization"] = "NO_TRADE"
                row["qualification"] = {
                    "filters": filters_v2,
                    "critical_gates": {"security": security_state, "liquidity": filters_v2["liquidity_depth"], "data_quality": filters_v2["pool_quality"], "exit": "UNKNOWN"},
                    "critical_gate_reasons": {"security": (row.get("security") or {}).get("reason", "Security evidence unavailable."), "liquidity": "DEX pool liquidity is not guaranteed executable depth.", "data_quality": "Exact pool comparison with GeckoTerminal.", "exit": "No execution/slippage simulation is performed."},
                    "market_metrics": {"holder_count": None, "top10_independent_holders_pct": (row.get("holders") or {}).get("top10_pct"), "buy_volume_share_pct": None, "price_usd": row.get("price_usd"), "liquidity_usd": row.get("liquidity_usd"), "volume_24h_usd": row.get("volume_24h_usd")},
                    "opportunity_score": row.get("score"), "risk_score": None, "evaluation_status": row.get("status"), "market_data_sources": row.get("sources", []), "data_as_of_utc": row.get("observed_at_utc"), "whale_evidence": {"status": "NOT_AVAILABLE", "coverage_status": "NOT_CONFIGURED", "events": []},
                }
                row["security_evidence"] = row.get("security") or {"status": "NOT_AVAILABLE", "provider": "NOT_CONFIGURED"}
                row["market_reconciliation"] = {"status": "PASS" if row.get("gecko_match") else "UNKNOWN", "verified_liquidity_usd": row.get("gecko_liquidity_usd"), "sources": row.get("sources", [])}
                mapped.append(row)
            payload["diagnostic_candidates"] = mapped
            payload["candidates"] = []
            payload["discovered_candidate_count"] = len(mapped)
            payload["evaluation_record_count"] = len(mapped)
            payload["complete_evaluation_count"] = sum(1 for x in mapped if x.get("security", {}).get("status") == "PASS")
            payload["incomplete_evaluation_count"] = len(mapped) - payload["complete_evaluation_count"]
            payload["unassessed_candidate_count"] = 0
            payload["qualified_buy_count"] = 0
            payload["refreshing"] = False
            payload["diagnostic_freshness"] = "LATEST_STORED_TOP_10"
            payload["diagnostic_fa"] = "Gem Hunter V2 مستقل و فقط‌خواندنی است؛ امتیازدهی سبک شش‌معیاره انجام شده و هیچ سفارش یا سیگنال BUY صادر نمی‌شود."
            payload["report_mode"] = "GEM_HUNTER_V2_READ_ONLY_NO_TRADE"
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if parsed.path == "/api/gem-hunter/discover":
            raw_limit = parse_qs(parsed.query).get("limit", ["20"])[0]
            try:
                limit = max(1, min(int(raw_limit), 50))
            except (TypeError, ValueError):
                return self._send(400, b'{"error":"limit must be an integer from 1 to 50"}', "application/json")
            payload = _gem_hunter_api_payload(limit=limit)
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/data-health":
            payload = build_feed_health_report(_crypto_market_board())
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/crypto-markets":
            payload = _crypto_market_board()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/live-market-radar":
            query = parse_qs(parsed.query)
            symbol = query.get("symbol", ["BTCUSDT"])[0]
            interval = query.get("interval", ["5m"])[0]
            force_refresh = query.get("refresh", ["0"])[0] == "1"
            try:
                payload = _live_market_radar_payload(symbol, interval, force_refresh=force_refresh)
                return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            except ValueError as exc:
                payload = {"status": "UNKNOWN", "error": str(exc), "signal": {"signal": "UNKNOWN", "status": "BLOCKED"},
                           "candles": [], "orders_submitted": False}
                return self._send(400, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            except Exception as exc:
                payload = {"status": "UNKNOWN", "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                           "signal": {"signal": "UNKNOWN", "status": "BLOCKED",
                                      "reason": "دادهٔ کندل بازار در دسترس نیست."},
                           "candles": [], "orders_submitted": False}
                return self._send(502, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
        if parsed.path.startswith("/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
            if job is None:
                return self._send(404, b'{"error":"job not found"}', "application/json")
            return self._send(200, json.dumps(job, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/paper-bot/status":
            payload = PAPER_BOT.state()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/futures-paper-bot/status":
            payload = FUTURES_PAPER_BOT.state()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/history":
            asset = parse_qs(parsed.query).get("asset", [""])[0]
            try:
                AssetSpec.parse(asset)
                data = STORE.summary(asset)
                return self._send(200, json.dumps(data, ensure_ascii=False, default=str).encode(),
                                  "application/json; charset=utf-8")
            except Exception:
                return self._send(400, b'{"error":"invalid asset"}', "application/json")
        if parsed.path != "/":
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        body = """
        <header class="app-header">
          <div class="brand-mark" aria-hidden="true">🐋</div>
          <div class="brand-copy">
            <span class="eyebrow">پایش بازار · دادهٔ عمومی و رایگان</span>
            <h1>CRYPTO SUPER RADAR</h1>
            <p class="muted">پژوهش بازار با دادهٔ عمومی و Paper Research / Paper Trading؛ هیچ اتصال کیف‌پول یا سفارش Live ندارد. سفارش آزمایشی فقط در ماژول مستقل BloFin Demo و با کلید Trade جداگانه ممکن است.</p>
          </div>
          <span class="mode-pill"><span class="mode-dot"></span>Spot Research: فقط خواندنی · BloFin: Demo</span>
        </header>
        <section class="card" id="external-analysis-tools">
          <h2>ابزارهای تحلیل بازار</h2>
          <p class="muted">دو منبع تحلیلی را هم‌زمان و در کنار داشبورد اصلی باز کنید؛ هرکدام در زبانهٔ جداگانه باز می‌شود.</p>
          <div class="analysis-links-grid">
            <article class="analysis-link-card">
              <h3>CoinGlass · مشتقات و لیکوییدیشن</h3>
              <p>Open Interest، Funding Rate، نسبت Long/Short و نقشهٔ لیکوییدیشن‌ها.</p>
              <a class="analysis-link" href="https://www.coinglass.com/" target="_blank" rel="noopener noreferrer">باز کردن CoinGlass ↗</a>
            </article>
            <article class="analysis-link-card">
              <h3>DEX Screener · بازارهای غیرمتمرکز</h3>
              <p>قیمت، حجم، نقدینگی، جفت‌ارزها و فعالیت توکن‌های DEX.</p>
              <a class="analysis-link" href="https://dexscreener.com/" target="_blank" rel="noopener noreferrer">باز کردن DEX Screener ↗</a>
            </article>
          </div>
        </section>
        <section class="card" id="blofin-demo-panel">
          <h2>BloFin Demo Futures · مانیتور حساب خواندنی و اجرای Demo جداگانه</h2>
          <p class="muted">اتصال حساب با درخواست‌های خواندنی بررسی می‌شود. سطح واقعی مجوز خود API Key جداگانه از BloFin خوانده می‌شود؛ حتی کلید Read/Write نیز در این صفحه فقط برای GET استفاده می‌شود. توکن پنل با API Key صرافی متفاوت است و در مرورگر ذخیره نمی‌شود.</p>
          <div class="grid" style="align-items:center;margin:12px 0">
            <button id="blofin-demo-check" type="button">بررسی اتصال BloFin Demo</button>
            <span id="blofin-demo-status" class="muted" aria-live="polite">اتصال هنوز بررسی نشده است.</span>
          </div>
          <div class="grid" style="align-items:center;margin:12px 0">
            <input id="blofin-dashboard-token" type="password" autocomplete="off" placeholder="توکن دسترسی پنل حساب" aria-label="توکن دسترسی پنل حساب">
            <button id="blofin-demo-account-read" type="button">خواندن اطلاعات حساب</button>
            <button id="blofin-demo-account-clear" type="button">پاک‌کردن توکن و اطلاعات</button>
          </div>
          <p class="muted" id="blofin-demo-account-status" aria-live="polite">اطلاعات خصوصی حساب تا زمان تأیید توکن نمایش داده نمی‌شود.</p>
          <div id="blofin-demo-account-view" style="overflow-x:auto"></div>
          <p class="muted" id="blofin-demo-safety">محیط: Demo · مجوز Read برای مانیتور؛ ارسال سفارش فقط با کلید Trade آزمایشی و تأیید دستی</p>
          <div class="metric" style="margin-top:16px">
            <h3>معاملات آزمایشی نامحدود · انتخاب انسانی بازار و ربات</h3>
            <p class="muted">بازار فعلی این اتصال: BloFin Demo · USDT-M Perpetual · قرارداد خطی SWAP. نماد قرارداد را خودت انتخاب می‌کنی؛ جهت LONG/SHORT فقط از سیگنال زنده صادر می‌شود. سقف تعداد سفارش در روز یا در کل عمر سیستم تعریف نشده است؛ بااین‌حال هم‌زمان فقط یک پوزیشن مجاز است. هر کندل بسته‌شدهٔ ۵ دقیقه‌ای حداکثر یک سیگنال یکتا می‌سازد و تا بسته‌شدن و تعیین‌تکلیف کامل پوزیشن قبلی ورود تازه ممنوع است. اهرم ایزوله 1x، حداقل حجم مجاز، SL/TP، کنترل موجودی و حد ریسک اجباری‌اند.</p>
            <div class="grid" style="margin:10px 0">
              <div>
                <label for="blofin-market-type">نوع بازار</label>
                <select id="blofin-market-type" disabled><option value="USDT-LINEAR-SWAP">USDT Perpetual · Linear SWAP</option></select>
              </div>
              <div>
                <label for="blofin-instrument">رمزارز / قرارداد BloFin</label>
                <select id="blofin-instrument" disabled><option value="">در حال دریافت فهرست قراردادها...</option></select>
              </div>
              <div>
                <label for="blofin-engine-profile">استراتژی معامله Demo</label>
                <select id="blofin-engine-profile">
                  <option value="CHALLENGE_1" disabled>CHALLENGE_1 · مسیر قدیمی AutoPilot (غیرفعال در اجرای دستی)</option>
                  <option value="CHALLENGE_1_V2" selected>CHALLENGE_1_V2 · نسخه چندبازه‌ای Single-Shot</option>
                </select>
              </div>
            </div>
            <p id="blofin-instrument-status" class="muted" aria-live="polite">قراردادهای زنده با مجوز Full برای بازکردن موقعیت در حال دریافت‌اند.</p>
            <p class="muted"><a href="https://blofin.com/trade/en/futures/BTC-USDT" target="_blank" rel="noopener">بازار BTC-USDT Perpetual در BloFin</a> · <a href="https://blofin.com/market/derivatives" target="_blank" rel="noopener">فهرست بازارهای مشتقه</a> · <a href="https://blofin.com/en/support/FAQ/Futures-Trading/11742286589455-What-is-Demo-Trading" target="_blank" rel="noopener">راهنمای رسمی ورود به BloFin Demo</a></p>
            <p class="muted">استراتژی اجرای دستی CHALLENGE_1_V2: تحلیل فقط‌خواندنی کندل‌های بسته‌شدهٔ ۱۵ دقیقه، ۱ ساعت و ۴ ساعت، تأیید BTC در ۱۵ دقیقه، Funding و مشخصات قرارداد؛ سپس بررسی الگوهای ساختاری، جهت و فاز موج و شواهد چندبازه‌ای. ورود فقط پس از دو تأیید مستقل و معتبر AI، هم‌جهتی تصمیم‌ها و عبور از کنترل‌های حساب و ریسک مجاز است. VETO، UNKNOWN، ERROR، داده ناقص یا اختلاف ناظران ورود را مسدود می‌کند. ۱۲ فیلتر عمومی فقط نظارتی‌اند و شرط ورود نیستند. AutoPilot قدیمی در مسیر جداگانه با استراتژی اصلی Challenge 1 کار می‌کند و این دکمه آن را روشن نمی‌کند. سفارش فقط در BloFin Demo و با قفل یک‌بارمصرف مجاز است؛ اهرم ایزوله 1x و TP/SL اجباری‌اند.</p>
            <p id="blofin-execution-readiness" class="muted" aria-live="polite">در حال بررسی پیش‌نیازهای Demo...</p>
            <button id="blofin-demo-execute-once" type="button" disabled>اجرای دستی یک چرخه نسخه جدید (CHALLENGE_1_V2 Single-Shot)</button>
            <div class="metric" id="blofin-autopilot-panel" style="margin-top:14px;border-color:#2d6470">
              <h3>حالت اتوماتیک · Challenge 1</h3>
              <p class="muted">پس از فعال‌سازی، ربات بازارهای تنظیم‌شده را در هر کندل بسته‌شدهٔ ۵ دقیقه‌ای بررسی می‌کند. ورود همچنان به دو تأیید مستقل AI، مجوز Demo، حد ضرر/سود و کنترل ریسک وابسته است. توقف، چرخه جاری را قطع نمی‌کند و فقط از شروع چرخه‌های بعدی جلوگیری می‌کند. کنترل داشبورد تا راه‌اندازی مجدد همین سرویس معتبر است؛ پس از Restart، مقدار BLOFIN_DEMO_AUTOPILOT_ENABLED در Environment مبنا خواهد بود.</p>
              <div class="grid" style="margin:10px 0">
                <button id="blofin-autopilot-start" type="button" disabled>شروع پایش و معامله اتوماتیک</button>
                <button id="blofin-autopilot-stop" type="button" disabled>توقف چرخه‌های اتوماتیک</button>
              </div>
              <p id="blofin-autopilot-status" class="muted" aria-live="polite">در حال دریافت وضعیت اتوپایلوت...</p>
            </div>
            <div id="blofin-execution-report" class="execution-report" aria-live="polite" style="margin-top:14px">
              <p class="muted">گزارش فارسی اجرای معامله پس از آغاز چرخه در این قسمت نمایش داده می‌شود.</p>
            </div>
            <div class="metric" id="blofin-v2-autopilot-panel" style="margin-top:14px;border-color:#2d6470">
              <h3>اجرای مستقیم V2 · پنج بازار و دو ناظر مستقل</h3>
              <p class="muted">این دکمه مستقیماً چرخهٔ مشترک challenge1_v2_autopilot.run_cycle را در Render اجرا می‌کند. قبل از ارسال درخواست، توکن پنل لازم است. هر سفارش احتمالی فقط به BloFin Demo و با تأیید هر دو ناظر، حد ضرر/سود و سقف چهار پوزیشن می‌رود.</p>
              <button id="blofin-v2-run-once" type="button">اجرای یک چرخه V2 در BloFin Demo</button>
              <p id="blofin-v2-cycle-status" class="muted" aria-live="polite">برای اجرای دستی، توکن پنل را وارد کن.</p>
              <div id="blofin-v2-cycle-history" style="overflow-x:auto"></div>
            </div>
          </div>
          <div class="metric" id="blofin-smoke-test-panel" style="margin-top:16px;border-color:#2d6470">
            <h3>تست ربات در محیط دمو صرافی</h3>
            <p class="muted">راه‌انداز مستقل و بدون سیگنال: خود ربات فقط روی BTC-USDT در BloFin Demo یک سفارش LONG با حداقل حجم، اهرم ایزوله 1x و TP/SL حفاظتی می‌فرستد؛ پس از تأیید Fill، همان پوزیشن را می‌بندد و تخت‌شدن حساب را بررسی می‌کند. این تست به ۱۲ گیت یا Nemotron وابسته نیست و به محیط Live دسترسی ندارد. اگر روی BTC-USDT پوزیشن یا سفارش بازی از قبل وجود داشته باشد، تست بدون تغییر آن متوقف می‌شود.</p>
            <p class="muted">بررسی دستی حساب فقط اطلاعات BTC-USDT را از BloFin Demo با درخواست‌های GET می‌خواند؛ سفارش‌گذاری، لغو سفارش و بستن پوزیشن در این بررسی انجام نمی‌شود.</p>
            <button id="blofin-smoke-test-refresh" type="button">بررسی دستی وضعیت حساب BTC-USDT (فقط‌خواندنی)</button>
            <p id="blofin-smoke-test-readiness" class="muted" aria-live="polite">برای بررسی دستی، دکمهٔ بالا را بزن.</p>
            <button id="blofin-smoke-test-run" type="button" disabled>شروع «تست ربات در محیط دمو صرافی»</button>
            <p id="blofin-smoke-test-status" class="muted" aria-live="polite">برای اجرای تست، توکن پنل را در کادر بالاتر وارد کن.</p>
            <pre id="blofin-smoke-test-report" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre>
          </div>
          <div class="metric" style="margin-top:16px">
            <h3>مانیتور زنده ورود و خروج ربات</h3>
            <p class="muted">زمان تصمیم، ارسال سفارش، Fill واقعی، قیمت ورود، Mark Price، سود/زیان شناور، سفارش‌های حد سود/ضرر، زمان خروج و PnL نهایی از داده‌های حساب Demo نمایش داده می‌شود.</p>
            <button id="blofin-demo-monitor-toggle" type="button">شروع مانیتور زنده هر ۵ ثانیه</button>
            <p id="blofin-monitor-status" class="muted" aria-live="polite">برای مشاهده اطلاعات خصوصی، توکن پنل را در کادر بالا وارد کن.</p>
            <div id="blofin-monitor-view" style="overflow-x:auto"></div>
            <div id="blofin-monitor-events" style="overflow-x:auto"></div>
          </div>
          <script>
          (function(){
            const checkButton=document.getElementById('blofin-demo-check');
            const status=document.getElementById('blofin-demo-status');
            const accountButton=document.getElementById('blofin-demo-account-read');
            const clearButton=document.getElementById('blofin-demo-account-clear');
            const tokenInput=document.getElementById('blofin-dashboard-token');
            const accountStatus=document.getElementById('blofin-demo-account-status');
            const accountView=document.getElementById('blofin-demo-account-view');
            const safety=document.getElementById('blofin-demo-safety');
            const executeButton=document.getElementById('blofin-demo-execute-once');
            const autopilotStartButton=document.getElementById('blofin-autopilot-start');
            const autopilotStopButton=document.getElementById('blofin-autopilot-stop');
            const autopilotStatus=document.getElementById('blofin-autopilot-status');
            const smokeButton=document.getElementById('blofin-smoke-test-run');
            const smokeRefreshButton=document.getElementById('blofin-smoke-test-refresh');
            const smokeReadiness=document.getElementById('blofin-smoke-test-readiness');
            const smokeStatus=document.getElementById('blofin-smoke-test-status');
            const smokeReport=document.getElementById('blofin-smoke-test-report');
            const instrumentSelect=document.getElementById('blofin-instrument');
            const profileSelect=document.getElementById('blofin-engine-profile');
            const instrumentStatus=document.getElementById('blofin-instrument-status');
            const executionReadiness=document.getElementById('blofin-execution-readiness');
            const executionReport=document.getElementById('blofin-execution-report');
            const v2RunButton=document.getElementById('blofin-v2-run-once');
            const v2CycleStatus=document.getElementById('blofin-v2-cycle-status');
            const v2CycleHistory=document.getElementById('blofin-v2-cycle-history');
            const monitorToggle=document.getElementById('blofin-demo-monitor-toggle');
            const monitorStatus=document.getElementById('blofin-monitor-status');
            const monitorView=document.getElementById('blofin-monitor-view');
            const monitorEvents=document.getElementById('blofin-monitor-events');
            if(!checkButton||!status||!accountButton||!clearButton||!tokenInput||!accountStatus||!accountView||!safety||!executeButton||!autopilotStartButton||!autopilotStopButton||!autopilotStatus||!smokeButton||!smokeRefreshButton||!smokeReadiness||!smokeStatus||!smokeReport||!instrumentSelect||!profileSelect||!instrumentStatus||!executionReadiness||!executionReport||!v2RunButton||!v2CycleStatus||!v2CycleHistory||!monitorToggle||!monitorStatus||!monitorView||!monitorEvents)return;

            checkButton.addEventListener('click',async function(){
              checkButton.disabled=true;
              status.textContent='در حال بررسی اتصال امن به BloFin Demo...';
              try{
                const response=await fetch('/api/blofin/demo/status',{cache:'no-store'});
                if(!response.ok)throw new Error('HTTP '+response.status);
                const result=await response.json();
                const messages={
                  CONNECTED_READ_ONLY:'اتصال Demo برقرار است؛ درخواست‌های این مسیر فقط GET هستند. مجوز واقعی کلید: '+(result.api_key_permission||'UNKNOWN')+'.',
                  NOT_CONFIGURED:'متغیرهای API در Environment همین سرویس تنظیم نشده‌اند.',
                  AUTH_FAILED:'احراز هویت ناموفق بود؛ مقادیر و مجوز READ را بررسی کن.',
                  CONNECTION_FAILED:'اتصال به BloFin Demo برقرار نشد؛ شبکه یا محدودیت IP را بررسی کن.',
                  UNKNOWN:'نتیجه آزمون نامشخص است؛ آزمون را بعداً تکرار کن.'
                };
                status.textContent=messages[result.status]||messages.UNKNOWN;
                safety.textContent='محیط: Demo · عملیات این مسیر: فقط خواندن (GET) · مجوز واقعی کلید: '+(result.api_key_permission||'نامشخص')+' · ارسال سفارش از این مسیر: غیرفعال';
              }catch(error){
                status.textContent='دریافت نتیجه آزمون ممکن نشد؛ اتصال داشبورد را بررسی کن.';
              }finally{
                checkButton.disabled=false;
              }
            });

            function addText(parent,tag,value){
              const node=document.createElement(tag);
              node.textContent=value===null||value===undefined||value===''?'—':String(value);
              parent.appendChild(node);
              return node;
            }
            function renderAccount(result){
              accountView.replaceChildren();
              const balance=result.balance||{};
              const summary=document.createElement('div');
              summary.className='grid';
              [['ارزش کل حساب (USD)',balance.totalEquity],['ارزش مارجین ایزوله (USD)',balance.isolatedEquity],
               ['تعداد پوزیشن‌ها',(result.positions||[]).length],['سفارش‌های باز',(result.open_orders||[]).length],
               ['مجوز واقعی کلید API',result.api_key_permission||'UNKNOWN'],['محدودهٔ این مسیر','GET فقط؛ بدون ارسال سفارش']]
                .forEach(([label,value])=>{
                  const metric=document.createElement('div');
                  metric.className='metric';
                  addText(metric,'span',label);
                  addText(metric,'strong',value);
                  summary.appendChild(metric);
                });
              accountView.appendChild(summary);

              function table(title,rows,columns){
                const heading=document.createElement('h3');
                heading.textContent=title;
                accountView.appendChild(heading);
                if(!rows||!rows.length){
                  addText(accountView,'p','موردی ثبت نشده است.');
                  return;
                }
                const wrapper=document.createElement('div');
                wrapper.style.overflowX='auto';
                const tbl=document.createElement('table');
                const head=document.createElement('tr');
                columns.forEach(([key,label])=>addText(head,'th',label));
                tbl.appendChild(head);
                rows.forEach(row=>{
                  const tr=document.createElement('tr');
                  columns.forEach(([key])=>addText(tr,'td',row[key]));
                  tbl.appendChild(tr);
                });
                wrapper.appendChild(tbl);
                accountView.appendChild(wrapper);
              }
              table('دارایی‌های Futures',balance.details||[],[
                ['currency','ارز'],['equity','Equity'],['balance','Balance'],
                ['available','Available'],['frozen','Frozen'],['equityUsd','ارزش دلاری']
              ]);
              table('پوزیشن‌های باز',result.positions||[],[
                ['instId','نماد'],['positionSide','جهت'],['positions','حجم'],
                ['averagePrice','قیمت ورود'],['markPrice','Mark Price'],
                ['unrealizedPnl','سود/زیان شناور'],['leverage','اهرم'],['liquidationPrice','قیمت لیکویید']
              ]);
              table('سفارش‌های باز',result.open_orders||[],[
                ['instId','نماد'],['side','سمت'],['orderType','نوع'],
                ['price','قیمت'],['size','حجم'],['filledSize','پرشده'],['state','وضعیت']
              ]);
            }

            accountButton.addEventListener('click',async function(){
              const token=tokenInput.value.trim();
              if(!token){
                accountStatus.textContent='ابتدا توکن پنل را وارد کن.';
                return;
              }
              accountButton.disabled=true;
              accountStatus.textContent='در حال خواندن امن اطلاعات حساب...';
              accountView.replaceChildren();
              try{
                const response=await fetch('/api/blofin/demo/account',{
                  method:'GET',cache:'no-store',headers:{'Authorization':'Bearer '+token}
                });
                const result=await response.json();
                if(!response.ok){
                  accountStatus.textContent=result.message_fa||'خواندن اطلاعات حساب ناموفق بود.';
                  return;
                }
                if(result.status!=='CONNECTED_READ_ONLY'){
                  accountStatus.textContent=result.message_fa||'پاسخ حساب معتبر نیست.';
                  return;
                }
                renderAccount(result);
                accountStatus.textContent='اطلاعات حساب Demo دریافت شد. مجوز واقعی کلید: '+(result.api_key_permission||'UNKNOWN')+'. زمان دریافت: '+new Date().toLocaleString();
                safety.textContent='محیط: Demo · عملیات این مسیر: فقط خواندن (GET) · مجوز واقعی کلید: '+(result.api_key_permission||'نامشخص')+' · ارسال سفارش از این مسیر: غیرفعال';
              }catch(error){
                accountStatus.textContent='ارتباط با پنل حساب برقرار نشد.';
              }finally{
                accountButton.disabled=false;
              }
            });
            let monitorTimer=null;
            let monitorActive=false;
            let lastJobId=null;
            function displayMonitorValue(value){
              if(value===null||value===undefined||value==='')return 'داده موجود نیست';
              if(typeof value==='object')return JSON.stringify(value);
              return String(value);
            }
            function addCell(parent,tag,value){
              const node=document.createElement(tag);
              node.textContent=displayMonitorValue(value);
              parent.appendChild(node);
              return node;
            }
            function formatTime(raw){
              if(raw===null||raw===undefined||raw==='')return 'زمان ثبت نشده';
              let date;
              if(typeof raw==='number'||/^\\d+$/.test(String(raw))){
                const n=Number(raw);date=new Date(n>100000000000?n:n*1000);
              }else date=new Date(raw);
              return Number.isNaN(date.getTime())?String(raw):date.toLocaleString();
            }
            function renderEvents(title,items,fields,emptyText){
              const section=document.createElement('section');
              addCell(section,'h4',title);
              if(!Array.isArray(items)||!items.length){
                addCell(section,'p',emptyText||'در بازهٔ دریافت‌شده رویدادی از صرافی گزارش نشده است.');
                return section;
              }
              const wrapper=document.createElement('div');wrapper.style.overflowX='auto';
              const table=document.createElement('table'),head=document.createElement('tr');
              fields.forEach(([,label])=>addCell(head,'th',label));table.appendChild(head);
              items.forEach(item=>{
                const row=document.createElement('tr');
                fields.forEach(([key])=>{
                  let value=item&&item[key];
                  if(/time|timestamp|ts|occurred_at|createTime|updateTime/i.test(key))value=formatTime(value);
                  addCell(row,'td',value);
                });
                table.appendChild(row);
              });
              wrapper.appendChild(table);section.appendChild(wrapper);return section;
            }
            function renderLiveMonitor(snapshot){
              monitorView.replaceChildren();monitorEvents.replaceChildren();
              const m=snapshot.market||{},account=snapshot.account||{};
              const accountPositions=Array.isArray(snapshot.positions)?snapshot.positions:[];
              const positions=Array.isArray(snapshot.positions_for_instrument)
                ?snapshot.positions_for_instrument
                :accountPositions.filter(p=>String(p&&p.instId||'').toUpperCase()===String(snapshot.instrument_id||'').toUpperCase());
              const active=positions.filter(p=>{
                const quantity=Number(p&&p.positions);
                return Number.isFinite(quantity)&&Math.abs(quantity)>0;
              });
              // Never join an arbitrary account-wide position to the selected market ticker.
              const position=active.length===1?active[0]:{},detail=snapshot.order_detail||{};
              const localOrder=snapshot.latest_local_order||{};
              const fills=Array.isArray(snapshot.fills)?snapshot.fills:[];
              const closed=Array.isArray(snapshot.closed_positions)?snapshot.closed_positions:[];
              const events=Array.isArray(snapshot.instrument_execution_events)?snapshot.instrument_execution_events:[];
              const accountOpenPositions=accountPositions.filter(p=>{
                const quantity=Number(p&&p.positions);
                return Number.isFinite(quantity)&&Math.abs(quantity)>0;
              });
              const instrumentOpenOrders=Array.isArray(snapshot.open_orders_for_instrument)
                ?snapshot.open_orders_for_instrument
                :(snapshot.open_orders||[]).filter(x=>String(x&&x.instId||'').toUpperCase()===String(snapshot.instrument_id||'').toUpperCase());
              const instrumentOpenTpsl=Array.isArray(snapshot.open_tpsl_orders_for_instrument)
                ?snapshot.open_tpsl_orders_for_instrument
                :(snapshot.open_tpsl_orders||[]).filter(x=>String(x&&x.instId||'').toUpperCase()===String(snapshot.instrument_id||'').toUpperCase());
              const usdt=(Array.isArray(account.details)?account.details:[]).find(x=>String(x.currency||'').toUpperCase()==='USDT')||{};
              const lastEvent=type=>events.filter(x=>String(x.event_type||'').toUpperCase()===type).slice(-1)[0]||{};
              const reserved=lastEvent('RESERVED'),accepted=lastEvent('ACCEPTED'),filledEvent=lastEvent('FILLED');
              const latestFill=fills[0]||{},latestClosed=closed[0]||{};
              const summary=document.createElement('div');summary.className='grid';
              const metrics=[
                ['آخرین قیمت '+(snapshot.instrument_id||'قرارداد انتخابی'),m.last],['Mark Price واقعی همان قرارداد',m.markPrice],
                ['Index Price همان قرارداد',m.indexPrice],
                ['وضعیت پوزیشن '+(snapshot.instrument_id||''),active.length===1?'پوزیشن باز':(active.length>1?'چند پوزیشن باز؛ جزئیات جداگانه':'بدون پوزیشن باز برای این قرارداد')],
                ['جهت پوزیشن',active.length===1?position.positionSide:null],
                ['میانگین قیمت ورود',active.length===1?position.averagePrice:null],
                ['حجم باز (قرارداد)',active.length===1?position.positions:null],['حجم قابل‌بستن (قرارداد)',active.length===1?position.availablePositions:null],
                ['اهرم',active.length===1?position.leverage:null],
                ['سود/زیان شناور USDT',active.length===1?position.unrealizedPnl:null],
                ['Equity کل حساب (USD)',account.totalEquity],['Equity ایزوله (USD)',account.isolatedEquity],
                ['موجودی USDT',usdt.balance],['Equity موجودی USDT',usdt.equity],
                ['Available USDT',usdt.available],['Available Equity USDT',usdt.availableEquity],
                ['Frozen USDT',usdt.frozen],['پوزیشن‌های باز این قرارداد',active.length],
                ['پوزیشن‌های باز کل حساب',accountOpenPositions.length],
                ['سفارش‌های باز این قرارداد',instrumentOpenOrders.length],
                ['سفارش‌های باز کل حساب',Array.isArray(snapshot.open_orders)?snapshot.open_orders.length:0],
                ['TP/SL فعال این قرارداد',instrumentOpenTpsl.length],
                ['TP/SL فعال کل حساب',Array.isArray(snapshot.open_tpsl_orders)?snapshot.open_tpsl_orders.length:0],
                ['مجوز واقعی کلید API',snapshot.api_key_permission||'UNKNOWN'],
                ['آخرین به‌روزرسانی',formatTime(snapshot.updated_at_utc)]
              ];
              metrics.forEach(([label,value])=>{
                const card=document.createElement('div');card.className='metric';
                addCell(card,'span',label);addCell(card,'strong',value);summary.appendChild(card);
              });
              monitorView.appendChild(summary);
              const timing=document.createElement('section');timing.className='metric';timing.style.marginTop='12px';
              addCell(timing,'h4','زمان‌بندی واقعی چرخه و معامله');
              const timingGrid=document.createElement('div');timingGrid.className='grid';
              [
                ['زمان رزرو/تصمیم اجرایی ربات',reserved.occurred_at],
                ['زمان پذیرش سفارش در صرافی',detail.createTime??accepted.occurred_at],
                ['زمان آخرین Fill واقعی',latestFill.ts??(filledEvent.occurred_at?Number(filledEvent.occurred_at)*1000:null)],
                ['زمان خروج آخرین پوزیشن بسته',latestClosed.updateTime],
                ['زمان آخرین تغییر سفارش محلی',localOrder.updated_at?Number(localOrder.updated_at)*1000:null],
                ['وضعیت سفارش محلی',localOrder.status],
                ['شناسه سفارش صرافی',detail.orderId||localOrder.exchange_order_id],
                ['شناسه سفارش ربات',localOrder.client_order_id]
              ].forEach(([label,value])=>{
                const card=document.createElement('div');card.className='metric';
                addCell(card,'span',label);
                addCell(card,'strong',/زمان/.test(label)?formatTime(value):value);
                timingGrid.appendChild(card);
              });
              timing.appendChild(timingGrid);monitorView.appendChild(timing);
              if(snapshot.data_completeness==='PARTIAL'){
                const warning=document.createElement('section');warning.className='metric';warning.style.marginTop='12px';
                addCell(warning,'strong','هشدار: بخشی از داده‌های مانیتور دریافت نشد');
                addCell(warning,'p','مقادیر ناموجود با صفر جایگزین نشده‌اند؛ منابع ناموفق در جدول زیر مشخص شده‌اند.');
                monitorEvents.appendChild(warning);
                monitorEvents.appendChild(renderEvents('منابعی که در این نوبت خطا داشتند',snapshot.read_errors||[],[
                  ['endpoint','منبع داده'],['error_type','نوع خطا']
                ]));
              }else{
                const ok=document.createElement('p');ok.className='muted';
                ok.textContent='پوشش منابع مانیتور: همهٔ endpointهای درخواستی پاسخ داده‌اند.';
                monitorEvents.appendChild(ok);
              }
              monitorEvents.appendChild(renderEvents('پوزیشن‌های باز برای '+(snapshot.instrument_id||'قرارداد انتخابی'),active,[
                ['positionId','شناسه پوزیشن'],['instId','نماد'],['positionSide','جهت'],
                ['positions','حجم (قرارداد)'],['averagePrice','قیمت ورود'],['markPrice','Mark Price'],
                ['unrealizedPnl','PnL شناور'],['leverage','اهرم']
              ],'برای قرارداد انتخاب‌شده پوزیشن بازی گزارش نشد.'));
              monitorEvents.appendChild(renderEvents('Fillهای واقعی صرافی · قیمت، حجم قرارداد، کارمزد و زمان',fills,[
                ['tradeId','شناسه Fill'],['orderId','شناسه سفارش'],['side','خرید/فروش'],
                ['positionSide','جهت پوزیشن'],['fillPrice','قیمت Fill'],['fillSize','حجم Fill (قرارداد)'],
                ['fillPnl','PnL تحقق‌یافته'],['fee','کارمزد'],['ts','زمان Fill']
              ],'BloFin در سوابق دریافتی Fill ثبت‌شده‌ای برنگرداند.'));
              monitorEvents.appendChild(renderEvents('پوزیشن‌های بسته‌شده · ورود، خروج و PnL نهایی',closed,[
                ['historyId','شناسه سابقه'],['side','سمت'],['positionSide','جهت پوزیشن'],
                ['openAveragePrice','میانگین ورود'],['closeAveragePrice','میانگین خروج'],
                ['closePositions','حجم بسته‌شده (واحد گزارش‌شده BloFin)'],['realizedPnl','PnL نهایی'],
                ['realizedPnlRatio','درصد PnL'],['fee','کارمزد'],
                ['createTime','زمان بازشدن'],['updateTime','زمان بسته‌شدن/آخرین تغییر']
              ],'در تاریخچهٔ دریافتی، پوزیشن بسته‌شده‌ای برای '+(snapshot.instrument_id||'قرارداد انتخابی')+' گزارش نشد.'));
              monitorEvents.appendChild(renderEvents('سفارش‌های تکمیل‌شده و تاریخچه سفارش',snapshot.completed_orders||[],[
                ['orderId','شناسه سفارش'],['orderCategory','دسته سفارش'],['side','سمت'],
                ['orderType','نوع سفارش'],['state','وضعیت'],['price','قیمت درخواستی'],
                ['averagePrice','میانگین اجرای واقعی'],['size','حجم درخواست‌شده (قرارداد)'],
                ['filledSize','حجم اجراشده (قرارداد)'],['pnl','PnL'],['fee','کارمزد'],
                ['createTime','زمان ثبت'],['updateTime','زمان تکمیل/تغییر']
              ],'در تاریخچهٔ دریافت‌شده سفارش تکمیل‌شده‌ای گزارش نشد.'));
              monitorEvents.appendChild(renderEvents('سفارش‌های باز عادی برای '+(snapshot.instrument_id||'قرارداد انتخابی'),instrumentOpenOrders,[
                ['orderId','شناسه سفارش'],['side','سمت'],['orderType','نوع'],
                ['orderCategory','دسته'],['price','قیمت'],['size','حجم'],
                ['filledSize','پرشده'],['state','وضعیت'],['createTime','زمان ثبت'],
                ['attachAlgoOrders','حد سود/ضرر متصل']
              ],'در حال حاضر سفارش عادی بازی برای '+(snapshot.instrument_id||'قرارداد انتخابی')+' گزارش نشد.'));
              monitorEvents.appendChild(renderEvents('حد سود و حد ضرر فعال برای '+(snapshot.instrument_id||'قرارداد انتخابی'),instrumentOpenTpsl,[
                ['tpslId','شناسه TP/SL'],['side','سمت'],['positionSide','جهت پوزیشن'],
                ['tpTriggerPrice','قیمت فعال‌سازی TP'],['tpOrderPrice','قیمت اجرای TP'],
                ['tpTriggerPriceType','مبنای TP'],['slTriggerPrice','قیمت فعال‌سازی SL'],
                ['slOrderPrice','قیمت اجرای SL'],['slTriggerPriceType','مبنای SL'],
                ['size','حجم'],['actualSize','حجم مؤثر'],['state','وضعیت'],['createTime','زمان ثبت']
              ],'سفارشی از نوع TP/SL فعال برای '+(snapshot.instrument_id||'قرارداد انتخابی')+' گزارش نشد.'));
              monitorEvents.appendChild(renderEvents('تاریخچه سفارش‌های حد سود و حد ضرر',snapshot.tpsl_orders_history||[],[
                ['tpslId','شناسه TP/SL'],['side','سمت'],['tpTriggerPrice','TP'],
                ['tpOrderPrice','قیمت اجرای TP'],['slTriggerPrice','SL'],
                ['slOrderPrice','قیمت اجرای SL'],['state','وضعیت'],
                ['createTime','زمان ثبت'],['updateTime','آخرین تغییر']
              ],'در تاریخچهٔ دریافتی، سفارش TP/SL ثبت‌شده‌ای گزارش نشد.'));
              monitorEvents.appendChild(renderEvents('رویدادهای ثبت‌شدهٔ ربات برای '+(snapshot.instrument_id||'قرارداد انتخابی'),events,[
                ['sequence','ترتیب'],['client_order_id','شناسه سفارش ربات'],
                ['event_type','رویداد'],['occurred_at','زمان ثبت'],['payload','جزئیات']
              ],'هنوز رویدادی در دفتر ثبت داخلی ربات ذخیره نشده است.'));
            }
async function loadBlofinInstruments(){
              instrumentSelect.disabled=true;
              instrumentStatus.textContent='در حال دریافت قراردادهای زنده USDT Perpetual با مجوز Full...';
              try{
                const response=await fetch('/api/blofin/demo/instruments',{cache:'no-store'});
                const payload=await response.json();
                if(!response.ok||payload.status!=='PASS'||!Array.isArray(payload.instruments)||!payload.instruments.length)
                  throw new Error(payload.message_fa||'فهرست قرارداد مجاز در دسترس نیست.');
                const previous=instrumentSelect.value||'BTC-USDT';
                instrumentSelect.replaceChildren();
                payload.instruments.forEach(item=>{
                  const option=document.createElement('option');
                  option.value=item.inst_id;
                  option.textContent=item.inst_id+' · '+item.base_currency+'/USDT · Full · min '+item.min_size;
                  instrumentSelect.appendChild(option);
                });
                instrumentSelect.value=payload.instruments.some(x=>x.inst_id===previous)?previous:payload.instruments[0].inst_id;
                instrumentSelect.disabled=false;
                instrumentStatus.textContent=payload.count+' قرارداد زنده مجاز دریافت شد؛ انتخاب فعلی: '+instrumentSelect.value;
              }catch(error){
                instrumentSelect.replaceChildren();
                const option=document.createElement('option');option.value='';option.textContent='فهرست قراردادها دریافت نشد';
                instrumentSelect.appendChild(option);
                instrumentStatus.textContent='انتخاب بازار تا دریافت فهرست معتبر مسدود است: '+String(error.message||error);
              }
            }
            async function refreshExecutionReadiness(){
              executeButton.disabled=true;
              try{
                const response=await fetch('/api/blofin/demo/execution-readiness',{cache:'no-store'});
                const result=await response.json();
                executionReadiness.textContent=result.message_fa||'وضعیت پیش‌نیازهای Demo مشخص نیست.';
                const lock=result.single_shot_lock||{};
                const lockReadable=typeof lock.locked==='boolean'&&Number.isFinite(Number(lock.consumed_orders));
                const lockLabel=lockReadable?('قفل یک‌بارمصرف: '+Number(lock.consumed_orders)+'/1 · '+(lock.locked?'مصرف‌شده و بسته':'باز')):'وضعیت قفل یک‌بارمصرف قابل خواندن نیست';
                executionReadiness.textContent+=' · '+lockLabel;
                if(result.status==='READY'&&result.v2_orchestration_enabled===true&&result.single_shot_write_enabled===true&&result.autopilot_enabled===false&&lockReadable&&lock.locked===false&&Number(lock.consumed_orders)===0&&instrumentSelect.value){
                  executeButton.disabled=false;
                  executionReadiness.textContent+=' · آماده ثبت یک چرخه دستی V2 در BloFin Demo است؛ این دکمه AutoPilot را روشن نمی‌کند.';
                }
                refreshAutopilotStatus();
              }catch(error){executionReadiness.textContent='بررسی پیش‌نیازهای سفارش Demo ممکن نشد.';}
            }
            async function refreshAutopilotStatus(){
              autopilotStartButton.disabled=true;
              autopilotStopButton.disabled=true;
              try{
                const response=await fetch('/api/blofin/demo/autopilot/status',{cache:'no-store'});
                const state=await response.json();
                const enabled=state.enabled===true;
                const ready=state.readiness&&state.readiness.status==='READY';
                autopilotStartButton.disabled=!ready||enabled;
                autopilotStopButton.disabled=!enabled;
                const runtime=state.runtime_controlled_by_dashboard?'کنترل‌شده از همین داشبورد':'بر اساس تنظیم Render';
                const message=state.last_message_fa|| (enabled?'اتوپایلوت فعال است.':'اتوپایلوت خاموش است.');
                const blockers=(state.readiness&&Array.isArray(state.readiness.blockers_fa))
                  ?state.readiness.blockers_fa:[];
                autopilotStatus.textContent=(enabled?'وضعیت: فعال':'وضعیت: خاموش')
                  +' · '+runtime+' · '+message
                  +(blockers.length?' · موانع: '+blockers.join('؛ '):'');
              }catch(error){
                autopilotStatus.textContent='وضعیت اتوپایلوت دریافت نشد؛ شروع یا توقف تا بازیابی وضعیت غیرفعال است.';
              }
            }
            async function controlAutopilot(action){
              const token=tokenInput.value.trim();
              if(!token){autopilotStatus.textContent='ابتدا توکن پنل را در کادر بالاتر وارد کن.';return;}
              const prompt=action==='start'
                ?'پایش اتوماتیک Challenge 1 در BloFin DEMO فعال شود؟ ربات فقط در Demo و با تمام گیت‌های دو ناظر، SL/TP و کنترل ریسک مجاز است.'
                :'چرخه‌های جدید اتوماتیک متوقف شوند؟ چرخه در حال اجرا و مدیریت پوزیشن باز قطع نخواهد شد.';
              if(!window.confirm(prompt))return;
              autopilotStartButton.disabled=true;
              autopilotStopButton.disabled=true;
              autopilotStatus.textContent='درخواست '+(action==='start'?'شروع':'توقف')+' اتوپایلوت به سرویس Staging ارسال شد...';
              try{
                const response=await fetch('/api/blofin/demo/autopilot/control',{
                  method:'POST',cache:'no-store',
                  headers:{'Authorization':'Bearer '+token,'Content-Type':'application/x-www-form-urlencoded'},
                  body:new URLSearchParams({confirm:'1',action})
                });
                const result=await response.json();
                autopilotStatus.textContent=result.message_fa||'پاسخ کنترل اتوپایلوت دریافت شد.';
                await refreshAutopilotStatus();
                if(!response.ok)autopilotStatus.textContent=(result.message_fa||'درخواست پذیرفته نشد.')
                  +(Array.isArray(result.blockers_fa)?' · '+result.blockers_fa.join('؛ '):'');
              }catch(error){
                autopilotStatus.textContent='نتیجه درخواست کنترل اتوپایلوت نامشخص است؛ وضعیت را دوباره دریافت کن.';
                await refreshAutopilotStatus();
              }
            }
            autopilotStartButton.addEventListener('click',()=>controlAutopilot('start'));
            autopilotStopButton.addEventListener('click',()=>controlAutopilot('stop'));
            instrumentSelect.addEventListener('change',()=>{
              instrumentStatus.textContent='قرارداد انتخابی: '+instrumentSelect.value+' · جهت خرید/فروش از سیگنال زنده می‌آید.';
            });
            profileSelect.addEventListener('change',()=>{
              executionReadiness.textContent='استراتژی معامله Demo: '+profileSelect.options[profileSelect.selectedIndex].text+' · کنترل‌های ضروری پیش از سفارش بررسی می‌شوند.';
            });
            async function fetchLiveMonitor(){
              const token=tokenInput.value.trim();
              if(!token){monitorStatus.textContent='ابتدا توکن پنل را در کادر بالا وارد کن.';monitorActive=false;monitorToggle.textContent='شروع مانیتور زنده';return;}
              try{
                const response=await fetch('/api/blofin/demo/live-monitor?instrument='+encodeURIComponent(instrumentSelect.value||'BTC-USDT'),{
                  cache:'no-store',headers:{'Authorization':'Bearer '+token}
                });
                const snapshot=await response.json();
                if(!response.ok||snapshot.status!=='CONNECTED_READ_ONLY')throw new Error(snapshot.message_fa||'Monitor unavailable');
                renderLiveMonitor(snapshot);
                monitorStatus.textContent='اتصال مانیتور برقرار است · به‌روزرسانی هر ۵ ثانیه · '+formatTime(snapshot.updated_at_utc);
              }catch(error){monitorStatus.textContent='خواندن زنده حساب Demo ناموفق بود؛ اتصال READ و توکن پنل را بررسی کن.';}
              if(monitorActive)monitorTimer=setTimeout(fetchLiveMonitor,5000);
            }
            monitorToggle.addEventListener('click',function(){
              monitorActive=!monitorActive;
              if(monitorActive){monitorToggle.textContent='توقف مانیتور زنده';fetchLiveMonitor();}
              else{clearTimeout(monitorTimer);monitorStatus.textContent='مانیتور زنده متوقف شد.';monitorToggle.textContent='شروع مانیتور زنده هر ۵ ثانیه';}
            });
            async function refreshSmokeTestReadiness(manual=false){
               smokeButton.disabled=true;
               if(manual){
                 smokeRefreshButton.disabled=true;
                 smokeRefreshButton.textContent='در حال بررسی فقط‌خواندنی حساب...';
               }
               try{
                 const url='/api/blofin/demo/smoke-test-readiness'+(manual?'?refresh=1':'');
                 const response=await fetch(url,{cache:'no-store'});
                 const result=await response.json();
                 let details='';
                 if(result.read_only_account_check_performed){
                   const positions=result.smoke_test_open_position_count;
                   const orders=result.smoke_test_open_order_count;
                   details='\\nوضعیت بررسی: '+(result.read_only_account_status||'نامشخص')
                     +' | کامل‌بودن داده: '+(result.read_only_account_data_completeness||'نامشخص')
                     +' | پوزیشن باز BTC-USDT: '+(positions===null||positions===undefined?'نامشخص':positions)
                     +' | سفارش باز BTC-USDT: '+(orders===null||orders===undefined?'نامشخص':orders)
                     +' | سفارش ارسال‌شده در این بررسی: خیر';
                 }
                 smokeReadiness.textContent=(result.message_fa||'وضعیت تست مستقل نامشخص است.')+details;
                 smokeButton.disabled=result.status!=='READY';
               }catch(error){
                 smokeReadiness.textContent='بررسی پیش‌نیازهای تست مستقل ناموفق بود؛ ورود مسدود می‌ماند.';
                 smokeButton.disabled=true;
               }finally{
                 if(manual){
                   smokeRefreshButton.disabled=false;
                   smokeRefreshButton.textContent='بررسی دستی وضعیت حساب BTC-USDT (فقط‌خواندنی)';
                 }
               }
             }
             smokeRefreshButton.addEventListener('click',function(){
               refreshSmokeTestReadiness(true);
             });
             async function pollSmokeTestJob(jobId,token,attempt){
               try{
                 const response=await fetch('/api/blofin/demo/execution-jobs/'+jobId,{
                   cache:'no-store',headers:{'Authorization':'Bearer '+token}
                 });
                 const job=await response.json();
                 smokeStatus.textContent=job.message_fa||job.stage||'تست در حال اجراست.';
                 smokeReport.textContent=JSON.stringify({
                   test_name:'تست ربات در محیط دمو صرافی',
                   stage:job.stage, status:job.status, events:job.events||[],
                   result:job.result||null
                 },null,2);
                 if(job.status==='completed'||job.status==='failed'){
                   await refreshSmokeTestReadiness();
                   return;
                 }
               }catch(error){
                 smokeStatus.textContent='وضعیت همان تست در حال بازیابی است؛ تست جدید را اجرا نکن.';
               }
               if(attempt<90)setTimeout(()=>pollSmokeTestJob(jobId,token,attempt+1),2000);
               else smokeStatus.textContent='مهلت نمایش تمام شد؛ همان شناسه را دنبال کن و تست تازه نفرست: '+jobId;
             }
             smokeButton.addEventListener('click',async function(){
               const token=tokenInput.value.trim();
               if(!token){smokeStatus.textContent='ابتدا توکن پنل را در کادر بالاتر وارد کن.';return;}
               if(!window.confirm('ربات خودش یک معامله LONG با حداقل حجم در BloFin DEMO باز و همان پوزیشن را می‌بندد. هیچ سیگنال یا تحلیل بازار استفاده نمی‌شود. پوزیشن یا سفارش باز BTC-USDT باعث توقف تست خواهد شد. حساب Live انتخاب نشده است. ادامه؟'))return;
               smokeButton.disabled=true;
               smokeReport.textContent='';
               smokeStatus.textContent='درخواست تست مستقل به سرویس ربات ارسال می‌شود...';
               try{
                 const response=await fetch('/api/blofin/demo/smoke-test',{
                   method:'POST',cache:'no-store',
                   headers:{'Authorization':'Bearer '+token,'Content-Type':'application/x-www-form-urlencoded'},
                   body:new URLSearchParams({confirm:'1'})
                 });
                 const started=await response.json();
                 if(!response.ok||!started.job_id){
                   smokeStatus.textContent=started.message_fa||'درخواست تست پذیرفته نشد؛ پیش‌نیازها را بررسی کن.';
                   await refreshSmokeTestReadiness();
                   return;
                 }
                 smokeStatus.textContent='تست توسط ربات آغاز شد. شناسه: '+started.job_id;
                 pollSmokeTestJob(started.job_id,token,0);
               }catch(error){
                 smokeStatus.textContent='نتیجه درخواست نامشخص است؛ قبل از تلاش مجدد، وضعیت حساب Demo را بررسی کن.';
               }
             });

             function renderExecutionJob(job){
              const report=job&&job.report_fa?job.report_fa:{};
              executionReport.replaceChildren();
              function node(parent,tag,value,className){
                const el=document.createElement(tag);
                if(className)el.className=className;
                el.textContent=value===null||value===undefined||value===''?'—':String(value);
                parent.appendChild(el);
                return el;
              }
              function section(title,description){
                const box=document.createElement('section');
                box.className='metric';
                box.style.marginTop='12px';
                node(box,'h4',title);
                if(description)node(box,'p',description,'muted');
                executionReport.appendChild(box);
                return box;
              }
              function metricGrid(parent,items){
                const grid=document.createElement('div');
                grid.className='grid';
                items.forEach(([label,value])=>{
                  const card=document.createElement('div');
                  card.className='metric';
                  node(card,'span',label);
                  node(card,'strong',value);
                  grid.appendChild(card);
                });
                parent.appendChild(grid);
              }
              function makeTable(parent,title,rows,columns,emptyText){
                const box=section(title);
                if(!Array.isArray(rows)||!rows.length){
                  node(box,'p',emptyText||'موردی برای نمایش ثبت نشده است.','muted');
                  return;
                }
                const wrapper=document.createElement('div');
                wrapper.style.overflowX='auto';
                const table=document.createElement('table');
                const head=document.createElement('tr');
                columns.forEach(([,label])=>node(head,'th',label));
                table.appendChild(head);
                rows.forEach(item=>{
                  const tr=document.createElement('tr');
                  columns.forEach(([key])=>{
                    let value=item&&item[key];
                    if(/time|timestamp|confirmed_at/i.test(key))value=formatTime(value);
                    node(tr,'td',value);
                  });
                  table.appendChild(tr);
                });
                wrapper.appendChild(table);
                box.appendChild(wrapper);
              }
              if(!report||!Object.keys(report).length){
                node(executionReport,'p',(job&&job.message_fa)||'گزارش فارسی هنوز از سرور دریافت نشده است.','muted');
                return;
              }
              node(executionReport,'h3',report.title_fa||'گزارش فارسی اجرای معامله');
              node(executionReport,'p',report.reason_fa||'دلیل نهایی هنوز ثبت نشده است.','muted');
              const summary=section('خلاصه تصمیم و وضعیت چرخه');
              metricGrid(summary,[
                ['وضعیت',report.status_fa],
                ['قرارداد',report.instrument_id],
                ['ربات',report.execution_profile_fa||report.execution_profile],
                ['مرحله فعلی',report.stage_fa],
                ['جهت تحلیل',report.signal_fa],
                ['سفارش ارسال شد؟',report.orders_submitted===true?'بله، در Demo':report.orders_submitted==='UNKNOWN'?'وضعیت نامشخص':'خیر'],
                ['معامله واقعاً پر شد؟',report.trade_executed===true?'بله':'خیر / هنوز تأیید نشده'],
                ['وضعیت ۱۲ فیلتر',report.gate_summary_fa]
              ]);
              const timing=section('زمان‌بندی دقیق تصمیم و معامله');
              metricGrid(timing,[
                ['زمان تحلیل',formatTime(report.analysis_time_utc)],
                ['زمان آخرین کندل کامل',formatTime(report.last_completed_candle_time_utc)],
                ['قیمت بسته‌شدن کندل',report.last_candle_close],
                ['زمان آخرین رویداد',formatTime(report.decision_time_utc)],
                ['زمان ارسال سفارش',formatTime(report.submitted_at_utc)],
                ['زمان تأیید Fill',formatTime(report.filled_at_utc)]
              ]);
              if(report.wave_analysis_applicable!==false){
                const wave=report.elliott_wave||{};
                const waveBox=section('انتخاب ساختار موج الیوت در لحظه تحلیل',
                  'شمارش فقط از کندل‌های بسته‌شدهٔ تأییدشده ساخته می‌شود؛ موج بعدی سناریو است، نه پیش‌بینی قطعی.');
                metricGrid(waveBox,[
                  ['وضعیت شمارش',wave.status_fa],
                  ['ساختار شناسایی‌شده',wave.pattern_fa],
                  ['مرحله موج',wave.phase_fa],
                  ['سناریوی موج منتخب',wave.selected_wave_fa],
                  ['مقایسه با جهت معامله',wave.direction_alignment_fa],
                  ['سطح ابطال شمارش',wave.invalidation_price??wave.invalidation_price_note_fa],
                  ['آستانه تأیید پیوت (%)',wave.pivot_reversal_threshold_pct??wave.pivot_reversal_threshold_note_fa],
                  ['پیوت‌های تأییدشده',wave.confirmed_pivot_count],
                  ['تایم‌فریم ساختار',wave.timeframe_fa||wave.timeframe]
                ]);
                node(waveBox,'p',wave.source_fa||'منبع: کندل‌های بسته‌شدهٔ BloFin Demo','muted');
                node(waveBox,'p',wave.confidence_note_fa||'موج الیوت به‌تنهایی مجوز ورود صادر نمی‌کند.','muted');
                const exchangeLinks=document.createElement('div');
                exchangeLinks.style.display='flex';
                exchangeLinks.style.flexWrap='wrap';
                exchangeLinks.style.gap='8px';
                exchangeLinks.style.marginTop='10px';
                const selectedInstrument=report.instrument_id||instrumentSelect.value||'BTC-USDT';
                const blofinMarketLinks=[
                  {label:'بازار '+selectedInstrument+' Perpetual در BloFin ↗',href:'https://blofin.com/trade/en/futures/'+encodeURIComponent(selectedInstrument)},
                  {label:'بازار کلی قراردادهای Futures در BloFin ↗',href:'https://blofin.com/market/derivatives'},
                  {label:'راهنمای رسمی ورود به BloFin Demo ↗',href:'https://blofin.com/en/support/FAQ/Futures-Trading/11742286589455-What-is-Demo-Trading'}
                ];
                blofinMarketLinks.forEach(item=>{
                  const link=document.createElement('a');
                  link.className='analysis-link';
                  link.href=item.href;
                  link.target='_blank';
                  link.rel='noopener noreferrer';
                  link.textContent=item.label;
                  exchangeLinks.appendChild(link);
                });
                waveBox.appendChild(exchangeLinks);
                node(waveBox,'p','بازار مورد تحلیل ربات: '+selectedInstrument+' · USDT-M Perpetual · تایم‌فریم موج: '+(wave.timeframe_fa||wave.timeframe||'نامشخص')+' · تصمیم بر پایهٔ کندل‌های بسته‌شدهٔ ۵ دقیقه‌ای است. اجرا فقط در محیط BloFin Demo انجام می‌شود.','muted');
                makeTable(executionReport,'پیوت‌های تأییدشدهٔ موج الیوت',wave.pivots||[],[
                  ['kind_fa','نوع پیوت'],['price','قیمت'],['timestamp_utc','زمان وقوع پیوت'],['confirmed_at_utc','زمان تأیید پیوت']
                ],'پیوت تأییدشده‌ای برای شماره‌گذاری موج در دسترس نیست.');
              }else{
                section('دامنه تست مستقل Demo',
                  report.wave_analysis_note_fa||'این تست مستقل شامل تحلیل موج الیوت نیست.');
              }
              makeTable(executionReport,'وضعیت تک‌تک ۱۲ فیلتر',report.gates_fa||[],[
                ['gate_id','شماره'],['name_fa','فیلتر'],['status_fa','وضعیت'],['reason_fa','دلیل']
              ],'جزئیات فیلترها هنوز ثبت نشده است.');
              const order=section('جزئیات سفارش و قیمت‌های اجرایی');
              metricGrid(order,[
                ['قیمت مرجع ورود',report.entry_reference_price],
                ['میانگین قیمت Fill واقعی',report.average_fill_price],
                ['حد ضرر',report.stop_loss],
                ['حد سود',report.take_profit],
                ['حجم قرارداد',report.quantity_contracts],
                ['اهرم',report.leverage],
                ['شناسه سفارش صرافی',report.exchange_order_id]
              ]);
              makeTable(executionReport,'خط زمانی رویدادهای چرخه',report.events_fa||[],[
                ['time_utc','زمان'],['stage_fa','مرحله'],['status_fa','وضعیت'],['message_fa','شرح رویداد']
              ],'رویدادی ثبت نشده است.');
            }
            async function pollExecutionJob(jobId,token,attempt){
              try{
                const response=await fetch('/api/blofin/demo/execution-jobs/'+jobId,{
                  cache:'no-store',headers:{'Authorization':'Bearer '+token}
                });
                const job=await response.json();
                renderExecutionJob(job);
                const events=job.events||[];
                executionReadiness.textContent=events.map(e=>formatTime(e.time_utc)+' · '+(e.message_fa||e.stage)).join('\\n');
                if(job.status==='completed'||job.status==='failed'){
                  await refreshExecutionReadiness();
                  if(!monitorActive){monitorActive=true;monitorToggle.textContent='توقف مانیتور زنده';fetchLiveMonitor();}
                  return;
                }
              }catch(error){executionReadiness.textContent='در حال بازیابی همان شناسه سفارش؛ درخواست جدید ارسال نکن.';}
              if(attempt<180)setTimeout(()=>pollExecutionJob(jobId,token,attempt+1),5000);
              else executionReadiness.textContent='مهلت نمایش کار پایان یافت؛ شناسه همان عملیات: '+jobId+' . سفارش جدید ارسال نکن.';
            }
            executeButton.addEventListener('click',async function(){
              const token=tokenInput.value.trim();
              if(!token){executionReadiness.textContent='ابتدا توکن پنل را در کادر بالا وارد کن.';return;}
              const selectedInstrument=instrumentSelect.value;
              const selectedProfile='CHALLENGE_1_V2';
              if(!selectedInstrument){executionReadiness.textContent='ابتدا قرارداد مجاز را از فهرست زنده انتخاب کن.';return;}
              if(!window.confirm('یک چرخه '+selectedProfile+' روی '+selectedInstrument+' Perpetual فقط در BloFin Demo اجرا می‌شود؛ جهت LONG/SHORT از سیگنال زنده، اهرم ایزوله 1x و SL/TP اجباری است. در نبود سیگنال معتبر هیچ سفارشی ارسال نمی‌شود. ادامه؟'))return;
              executeButton.disabled=true;
              executionReport.replaceChildren();
              const runningMessage=document.createElement('p');
              runningMessage.className='muted';
              runningMessage.textContent='در حال تحلیل '+selectedInstrument+' با کندل بسته‌شدهٔ ۵ دقیقه‌ای، ساختار قیمت و موج الیوت و بررسی دو ناظر مستقل AI؛ هنوز سفارشی ارسال نشده است.';
              executionReport.appendChild(runningMessage);
              try{
                const response=await fetch('/api/blofin/demo/execute-once',{
                  method:'POST',cache:'no-store',
                  headers:{'Authorization':'Bearer '+token,'Content-Type':'application/x-www-form-urlencoded'},
                  body:new URLSearchParams({confirm:'1',instrument:selectedInstrument,profile:selectedProfile})
                });
                const started=await response.json();
                if(!response.ok||!started.job_id){
                  executionReport.replaceChildren();
                  const failureMessage=document.createElement('p');
                  failureMessage.textContent=started.message_fa||started.reason_fa||'ثبت درخواست اجرا نشد؛ جزئیات احراز هویت یا پیش‌نیازها را بررسی کن.';
                  executionReport.appendChild(failureMessage);
                  await refreshExecutionReadiness();
                  return;
                }
                lastJobId=started.job_id;
                const acceptedMessage=document.createElement('p');
                acceptedMessage.className='muted';
                acceptedMessage.textContent='درخواست ثبت شد. شناسه چرخه: '+started.job_id+'؛ گزارش در حال دریافت است.';
                executionReport.replaceChildren(acceptedMessage);
                pollExecutionJob(started.job_id,token,0);
              }catch(error){
                executionReport.textContent='نتیجه درخواست نامشخص است؛ پیش از هر تلاش مجدد وضعیت سفارش‌های BloFin را بررسی کن.';
                executionReadiness.textContent='از فشردن دوباره دکمه خودداری کن تا وضعیت درخواست روشن شود.';
              }
            });
            function renderV2CycleSnapshot(payload){
              v2CycleHistory.replaceChildren();
              const run=payload.run_state||{};
              v2CycleStatus.textContent='آخرین اسکن: '+(payload.scan_time_utc||'هنوز ثبت نشده')
                +' · وضعیت اتوپایلوت: '+(run.status||'UNKNOWN')
                +' · چرخه: '+(payload.latest_cycle_id||'—');
              function add(parent,tag,value){
                const node=document.createElement(tag);
                node.textContent=value===null||value===undefined||value===''?'—':String(value);
                parent.appendChild(node);return node;
              }
              const cycles=Array.isArray(payload.cycles)?payload.cycles:[];
              add(v2CycleHistory,'h4','آخرین چرخهٔ ذخیره‌شده در Turso · فقط‌خواندنی · پنج بازار');
              if(!cycles.length){
                add(v2CycleHistory,'p','هنوز چرخه‌ای در جدول V2 ثبت نشده است.');
                return;
              }
              const table=document.createElement('table');
              table.style.width='100%';table.style.borderCollapse='collapse';
              const headers=['نماد','وضعیت','سیگنال','دلیل','Nemotron','Groq'];
              const thead=document.createElement('thead'),headRow=document.createElement('tr');
              headers.forEach(label=>{const th=add(headRow,'th',label);th.style.padding='8px';th.style.textAlign='right';});
              thead.appendChild(headRow);table.appendChild(thead);
              const tbody=document.createElement('tbody');
              cycles.forEach(item=>{
                const tr=document.createElement('tr');
                const votes=Array.isArray(item.ai_votes)?item.ai_votes:[];
                const nemotron=votes.find(x=>x.reviewer_id==='nemotron'||String(x.provider||'').toLowerCase().includes('openrouter'))||{};
                const groq=votes.find(x=>x.reviewer_id==='second_ai'||String(x.provider||'').toLowerCase().includes('groq'))||{};
                const opinion=x=>[x.status||'NOT_RECORDED',x.opinion&&x.opinion!==x.status?x.opinion:''].filter(Boolean).join(' · ');
                [item.symbol,item.status,item.signal,item.reason,
                 opinion(nemotron),opinion(groq)].forEach(value=>{
                  const td=add(tr,'td',value);td.style.padding='8px';td.style.borderTop='1px solid rgba(128,128,128,.25)';
                });
                tbody.appendChild(tr);
              });
              table.appendChild(tbody);v2CycleHistory.appendChild(table);
              add(v2CycleHistory,'p','زمان اسکن (UTC): '+(payload.scan_time_utc||'—')
                +' · Cycle ID: '+(payload.latest_cycle_id||'—')+' · محیط: BloFin Demo');
            }
            async function refreshV2CycleSnapshot(){
              try{
                const response=await fetch('/api/blofin/demo/v2-autopilot/latest-public',{
                  cache:'no-store'
                });
                const payload=await response.json();
                if(!response.ok)throw new Error(payload.status||('HTTP '+response.status));
                renderV2CycleSnapshot(payload);return payload;
              }catch(error){
                v2CycleStatus.textContent='گزارش فقط‌خواندنی V2 از Turso در دسترس نیست؛ اجرای مجدد انجام نده.';
                return null;
              }
            }
            async function pollV2Cycle(jobId,attempt){
              const payload=await refreshV2CycleSnapshot();
              const run=payload&&payload.run_state||{};
              if(run.job_id===jobId&&run.status!=='QUEUED'&&run.status!=='RUNNING')return;
              if(attempt<180)setTimeout(()=>pollV2Cycle(jobId,attempt+1),4000);
              else v2CycleStatus.textContent='مهلت نمایش تمام شد؛ همان شناسه چرخه را بررسی کن و درخواست جدید نفرست: '+jobId;
            }
            v2RunButton.addEventListener('click',async function(){
              const token=tokenInput.value.trim();
              if(!token){v2CycleStatus.textContent='ابتدا توکن پنل را وارد کن.';return;}
              if(!window.confirm('یک چرخه کامل CHALLENGE_1_V2 روی پنج بازار در BloFin Demo اجرا می‌شود. اگر هر دو ناظر تأیید معتبر بدهند و همه کنترل‌های ریسک عبور کنند، سفارش واقعی Demo ممکن است ارسال شود. هیچ سفارش Live مجاز نیست. ادامه؟'))return;
              v2RunButton.disabled=true;v2CycleStatus.textContent='درخواست چرخه V2 به Render Staging ارسال شد...';
              try{
                const response=await fetch('/api/blofin/demo/v2-autopilot/run-once',{
                  method:'POST',cache:'no-store',
                  headers:{'Authorization':'Bearer '+token,'Content-Type':'application/x-www-form-urlencoded'},
                  body:new URLSearchParams({confirm:'1'})
                });
                const started=await response.json();
                if(!response.ok||!started.job_id){
                  v2CycleStatus.textContent=started.message_fa||started.blocker||'چرخه آغاز نشد؛ هیچ سفارش جدیدی فرض نکن.';
                  await refreshV2CycleSnapshot();return;
                }
                v2CycleStatus.textContent='چرخه V2 آغاز شد؛ شناسه: '+started.job_id;
                pollV2Cycle(started.job_id,0);
              }catch(error){
                v2CycleStatus.textContent='نتیجه درخواست نامشخص است؛ پیش از تلاش دوباره وضعیت همان چرخه را بررسی کن.';
              }finally{v2RunButton.disabled=false;}
            });
            refreshV2CycleSnapshot();
            setInterval(refreshV2CycleSnapshot,30000);
            loadBlofinInstruments().then(refreshExecutionReadiness);
            refreshExecutionReadiness();
            refreshAutopilotStatus();
             refreshSmokeTestReadiness();
            clearButton.addEventListener('click',function(){
              tokenInput.value='';
              accountView.replaceChildren();
              accountStatus.textContent='توکن و اطلاعات نمایش‌داده‌شده از صفحه پاک شدند.';
            });
            // Safely verify the read-only BloFin connection as soon as the dashboard loads.
            checkButton.click();
          })();
          </script>
        </section>
        <section class="card" id="exchange-shortcuts">
          <h2>ورود سریع به صرافی‌ها</h2>
          <p class="muted">میان‌بر به وب‌سایت رسمی سه صرافی؛ این لینک‌ها فقط صفحهٔ صرافی را باز می‌کنند و هیچ اتصال کیف‌پول یا سفارشی انجام نمی‌دهند. دسترسی و امکان معامله به کشور محل اقامت و شرایط حساب بستگی دارد.</p>
          <div class="exchange-links-grid">
            <a class="exchange-card" href="https://www.binance.com/en/trade" target="_blank" rel="noopener noreferrer">
              <img class="exchange-logo" src="https://cdn.simpleicons.org/binance/F0B90B" alt="لوگوی Binance" loading="lazy">
              <span class="exchange-copy"><strong>Binance</strong><span>ورود به بازار Spot ↗</span></span>
            </a>
            <a class="exchange-card" href="https://www.bybit.com/en/trade/spot" target="_blank" rel="noopener noreferrer">
              <img class="exchange-logo" src="https://cdn.simpleicons.org/bybit/F7A600" alt="لوگوی Bybit" loading="lazy">
              <span class="exchange-copy"><strong>Bybit</strong><span>ورود به بازار Spot ↗</span></span>
            </a>
            <a class="exchange-card" href="https://www.okx.com/en-eu/trade-spot" target="_blank" rel="noopener noreferrer">
              <img class="exchange-logo" src="https://cdn.simpleicons.org/okx/000000" alt="لوگوی OKX" loading="lazy">
              <span class="exchange-copy"><strong>OKX</strong><span>ورود به بازار Spot اروپا ↗</span></span>
            </a>
          </div>
        </section>
        <section class="card" id="gem-hunter-panel">
          <!--RADAR_GEM_HUNTER-->
          <h2>Gem Hunter · کشف زودهنگام توکن‌ها</h2>
          <p class="muted">Gem Hunter V3 یک موتور مستقل و سبک برای کشف زودهنگام توکن‌های Solana، Base و Ethereum است؛ از ۶ معیار اختصاصی استفاده می‌کند و به ۱۲ فیلتر سنگین Spot/Futures یا بکتست چندماهه وابسته نیست. خروجی فقط نامزد پژوهشی است و هرگز سفارش یا سیگنال BUY صادر نمی‌کند. اطلاعات ناقص با NOT_AVAILABLE مشخص می‌شود و نامزدهای دارای امنیت پایهٔ تأییدشده می‌توانند با هشدارهای روشن در Tier 2 نمایش داده شوند.</p>
          <div class="grid" style="align-items:center;margin:12px 0">
            <button id="gem-hunter-run" type="button">اجرای اسکن زنده Gem Hunter V3</button>
            <span id="gem-hunter-status" class="muted" aria-live="polite">هنوز اسکن Gem Hunter V3 اجرا نشده است.</span>
          </div>
          <div id="gem-hunter-results" style="overflow-x:auto">
            <p class="muted">برای اجرای اسکن زندهٔ Gem Hunter V3 دکمهٔ کشف را بزنید؛ نامزدهای Tier 1 و Tier 2 با نقاط قوت و احتیاط نمایش داده می‌شوند.</p>
          </div>
        </section>
        <section class="card" id="gem-hunter-watchlist">
          <h2>فهرست محلی زیر نظر · پیگیری ساعت‌ها و روزهای آینده</h2>
          <p class="muted">این فهرست فقط در همین مرورگر و دستگاه ذخیره می‌شود. نامزدهای اضافه‌شده سیگنال BUY نیستند. آدرس قرارداد یا لینک بازار را پیست کنید تا همراه با زمان بررسی بعدی و یادداشت ذخیره شود.</p>
          <form id="watchlist-manual-form" class="watchlist-form">
            <label>نام توکن / نماد
              <input id="watch-manual-name" placeholder="مثلاً TOKEN (TKN)" autocomplete="off">
            </label>
            <label>شبکه
              <select id="watch-manual-chain"><option value="solana">Solana</option><option value="base">Base</option><option value="unknown">نامشخص</option></select>
            </label>
            <label class="wide">آدرس قرارداد یا لینک توکن / بازار
              <input id="watch-manual-reference" required placeholder="آدرس Mint / Contract یا لینک DEX Screener">
            </label>
            <label>زمان بررسی بعدی
              <input id="watch-manual-next-check" type="datetime-local">
            </label>
            <label class="wide">یادداشت پیگیری
              <textarea id="watch-manual-notes" placeholder="چه چیزی را در بررسی بعدی کنترل کنم؟ مثلاً حجم، نقدینگی، هولدرها یا تراکنش نهنگ‌ها"></textarea>
            </label>
            <div class="wide"><button type="submit" class="gem-add-watch">افزودن به فهرست زیر نظر</button></div>
          </form>
          <p id="watchlist-status" class="muted" aria-live="polite">هنوز موردی برای پیگیری ذخیره نشده است.</p>
          <div id="watchlist-entries" class="watchlist-entries"></div>
        </section>
        <script>
        (() => {
          const button = document.getElementById('gem-hunter-run');
          const status = document.getElementById('gem-hunter-status');
          const results = document.getElementById('gem-hunter-results');
          const watchEntries = document.getElementById('watchlist-entries');
          const watchStatus = document.getElementById('watchlist-status');
          const watchForm = document.getElementById('watchlist-manual-form');
          if (!button || !status || !results || !watchEntries || !watchForm) return;
          const STORAGE_KEY = 'crypto-super-radar.gem-hunter-watchlist.v1';
          const filterLabels = {
            contract_security:'امنیت قرارداد و اختیارات',
            liquidity_depth:'عمق و پایداری نقدینگی',
            buy_sell_momentum:'نسبت خرید به فروش',
            holder_distribution:'توزیع هولدرها',
            age_activity:'سن و فعالیت بازار',
            pool_quality:'کیفیت استخر و تأیید داده'
          };
          const gateLabels = {security:'امنیت', liquidity:'نقدینگی', data_quality:'کیفیت داده', exit:'امکان خروج'};
          // Persist expanded reports across polling DOM replacement and page refreshes.
          const REPORT_OPEN_KEY = 'crypto-super-radar.gem-hunter-open-reports.v1';
          const readExpandedReportKeys = () => {
            try {
              const saved = JSON.parse(sessionStorage.getItem(REPORT_OPEN_KEY) || '[]');
              return new Set(Array.isArray(saved) ? saved.filter(value => typeof value === 'string') : []);
            } catch (_) { return new Set(); }
          };
          const expandedReportKeys = readExpandedReportKeys();
          const persistExpandedReportKeys = () => {
            try { sessionStorage.setItem(REPORT_OPEN_KEY, JSON.stringify(Array.from(expandedReportKeys))); }
            catch (_) { /* The current DOM still keeps the open state if session storage is unavailable. */ }
          };
          const candidateReportKey = item => [
            item.chain || '', item.token_address || '', item.pool_address || '',
            item.candidate_id || item.id || item.token_symbol || item.token_name || ''
          ].join('|').toLowerCase();
          const filterReasonFa = (key, state) => {
            const map = {
              trend:'برای تشخیص روند، تاریخچهٔ معتبر قیمت در چند بازهٔ زمانی لازم است؛ عمر استخر به‌تنهایی روند نیست.',
              momentum:'فقط تغییر قیمت یک‌ساعته در دسترس است؛ برای مومنتوم پایدار، چند بازهٔ زمانی لازم است.',
              relative_strength:'مقایسهٔ بازده توکن با BTC، ETH یا شاخص بازار هنوز متصل نیست.',
              volume_confirmation:state === 'PASS' ? 'حجم و نسبت حجم به نقدینگی برای همین Pool در دو منبع مستقل تطبیق داده شده‌اند؛ این به‌تنهایی حجم خرید دلاری را ثابت نمی‌کند.' : state === 'FAIL' ? 'تطبیق مستقل حجم یا نقدینگی همین Pool شکست خورده است.' : 'حجم و نقدینگی همین Pool هنوز در دو منبع مستقل تطبیق داده نشده‌اند.',
              buy_sell_pressure:'سهم دلاری خرید از فروش در دسترس نیست؛ تعداد تراکنش‌ها جایگزین حجم دلاری نیست.',
              capital_flow:'جریان خالص سرمایه با انتساب مستقل کیف‌پول‌ها هنوز محاسبه نشده است.',
              whale_activity:'تاریخچهٔ قابل‌ردیابی Swapهای واقعی خرید/فروش نهنگ‌ها متصل نیست.',
              liquidity_depth:state === 'FAIL' ? 'نقدینگی گزارش‌شده زیر حد اولیه است.' : 'عدد کل نقدینگی استخر عمق قابل‌اجرا و امکان خروج سفارش را اثبات نمی‌کند.',
              market_integrity:'تعداد تراکنش‌ها به‌تنهایی فعالیت مستقل را ثابت نمی‌کند؛ بررسی Wash Trading و خوشه‌های کیف‌پول متصل نیست.',
              contract_security:state === 'PASS' ? 'بررسی حداقلی امنیت قرارداد عبور کرده است؛ این ممیزی کامل نیست.' : state === 'FAIL' ? 'منبع امنیت قرارداد یک ریسک یا اختیار حساس شناسایی کرده است.' : 'شواهد امنیت قرارداد ناقص است یا هنوز در صف بررسی قرار دارد.',
              holder_distribution:'توزیع موجودی هولدرها و خوشه‌بندی مالکیت ذی‌نفعانه در دسترس نیست.',
              market_regime_validation:'اعتبارسنجی توکن نسبت به شرایط کلی بازار و رژیم ریسک تکمیل نشده است.'
            };
            return map[key] || 'برای این فیلتر هنوز دلیل فارسی مستند ثبت نشده است.';
          };
          const gateReasonFa = (key, state) => ({
            security:'بررسی مستقل امنیت قرارداد و ریسک Honeypot هنوز متصل نیست.',
            liquidity:state === 'PASS' ? 'نقدینگی Pool دقیق در دو منبع مستقل تطبیق داده شده؛ این PASS عمق اجرایی را تضمین نمی‌کند.' : state === 'FAIL' ? 'تطبیق Pool شکست خورده یا نقدینگی تأییدشده زیر آستانه است.' : 'تطبیق دو منبع مستقل برای همین Pool کامل نشده است.',
            data_quality:state === 'PASS' ? 'هویت توکن و Pool و داده‌های بازار در دو منبع مستقل تطبیق داده شده‌اند؛ سایر شواهد همچنان ناقص‌اند.' : state === 'FAIL' ? 'اختلاف داده یا هویت Pool بین منابع مستقل شناسایی شده است.' : 'اعتبارسنجی مستقل همین Pool هنوز تکمیل نشده است.',
            exit:'عمق اجرایی و لغزش برای اندازهٔ سفارش مشخص نیست؛ امکان خروج تأیید نشده است.'
          })[key];
          const el = (tag, value, className) => {
            const node = document.createElement(tag);
            node.textContent = value === null || value === undefined || value === '' ? '—' : String(value);
            if (className) node.className = className;
            return node;
          };
          const safeUrl = value => {
            try {
              const raw = String(value || '').trim();
              if (!/^https?:\\/\\//i.test(raw)) return '';
              const u = new URL(raw);
              return ['http:', 'https:'].includes(u.protocol) ? u.href : '';
            } catch (_) { return ''; }
          };
          const addLink = (parent, label, href) => {
            const url = safeUrl(href);
            if (!url) return;
            const link = document.createElement('a');
            link.textContent = label; link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
            parent.appendChild(link);
          };
          const explorerBase = chain => chain === 'solana' ? 'https://solscan.io' : 'https://basescan.org';
          const tokenExplorer = item => explorerBase(item.chain) + '/token/' + encodeURIComponent(item.token_address || '');
          const poolUrl = item => 'https://dexscreener.com/' + encodeURIComponent(item.chain || '') + '/' + encodeURIComponent(item.pool_address || '');
          const readWatchlist = () => {
            try {
              const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
              return Array.isArray(value) ? value.filter(x => x && typeof x === 'object') : [];
            } catch (_) { return []; }
          };
          const writeWatchlist = list => {
            try {
              localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
              watchStatus.textContent = list.length + ' مورد در حافظهٔ محلی همین مرورگر ذخیره شده است.';
              return true;
            } catch (_) {
              watchStatus.textContent = 'ذخیره‌سازی محلی در این مرورگر در دسترس نیست؛ فضای ذخیره‌سازی مرورگر را بررسی کنید.';
              return false;
            }
          };
          const watchlistIdentity = item => [
            String(item.chain || 'unknown').trim().toLowerCase(),
            String(item.token_address || item.pool_address || item.reference_url || '').trim().toLowerCase()
          ].join(':');
          const addToWatchlist = item => {
            const list = readWatchlist();
            const key = watchlistIdentity(item);
            if (!key.split(':')[1]) {
              watchStatus.textContent = 'افزودن انجام نشد: آدرس قرارداد، Pool یا لینک معتبر موجود نیست.';
              return 'invalid';
            }
            const exists = list.some(x => watchlistIdentity(x) === key);
            if (exists) {
              watchStatus.textContent = 'این توکن از قبل در فهرست زیر نظر وجود دارد.';
              return 'exists';
            }
            list.unshift(Object.assign({
              id: String(Date.now()) + '-' + Math.random().toString(36).slice(2, 8),
              added_at: new Date().toISOString(), next_check_at: '', notes: '', status: 'WATCH'
            }, item));
            if (!writeWatchlist(list)) return 'error';
            renderWatchlist();
            watchStatus.textContent = '✓ مورد با موفقیت در فهرست زیر نظر همین مرورگر ذخیره شد.';
            return 'added';
          };
          const renderWatchlist = () => {
            const list = readWatchlist();
            watchEntries.replaceChildren();
            if (!list.length) {
              watchEntries.appendChild(el('p', 'هنوز موردی برای پیگیری ذخیره نشده است.', 'muted'));
              watchStatus.textContent = 'فهرست محلی خالی است؛ از گزارش کشف یا فرم افزودن دستی استفاده کنید.';
              return;
            }
            watchStatus.textContent = list.length + ' مورد در حافظهٔ محلی همین مرورگر ذخیره شده است.';
            list.forEach(item => {
              const card = document.createElement('article'); card.className = 'watch-entry';
              card.appendChild(el('h3', (item.token_name || item.name || 'توکن بدون نام') + ' · ' + (item.token_symbol || item.symbol || '—')));
              card.appendChild(el('p', (item.chain || 'شبکه نامشخص').toUpperCase() + ' · ' + (item.token_address || item.reference_url || 'بدون آدرس'), 'candidate-meta'));
              const links = document.createElement('div'); links.className = 'gem-links';
              if (item.reference_url) addLink(links, 'لینک ذخیره‌شده ↗', item.reference_url);
              if (item.token_address && ['solana','base','ethereum'].includes(item.chain)) addLink(links, 'اکسپلورر ↗', explorerBase(item.chain) + '/token/' + encodeURIComponent(item.token_address));
              if (item.pool_address && ['solana','base'].includes(item.chain)) addLink(links, 'DEX Screener ↗', 'https://dexscreener.com/' + item.chain + '/' + encodeURIComponent(item.pool_address));
              card.appendChild(links);
              const timeLabel = el('label', 'زمان بررسی بعدی');
              const timeInput = document.createElement('input'); timeInput.type = 'datetime-local'; timeInput.value = item.next_check_at || '';
              timeLabel.appendChild(timeInput); card.appendChild(timeLabel);
              const notesLabel = el('label', 'یادداشت پیگیری');
              const notes = document.createElement('textarea'); notes.value = item.notes || '';
              notesLabel.appendChild(notes); card.appendChild(notesLabel);
              const actions = document.createElement('div'); actions.className = 'watch-entry-actions';
              const save = el('button', 'ذخیره تغییرات'); save.type = 'button';
              save.addEventListener('click', () => {
                const current = readWatchlist(); const index = current.findIndex(x => x.id === item.id);
                if (index >= 0) { current[index].next_check_at = timeInput.value; current[index].notes = notes.value; if (writeWatchlist(current)) renderWatchlist(); }
              });
              const remove = el('button', 'حذف', 'watch-delete'); remove.type = 'button';
              remove.addEventListener('click', () => { const current = readWatchlist().filter(x => x.id !== item.id); if (writeWatchlist(current)) renderWatchlist(); });
              actions.append(save, remove); card.appendChild(actions); watchEntries.appendChild(card);
            });
          };
          const fact = (parent, label, value) => {
            const box = document.createElement('div'); box.className = 'gem-fact';
            box.appendChild(el('span', label)); box.appendChild(el('strong', value)); parent.appendChild(box);
          };
          const renderCandidate = item => {
            const q = item.qualification || {}; const m = q.market_metrics || {};
            const filters = q.filters || {}; const gates = q.critical_gates || {};
            const card = document.createElement('article'); card.className = 'gem-candidate';
            card.appendChild(el('h3', (item.token_name || 'توکن بدون نام') + ' (' + (item.token_symbol || '—') + ')'));
            const tier = item.gem_hunter_v3_status || item.tier || 'RESEARCH_WATCH';
            const tierLabel = item.tier_label_fa || ({TIER_1_VERIFIED_GEM:'⭐ ستاره طلایی · Tier 1',TIER_2_EARLY_ALPHA:'🔥 فرصت اولیه · Tier 2',BLOCKED_SCAM:'مسدود · خطر فروش/کلاهبرداری',BLOCKED_LOW_LIQUIDITY:'مسدود · نقدینگی بسیار کم',REVIEW_REQUIRED:'در انتظار بررسی امنیت',RESEARCH_WATCH:'پایش پژوهشی'}[tier] || tier);
            card.appendChild(el('p', (item.chain || 'unknown').toUpperCase() + ' · ' + tierLabel + ' · مجوز: NO_TRADE', 'candidate-meta'));
            card.appendChild(el('p', 'اولویت کشف اولیه: ' + (item.discovery_priority_score ?? 'ناموجود') + '/100 · امنیت پایه: ' + (item.tier_base_security_status || 'UNKNOWN') + ' · بررسی کامل امنیت: ' + ((item.security_evidence || {}).status || 'UNKNOWN') + ' · بازار: ' + ((item.market_reconciliation || {}).status || 'UNKNOWN'), 'candidate-meta'));
            const exit = item.exit_slippage_evidence || {};
            card.appendChild(el('p', 'مومنتوم مشاهده‌شده: ' + (item.short_term_momentum_score ?? item.short_term_momentum_smart_money_score ?? 'ناموجود') + '/100 · ایمنی/خروج: ' + (item.safety_exit_liquidity_score ?? 'ناموجود') + '/100 · اعتماد داده: ' + (item.data_confidence_score ?? 'ناموجود') + '/100 · لغزش خروج: ' + (exit.estimated_slippage_bps ?? 'UNKNOWN') + ' bps (' + (exit.status || 'UNKNOWN') + ')', 'candidate-meta'));
            (item.tier_strengths_fa || []).forEach(reason => card.appendChild(el('p', 'نقطه قوت: ' + reason, 'candidate-meta')));
            (item.tier_cautions_fa || []).forEach(reason => card.appendChild(el('p', 'نکته احتیاط: ' + reason, 'candidate-meta')));
            card.appendChild(el('p', 'Turso: ' + (item.gem_hunter_storage_status || 'UNKNOWN') + ' · هشدار تلگرام: ' + ((item.telegram_alert_status || {}).status || 'NOT_SENT') + ' · مجوز معامله: NO_TRADE', 'candidate-meta'));
            card.appendChild(el('p', 'Read-Only · هیچ دسترسی سفارش‌گذاری وجود ندارد · ' + (item.eu_mica_warning || 'بررسی مجوز MiCA و دسترسی آلمان مستقل است.'), 'candidate-meta'));
            const facts = document.createElement('div'); facts.className = 'gem-facts';
            const reconciliation = item.market_reconciliation || q.market_reconciliation || {};
            fact(facts, 'قیمت گزارش کشف (USD)', item.price_usd);
            fact(facts, 'قیمت تطبیق‌شده (USD)', reconciliation.verified_price_usd ?? m.price_usd);
            fact(facts, 'نقدینگی گزارش کشف (USD)', item.liquidity_usd);
            fact(facts, 'نقدینگی تطبیق‌شده (USD)', reconciliation.verified_liquidity_usd ?? m.liquidity_usd);
            fact(facts, 'وضعیت تطبیق Pool', reconciliation.status || 'UNKNOWN');
            fact(facts, 'حجم ۲۴ساعتهٔ تطبیق‌شده (USD)', reconciliation.verified_volume_24h_usd ?? m.volume_24h_usd ?? item.volume_24h_usd);
            fact(facts, 'حجم / نقدینگی', item.volume_to_liquidity_24h);
            fact(facts, 'تغییر قیمت ۱ساعته (%)', item.price_change_1h_pct);
            fact(facts, 'عمر استخر (ساعت)', item.pool_age_hours);
            fact(facts, 'خرید / فروش ۲۴ساعته (تعداد)', String(item.buys_24h ?? '—') + ' / ' + String(item.sells_24h ?? '—'));
            fact(facts, 'تعداد هولدرها', m.holder_count);
            fact(facts, 'معامله‌گران یکتای ۲۴ساعته', m.unique_traders_24h);
            fact(facts, 'سهم حجمی خرید (%)', m.buy_volume_share_pct);
            fact(facts, 'لغزش تخمینی (bps)', m.estimated_slippage_bps);
            fact(facts, 'امتیاز فرصت مستقل', q.opportunity_score);
            fact(facts, 'امتیاز ریسک مستقل', q.risk_score);
            card.appendChild(facts);
            const addresses = document.createElement('p'); addresses.className = 'candidate-meta';
            addresses.appendChild(el('span', 'Mint / Contract: ')); addresses.appendChild(el('code', item.token_address || '—'));
            addresses.appendChild(el('span', ' · Pool: ')); addresses.appendChild(el('code', item.pool_address || '—'));
            card.appendChild(addresses);
            card.appendChild(el('p', 'DEX / جفت‌ارز: ' + (item.dex || 'نامشخص') + ' / ' + (item.pool_name || 'نامشخص') +
              ' · زمان ایجاد استخر: ' + (item.pool_created_at_utc || 'نامشخص') + ' · منبع کشف: ' + (item.source || 'نامشخص'), 'candidate-meta'));
            const links = document.createElement('div'); links.className = 'gem-links';
            if (item.token_address && ['solana','base'].includes(item.chain)) addLink(links, 'اکسپلورر توکن ↗', tokenExplorer(item));
            if (item.token_address && item.chain === 'ethereum') addLink(links, 'اکسپلورر توکن ↗', 'https://etherscan.io/token/' + encodeURIComponent(item.token_address));
            if (item.pool_address && ['solana','base','ethereum'].includes(item.chain)) addLink(links, 'بازار در DEX Screener ↗', poolUrl(item));
            card.appendChild(links);
            const details = document.createElement('details');
            const reportKey = candidateReportKey(item);
            details.dataset.reportKey = reportKey;
            details.open = expandedReportKeys.has(reportKey);
            // Capture the user's intent before the browser dispatches the async toggle event.
            details.addEventListener('toggle', () => {
              if (details.open) expandedReportKeys.add(reportKey);
              else expandedReportKeys.delete(reportKey);
              persistExpandedReportKeys();
            });
            details.appendChild(el('summary', 'گزارش کامل Gem Hunter V3: امتیاز فرصت، ایمنی و اعتماد داده'));
            details.appendChild(el('h4', 'سه امتیاز مستقل Gem Hunter V3'));
            const filterGrid = document.createElement('div'); filterGrid.className = 'gem-detail-grid';
            Object.keys(filterLabels).forEach(key => {
              const state = filters[key] || 'UNKNOWN'; const row = document.createElement('div'); row.className = 'gem-detail';
              row.appendChild(el('strong', filterLabels[key] + ' · ' + state, 'gem-status-' + String(state).toLowerCase()));
              row.appendChild(el('span', filterReasonFa(key, state)));
              filterGrid.appendChild(row);
            });
            details.appendChild(filterGrid); details.appendChild(el('h4', '۴ گیت حیاتی'));
            const gateGrid = document.createElement('div'); gateGrid.className = 'gem-detail-grid';
            Object.keys(gateLabels).forEach(key => {
              const state = gates[key] || 'UNKNOWN'; const row = document.createElement('div'); row.className = 'gem-detail';
              row.appendChild(el('strong', gateLabels[key] + ' · ' + state, 'gem-status-' + String(state).toLowerCase()));
              row.appendChild(el('span', (q.critical_gate_reasons || {})[key] || gateReasonFa(key, state)));
              gateGrid.appendChild(row);
            });
            details.appendChild(gateGrid);
            details.appendChild(el('p', 'وضعیت پرونده: ' + (q.evaluation_status || 'INCOMPLETE') + ' · علت: شواهد مستقل امنیت، هولدرها، جریان خرید و امکان خروج هنوز کامل نشده‌اند.'));
            details.appendChild(el('h4', 'تطبیق مستقل داده‌های بازار'));
            const reconSources = Array.isArray(reconciliation.sources) ? reconciliation.sources : [];
            details.appendChild(el('p', 'وضعیت: ' + (reconciliation.status || 'UNKNOWN') +
              ' · منابع تأییدشده: ' + reconSources.map(source => source.provider || 'نامشخص').join('، ')));
            (reconciliation.reason_fa || []).forEach(reason => details.appendChild(el('p', reason)));
            reconSources.forEach(source => details.appendChild(el('p',
              (source.provider || 'منبع') + ' · نقدینگی: ' + (source.liquidity_usd ?? 'ناموجود') +
              ' USD · حجم ۲۴ساعته: ' + (source.volume_24h_usd ?? 'ناموجود') + ' USD · Pool: ' +
              (source.pool_address || 'نامشخص'), 'candidate-meta')));
            const securityEvidence = item.security_evidence || q.security_evidence || {};
            details.appendChild(el('h4', 'بررسی امنیت قرارداد'));
            details.appendChild(el('p', 'وضعیت: ' + (securityEvidence.status || 'UNKNOWN') +
              ' · منبع: ' + (securityEvidence.provider || 'نامشخص')));
            (securityEvidence.reason_fa || []).forEach(reason => details.appendChild(el('p', reason)));
            if (securityEvidence.coverage_note) details.appendChild(el('p', securityEvidence.coverage_note, 'candidate-meta'));
            details.appendChild(el('p', 'منابع داده: ' + (Array.isArray(q.market_data_sources) ? q.market_data_sources.join(', ') : 'نامشخص') + ' · زمان ارزیابی UTC: ' + (q.data_as_of_utc || 'نامشخص')));
            details.appendChild(el('p', 'پرچم‌های ریسک کشف: ' + ((item.risk_flags || []).join('، ') || 'موردی گزارش نشده است.')));
            const whale = q.whale_evidence || {};
            const whaleEvents = Array.isArray(whale.events) ? whale.events : [];
            details.appendChild(el('p', 'وضعیت شواهد Swap نهنگ: ' + (whale.status || 'UNKNOWN') +
              ' · پوشش: ' + (whale.coverage_status || 'UNKNOWN') + ' · خریداران مستقل: ' +
              (whale.independent_buy_wallets ?? 0) + ' · توضیح: ' + (whaleEvents.length ? 'رویدادهای ثبت‌شده در فهرست زیر آمده‌اند.' : 'تاریخچهٔ قابل‌ردیابی Swap نهنگ‌ها به منبع داده متصل نیست.')));
            if (whaleEvents.length) {
              const eventList = document.createElement('ul');
              whaleEvents.forEach(event => {
                const li = document.createElement('li');
                li.appendChild(el('span', (event.event_type || 'SWAP') + ' · ' + (event.amount_usd ?? '—') +
                  ' USD · زمان: ' + (event.timestamp_utc || event.timestamp || 'نامشخص') + ' · کیف‌پول: '));
                const explorer = explorerBase(item.chain);
                if (event.wallet_address) addLink(li, event.wallet_address, explorer + (item.chain === 'solana' ? '/account/' : '/address/') + encodeURIComponent(event.wallet_address));
                if (event.tx_hash) {
                  li.appendChild(el('span', ' · تراکنش: '));
                  addLink(li, event.tx_hash, explorer + '/tx/' + encodeURIComponent(event.tx_hash));
                }
                if (event.source) li.appendChild(el('span', ' · منبع: ' + event.source));
                eventList.appendChild(li);
              });
              details.appendChild(eventList);
            } else details.appendChild(el('p', 'هیچ رویداد Swap نهنگ قابل‌ردیابی در پرونده ثبت نشده است.'));
            card.appendChild(details);
            const add = el('button', '＋ افزودن به فهرست زیر نظر'); add.type = 'button'; add.className = 'gem-add-watch';
            add.addEventListener('click', event => {
              event.preventDefault();
              event.stopPropagation();
              const outcome = addToWatchlist({
                chain: item.chain, token_name: item.token_name, token_symbol: item.token_symbol,
                token_address: item.token_address, pool_address: item.pool_address, dex: item.dex,
                discovery_priority_score: item.discovery_priority_score, price_usd: item.price_usd,
                liquidity_usd: item.liquidity_usd, volume_24h_usd: item.volume_24h_usd,
                reference_url: item.pool_address ? poolUrl(item) : (item.token_address ? tokenExplorer(item) : '')
              });
              if (outcome === 'added') {
                add.textContent = '✓ اضافه شد';
                add.disabled = true;
              } else if (outcome === 'exists') {
                add.textContent = 'قبلاً اضافه شده';
                add.disabled = true;
              } else {
                add.textContent = 'افزودن ناموفق؛ جزئیات را بررسی کنید';
              }
            });
            card.appendChild(add); return card;
          };
          watchForm.addEventListener('submit', event => {
            event.preventDefault();
            const reference = document.getElementById('watch-manual-reference').value.trim();
            const chain = document.getElementById('watch-manual-chain').value;
            const url = safeUrl(reference);
            if (!reference) { watchStatus.textContent = 'ابتدا آدرس قرارداد یا لینک معتبر را وارد کنید.'; return; }
            const outcome = addToWatchlist({
              chain, token_name: document.getElementById('watch-manual-name').value.trim() || 'توکن پیگیری دستی',
              token_symbol: '', token_address: url ? '' : reference, reference_url: url || '',
              next_check_at: document.getElementById('watch-manual-next-check').value,
              notes: document.getElementById('watch-manual-notes').value.trim()
            });
            if (outcome === 'added') watchForm.reset();
          });
          renderWatchlist();
          const fetchGemHunterReport = async () => {
            let lastError = null;
            for (let retry = 0; retry < 4; retry += 1) {
              try {
                const response = await fetch('/api/gem-hunter/report?limit=10&refresh=1', {
                  cache: 'no-store',
                  credentials: 'same-origin',
                  headers: { 'Accept': 'application/json' }
                });
                if (response.status === 429 || response.status >= 500) {
                  lastError = new Error('HTTP ' + response.status);
                } else {
                  const raw = await response.text();
                  let payload;
                  try {
                    payload = JSON.parse(raw);
                  } catch (_) {
                    throw new Error('پاسخ API معتبر نیست (HTTP ' + response.status + ')');
                  }
                  if (!response.ok) {
                    throw new Error(payload.error || 'HTTP ' + response.status);
                  }
                  return payload;
                }
              } catch (error) {
                lastError = error;
                const message = String(error && error.message || error);
                const retryable = error instanceof TypeError ||
                  (error && error.name === 'AbortError') ||
                  /^HTTP (429|5\\d\\d)$/.test(message);
                if (!retryable) throw error;
              }
              if (retry === 3) break;
              const delay = 1000 * (2 ** retry);
              status.textContent = 'ارتباط با API موقتاً قطع/کند شد؛ تلاش مجدد خودکار (' +
                (retry + 1) + '/3)...';
              await new Promise(resolve => setTimeout(resolve, delay));
            }
            throw new Error('API پس از ۴ تلاش پاسخ نداد: ' +
              String(lastError && lastError.message || lastError || 'network error'));
          };
          const renderPendingReport = data => {
            const pendingCandidates = (data.diagnostic_candidates || [])
              .filter(item => item && item.trade_authorization !== 'BUY');
            const discovered = Number(data.discovered_candidate_count || pendingCandidates.length || 0);
             window.CryptoRadar.update('radar-gem-hunter', [...(data.candidates || []), ...pendingCandidates], {state:data.refreshing ? 'scanning' : 'complete', caption:'کشف‌شده: ' + discovered + ' · BUY تأییدشده: ' + Number(data.qualified_buy_count || 0) + ' · نامزدهای فاقد تأیید کامل به‌عنوان BUY نمایش داده نمی‌شوند.'});
            const isStale = data.diagnostic_freshness === 'STALE_LAST_COMPLETED_SCAN';
            const summary = document.createElement('div');
            summary.appendChild(el('p', data.diagnostic_fa || 'اعتبارسنجی در پس‌زمینه ادامه دارد.', 'muted'));
            summary.appendChild(el('p',
              isStale
                ? 'این‌ها نامزدهای آخرین اسکن کامل‌شده‌اند؛ دادهٔ آن‌ها قدیمی است و فقط برای پیگیری نمایش داده می‌شوند.'
                : 'این‌ها نامزدهای اسکن جاری‌اند؛ مرحلهٔ تطبیق بازار و غربال امنیت هنوز کامل نشده است.',
              'muted'));
            results.replaceChildren(summary);
            if (pendingCandidates.length) {
              results.appendChild(el('h3', isStale
                ? 'آخرین نامزدهای کشف‌شده · دادهٔ قدیمی'
                : 'نامزدهای کشف‌شده · در انتظار اعتبارسنجی'));
              const list = document.createElement('div'); list.className = 'gem-report-list';
              pendingCandidates.forEach(item => list.appendChild(renderCandidate(item)));
              results.appendChild(list);
            } else {
              results.appendChild(el('p', discovered
                ? discovered + ' نامزد پیدا شده؛ جزئیات آن هنوز به پاسخ گزارش نرسیده است.'
                : 'هنوز نامزدی دریافت نشده است؛ به‌محض پایان مرحلهٔ کشف، فهرست در همین‌جا ظاهر می‌شود.',
                'muted'));
            }
            results.appendChild(el('p',
              'هیچ سیگنال BUY از دادهٔ ناقص یا منقضی صادر نمی‌شود.',
              'candidate-meta'));
          };
          button.addEventListener('click', async () => {
            button.disabled = true; results.replaceChildren(el('p', 'در حال اجرای اسکن زندهٔ Gem Hunter V3 روی Solana، Base و Ethereum و بررسی طبقه‌های Tier 1 و Tier 2...'));
             window.CryptoRadar.scanning('radar-gem-hunter', 'در حال کشف و ارزیابی نامزدهای Solana، Base و Ethereum...');
            status.textContent = 'در حال کشف و اعتبارسنجی Solana، Base و Ethereum...';
            try {
              let data = null;
              for (let attempt = 0; attempt < 180; attempt += 1) {
                data = await fetchGemHunterReport();
                if (!data.refreshing) break;
                status.textContent = 'اسکن در پس‌زمینه ادامه دارد... (' + (attempt + 1) + '/180)';
                renderPendingReport(data);
                await new Promise(resolve => setTimeout(resolve, 2000));
              }
              if (data && data.refreshing) {
                status.textContent = 'اسکن هنوز ادامه دارد؛ نامزدهای پیدا‌شده در همین پنل نمایش داده می‌شوند. برای دریافت نتیجهٔ نهایی دوباره کلیک کن.';
                renderPendingReport(data);
                return;
              }
              const buys = data.candidates || []; const candidates = (data.diagnostic_candidates || []).filter(item => item.trade_authorization !== 'BUY');
              const discovered = Number(data.discovered_candidate_count || candidates.length || 0);
              status.textContent = 'کشف‌شده: ' + discovered + ' · پرونده‌های ارزیابی: ' +
                Number(data.evaluation_record_count || 0) + ' · ساختار کامل پرونده: ' +
                Number(data.complete_evaluation_count || 0) + ' · BUY تأییدشده: ' + buys.length;
              const summary = document.createElement('div');
              summary.appendChild(el('p', data.diagnostic_fa || 'گزارش مرحلهٔ کشف آماده شد.', 'muted'));
              summary.appendChild(el('p', 'کشف: ' + discovered +
                ' | ⭐ Tier 1: ' + Number(data.gem_hunter_v3_tier1_count || 0) +
                ' | 🔥 Tier 2: ' + Number(data.gem_hunter_v3_tier2_count || 0) +
                ' | بررسی امنیت: ' + Number(data.gem_hunter_v3_review_count || 0) +
                ' | مسدود: ' + Number(data.gem_hunter_v3_blocked_count || 0) +
                ' | BUY معاملاتی: صفر (NO_TRADE)', 'muted'));
              const reconciliationCounts = data.market_reconciliation_counts || {};
              summary.appendChild(el('p', 'تطبیق مستقل Pool: ' +
                Number(reconciliationCounts.PASS || 0) + ' PASS · ' +
                Number(reconciliationCounts.FAIL || 0) + ' FAIL · ' +
                Number(reconciliationCounts.UNKNOWN || 0) + ' UNKNOWN · ' +
                Number(reconciliationCounts.NOT_CHECKED_BUDGET || 0) + ' در صف بررسی بعدی', 'muted'));
              const securityCounts = data.security_screening_counts || {};
              summary.appendChild(el('p', 'بررسی امنیت قرارداد: ' +
                Number(securityCounts.PASS || 0) + ' PASS · ' +
                Number(securityCounts.FAIL || 0) + ' FAIL · ' +
                Number(securityCounts.UNKNOWN || 0) + ' UNKNOWN · ' +
                Number(securityCounts.NOT_CHECKED_BUDGET || 0) + ' در صف بررسی بعدی', 'muted'));
              const sourceDetails = document.createElement('details'); sourceDetails.appendChild(el('summary', 'وضعیت منابع داده'));
              const sourceList = document.createElement('ul');
              Object.entries(data.sources || {}).forEach(([name, source]) => {
                sourceList.appendChild(el('li', name + ': ' + ((source && source.status) || 'UNKNOWN') +
                  (source && source.reason ? ' · ' + source.reason : '')));
              });
              sourceDetails.appendChild(sourceList); summary.appendChild(sourceDetails); results.replaceChildren(summary);
              if (buys.length) {
                results.appendChild(el('h3', 'فرصت‌های BUY تأییدشده'));
                buys.forEach(item => results.appendChild(renderCandidate(item)));
              } else results.appendChild(el('p', 'در این اسکن هیچ فرصت خریدی تمام استانداردها را نگذرانده است.', 'muted'));
              results.appendChild(el('h3', 'گزارش نامزدهای کشف‌شده · فقط برای بررسی و زیر نظر گرفتن'));
              results.appendChild(el('p', 'نامزدهای زیر BUY نیستند. وضعیت UNKNOWN یا پروندهٔ ناقص به معنی رد قطعی یا تأیید خرید نیست.', 'muted'));
              const list = document.createElement('div'); list.className = 'gem-report-list';
              const tierGroups = [
                ['TIER_1_VERIFIED_GEM', '⭐ Tier 1 · ستاره‌های طلایی'],
                ['TIER_2_EARLY_ALPHA', '🔥 Tier 2 · فرصت‌های اولیه / واچ‌لیست داغ'],
                ['REVIEW_REQUIRED', 'در انتظار بررسی امنیت'],
                ['RESEARCH_WATCH', 'پایش پژوهشی'],
                ['BLOCKED_SCAM', 'مسدود · خطر قطعی فروش'],
                ['BLOCKED_LOW_LIQUIDITY', 'مسدود · نقدینگی بسیار کم']
              ];
              const rendered = new Set();
              tierGroups.forEach(([key, title]) => {
                const group = candidates.filter(item => (item.gem_hunter_v3_status || item.tier || 'RESEARCH_WATCH') === key);
                if (!group.length) return;
                results.appendChild(el('h3', title + ' (' + group.length + ')'));
                group.forEach(item => { list.appendChild(renderCandidate(item)); rendered.add(item); });
              });
              candidates.filter(item => !rendered.has(item)).forEach(item => list.appendChild(renderCandidate(item)));
              if (!candidates.length) list.appendChild(el('p', 'در این اسکن نامزدی برای گزارش دریافت نشد.', 'muted'));
              results.appendChild(list);
              results.appendChild(el('p', 'زمان گزارش UTC: ' + (data.generated_at_utc || data.updated_at_utc || 'در دسترس نیست') +
                ' · این گزارش برای تحقیق است و سفارش یا تراکنش ارسال نمی‌کند.', 'candidate-meta'));
            } catch (error) {
              status.textContent = 'خطا در دریافت داده: ' + error.message;
               window.CryptoRadar.error('radar-gem-hunter', 'دریافت گزارش Gem Hunter ناموفق بود؛ هیچ سیگنالی از دادهٔ ناقص تولید نشده است.');
              results.replaceChildren(el('p', 'گزارش دریافت نشد؛ هیچ سیگنال خریدی از دادهٔ ناقص صادر نمی‌شود.', 'muted'));
            } finally { button.disabled = false; }
          });
        })();
        </script>
        <section class="card" id="data-health">
          <h2>سلامت منابع داده · Data Health</h2>
          <p id="data-health-status" class="muted" aria-live="polite">در حال بررسی تازگی و پوشش داده‌ها...</p>
          <div id="data-health-details" class="grid"></div>
          <button type="button" id="data-health-refresh">بررسی دوباره سلامت داده</button>
          <p class="muted">این سلامت‌سنج فقط فهرست ۲۰ نماد Spot را ارزیابی می‌کند و با منبع مستقل نمودار کندل یکی نیست. LIVE یعنی همهٔ ردیف‌های همین فهرست در پنجرهٔ تازگی تأیید شده‌اند؛ نه پوشش کل بازار و نه اتصال WebSocket.</p>
        </section>
        <script>
        (function(){
          const status=document.getElementById('data-health-status');
          const details=document.getElementById('data-health-details');
          const button=document.getElementById('data-health-refresh');
          if(!status||!details||!button)return;
          const labels={LIVE:'تازه و تأییدشده',PARTIAL:'پوشش ناقص',STALE:'داده قدیمی',DRIFT:'اختلاف زمان/داده',UNKNOWN:'نامشخص'};
          async function refresh(){
            button.disabled=true;status.textContent='در حال دریافت گزارش سلامت...';
            try{
              const response=await fetch('/api/data-health',{cache:'no-store'});
              const data=await response.json();
              if(!response.ok)throw new Error('HTTP '+response.status);
              status.textContent='وضعیت: '+(labels[data.status]||data.status)+' · '+(data.reason||'');
              const rows=[
                ['نمادهای تأییدشده',String(data.verified_symbols??0)+' / '+String(data.expected_symbols??0)],
                ['سن مشاهده',data.age_seconds===null?'—':String(data.age_seconds)+' ثانیه'],
                ['حد تازگی',String(data.stale_after_seconds??'—')+' ثانیه'],
                ['منابع',Array.isArray(data.providers)?data.providers.join(' + '):'—']
              ];
              details.replaceChildren(...rows.map(([label,value])=>{
                const card=document.createElement('div');card.className='metric';
                const title=document.createElement('span');title.textContent=label;
                const content=document.createElement('strong');content.textContent=value;
                card.append(title,content);return card;
              }));
            }catch(error){status.textContent='گزارش سلامت دریافت نشد: '+String(error.message||error);}
            finally{button.disabled=false;}
          }
          button.addEventListener('click',refresh);refresh();
        })();
        </script>
        <section class="card" id="paper-performance">
          <h2>ارزیابی عملکرد معاملات آزمایشی</h2>
          <p id="paper-performance-status" class="muted" aria-live="polite">در حال دریافت گزارش معاملات بسته‌شده...</p>
          <div id="paper-performance-metrics" class="grid"></div>
          <button type="button" id="paper-performance-refresh">به‌روزرسانی عملکرد</button>
          <p class="muted">این گزارش فقط معاملات بسته‌شدهٔ Paper داخلی را می‌سنجد و معاملات حساب BloFin Demo را وارد نمی‌کند. تا ثبت حداقل ۳۰ معاملهٔ Paper، نمونه برای ارزیابی کافی نیست؛ حتی پس از آن نیز عملکرد گذشته تضمین سود آینده نیست. لغزش قیمت جداگانه از قیمت اجرای شبیه‌سازی‌شده تفکیک نشده است.</p>
        </section>
        <script>
        (function(){
          const status=document.getElementById('paper-performance-status');
          const metrics=document.getElementById('paper-performance-metrics');
          const button=document.getElementById('paper-performance-refresh');
          if(!status||!metrics||!button)return;
          const fmt=(value,suffix='')=>value===null||value===undefined?'—':Number(value).toLocaleString(undefined,{maximumFractionDigits:4})+suffix;
          async function refresh(){
            button.disabled=true;status.textContent='در حال دریافت دفتر معاملات آزمایشی...';
            try{
              const response=await fetch('/paper-bot/status',{cache:'no-store'});
              const data=await response.json();
              if(!response.ok)throw new Error('HTTP '+response.status);
              const p=data.performance||{};
              status.textContent=(p.status==='REVIEWABLE'?'نمونه به حداقل تعداد رسیده؛ نیازمند بررسی مستقل است.':'نمونه هنوز کافی نیست.')+' '+(p.reason||'');
              const feeStatus=p.fee_data_status==='COMPLETE'?'کامل':p.fee_data_status==='INCOMPLETE'?'ناقص':'بدون داده';
              const feeRows=p.fee_data_status==='INCOMPLETE'
                ? [['اجزای کارمزد مشاهده‌شده (جمع جزئی)',fmt(p.known_recorded_fees_usd,' USD')]]
                : [];
              const rows=[
                ['تعداد معاملات بسته‌شده',fmt(p.sample_size)+' / '+fmt(p.minimum_sample_size)],
                ['نرخ برد',fmt(p.win_rate_pct,'%')],
                ['امید ریاضی خالص هر معامله',fmt(p.net_expectancy_usd_per_trade,' USD')],
                ['Profit Factor خالص',fmt(p.profit_factor_net)],
                ['حداکثر افت سرمایه تحقق‌یافته',fmt(p.max_realized_drawdown_pct,'%')],
                ['سود/زیان خالص تحقق‌یافته',fmt(p.net_pnl_usd,' USD')],
                ['پوشش دادهٔ کارمزد',feeStatus+' · '+fmt(p.fee_coverage_pct,'%')+' · ردیف ناقص: '+fmt(p.fee_incomplete_rows)],
                ['کل کارمزد ثبت‌شده (فقط با پوشش کامل)',fmt(p.total_recorded_fees_usd,' USD')],
                ...feeRows,
                ['نمونهٔ معتبر R',fmt(p.r_sample_size)+' · Expectancy R: '+fmt(p.net_expectancy_r)]
              ];
              metrics.replaceChildren(...rows.map(([label,value])=>{
                const card=document.createElement('div');card.className='metric';
                const title=document.createElement('span');title.textContent=label;
                const content=document.createElement('strong');content.textContent=value;
                card.append(title,content);return card;
              }));
            }catch(error){status.textContent='گزارش عملکرد دریافت نشد: '+String(error.message||error);}
            finally{button.disabled=false;}
          }
          button.addEventListener('click',refresh);refresh();
        })();
        </script>
        <section class="card" id="crypto-markets">
          <!--RADAR_MARKET-->
          <h2>بازار رمزارزها</h2>
          <p class="muted">قیمت‌های Spot منتخب از APIهای عمومی و رایگان Binance و Bybit؛ نرخ‌ها فقط زمانی نمایش داده می‌شوند که هر دو منبع پاسخ معتبر بدهند. رتبه‌بندی ارزش بازار نیست.</p>
          <p id="crypto-market-status" class="muted" aria-live="polite">در حال دریافت دادهٔ عمومی بازار...</p>
          <div class="grid" style="margin:12px 0">
            <label>جست‌وجوی نام یا نماد
              <input id="crypto-market-search" type="search" placeholder="مثلاً BTC، بیت‌کوین یا SUI" autocomplete="off">
              <span id="crypto-market-search-activity" class="market-search-activity" role="status" aria-live="polite">
                <svg class="market-search-dog market-search-spin" viewBox="0 0 120 82" role="img" aria-label="سگ شیبا در حال بو کشیدن و جست‌وجو">
                  <g class="shiba-body">
                    <path class="shiba-tail" d="M25 37 C3 35 8 5 27 9 C43 12 36 29 24 25" fill="none" stroke="#d9782d" stroke-width="11" stroke-linecap="round"/>
                    <path d="M26 44 Q36 26 68 34 Q91 39 91 57 Q83 72 48 67 Q23 65 26 44" fill="#d9782d" stroke="#8c451f" stroke-width="2"/>
                    <path d="M43 55 Q62 65 83 56 Q81 70 57 70 Q42 67 43 55" fill="#fff0d7"/>
                    <g class="shiba-leg-back"><path d="M36 59 L32 73 Q31 78 43 77 L49 74 L48 64" fill="#d9782d" stroke="#8c451f" stroke-width="1.5"/><path d="M31 74 Q39 71 48 75 Q49 80 35 80 Q28 79 31 74" fill="#fff0d7"/></g>
                    <g class="shiba-leg-front"><path d="M72 57 L70 73 Q69 78 82 77 L88 74 L84 59" fill="#d9782d" stroke="#8c451f" stroke-width="1.5"/><path d="M69 74 Q78 71 87 75 Q89 80 75 80 Q67 79 69 74" fill="#fff0d7"/></g>
                  </g>
                  <g class="shiba-head">
                    <path d="M72 39 L74 9 Q77 2 86 15 L91 39" fill="#d9782d" stroke="#8c451f" stroke-width="2"/>
                    <path d="M76 15 L78 31 L86 22" fill="#f8c7a0"/>
                    <path d="M92 36 L104 12 Q111 7 113 23 L109 47" fill="#d9782d" stroke="#8c451f" stroke-width="2"/>
                    <path d="M101 23 L108 18 L107 37" fill="#f8c7a0"/>
                    <path d="M69 35 Q80 20 102 34 Q117 45 107 60 Q96 72 76 61 Q62 53 69 35" fill="#e9893b" stroke="#8c451f" stroke-width="2"/>
                    <path d="M71 49 Q80 39 91 49 Q101 55 111 47 Q113 62 98 66 Q79 67 71 49" fill="#fff0d7"/>
                    <ellipse cx="104" cy="49" rx="3" ry="2.5" fill="#241b18"/>
                    <path d="M108 57 Q112 60 115 56" fill="none" stroke="#40251d" stroke-width="2" stroke-linecap="round"/>
                    <circle cx="87" cy="40" r="2.5" fill="#241b18"/>
                    <path d="M74 30 L88 26 L101 31" fill="none" stroke="#ffc36b" stroke-width="2" stroke-linecap="round"/>
                  </g>
                </svg>
                <span class="market-search-searching-copy">شیبا داره بو می‌کشه... چیزی پیدا می‌کنه؟ 🐾</span>
                <span class="market-search-found-copy">پیداش کرد! گنج اینجاست! 🎉</span>
                <span class="market-search-treasure" aria-hidden="true">💰</span>
              </span>
            </label>
            <label>مرتب‌سازی
              <select id="crypto-market-sort">
                <option value="volume">بیشترین حجم ۲۴ساعته</option>
                <option value="name">نام رمزارز</option>
                <option value="symbol">نماد</option>
              </select>
            </label>
          </div>
          <p id="crypto-market-count" class="muted">۰ رمزارز</p>
          <div style="overflow-x:auto"><table>
            <thead><tr><th>رمزارز</th><th>نماد</th><th>قیمت مرجع (USD)</th><th>حجم ۲۴ساعته (USD)</th><th>اختلاف منابع</th><th>وضعیت</th><th>اقدام</th></tr></thead>
            <tbody id="crypto-market-rows"><tr><td colspan="7"><div class="market-loading-mascot" role="status" aria-live="polite">
            <svg class="market-loading-rabbit" viewBox="0 0 120 92" role="img" aria-label="خرگوش در حال کندن زمین و پیدا کردن هویج">
              <g class="rabbit-body">
                <ellipse cx="55" cy="59" rx="31" ry="22" fill="#eee0c9" stroke="#9e8061" stroke-width="2"/>
                <circle cx="31" cy="57" r="9" fill="#fff7e9" stroke="#9e8061" stroke-width="1.5"/>
                <g class="rabbit-ear"><ellipse cx="39" cy="23" rx="8" ry="23" transform="rotate(-15 39 23)" fill="#f4e7d4" stroke="#9e8061" stroke-width="2"/><ellipse cx="40" cy="24" rx="3.5" ry="15" transform="rotate(-15 40 24)" fill="#e9a9a5"/></g>
                <ellipse cx="67" cy="46" rx="22" ry="19" fill="#f4e7d4" stroke="#9e8061" stroke-width="2"/>
                <ellipse cx="81" cy="52" rx="13" ry="10" fill="#fff7e9"/>
                <circle cx="72" cy="43" r="2.5" fill="#34271e"/><circle cx="91" cy="53" r="3" fill="#d98791"/>
                <path d="M79 59q5 4 9 0" fill="none" stroke="#795c4a" stroke-width="1.5" stroke-linecap="round"/>
                <g class="rabbit-paw"><ellipse cx="83" cy="72" rx="13" ry="6" fill="#f4e7d4" stroke="#9e8061" stroke-width="1.5"/><path d="M88 70l8 5" stroke="#9e8061" stroke-width="2" stroke-linecap="round"/></g>
              </g>
              <g class="rabbit-dirt" fill="#a66a3f"><circle cx="100" cy="73" r="3"/><circle cx="109" cy="65" r="2"/><circle cx="96" cy="61" r="2.5"/></g>
              <path d="M17 79q24-13 47 0q20-10 42 0" fill="none" stroke="#a66a3f" stroke-width="5" stroke-linecap="round"/>
              <g class="rabbit-carrot"><path d="M105 72q-7 11-4 17q8-4 11-15z" fill="#f28b28" stroke="#b85a1b" stroke-width="1.2"/><path d="M106 72q-6-8-12-6q4 7 10 9q1-9 8-11q1 7-6 12" fill="#59b66b" stroke="#347c49" stroke-width="1"/></g>
            </svg>
            <span>خرگوش داره زمین رو می‌کَنه تا هویجِ گنج رو پیدا کنه... 🥕</span>
            <span class="market-loading-carrot" aria-hidden="true">🥕</span>
          </div></td></tr></tbody>
          </table></div>
          <button type="button" id="crypto-market-refresh">به‌روزرسانی بازار</button>
          <p class="muted">برای آماده‌کردن اسکن، «انتخاب برای اسکن» را بزنید؛ اجرای اسکن فقط با دکمهٔ فرم انجام می‌شود. به‌روزرسانی سمت سرور هر ۶۰ ثانیه cache می‌شود. این بخش هیچ API پولی یا کلید API نمی‌خواهد؛ محدودیت منطقه‌ای و قطع سرویس عمومی ممکن است بعضی ردیف‌ها را UNKNOWN کند.</p>
        </section>
        <script>
        (function(){
          const status=document.getElementById('crypto-market-status');
          const rows=document.getElementById('crypto-market-rows');
          const button=document.getElementById('crypto-market-refresh');
          const search=document.getElementById('crypto-market-search');
          const sort=document.getElementById('crypto-market-sort');
          const count=document.getElementById('crypto-market-count');
          const searchActivity=document.getElementById('crypto-market-search-activity');
          let marketItems=[];
          let searchTimer=null;
          const fmt=(value,max=8)=>Number(value).toLocaleString(undefined,{maximumFractionDigits:max});
          function cell(tag,value){const node=document.createElement(tag);node.textContent=value??'—';return node;}
          function renderRows(){
            const query=search.value.trim().toLocaleLowerCase();
            const visible=marketItems.filter(item=>
              (item.name_fa+' '+item.name_en+' '+item.symbol).toLocaleLowerCase().includes(query)
            );
            visible.sort((a,b)=>{
              if(sort.value==='name')return (a.name_en||'').localeCompare(b.name_en||'');
              if(sort.value==='symbol')return (a.symbol||'').localeCompare(b.symbol||'');
              return (Number(b.volume_24h_usd)||-1)-(Number(a.volume_24h_usd)||-1);
            });
            const tableRows=visible.map(item=>{
              const tr=document.createElement('tr');
              const values=[item.name_fa+' · '+item.name_en,item.symbol,
                item.status==='PASS'?fmt(item.price_usd):'—',
                item.status==='PASS'?fmt(item.volume_24h_usd,2):'—',
                item.status==='PASS'?fmt(item.price_discrepancy_pct,4)+'%':'—',
                item.status==='PASS'?'تأیید دو منبع':'نامشخص · UNKNOWN'];
              values.forEach((value,index)=>{const itemCell=cell('td',value);if(index>=2&&index<=4)itemCell.className='numeric';tr.appendChild(itemCell);});
              const actionCell=document.createElement('td');
              const scanButton=document.createElement('button');
              scanButton.type='button';scanButton.className='market-scan-button';
              scanButton.textContent='انتخاب برای اسکن';
              scanButton.disabled=item.status!=='PASS';
              scanButton.addEventListener('click',()=>{
                const input=document.getElementById('scan-asset');
                input.value='cex:'+item.symbol;
                document.getElementById('scan-section').scrollIntoView({behavior:'smooth',block:'start'});
                input.focus();
              });
              actionCell.appendChild(scanButton);tr.appendChild(actionCell);
              if(item.status!=='PASS')tr.className='status-unknown';
              return tr;
            });
            rows.replaceChildren(...tableRows);
            count.textContent='نمایش '+visible.length+' از '+marketItems.length+' رمزارز';
            searchActivity.classList.toggle('is-found',query.length>0&&visible.length>0);
            if(!visible.length){const empty=cell('td','موردی با این جست‌وجو پیدا نشد');empty.colSpan=7;const tr=document.createElement('tr');tr.appendChild(empty);rows.replaceChildren(tr);}
          }
          function showMarketLoading(){{
            const tr=document.createElement('tr'),td=document.createElement('td');
            td.colSpan=7;
            td.innerHTML='<div class="market-loading-mascot" role="status" aria-live="polite"><svg class="market-loading-rabbit" viewBox="0 0 120 92" role="img" aria-label="خرگوش در حال کندن زمین و پیدا کردن هویج"><g class="rabbit-body"><ellipse cx="55" cy="59" rx="31" ry="22" fill="#eee0c9" stroke="#9e8061" stroke-width="2"/><circle cx="31" cy="57" r="9" fill="#fff7e9" stroke="#9e8061" stroke-width="1.5"/><g class="rabbit-ear"><ellipse cx="39" cy="23" rx="8" ry="23" transform="rotate(-15 39 23)" fill="#f4e7d4" stroke="#9e8061" stroke-width="2"/><ellipse cx="40" cy="24" rx="3.5" ry="15" transform="rotate(-15 40 24)" fill="#e9a9a5"/></g><ellipse cx="67" cy="46" rx="22" ry="19" fill="#f4e7d4" stroke="#9e8061" stroke-width="2"/><ellipse cx="81" cy="52" rx="13" ry="10" fill="#fff7e9"/><circle cx="72" cy="43" r="2.5" fill="#34271e"/><circle cx="91" cy="53" r="3" fill="#d98791"/><path d="M79 59q5 4 9 0" fill="none" stroke="#795c4a" stroke-width="1.5" stroke-linecap="round"/><g class="rabbit-paw"><ellipse cx="83" cy="72" rx="13" ry="6" fill="#f4e7d4" stroke="#9e8061" stroke-width="1.5"/><path d="M88 70l8 5" stroke="#9e8061" stroke-width="2" stroke-linecap="round"/></g></g><g class="rabbit-dirt" fill="#a66a3f"><circle cx="100" cy="73" r="3"/><circle cx="109" cy="65" r="2"/><circle cx="96" cy="61" r="2.5"/></g><path d="M17 79q24-13 47 0q20-10 42 0" fill="none" stroke="#a66a3f" stroke-width="5" stroke-linecap="round"/><g class="rabbit-carrot"><path d="M105 72q-7 11-4 17q8-4 11-15z" fill="#f28b28" stroke="#b85a1b" stroke-width="1.2"/><path d="M106 72q-6-8-12-6q4 7 10 9q1-9 8-11q1 7-6 12" fill="#59b66b" stroke="#347c49" stroke-width="1"/></g></svg><span>خرگوش دوباره مشغول کندن زمینه... 🥕</span><span class="market-loading-carrot" aria-hidden="true">🥕</span></div>';
            tr.appendChild(td);rows.replaceChildren(tr);
          }}
          async function refresh(){
            button.disabled=true;status.textContent='در حال دریافت قیمت‌های عمومی...';showMarketLoading();window.CryptoRadar.scanning('radar-market', 'در حال دریافت قیمت و تطبیق منابع عمومی بازار...');
            try{
              const response=await fetch('/api/crypto-markets');
              const data=await response.json();
              if(!response.ok)throw new Error('HTTP '+response.status);
              marketItems=data.items||[];
              window.CryptoRadar.update('radar-market', marketItems, {state:data.refreshing?'scanning':'complete', caption:'آبی: قیمت تأییدشده از دو منبع مستقل · زرد: دادهٔ نامشخص. این رادار جهت خرید/فروش تولید نمی‌کند.'});
              renderRows();
              const receivedDate=data.updated_at_utc?new Date(data.updated_at_utc):null;
              const received=receivedDate&&!Number.isNaN(receivedDate.getTime())
                ?receivedDate.toLocaleString()
                :'هنوز مشاهدهٔ معتبر ثبت نشده';
              status.textContent='منابع: '+(data.sources||[]).join(' و ')+' · قدیمی‌ترین مشاهدهٔ معتبر: '+received+
                ' · وضعیت کلی: '+data.status+' · cache: '+data.cache_ttl_seconds+' ثانیه';
            }catch(error){
              status.textContent='دادهٔ بازار در دسترس نیست: '+String(error.message||error);window.CryptoRadar.error('radar-market', 'دریافت قیمت بازار ناموفق بود؛ وضعیت قبلی به‌عنوان سیگنال تازه تلقی نمی‌شود.');
            }finally{button.disabled=false;}
          }
          search.addEventListener('input',()=>{
            searchActivity.classList.add('is-searching');
            if(searchTimer!==null)clearTimeout(searchTimer);
            searchTimer=setTimeout(()=>{
              renderRows();
              searchActivity.classList.remove('is-searching');
              searchTimer=null;
            },650);
          });
          sort.addEventListener('change',renderRows);
          button.addEventListener('click',refresh);refresh();
        })();
        </script>
        <section class="card" id="paper-bot">
          <h2>ربات معامله‌گر آزمایشی (Paper Trading)</h2>
          <style>
            .live-radar-controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin:12px 0}
            .live-radar-controls label{display:grid;gap:5px;color:var(--muted);font-size:.85rem}
            .live-candle-wrap{width:100%;overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:#07131f}
            #live-candle-chart{display:block;width:100%;min-width:820px;height:390px}
            .live-radar-divergence{margin:10px 0;padding:12px 14px;border:1px solid #52677b;border-inline-start:5px solid #ffc66d;border-radius:10px;background:#101e2b;color:#dce9f5;font-weight:700;line-height:1.8}
            .live-radar-divergence.bullish{border-color:#19d18b;background:#092a25;color:#a9f5d5}
            .live-radar-divergence.bearish{border-color:#ff4d83;background:#351523;color:#ffc0d4}
            .live-radar-divergence.neutral{border-color:#ffc66d;background:#2a2417;color:#ffe0a1}
            .live-radar-signal{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:12px 0}
            .live-radar-signal .metric strong{font-size:1.05rem}
            .live-radar-note{color:var(--muted);font-size:.82rem;line-height:1.8}
            .live-radar-analysis{display:grid;gap:12px;margin:16px 0}
            .live-radar-direction{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:10px}
            .live-radar-pattern-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
            .live-radar-pattern-panel{border:1px solid var(--line);border-radius:12px;padding:12px;background:var(--surface)}
            .live-radar-pattern-panel h4{margin:0 0 9px}
            .live-pattern-list{display:grid;gap:8px}
            .live-pattern-item{border:1px solid var(--line);border-radius:9px;padding:9px;background:var(--surface-raised,transparent)}
            .live-pattern-item strong{display:block;font-size:.9rem;margin-bottom:4px}
            .live-pattern-meta{font-size:.74rem;color:var(--muted);margin-bottom:4px}
            .live-pattern-detail{font-size:.8rem;line-height:1.7;color:var(--muted)}
            .live-radar-indicators{color:var(--muted);font-size:.82rem;line-height:1.8}
            .live-radar-flow{border:1px solid var(--line);border-radius:12px;padding:14px;background:var(--surface);display:grid;gap:10px}
            .live-flow-track{display:flex;overflow:hidden;height:18px;border-radius:999px;background:var(--surface-raised)}
            .live-flow-buy{background:#19a974;height:100%;width:50%;transition:width .25s}
            .live-flow-sell{background:#e05260;height:100%;width:50%;transition:width .25s}
            .live-flow-legend,.live-flow-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px}
            .live-flow-legend span,.live-flow-stats span{font-size:.8rem;color:var(--muted)}
            .live-flow-events{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:7px}
            .live-flow-event{padding:8px;border-radius:8px;border:1px solid var(--line);font-size:.78rem}
            .live-flow-event.buy{border-inline-start:3px solid #19a974}.live-flow-event.sell{border-inline-start:3px solid #e05260}

            .easy-trader-robot{margin:14px 0;border:1px solid var(--line);border-radius:18px;padding:12px;background:linear-gradient(135deg,#0b1d2c,#102b3b 60%,#142338);overflow:hidden}
            .easy-robot-stage{position:relative;min-height:240px;display:flex;align-items:flex-end;justify-content:center}
            .easy-robot-bubble{position:relative;z-index:2;width:min(560px,94%);margin:0 auto 96px;padding:12px;border:1px solid #3a7189;border-radius:15px;background:#081825;color:#e7f5ff;text-align:center}
            .easy-robot-bubble:after{content:"";position:absolute;left:calc(50% - 8px);bottom:-8px;width:15px;height:15px;background:#081825;border-right:1px solid #3a7189;border-bottom:1px solid #3a7189;transform:rotate(45deg)}
            .easy-robot-pattern{display:block;font-size:1rem;font-weight:900;line-height:1.6;color:#85dfff}
            .easy-robot-chips{display:flex;flex-wrap:wrap;justify-content:center;gap:7px;margin-top:8px}
            .easy-robot-chip{padding:4px 9px;border:1px solid #426176;border-radius:999px;background:#102b3d;color:#d7edfa;font-size:.72rem;font-weight:700}
            .easy-robot-chip[data-tone="bullish"]{border-color:#1dbb86;color:#aaf2d3}.easy-robot-chip[data-tone="bearish"]{border-color:#e45b82;color:#ffc4d5}.easy-robot-chip[data-tone="neutral"]{border-color:#a78343;color:#ffe0a1}
            .easy-robot-figure{position:absolute;left:50%;bottom:0;width:168px;height:154px;transform:translateX(-50%);animation:easy-robot-float 2.8s ease-in-out infinite}
            .easy-robot-arm-left{transform-origin:55px 104px;animation:easy-robot-wave 2.1s ease-in-out infinite}.easy-robot-arm-right{transform-origin:145px 104px;animation:easy-robot-wave 2.1s ease-in-out infinite reverse}
            .easy-robot-eyes{transform-origin:center;animation:easy-robot-blink 4.8s infinite}.easy-robot-antenna-light{transform-origin:center;animation:easy-robot-pulse 1.5s ease-in-out infinite}
            .easy-robot-caption{text-align:center;margin:0;color:var(--muted);font-size:.76rem}
            @keyframes easy-robot-float{0%,100%{transform:translate(-50%,0)}50%{transform:translate(-50%,-7px)}}
            @keyframes easy-robot-wave{0%,100%{transform:rotate(0)}50%{transform:rotate(-9deg)}}
            @keyframes easy-robot-blink{0%,44%,48%,100%{transform:scaleY(1)}46%{transform:scaleY(.12)}}
            @keyframes easy-robot-pulse{0%,100%{opacity:.55;transform:scale(.9)}50%{opacity:1;transform:scale(1.18)}}
            @media(prefers-reduced-motion:reduce){.easy-robot-figure,.easy-robot-arm-left,.easy-robot-arm-right,.easy-robot-eyes,.easy-robot-antenna-light{animation:none}}
          </style>
          <div id="live-market-radar">
            <h3>نمودار زندهٔ کندل و رادار موج‌ها</h3>
            <p class="muted">کندل‌های Spot از Binance و Bybit دریافت می‌شوند. نمودار هر ۱۵ ثانیه تازه‌سازی می‌شود؛ فقط کندل‌های بسته‌شده در تشخیص سقف/کف و سیگنال استفاده می‌شوند.</p>
            <div class="live-radar-controls">
              <label>رمزارز<select id="live-radar-symbol"><option value="BTCUSDT">BTC</option><option value="ETHUSDT">ETH</option><option value="SOLUSDT">SOL</option><option value="BNBUSDT">BNB</option><option value="XRPUSDT">XRP</option><option value="DOGEUSDT">DOGE</option><option value="ADAUSDT">ADA</option><option value="AVAXUSDT">AVAX</option><option value="LINKUSDT">LINK</option><option value="DOTUSDT">DOT</option><option value="LTCUSDT">LTC</option><option value="TRXUSDT">TRX</option><option value="GRAMUSDT">GRAM</option><option value="PEPEUSDT">PEPE</option><option value="SHIBUSDT">SHIB</option><option value="BCHUSDT">BCH</option><option value="SUIUSDT">SUI</option><option value="HBARUSDT">HBAR</option><option value="NEARUSDT">NEAR</option><option value="UNIUSDT">UNI</option></select></label>
              <label>تایم‌فریم
                <select id="live-radar-interval">
                  <option value="1s">۱ ثانیه</option><option value="1m">۱ دقیقه</option><option value="5m" selected>۵ دقیقه</option>
                  <option value="15m">۱۵ دقیقه</option><option value="1h">۱ ساعت</option>
                  <option value="4h">۴ ساعت</option><option value="1d">۱ روز</option>
                  <option value="1w">۱ هفته</option><option value="1M">۱ ماه</option>
                </select>
              </label>
              <button type="button" id="live-radar-refresh">به‌روزرسانی کندل‌ها</button>
            </div>

            <section class="easy-trader-robot" id="easy-trader-robot" aria-label="ربات متحرک تحلیل‌گر حالت آسان">
              <div class="easy-robot-stage">
                <div class="easy-robot-bubble" role="status" aria-live="polite">
                  <span class="easy-robot-bubble-title">گزارش زنده تحلیل Spot · الگوهای کندلی و نموداری</span>
                  <strong class="easy-robot-pattern" id="easy-robot-pattern">در انتظار الگوی تأییدشدهٔ بازار Spot...</strong>
                  <div class="easy-robot-chips">
                    <span class="easy-robot-chip" id="easy-robot-direction">جهت الگو: نامشخص</span>
                    <span class="easy-robot-chip" id="easy-robot-signal">سیگنال موتور: در انتظار داده</span>
                    <span class="easy-robot-chip" id="easy-robot-confirmation">وضعیت تأیید: نامشخص</span>
                  </div>
                </div>
                <svg class="easy-robot-figure" viewBox="0 0 200 184" aria-hidden="true">
                  <path d="M100 14V28" stroke="#7edfff" stroke-width="5" stroke-linecap="round"/><circle class="easy-robot-antenna-light" cx="100" cy="11" r="8" fill="#6ee7ff" stroke="#c8f7ff" stroke-width="2"/>
                  <g class="easy-robot-arm-left"><path d="M60 105L37 121L28 108" fill="none" stroke="#5bc8e8" stroke-width="13" stroke-linecap="round"/><circle cx="27" cy="105" r="10" fill="#d6f6ff"/></g>
                  <g class="easy-robot-arm-right"><path d="M140 105L163 121L172 108" fill="none" stroke="#5bc8e8" stroke-width="13" stroke-linecap="round"/><circle cx="173" cy="105" r="10" fill="#d6f6ff"/></g>
                  <rect x="49" y="29" width="102" height="91" rx="25" fill="#1b526c" stroke="#83e4ff" stroke-width="4"/><rect x="59" y="40" width="82" height="68" rx="18" fill="#0a1a29"/>
                  <g class="easy-robot-eyes"><ellipse cx="83" cy="70" rx="7" ry="10" fill="#67e5ff"/><ellipse cx="117" cy="70" rx="7" ry="10" fill="#67e5ff"/></g><path d="M84 91Q100 101 116 91" fill="none" stroke="#80eec4" stroke-width="4" stroke-linecap="round"/>
                  <rect x="69" y="119" width="62" height="48" rx="17" fill="#24718d" stroke="#83e4ff" stroke-width="3"/><rect x="83" y="130" width="34" height="18" rx="6" fill="#0b2435"/>
                  <circle cx="91" cy="139" r="3" fill="#62e9c0"/><circle cx="100" cy="139" r="3" fill="#ffc66d"/><circle cx="109" cy="139" r="3" fill="#ff7798"/>
                  <path d="M82 168L77 177M118 168L123 177" stroke="#5bc8e8" stroke-width="9" stroke-linecap="round"/><path d="M72 176H84M116 176H128" stroke="#d6f6ff" stroke-width="5" stroke-linecap="round"/>
                </svg>
              </div>
              <p class="easy-robot-caption"><strong id="easy-robot-market">بازار: در انتظار داده</strong> · ربات فقط تحلیل را نمایش می‌دهد و مستقلاً سفارش نمی‌فرستد.</p>
            </section>
            <p id="live-radar-status" class="muted" aria-live="polite">در حال دریافت کندل‌های زنده...</p>
            <div class="live-candle-wrap">
              <canvas id="live-candle-chart" width="1440" height="460" role="img" aria-label="نمودار کندل و امواج الیوت با سه سطح کف، تعادل و سقف، قیمت لحظه‌ای و کندل‌های واگرایی"></canvas>
            </div>
            <div id="live-radar-divergence" class="live-radar-divergence neutral" role="status" aria-live="polite">واگرایی RSI: در انتظار تحلیل پیوت‌های تأییدشده...</div>
            <div class="live-radar-signal">
              <div class="metric"><span>سیگنال فعلی</span><strong id="live-radar-action">در انتظار داده</strong></div>
              <div class="metric"><span>قیمت لحظه‌ای</span><strong id="live-radar-current-price">—</strong></div>
              <div class="metric"><span>قیمت مرجع سیگنال</span><strong id="live-radar-entry">—</strong></div>
              <div class="metric"><span>حد ضرر</span><strong id="live-radar-stop">—</strong></div>
              <div class="metric"><span>حد سود</span><strong id="live-radar-target">—</strong></div>
              <div class="metric"><span>نسبت ریسک به بازده</span><strong id="live-radar-rr">—</strong></div>
              <div class="metric"><span>ساختار موج الیوت</span><strong id="live-radar-wave">نامشخص</strong></div>
            </div>
            <section class="live-radar-analysis" aria-label="تحلیل چندگانه بازار">
              <section class="live-radar-flow" aria-label="فشار حجم و جریان معاملات">
                <h4>حجم معاملات و فشار خرید / فروش</h4>
                <div class="live-flow-track" role="img" aria-label="نسبت فشار خرید و فروش">
                  <span id="live-flow-buy-bar" class="live-flow-buy"></span><span id="live-flow-sell-bar" class="live-flow-sell"></span>
                </div>
                <div class="live-flow-legend"><span id="live-flow-buy-label">خرید: —</span><span id="live-flow-sell-label">فروش: —</span></div>
                <div class="live-flow-stats">
                  <span id="live-flow-volume">حجم دلاری کندل: —</span><span id="live-flow-rvol">RVOL: —</span>
                  <span id="live-flow-cvd">دلتا معاملات واقعی: —</span><span id="live-flow-book">عدم‌تعادل دفتر سفارش: —</span>
                </div>
                <div><strong id="live-flow-whale-bias">جریان معاملات بزرگ: در انتظار داده</strong><p id="live-flow-whale-evidence" class="live-radar-note">معاملات بزرگ با دادهٔ عمومی و بدون شناسایی مالک تحلیل می‌شوند.</p></div>
                <div id="live-flow-whale-trades" class="live-flow-events"><span class="live-radar-note">در انتظار معاملات بزرگ...</span></div>
              </section>
              <div class="live-radar-direction">
                <div class="metric"><span>جهت غالب بازار</span><strong id="live-radar-bias">در انتظار داده</strong></div>
                <div class="metric"><span>امتیاز همگرایی شواهد</span><strong id="live-radar-score">—</strong></div>
                <div class="metric"><span>رژیم بازار</span><strong id="live-radar-regime">—</strong></div>
                <div class="metric"><span>قدرت روند</span><strong id="live-radar-trend">—</strong></div>
              </div>
              <p id="live-radar-indicators" class="live-radar-indicators">EMA، RSI، MACD، ATR و نسبت حجم پس از دریافت داده نمایش داده می‌شوند.</p>
              <p id="live-radar-analysis-note" class="live-radar-note">این امتیاز احتمال آماری موفقیت نیست؛ ترکیبی از شواهد روند، مومنتوم، حجم، الگوهای کندلی، هندسی و موجی است.</p>
              <div class="live-radar-pattern-grid">
                <article class="live-radar-pattern-panel"><h4>الگوهای کندلی</h4><div id="live-radar-candle-patterns" class="live-pattern-list"><p class="live-radar-note">در انتظار کندل بسته‌شده...</p></div></article>
                <article class="live-radar-pattern-panel"><h4>الگوهای هندسی قیمت</h4><div id="live-radar-geometric-patterns" class="live-pattern-list"><p class="live-radar-note">در انتظار پیوت‌های تأییدشده...</p></div></article>
                <article class="live-radar-pattern-panel"><h4>امواج و ساختار بازار</h4><div id="live-radar-wave-analysis" class="live-pattern-list"><p class="live-radar-note">در انتظار ساختار موج...</p></div></article>
              </div>
            </section>
            <p id="live-radar-reason" class="live-radar-note" aria-live="polite">سیگنال خرید/فروش پس از تأیید ساختار قیمت و اعتبارسنجی دو منبع نمایش داده می‌شود.</p>
            <p id="live-radar-provider" class="live-radar-note">اعتبارسنجی چندمنبعی: در انتظار داده</p>
            <p class="live-radar-note">این نمودار رادار پژوهشی Spot از Binance/Bybit و تایم‌فریم انتخابی است؛ سفارش نمی‌فرستد و از موتور Challenge 1 فیوچرز BloFin جداست. Challenge 1 به‌طور مستقل فقط کندل‌های بسته‌شدهٔ ۵ دقیقه‌ای BloFin را همراه با ساختار قیمت، موج الیوت و دو ناظر مستقل بررسی می‌کند؛ تایم‌فریم‌های ۱۵ دقیقه و یک‌ساعته شرط اجباری ورود نیستند. هیچ الگوریتمی کف یا سقف مطلق را تضمین نمی‌کند. کندل در حال تشکیل صرفاً برای نمایش است و ممکن است تغییر کند.</p>
          </div>
          <script>
          (function(){
            const symbol=document.getElementById('live-radar-symbol');
            const interval=document.getElementById('live-radar-interval');
            const button=document.getElementById('live-radar-refresh');
            const status=document.getElementById('live-radar-status');
            const canvas=document.getElementById('live-candle-chart');
            const ctx=canvas.getContext('2d');
            const fields={
              action:document.getElementById('live-radar-action'),
              currentPrice:document.getElementById('live-radar-current-price'),
              divergence:document.getElementById('live-radar-divergence'),
              entry:document.getElementById('live-radar-entry'),
              stop:document.getElementById('live-radar-stop'),
              target:document.getElementById('live-radar-target'),
              rr:document.getElementById('live-radar-rr'),
              wave:document.getElementById('live-radar-wave'),
              reason:document.getElementById('live-radar-reason'),
              provider:document.getElementById('live-radar-provider'),
              bias:document.getElementById('live-radar-bias'),
              score:document.getElementById('live-radar-score'),
              regime:document.getElementById('live-radar-regime'),
              trend:document.getElementById('live-radar-trend'),
              indicators:document.getElementById('live-radar-indicators'),
              analysisNote:document.getElementById('live-radar-analysis-note'),
              candlePatterns:document.getElementById('live-radar-candle-patterns'),
              geometricPatterns:document.getElementById('live-radar-geometric-patterns'),
              waveAnalysis:document.getElementById('live-radar-wave-analysis'),
              flowBuyBar:document.getElementById('live-flow-buy-bar'),
              flowSellBar:document.getElementById('live-flow-sell-bar'),
              flowBuyLabel:document.getElementById('live-flow-buy-label'),
              flowSellLabel:document.getElementById('live-flow-sell-label'),
              flowVolume:document.getElementById('live-flow-volume'),
              flowRvol:document.getElementById('live-flow-rvol'),
              flowCvd:document.getElementById('live-flow-cvd'),
              flowBook:document.getElementById('live-flow-book'),
              whaleBias:document.getElementById('live-flow-whale-bias'),
              whaleEvidence:document.getElementById('live-flow-whale-evidence'),
              whaleTrades:document.getElementById('live-flow-whale-trades')
            };

            const easyRobot={root:document.getElementById('easy-trader-robot'),pattern:document.getElementById('easy-robot-pattern'),direction:document.getElementById('easy-robot-direction'),signal:document.getElementById('easy-robot-signal'),confirmation:document.getElementById('easy-robot-confirmation'),market:document.getElementById('easy-robot-market')};
            function updateEasyTraderRobot(analysis,signal,symbolValue,intervalValue){
              if(!easyRobot.root)return;
              const items=Array.isArray(analysis&&analysis.candlestick_patterns)?analysis.candlestick_patterns:[];
              const rank=x=>x.state==='CONFIRMED'?2:(x.state==='NEEDS_CONFIRMATION'||x.state==='FORMING'?1:0);
              const pattern=items.slice().sort((a,b)=>rank(b)-rank(a)||Math.abs(Number(b.score)||0)-Math.abs(Number(a.score)||0))[0];
              const dir=pattern?(pattern.direction==='NEUTRAL'&&pattern.potential_direction?pattern.potential_direction:pattern.direction):'UNKNOWN';
              const faDir={BULLISH:'صعودی',BEARISH:'نزولی',NEUTRAL:'خنثی',UNKNOWN:'نامشخص'}[dir]||'نامشخص';
              const state=pattern?({CONFIRMED:'تأییدشده',NEEDS_CONFIRMATION:'در انتظار تأیید کندل بعدی',FORMING:'در حال شکل‌گیری'}[pattern.state]||'نامشخص'):'الگوی معتبر شناسایی نشد';
              const sig=String(signal&&signal.signal||'UNKNOWN').toUpperCase();
              const faSig={BUY:'خرید تأییدشده',SELL:'فروش تأییدشده',WAIT:'در انتظار تأیید',UNKNOWN:'سیگنال نامشخص'}[sig]||'سیگنال نامشخص';
              const tone=sig==='BUY'?'bullish':sig==='SELL'?'bearish':sig==='WAIT'?'neutral':'unknown';
              easyRobot.pattern.textContent=pattern?((pattern.name_fa||pattern.name_en||'الگوی کندلی')+' · '+(pattern.name_en||'')):'در کندل بسته‌شده الگوی شاخصی شناسایی نشد';
              easyRobot.direction.textContent='جهت الگو: '+faDir;easyRobot.direction.dataset.tone=dir==='BULLISH'?'bullish':dir==='BEARISH'?'bearish':'neutral';
              easyRobot.signal.textContent='سیگنال موتور: '+faSig;easyRobot.signal.dataset.tone=tone;
              easyRobot.confirmation.textContent='وضعیت الگو: '+state;easyRobot.confirmation.dataset.tone=pattern&&pattern.state==='CONFIRMED'?'bullish':pattern?'neutral':'unknown';
              easyRobot.market.textContent='بازار: '+(symbolValue||'—')+' · '+(intervalValue||'—');
            }
            const number=v=>v===null||v===undefined?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:8});
            const faDirection=v=>({BULLISH:'صعودی',BEARISH:'نزولی',NEUTRAL:'خنثی',UNKNOWN:'نامشخص'}[v]||v||'نامشخص');
            const faState=v=>({CONFIRMED:'تأییدشده',FORMING:'در حال شکل‌گیری',NEEDS_CONFIRMATION:'نیازمند تأیید',CANDIDATE:'سناریوی ساختاری',AMBIGUOUS:'مبهم',UNKNOWN:'نامشخص'}[v]||v||'نامشخص');
            function renderPatternList(target,items,emptyText){
              if(!target)return;target.replaceChildren();
              if(!Array.isArray(items)||!items.length){
                const empty=document.createElement('p');empty.className='live-radar-note';empty.textContent=emptyText;target.appendChild(empty);return;
              }
              items.forEach(item=>{
                const card=document.createElement('article');card.className='live-pattern-item';
                const title=document.createElement('strong');
                const displayDirection=item.direction==='NEUTRAL'&&item.potential_direction?'گرایش '+faDirection(item.potential_direction):faDirection(item.direction);
                title.textContent=(item.name_fa||item.name_en||'الگوی ناشناخته')+' · '+displayDirection;
                const meta=document.createElement('p');meta.className='live-pattern-meta';
                meta.textContent=faState(item.state)+' · قدرت شواهد: '+(item.evidence_strength||'نامشخص')+
                  (item.score?' · امتیاز: '+item.score:'');
                const detail=document.createElement('p');detail.className='live-pattern-detail';detail.textContent=item.description||'شرح الگو موجود نیست.';
                card.append(title,meta,detail);
                if(item.trigger_price!==null&&item.trigger_price!==undefined){
                  const trigger=document.createElement('p');trigger.className='live-pattern-meta';
                  trigger.textContent='سطح ماشه/شکست: '+number(item.trigger_price);card.appendChild(trigger);
                }
                target.appendChild(card);
              });
            }
            function renderFlow(flow){
              flow=flow||{};const p=flow.pressure||{},a=flow.aggressor_flow||{},book=flow.order_book||{},whale=flow.whale_flow||{};
              const buy=p.buy_pct===null||p.buy_pct===undefined?50:Math.max(0,Math.min(100,Number(p.buy_pct)||0));
              const sell=p.sell_pct===null||p.sell_pct===undefined?50:Math.max(0,Math.min(100,Number(p.sell_pct)||0));
              fields.flowBuyBar.style.width=buy+'%';fields.flowSellBar.style.width=sell+'%';
              fields.flowBuyLabel.textContent='خرید تخمینی: '+number(p.buy_pct)+'% · '+number(p.buy_quote)+' USDT';
              fields.flowSellLabel.textContent='فروش تخمینی: '+number(p.sell_pct)+'% · '+number(p.sell_quote)+' USDT';
              fields.flowVolume.textContent='حجم دلاری کندل: '+number(flow.current_quote_volume)+' USDT';
              fields.flowRvol.textContent='RVOL: '+number(flow.rvol)+'x · '+({EXTREME:'بسیار غیرعادی',HIGH:'بالا',NORMAL:'عادی',UNKNOWN:'نامشخص'}[flow.volume_state]||'نامشخص');
              fields.flowCvd.textContent='دلتا معاملات واقعی: '+number(a.net_delta_quote)+' USDT';
              fields.flowBook.textContent='عدم‌تعادل عمق ۲۰ سطح: '+number(book.imbalance_pct)+'%';
              fields.whaleBias.textContent=(whale.label||'جریان معاملات بزرگ نامشخص')+' · امتیاز '+number(whale.score);
              fields.whaleEvidence.textContent=(whale.evidence||[]).join(' · ')+' · '+(flow.disclaimer||'');
              fields.whaleTrades.replaceChildren();
              const events=flow.large_trades||[];
              if(!events.length){const empty=document.createElement('span');empty.className='live-radar-note';empty.textContent='در پنجرهٔ معاملات اخیر، معامله‌ای بالاتر از آستانهٔ تطبیقی شناسایی نشد.';fields.whaleTrades.appendChild(empty);}
              events.forEach(event=>{
                const card=document.createElement('article');card.className='live-flow-event '+(event.side==='BUY'?'buy':'sell');
                const side=event.side==='BUY'?'خرید تهاجمی':'فروش تهاجمی';
                const time=event.timestamp_ms?new Date(event.timestamp_ms).toLocaleTimeString():'زمان نامشخص';
                card.textContent=side+' · '+number(event.quote_value)+' USDT · قیمت '+number(event.price)+' · '+time;
                fields.whaleTrades.appendChild(card);
              });
            }
            let busy=false,lastSignalKey=null,timer=null;
            function draw(data){
              const bars=data.candles||[];
              ctx.clearRect(0,0,canvas.width,canvas.height);
              ctx.fillStyle='#07131f';ctx.fillRect(0,0,canvas.width,canvas.height);
              if(!bars.length){ctx.fillStyle='#ffc66d';ctx.font='18px sans-serif';ctx.fillText('کندل معتبر برای نمایش در دسترس نیست',24,40);return;}
              const pad={left:82,right:190,top:34,bottom:58};
              const rawMin=Math.min.apply(null,bars.map(x=>x.low));
              const rawMax=Math.max.apply(null,bars.map(x=>x.high));
              const rawSpread=Math.max(rawMax-rawMin,Math.abs(rawMax)*0.0001,1e-9);
              const min=rawMin-rawSpread*.10,max=rawMax+rawSpread*.10,spread=max-min;
              const y=v=>canvas.height-pad.bottom-(v-min)/spread*(canvas.height-pad.top-pad.bottom);
              const slot=(canvas.width-pad.left-pad.right)/bars.length;
              const x=i=>pad.left+(i+.5)*slot;
              const indexByTime=new Map(bars.map((bar,i)=>[bar.time,i]));
              const analysis=data.market_analysis||{},direction=analysis.direction||{},divergence=direction.divergence||{};
              const divType=divergence.type||'NONE';
              const divIndexes=new Set();
              [divergence.price_pivot_1_time,divergence.price_pivot_2_time].forEach(ts=>{
                const key=ts?Date.parse(ts):NaN;
                if(Number.isFinite(key)){const idx=indexByTime.get(key);if(idx!==undefined)divIndexes.add(idx);}
              });
              [divergence.price_pivot_1_index,divergence.price_pivot_2_index].forEach(idx=>{
                if(Number.isInteger(idx)&&idx>=0&&idx<bars.length)divIndexes.add(idx);
              });
              const levels=[
                {name:'سقف بازه',value:rawMax,color:'#ffb454',dash:[7,5]},
                {name:'خط میانی بازه',value:(rawMax+rawMin)/2,color:'#74b7ff',dash:[4,5]},
                {name:'کف بازه',value:rawMin,color:'#42d6a0',dash:[7,5]}
              ];
              levels.forEach(level=>{
                const yy=y(level.value);
                ctx.save();ctx.strokeStyle=level.color;ctx.lineWidth=1.8;ctx.setLineDash(level.dash);
                ctx.beginPath();ctx.moveTo(pad.left,yy);ctx.lineTo(canvas.width-pad.right+8,yy);ctx.stroke();ctx.restore();
                ctx.fillStyle=level.color;ctx.font='bold 13px sans-serif';
                ctx.fillText(level.name,canvas.width-pad.right+16,yy-5);
                ctx.font='12px sans-serif';ctx.fillText(number(level.value),canvas.width-pad.right+16,yy+13);
              });
              ctx.strokeStyle='#274156';ctx.lineWidth=1;
              for(let k=0;k<=4;k++){
                const value=max-k*spread/4,yy=y(value);
                ctx.beginPath();ctx.moveTo(pad.left,yy);ctx.lineTo(canvas.width-pad.right,yy);ctx.stroke();
                ctx.fillStyle='#9eb3c7';ctx.font='12px sans-serif';ctx.fillText(number(value),4,yy+4);
              }
              bars.forEach((bar,i)=>{
                const xx=x(i),up=bar.close>=bar.open;
                const baseColor=up?'#53d6a0':'#ff7b85';
                const special=bar.is_closed&&divIndexes.has(i);
                const color=special?(divType==='BULLISH'?'#00f0a0':'#ff3d83'):baseColor;
                if(special){
                  ctx.save();ctx.fillStyle=divType==='BULLISH'?'rgba(0,240,160,.18)':'rgba(255,61,131,.20)';
                  ctx.fillRect(xx-slot*.43,pad.top,slot*.86,canvas.height-pad.top-pad.bottom);
                  ctx.shadowColor=color;ctx.shadowBlur=18;ctx.strokeStyle=color;ctx.lineWidth=3;
                  ctx.strokeRect(xx-Math.max(4,slot*.42),y(Math.max(bar.open,bar.close))-5,
                    Math.max(8,slot*.84),Math.max(10,Math.abs(y(bar.open)-y(bar.close))+10));ctx.restore();
                }
                ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=special?3:Math.max(1,slot*.12);
                ctx.beginPath();ctx.moveTo(xx,y(bar.high));ctx.lineTo(xx,y(bar.low));ctx.stroke();
                const top=y(Math.max(bar.open,bar.close)),bottom=y(Math.min(bar.open,bar.close));
                const bodyW=Math.max(1,Math.min(12,slot*.66));
                ctx.fillRect(xx-bodyW/2,top,bodyW,Math.max(1,bottom-top));
                if(special){
                  ctx.fillStyle=color;ctx.font='bold 12px sans-serif';
                  ctx.fillText(divType==='BULLISH'?'واگرایی +':'واگرایی −',xx+6,Math.max(pad.top+14,top-8));
                }
                if(!bar.is_closed){
                  ctx.save();ctx.strokeStyle='#ffc66d';ctx.setLineDash([3,3]);
                  ctx.strokeRect(xx-bodyW/2-2,top-2,bodyW+4,Math.max(5,bottom-top+4));ctx.restore();
                }
              });
              const waveLabels=new Map((data.wave_pivots||[]).map(p=>[Date.parse(p.timestamp),p]));
              let prev=null;
              (data.pivots||[]).forEach(p=>{
                const i=indexByTime.get(Date.parse(p.timestamp));
                if(i===undefined)return;
                const xx=x(i),yy=y(p.price);
                if(prev){
                  ctx.save();ctx.strokeStyle='#72a7ff';ctx.lineWidth=2;ctx.setLineDash([5,4]);
                  ctx.beginPath();ctx.moveTo(prev.x,prev.y);ctx.lineTo(xx,yy);ctx.stroke();ctx.restore();
                }
                ctx.fillStyle=p.kind==='LOW'?'#53d6a0':'#ffc66d';
                ctx.beginPath();ctx.arc(xx,yy,5,0,Math.PI*2);ctx.fill();
                const wavePoint=waveLabels.get(Date.parse(p.timestamp));
                ctx.fillStyle='#edf4fb';ctx.font='11px sans-serif';
                ctx.fillText(wavePoint?wavePoint.label:(p.kind==='LOW'?'کف':'سقف'),xx+5,yy+(p.kind==='LOW'?16:-8));
                prev={x:xx,y:yy};
              });
              let wavePrev=null;
              (data.wave_pivots||[]).forEach(p=>{
                const i=indexByTime.get(Date.parse(p.timestamp));
                if(i===undefined)return;
                const point={x:x(i),y:y(p.price)};
                if(wavePrev){
                  ctx.save();ctx.strokeStyle='#d7a9ff';ctx.lineWidth=2.5;ctx.setLineDash([]);
                  ctx.beginPath();ctx.moveTo(wavePrev.x,wavePrev.y);ctx.lineTo(point.x,point.y);ctx.stroke();ctx.restore();
                }
                ctx.fillStyle='#d7a9ff';ctx.beginPath();ctx.arc(point.x,point.y,7,0,Math.PI*2);ctx.fill();
                ctx.fillStyle='#f5eaff';ctx.font='bold 12px sans-serif';ctx.fillText(p.label,point.x+7,point.y-9);
                wavePrev=point;
              });
              const signal=data.signal||{};
              const current=bars[bars.length-1];
              const currentPrice=current?current.close:null;
              if(currentPrice!==null&&currentPrice!==undefined){
                const yy=y(currentPrice);
                ctx.save();ctx.strokeStyle='#55d7ff';ctx.lineWidth=2.7;ctx.setLineDash([]);
                ctx.beginPath();ctx.moveTo(pad.left,yy);ctx.lineTo(canvas.width-pad.right+8,yy);ctx.stroke();ctx.restore();
                ctx.fillStyle='#55d7ff';ctx.font='bold 13px sans-serif';
                ctx.fillText('قیمت آخرین کندل '+number(currentPrice),canvas.width-pad.right+16,yy-9);
              }
              if(signal.entry_reference_price){
                const yy=y(signal.entry_reference_price);
                ctx.save();ctx.strokeStyle=signal.signal==='BUY'?'#53d6a0':signal.signal==='SELL'?'#ff7b85':'#ffc66d';
                ctx.setLineDash([8,5]);ctx.beginPath();ctx.moveTo(pad.left,yy);
                ctx.lineTo(canvas.width-pad.right,yy);ctx.stroke();ctx.restore();
              }
              const tickIndexes=[0,Math.floor((bars.length-1)/3),Math.floor(2*(bars.length-1)/3),bars.length-1];
              ctx.fillStyle='#9eb3c7';ctx.font='12px sans-serif';ctx.textAlign='center';
              tickIndexes.forEach(i=>{
                const xx=x(i),stamp=new Date(bars[i].time);
                const label=data.interval==='1s'?stamp.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'}):stamp.toLocaleString(undefined,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
                ctx.fillText(label,xx,canvas.height-22);
              });
              ctx.textAlign='start';
              ctx.fillStyle='#d7e8f5';ctx.font='bold 14px sans-serif';
              ctx.fillText(data.symbol+' · '+data.interval+' · محور افقی: زمان | محور عمودی: قیمت',pad.left,19);
            }
            async function refresh(force){
              if(busy)return;busy=true;button.disabled=true;status.textContent='در حال دریافت و تطبیق کندل‌ها...';
              try{
                const url='/api/live-market-radar?symbol='+encodeURIComponent(symbol.value)+'&interval='+encodeURIComponent(interval.value)+(force?'&refresh=1':'');
                const response=await fetch(url,{cache:'no-store'});const data=await response.json();
                if(!response.ok)throw new Error(data.error||('HTTP '+response.status));
                draw(data);
                const s=data.signal||{},w=data.elliott_waves||{},cv=data.cross_validation||{};
                const analysis=data.market_analysis||{},d=analysis.direction||{},wave=analysis.wave_structure||{},quality=analysis.data_quality||{};
                renderFlow(data.market_flow||{});
                updateEasyTraderRobot(analysis,s,symbol.value,interval.value);
                const rsiDivergence=analysis.direction&&analysis.direction.divergence?analysis.direction.divergence:{};
                const divergenceType=rsiDivergence.type||'NONE';
                fields.divergence.className='live-radar-divergence '+(divergenceType==='BULLISH'?'bullish':divergenceType==='BEARISH'?'bearish':'neutral');
                fields.divergence.textContent=divergenceType==='BULLISH'?'واگرایی مثبت (صعودی) شناسایی شد · دو کندل پیوت مرجع با سبز نئونی مشخص شده‌اند · RSI '+number(rsiDivergence.rsi_pivot_1)+' ← '+number(rsiDivergence.rsi_pivot_2)+' · هشدار، نه سیگنال ورود مستقل':divergenceType==='BEARISH'?'واگرایی منفی (نزولی) شناسایی شد · دو کندل پیوت مرجع با صورتی پررنگ مشخص شده‌اند · RSI '+number(rsiDivergence.rsi_pivot_1)+' ← '+number(rsiDivergence.rsi_pivot_2)+' · هشدار، نه سیگنال ورود مستقل':'واگرایی RSI در پیوت‌های تأییدشده شناسایی نشد · کندل‌های مرجع فقط هنگام تأیید واگرایی رنگ ویژه می‌گیرند.';
                const labels={BUY:'خرید تأییدشده',SELL:'فروش تأییدشده',WAIT:'در انتظار تأیید',UNKNOWN:'نامشخص'};
                fields.action.textContent=labels[s.signal]||s.signal||'نامشخص';
                const latestBarForPrice=(data.candles||[]).slice(-1)[0];
                fields.currentPrice.textContent=number(latestBarForPrice?latestBarForPrice.close:null);
                fields.entry.textContent=number(s.entry_reference_price);
                fields.stop.textContent=number(s.stop_loss);
                fields.target.textContent=number(s.take_profit);
                fields.rr.textContent=s.reward_risk===null||s.reward_risk===undefined?'—':s.reward_risk+'R';
                fields.wave.textContent=(w.pattern||'نامشخص')+' · '+(w.phase||'داده ناکافی');
                fields.bias.textContent=d.label_fa||faDirection(d.bias);
                fields.score.textContent=d.score===null||d.score===undefined?'—':(d.score>0?'+':'')+d.score+'/100';
                fields.regime.textContent=({TRENDING:'رونددار',RANGE_OR_TRANSITION:'رنج / گذار'}[d.regime]||'نامشخص');
                fields.trend.textContent=faDirection(d.trend);
                const divergence=d.divergence||{},fib=d.fibonacci||{};
                fields.indicators.textContent='EMA20: '+number(d.ema20)+' · EMA50: '+number(d.ema50)+' · EMA200: '+number(d.ema200)+
                  ' · ADX(14): '+number(d.adx14)+' · RSI(14): '+number(d.rsi14)+' ('+({OVERSOLD:'اشباع فروش',OVERBOUGHT:'اشباع خرید',NEUTRAL:'میانه',UNKNOWN:'نامشخص'}[d.rsi_state]||'')+')'+
                  ' · MACD Histogram: '+number(d.macd_histogram)+' · ATR: '+number(d.atr_pct)+'%'+
                  ' · RVOL کندلی: '+number(d.volume_ratio)+'x · واگرایی: '+(divergence.type||'NONE')+
                  ' · فیبوناچی: '+(fib.status||'نامشخص')+' '+number(fib.retracement)+' · تغییر ۱۰ کندل: '+number(d.price_change_10_candles_pct)+'%';
                const qualityLabel=({PASS:'تطبیق دو منبع موفق',FAIL:'اختلاف دو منبع؛ سیگنال مسدود',UNKNOWN:'تطبیق دو منبع نامشخص'}[quality.cross_validation_status]||'تطبیق نامشخص');
                const freshnessLabel=quality.freshness_status==='FRESH'?'داده تازه':quality.freshness_status==='STALE'?'داده کهنه':'تازگی نامشخص';
                fields.analysisNote.textContent='منبع تحلیل: '+(quality.pattern_data_source||'نامشخص')+' · '+qualityLabel+' · '+freshnessLabel+
                  ' · '+((analysis.summary||[]).join(' · ')||'امتیاز جهت بازار صرفاً تجمیع شواهد است و احتمال موفقیت نیست.');
                renderPatternList(fields.candlePatterns,analysis.candlestick_patterns,'در آخرین کندل بسته‌شده الگوی شاخصی تشخیص داده نشد.');
                renderPatternList(fields.geometricPatterns,analysis.geometric_patterns,'ساختار هندسی معتبر یا پیوت کافی هنوز شناسایی نشده است.');
                renderPatternList(fields.waveAnalysis,[{
                  name_fa:'موج الیوت · '+faDirection(wave.pattern),
                  name_en:'Elliott structure',
                  direction:wave.pattern,
                  state:wave.status,
                  evidence_strength:'ساختاری، نه احتمالاتی',
                  score:0,
                  description:(wave.phase||'مرحله نامشخص')+' · '+(wave.reason||'شواهد کافی وجود ندارد.')+
                    (w.status==='AMBIGUOUS'&&Array.isArray(w.pivots)&&w.pivots.length?' · پیوت‌های تأییدشدهٔ سقف/کف در نمودار مشخص شده‌اند؛ شمارش شماره‌گذاری‌شدهٔ موج هنوز معتبر نیست.':'')+
                    (wave.invalidation_price!==null&&wave.invalidation_price!==undefined?' · سطح ابطال: '+number(wave.invalidation_price):'')
                }],'تحلیل موج در دسترس نیست.');
                fields.reason.textContent=s.reason||'دلیل سیگنال در دسترس نیست.';
                fields.provider.textContent='منابع: '+(cv.providers||[]).join(' و ')+' · تطبیق: '+cv.status+
                  ' · اختلاف بیشینه: '+number(cv.max_close_discrepancy_pct)+'% · کندل بسته‌شده: '+(data.last_closed_candle_at||'—');
                const lastClosed=(data.candles||[]).filter(x=>x.is_closed).slice(-1)[0];
                status.textContent='قیمت آخرین کندل بسته‌شده: '+number(lastClosed&&lastClosed.close)+
                  ' · سن کندل: '+number(data.candle_age_seconds)+' ثانیه · دریافت: '+new Date(data.updated_at_utc).toLocaleTimeString();
                if(s.signal_key&&s.signal_key!==lastSignalKey){
                  lastSignalKey=s.signal_key;status.textContent+=' · هشدار جدید '+(labels[s.signal]||s.signal);
                }
              }catch(error){
                status.textContent='دادهٔ تازه در دسترس نیست؛ سیگنال تازه صادر نمی‌شود: '+String(error.message||error);
                draw({candles:[]});
                fields.action.textContent='نامشخص';
                updateEasyTraderRobot({candlestick_patterns:[]},{signal:'UNKNOWN'},symbol.value,interval.value);
                fields.bias.textContent='نامشخص';
                fields.score.textContent='—';
                renderPatternList(fields.candlePatterns,[],'دادهٔ تازه برای تحلیل الگوها در دسترس نیست.');
                renderPatternList(fields.geometricPatterns,[],'دادهٔ تازه برای تحلیل الگوها در دسترس نیست.');
                renderPatternList(fields.waveAnalysis,[],'دادهٔ تازه برای تحلیل موج در دسترس نیست.');
                fields.reason.textContent='آخرین داده تأیید نشد. از سیگنال قبلی برای تصمیم تازه استفاده نکنید.';
              }finally{busy=false;button.disabled=false;}
            }
            button.addEventListener('click',()=>refresh(true));
            symbol.addEventListener('change',()=>refresh(true));
            function scheduleRefresh(){if(timer!==null)clearInterval(timer);timer=setInterval(()=>refresh(false),interval.value==='1s'?5000:15000);}
            interval.addEventListener('change',()=>{scheduleRefresh();refresh(true);});
            refresh(true);
            scheduleRefresh();
            window.addEventListener('beforeunload',()=>{if(timer!==null)clearInterval(timer);});
          })();
          </script>
          <p class="muted">موتور مستقل Spot Paper فقط LONG را با ۱۲ فیلتر ارزیابی می‌کند؛ این موتور با معاملهٔ BloFin Demo Challenge 1 یکی نیست. هر بار یک چرخه اجرا می‌شود. ریسک هر موقعیت حداکثر ۰٫۵٪ سرمایه، ارزش موقعیت حداکثر ۱۰٪، بدون اهرم و بدون سفارش واقعی است. کارمزد و لغزش اعمال می‌شوند؛ حد سربه‌سر متحرک، خروج زمان‌دار، سقف زیان روزانه، افت سرمایه و وقفه پس از زیان هم ورودهای تازه را کنترل می‌کنند.</p>
          <div class="metric" style="margin:12px 0">
            <strong>اتصال الزامی به هوش مصنوعی رایگان · OpenRouter / NVIDIA Nemotron</strong>
            <p class="muted">برای هر ورود جدید، مدل زبانی واقعی باید نقش تحلیل‌گر بازار، تحلیل‌گر تکنیکال، تحلیل‌گر احساسات، پژوهشگر صعودی/نزولی، معامله‌گر و مدیر ریسک را بررسی و خروجی معتبر بدهد. مدل رایگان <code>nvidia/nemotron-3-ultra-550b-a55b:free</code> با هزینهٔ مدل صفر استفاده می‌شود. سقف برنامه ۳۵ درخواست در روز و حافظهٔ نتیجه ۴ ساعت است؛ در صورت قطعی یا اتمام سهمیه، ورود جدید متوقف می‌شود اما مدیریت و خروج موقعیت باز ادامه دارد.</p>
            <p class="muted">برای فعال‌سازی: در <a href="https://openrouter.ai/keys" target="_blank" rel="noopener noreferrer">OpenRouter</a> یک API Key رایگان بساز و آن را فقط در Render → Environment با نام <code>OPENROUTER_API_KEY</code> ثبت کن. کلید را در GitHub، فرم داشبورد یا مرورگر قرار نده. فقط دادهٔ عمومی و خلاصه‌شدهٔ بازار ارسال می‌شود؛ برای مدل رایگان ارائه‌دهنده ممکن است درخواست‌ها را برای امنیت و بهبود محصول نگه‌داری/استفاده کند.</p>
            <div id="paper-bot-ai-status" class="muted" aria-live="polite">وضعیت اتصال AI در حال بررسی...</div>
          </div>
          <form method="post" action="/paper-bot/tick">
            <label>دارایی</label>
            <select name="asset">
              <option value="cex:BTCUSDT">BTC / USDT</option>
              <option value="cex:ETHUSDT">ETH / USDT</option>
              <option value="cex:SOLUSDT">SOL / USDT</option>
              <option value="cex:XRPUSDT">XRP / USDT</option>
              <option value="cex:BNBUSDT">BNB / USDT</option>
              <option value="cex:ADAUSDT">ADA / USDT</option>
              <option value="cex:DOGEUSDT">DOGE / USDT</option>
              <option value="cex:AVAXUSDT">AVAX / USDT</option>
              <option value="cex:DOTUSDT">DOT / USDT</option>
              <option value="cex:LTCUSDT">LTC / USDT</option>
              <option value="cex:TRXUSDT">TRX / USDT</option>
              <option value="cex:GRAMUSDT">GRAM / USDT</option>
            </select>
            <button type="submit">اجرای یک چرخهٔ ربات آزمایشی</button>
          </form>
          <div id="paper-bot-status" class="muted" style="margin-top:12px" aria-live="polite">در حال دریافت وضعیت حساب آزمایشی...</div>
          <p class="muted">برای اجرای دوره‌ای رایگان روی رایانهٔ خود: <code>python scripts/paper_trading_bot.py --asset cex:BTCUSDT --interval-seconds 900</code>. فایل SQLite محلی است؛ سرویس رایگان Render ممکن است هنگام خواب یا راه‌اندازی مجدد فایل‌های محلی را پاک کند.</p>
        </section>
        <script>
        (async function(){
          const box=document.getElementById('paper-bot-status');
          try{
            const r=await fetch('/paper-bot/status');const s=await r.json();
            const a=s.account||{};
            box.textContent='حالت: Paper فقط · موجودی نقدی: '+Number(a.cash_balance_usd||0).toFixed(2)+' USD · ارزش کل حساب: '+Number(a.equity_usd||0).toFixed(2)+' USD · سود/زیان تحقق‌یافته: '+Number(a.realized_pnl_usd||0).toFixed(2)+' USD · موقعیت باز: '+((s.open_position||{}).asset||'ندارد')+' · ترمز ریسک: '+(s.risk_controls&&s.risk_controls.entry_allowed?'فعال':'متوقف')+' · زیان روز: '+Number((s.risk_controls||{}).daily_realized_loss_pct||0).toFixed(3)+'%';
            const ai=s.ai_connection||{},aiBox=document.getElementById('paper-bot-ai-status');
            if(aiBox)aiBox.textContent='اتصال AI: '+(ai.status_fa||'وضعیت نامشخص')+' · مدل: '+(ai.model||'ثبت نشده')+' · سقف محافظه‌کارانه: '+(ai.requests_per_day_app_cap||35)+' درخواست در روز / '+(ai.requests_per_minute_app_cap||18)+' در دقیقه (هر فرایند)';
          }catch(e){box.textContent='وضعیت حساب آزمایشی در دسترس نیست.';}
        })();
        </script>
        <section class="card" id="futures-paper-bot">
          <h2>ربات فیوچرز هوشمند · USDT-M Perpetual</h2>
          <p class="muted">این موتور به‌صورت مستقل LONG و SHORT را ارزیابی می‌کند. ورود فقط با عبور هر ۱۲ فیلتر، تأیید شورای AI، دادهٔ تازهٔ Binance و Bybit، Funding معتبر، حد ضرر و حد سود ساختاری و نسبت سود به ریسک خالص حداقل ۱٫۸ مجاز است.</p>
          <p class="muted"><strong>ایمنی:</strong> Paper Trading فقط · اهرم حداکثر ۱× · ریسک هر معامله ۰٫۵٪ سرمایه · ارزش اسمی موقعیت حداکثر ۱۰٪ سرمایه · حداکثر یک موقعیت باز · بدون API Key صرافی، بدون سفارش واقعی. محاسبهٔ لیکوییدیشن وابسته به Tier مارجین هر صرافی هنوز فعال نیست.</p>
          <form method="post" action="/futures-paper-bot/tick">
            <label>قرارداد دائمی</label>
            <select name="asset">
              <option value="perp:BTCUSDT">BTCUSDT Perpetual</option>
              <option value="perp:ETHUSDT">ETHUSDT Perpetual</option>
              <option value="perp:SOLUSDT">SOLUSDT Perpetual</option>
              <option value="perp:XRPUSDT">XRPUSDT Perpetual</option>
              <option value="perp:BNBUSDT">BNBUSDT Perpetual</option>
              <option value="perp:ADAUSDT">ADAUSDT Perpetual</option>
              <option value="perp:DOGEUSDT">DOGEUSDT Perpetual</option>
              <option value="perp:AVAXUSDT">AVAXUSDT Perpetual</option>
              <option value="perp:DOTUSDT">DOTUSDT Perpetual</option>
              <option value="perp:LTCUSDT">LTCUSDT Perpetual</option>
              <option value="perp:TRXUSDT">TRXUSDT Perpetual</option>
            </select>
            <button type="submit">اجرای یک چرخهٔ فیوچرز آزمایشی</button>
          </form>
          <div id="futures-paper-status" class="muted" style="margin-top:12px" aria-live="polite">در حال دریافت وضعیت حساب فیوچرز آزمایشی...</div>
          <script>(async function(){const box=document.getElementById('futures-paper-status');try{const r=await fetch('/futures-paper-bot/status',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);const s=await r.json();const a=s.account||{},p=s.open_position||{},risk=s.risk_controls||{};const breakerClear=risk.risk_circuit_breaker_clear===true||(risk.risk_circuit_breaker_clear===undefined&&risk.entry_allowed===true);const riskText=breakerClear?'فعال نیست؛ اما این به‌معنای مجوز نهایی ورود نیست':'فعال یا نامشخص است؛ ورود متوقف بماند';box.textContent='حالت: حساب فرضی Paper · موجودی: '+Number(a.balance_usd||0).toFixed(2)+' USD · ارزش حساب: '+Number(a.equity_usd||0).toFixed(2)+' USD · موقعیت: '+(p.direction||'ندارد')+' '+(p.asset||'')+' · ترمز ریسک حساب: '+riskText+' · سفارش واقعی: غیرفعال';}catch(e){box.textContent='وضعیت حساب فیوچرز در دسترس نیست؛ تا دریافت پاسخ معتبر، ورود مجاز نیست.';}})();</script>
        </section>
        <section class="card" id="ai-diagnostic-panel">
          <h2>آزمایش اتصال هوش مصنوعی · NVIDIA Nemotron</h2>
          <p class="muted">این آزمون فقط یک پیام ثابت و کوتاه برای بررسی احراز هویت و پاسخ مدل می‌فرستد؛ داده بازار، اطلاعات حساب و کلید API به متن پیام اضافه نمی‌شود. هیچ چرخه معاملاتی یا موقعیت Paper اجرا نمی‌شود. سقف آزمون: ۳ بار در روز و حداقل ۵ دقیقه فاصله.</p>
          <div id="ai-diagnostic-current" class="muted">در حال بررسی تنظیمات مدل...</div>
          <button id="ai-diagnostic-run" type="button">آزمایش واقعی اتصال Nemotron</button>
          <pre id="ai-diagnostic-result" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre>
          <script>
          (function(){
            const button=document.getElementById('ai-diagnostic-run');
            const current=document.getElementById('ai-diagnostic-current');
            const output=document.getElementById('ai-diagnostic-result');
            fetch('/api/ai/status',{cache:'no-store'}).then(r=>r.json()).then(s=>{
              current.textContent='ارائه‌دهنده: '+s.provider+' · مدل: '+s.model+' · وضعیت کلید: '+s.status;
            }).catch(()=>{current.textContent='وضعیت تنظیمات هوش مصنوعی دریافت نشد.';});
            button.addEventListener('click',async function(){
              button.disabled=true;
              output.textContent='در حال ارسال درخواست تشخیصی محدود به OpenRouter...';
              try{
                const response=await fetch('/api/ai/diagnostic',{
                  method:'POST',
                  credentials:'same-origin',
                  headers:{'Content-Type':'application/x-www-form-urlencoded'},
                  body:'confirm=1'
                });
                const result=await response.json();
                output.textContent=JSON.stringify(result,null,2);
              }catch(error){
                output.textContent='خطای ارتباط با مسیر تشخیصی؛ وضعیت سرویس را دوباره بررسی کن.';
              }finally{button.disabled=false;}
            });
          })();
          </script>
        </section>
        <section class="card" id="ai-dry-run-panel">
          <h2>تست واقعی یکپارچگی Nemotron · بدون معامله</h2>
          <p class="muted">در این تست، درخواست واقعی و تازه به مدل انتخاب‌شده ارسال می‌شود؛ ورودی فقط یک سناریوی ساختگی BTC با ۱۲ فیلتر نمونه است، نه قیمت یا سیگنال زنده. قالب JSON، جهت LONG، نظر مدیریت ریسک و شناسه مدل بررسی می‌شود. حتی اگر مدل سناریو را تأیید کند، این خروجی اجازه ورود به Paper ندارد و هیچ پوزیشن یا سفارشی ساخته نمی‌شود. این تست از سهمیه API رایگان استفاده می‌کند.</p>
          <button id="ai-dry-run-run" type="button">اجرای تست واقعی Nemotron (Dry-Run)</button>
          <pre id="ai-dry-run-result" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre>
          <script>
          (function(){
            const button=document.getElementById('ai-dry-run-run');
            const output=document.getElementById('ai-dry-run-result');
            if(!button||!output)return;
            button.addEventListener('click',async function(){
              button.disabled=true;
              output.textContent='در حال ارسال سناریوی ساختگی به Nemotron و اعتبارسنجی پاسخ واقعی...';
              try{
                const response=await fetch('/api/ai/dry-run',{
                  method:'POST',
                  credentials:'same-origin',
                  headers:{'Content-Type':'application/x-www-form-urlencoded'},
                  body:'confirm=1'
                });
                const result=await response.json();
                output.textContent=JSON.stringify(result,null,2);
              }catch(error){
                output.textContent='خطای ارتباط با مسیر Dry-Run؛ وضعیت سرویس را بررسی کن.';
              }finally{button.disabled=false;}
            });
          })();
          </script>
        </section>
        <section class="card" id="global-market-board">
          <h2>پایش بازارهای جهانی · آمریکا، اروپا، ETF و ارز</h2>
          <p class="muted">این فهرست، لایهٔ دادهٔ بازارهای سنتی است و در کنار رادار رمزارز کار می‌کند. آمریکا، اروپا، ETF، جفت‌ارز و فلزات گران‌بها از مسیر مستقل داده دریافت می‌شوند؛ ساعات، ارز پایه و سطح دسترسی هر بازار جدا نگه داشته می‌شود.</p>
          <p class="muted">اتصال دادهٔ این بخش به <code>TWELVEDATA_API_KEY</code> نیاز دارد. کلید OpenRouter جایگزین آن نیست. پلن رایگان Twelve Data شامل دادهٔ لحظه‌ای سهام آمریکا/ETF و FX است؛ پوشش گستردهٔ اروپا و کالاها به پلن بالاتر وابسته است.</p>
          <div class="global-market-controls">
            <button type="button" id="global-market-refresh">به‌روزرسانی بازارهای جهانی</button>
            <span id="global-market-status" class="muted" aria-live="polite">در حال بررسی تنظیمات منبع جهانی...</span>
          </div>
          <div id="global-market-table" style="overflow-x:auto"></div>
          <p class="muted">محدودیت پذیرش: قیمت‌های این پنل تک‌منبعی‌اند، دادهٔ آمریکا لزوماً فید تجمیعی SIP نیست و دادهٔ اروپا ممکن است EOD باشد. تا زمان cross-validation مستقل، بک‌تست بازارمحور و دفتر Paper چنددارایی، <strong>هیچ سیگنال ورود یا سفارش معاملاتی از این پنل مجاز نیست</strong>.</p>
        </section>
        <script>
        (function(){
          const status=document.getElementById('global-market-status');
          const tableHost=document.getElementById('global-market-table');
          const button=document.getElementById('global-market-refresh');
          if(!status||!tableHost||!button)return;
          const labelStatus={
            API_KEY_MISSING:'کلید داده ثبت نشده',
            OBSERVED_SINGLE_SOURCE:'داده دریافت شد · تک‌منبعی',
            ENTITLEMENT_REQUIRED:'پلن ارائه‌دهنده کافی نیست',
            RATE_LIMITED:'سقف درخواست ارائه‌دهنده پر شده',
            DAILY_BUDGET_OR_REFRESH_COOLDOWN:'محدودیت به‌روزرسانی / سهمیه روزانه',
            UNKNOWN:'نامشخص',
            STALE:'قدیمی'
          };
          function cell(row,value,header){
            const node=document.createElement(header?'th':'td');
            node.textContent=value===null||value===undefined||value===''?'—':String(value);
            row.appendChild(node);
          }
          function render(data){
            tableHost.replaceChildren();
            const items=Array.isArray(data.items)?data.items:[];
            if(!items.length){
              tableHost.textContent=data.status==='LOADING'?'در حال دریافت داده‌ها؛ محدودیت نرخ ارائه‌دهنده رعایت می‌شود.':'هنوز داده‌ای برای نمایش وجود ندارد.';
              return;
            }
            const table=document.createElement('table');
            const head=document.createElement('thead');
            const headRow=document.createElement('tr');
            ['بازار','دارایی','نماد / بورس','قیمت','ارز','تازگی','وضعیت'].forEach(v=>cell(headRow,v,true));
            head.appendChild(headRow);table.appendChild(head);
            const body=document.createElement('tbody');
            items.forEach(item=>{
              const row=document.createElement('tr');
              cell(row,item.asset_class+' · '+item.region);
              cell(row,item.display_name);
              cell(row,item.symbol+(item.mic_code?' · '+item.mic_code:''));
              cell(row,item.price);
              cell(row,item.currency||item.currency_hint);
              cell(row,item.freshness);
              cell(row,labelStatus[item.status]||item.status||'نامشخص');
              body.appendChild(row);
            });
            table.appendChild(body);tableHost.appendChild(table);
          }
          async function refresh(force){
            button.disabled=true;
            status.textContent='در حال دریافت وضعیت فید جهانی...';
            try{
              const response=await fetch('/api/global-markets'+(force?'?refresh=1':''),{cache:'no-store'});
              const data=await response.json();
              if(!response.ok)throw new Error('HTTP '+response.status);
              render(data);
              const counts=data.counts||{};
              status.textContent='وضعیت: '+data.status+' · منبع: '+(data.provider||'—')+
                ' · دریافت‌شده: '+(counts.observed||0)+' / '+(counts.total||0)+
                (data.refreshing?' · به‌روزرسانی ادامه دارد':'');
              if(data.status==='CONFIGURATION_REQUIRED'){
                status.textContent+=' · متغیر لازم: TWELVEDATA_API_KEY';
              }
            }catch(error){
              status.textContent='فید جهانی در دسترس نیست: '+String(error.message||error);
            }finally{button.disabled=false;}
          }
          button.addEventListener('click',()=>refresh(true));
          refresh(false);
        })();
        </script>
        <section class="card" id="scan-section">
          <h2>اسکن دارایی</h2>
          <div style="margin:12px 0 18px;padding:14px;border:1px solid #2b4660;border-radius:12px;background:#0a1725">
            <label for="dex-token-query">جست‌وجوی نام یا نماد آلت‌کوین در DEX</label>
            <div style="display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px">
              <input id="dex-token-query" placeholder="مثلاً PEPE یا BONK">
              <button id="dex-token-search" type="button" style="width:auto">جست‌وجو</button>
            </div>
            <p class="muted">توکن را با نام، نماد، شبکه و آدرس قرارداد انتخاب کن. شبکه‌های جست‌وجو: Ethereum، Base و Solana.</p>
            <div id="dex-search-status" class="muted" aria-live="polite">نمادهای تکراری ممکن است قراردادهای متفاوت داشته باشند.</div>
            <div id="dex-search-results"></div>
            <!--RADAR_DEX_SEARCH-->
          </div>
          <form method="post" action="/scan">
            <label>بازار و نماد / آدرس</label>
            <input id="scan-asset" name="asset" required value="BTC" placeholder="XRP، BTCUSDT یا eth:آدرس‌قرارداد">
            <p class="muted">نمونه‌ها: XRP، BTC، XRPUSDT · برای توکن DEX آدرس قرارداد را با شبکه وارد کن؛ مثل base:0x... یا solana:...</p>
            <label>تیتر خبری تکمیلی (اختیاری)</label>
            <input name="headline" placeholder="تیتر خبر">
            <label>متن اجتماعی تکمیلی (اختیاری)</label>
            <input name="social_post" placeholder="متن عمومی شبکه اجتماعی">
            <button type="submit">اجرای اسکن پژوهشی</button>
          </form>
        </section>
        <script>
        (function(){
          const q=document.getElementById('dex-token-query'),b=document.getElementById('dex-token-search');
          const st=document.getElementById('dex-search-status'),out=document.getElementById('dex-search-results');
          const asset=document.getElementById('scan-asset');
          b.addEventListener('click',async()=>{
            const term=(q.value||'').trim();if(term.length<2){st.textContent='حداقل دو نویسه وارد کن.';return;}
            b.disabled=true;st.textContent='در حال جست‌وجو...';out.replaceChildren();
            try{const r=await fetch('/api/dex-search?q='+encodeURIComponent(term));const data=await r.json();
              window.CryptoRadar.update('radar-dex-search', (data.results || []).map(item=>({...item, radar_signal:'NEUTRAL'})), {state:'complete', caption:'نتایج جست‌وجوی توکن روی رادار قرار گرفتند؛ این نتایج هنوز سیگنال خرید یا فروش نیستند.'});
              if(!Array.isArray(data.results)||!data.results.length){st.textContent=data.reason||'نتیجه‌ای پیدا نشد.';return;}
              st.textContent='قرارداد دقیق و شبکه موردنظر را انتخاب کن.';
              data.results.forEach(item=>{const opt=document.createElement('button');opt.type='button';opt.textContent=item.name+' ('+item.symbol+') · '+item.chain+' · '+item.address;opt.addEventListener('click',()=>{asset.value=item.asset;st.textContent='انتخاب شد: '+item.chain+' / '+item.address;out.replaceChildren();});out.appendChild(opt);});
            }catch(e){st.textContent='سرویس جست‌وجوی DEX در دسترس نیست.';}finally{b.disabled=false;}
          });
          q.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();b.click();}});
        })();
        </script>
        <section class="card" id="market-tools">
          <h2>ماشین‌حساب حرفه‌ای سناریوی Spot</h2>
          <p class="muted">محاسبهٔ سود/زیان فرضی Long بدون اهرم؛ زمان انتخابی فقط افق سناریو است، نه پیش‌بینی زمان سود.</p>
          <div class="calc-grid">
            <div><label for="calc-amount">سرمایه (EUR)</label><input id="calc-amount" type="number" min="0.01" step="0.01" value="10"></div>
            <div><label for="calc-move">تغییر فرضی قیمت (%)</label><input id="calc-move" type="number" min="-99.9" step="0.1" value="10"></div>
            <div><label for="calc-days">افق سناریو (روز)</label><input id="calc-days" type="number" min="1" max="3650" value="14"></div>
            <div><label for="calc-buy-fee">کارمزد خرید (%)</label><input id="calc-buy-fee" type="number" min="0" max="20" step="0.01" value="0.1"></div>
            <div><label for="calc-sell-fee">کارمزد فروش (%)</label><input id="calc-sell-fee" type="number" min="0" max="20" step="0.01" value="0.1"></div>
            <div><label for="calc-buy-slip">لغزش خرید (%)</label><input id="calc-buy-slip" type="number" min="0" max="20" step="0.01" value="0.2"></div>
            <div><label for="calc-sell-slip">لغزش فروش (%)</label><input id="calc-sell-slip" type="number" min="0" max="20" step="0.01" value="0.2"></div>
          </div>
          <div class="grid" style="margin-top:12px">
            <div class="metric">سود/زیان خالص<strong id="calc-pnl">—</strong></div>
            <div class="metric">ارزش نهایی<strong id="calc-value">—</strong></div>
            <div class="metric">بازده خالص<strong id="calc-return">—</strong></div>
            <div class="metric">رشد سربه‌سر<strong id="calc-break-even">—</strong></div>
          </div>
          <h3>مقایسهٔ سناریوهای قیمت</h3><div id="calc-scenarios"></div>
          <p class="muted">فرمول: Vnet = I(1−fb)(1−sb)(1+r)(1−ss)(1−fs). مالیات، تغییر EUR/USD، هزینه برداشت/شبکه و اثر اندازه سفارش خارج از محاسبه‌اند.</p>
          <p class="muted">این ابزار پیش‌بینی، توصیه سرمایه‌گذاری یا سفارش معاملاتی نیست.</p>
        </section>
        <script>
        (function(){
          const ids=['calc-amount','calc-move','calc-days','calc-buy-fee','calc-sell-fee','calc-buy-slip','calc-sell-slip'];
          const el=id=>document.getElementById(id);
          function calc(move){
            const amount=Number(el('calc-amount').value), change=Number(move);
            const bf=Number(el('calc-buy-fee').value)/100, sf=Number(el('calc-sell-fee').value)/100;
            const bs=Number(el('calc-buy-slip').value)/100, ss=Number(el('calc-sell-slip').value)/100;
            const factor=(1-bf)*(1-bs)*(1+change/100)*(1-ss)*(1-sf);
            const net=amount*factor, pnl=net-amount;
            const base=(1-bf)*(1-bs)*(1-ss)*(1-sf);
            return {net,pnl,ret:pnl/amount*100,be:(1/base-1)*100};
          }
          function update(){
            const amount=Number(el('calc-amount').value),move=Number(el('calc-move').value);
            const days=Number(el('calc-days').value);
            const rates=ids.slice(3).map(id=>Number(el(id).value));
            if(!Number.isFinite(amount)||amount<=0||!Number.isFinite(move)||move<=-100||!Number.isFinite(days)||days<1||rates.some(x=>!Number.isFinite(x)||x<0||x>=100)){
              el('calc-pnl').textContent='ورودی نامعتبر';return;
            }
            const out=calc(move),eur=x=>x.toFixed(4)+' €';
            el('calc-pnl').textContent=(out.pnl>=0?'+':'')+eur(out.pnl);
            el('calc-value').textContent=eur(out.net);
            el('calc-return').textContent=(out.ret>=0?'+':'')+out.ret.toFixed(3)+'%';
            el('calc-break-even').textContent=out.be.toFixed(3)+'%';
            const rows=[-20,-10,0,5,10,20].map(v=>{const x=calc(v);return '<tr><td>'+v.toFixed(1)+'%</td><td>'+eur(x.pnl)+'</td><td>'+x.ret.toFixed(3)+'%</td><td>'+days+' روز</td></tr>';}).join('');
            el('calc-scenarios').innerHTML='<table><thead><tr><th>تغییر قیمت</th><th>سود/زیان خالص</th><th>بازده خالص</th><th>افق</th></tr></thead><tbody>'+rows+'</tbody></table>';
          }
          ids.forEach(id=>el(id).addEventListener('input',update));update();
        })();
        </script>
        <section class="card">
          <h3>۱۲ فیلتر اجباری</h3>
          <p>داده بازار · رژیم · روند · مومنتوم · جریان سرمایه · امنیت · احساسات · بک‌تست · ریسک · نهنگ‌ها · پوشش احساسات · Walk-forward</p>
          <p class="muted">هر فیلتر FAIL یا UNKNOWN باشد، خروجی NO TRADE است. سولانا در نسخه فعلی فقط نمونه‌برداری محدود از تراکنش‌های اخیر دارد.</p>
        </section>
        <p class="muted">تاریخچه: <code>/history?asset=cex:BTCUSDT</code></p>
        """
        return self._send(200, _page(body))

    def do_POST(self):
        global SCAN_RUNNING
        route = urlparse(self.path).path
        if route == "/api/gem-hunter/v2/ingest":
            supplied = self.headers.get("Authorization", "")
            if not supplied.startswith("Bearer ") or len(supplied) <= 7:
                return self._send(401, b'{"status":"UNAUTHORIZED"}', "application/json; charset=utf-8")
            source_token = supplied[7:].strip()
            if self.headers.get("Origin"):
                return self._send(403, b'{"status":"SERVER_TO_SERVER_ONLY"}', "application/json; charset=utf-8")
            if "application/json" not in self.headers.get("Content-Type", "").lower():
                return self._send(415, b'{"status":"JSON_REQUIRED"}', "application/json; charset=utf-8")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return self._send(400, b'{"status":"INVALID_CONTENT_LENGTH"}', "application/json; charset=utf-8")
            if length <= 0 or length > 524_288:
                return self._send(413, b'{"status":"INVALID_SNAPSHOT_SIZE"}', "application/json; charset=utf-8")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or payload.get("read_only") is not True or payload.get("trade_authorization") != "NO_TRADE":
                    return self._send(422, b'{"status":"READ_ONLY_SNAPSHOT_REQUIRED"}', "application/json; charset=utf-8")
                source_url = str(payload.get("source_database_url") or "").strip()
                source_db = connect_turso_database(source_url, source_token)
                try:
                    source_db.execute("SELECT 1").fetchone()
                finally:
                    source_db.close()
                candidates = payload.get("candidates")
                saved = gem_hunter_v2_ingest_candidates(candidates)
                result = {
                    "status": "OK", "ingested": len(saved),
                    "candidate_count": len(saved), "read_only": True,
                    "trade_authorization": "NO_TRADE",
                }
                return self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            except (ValueError, TypeError, KeyError) as exc:
                return self._send(422, json.dumps({"status":"REJECTED","reason":str(exc)[:120]},ensure_ascii=False).encode(),"application/json; charset=utf-8")
            except Exception as exc:
                return self._send(503, json.dumps({"status":"UNAVAILABLE","error_type":type(exc).__name__},ensure_ascii=False).encode(),"application/json; charset=utf-8")
        if route == "/api/blofin/demo/v2-autopilot/publish":
            try:
                _verify_staging_github_oidc_token(self.headers.get("Authorization", ""))
            except PermissionError:
                return self._send(401, b'{"status":"UNAUTHORIZED"}', "application/json; charset=utf-8")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return self._send(400, b'{"status":"INVALID_CONTENT_LENGTH"}', "application/json; charset=utf-8")
            if length <= 0 or length > 131_072:
                return self._send(413, b'{"status":"INVALID_REPORT_SIZE"}', "application/json; charset=utf-8")
            try:
                report = json.loads(self.rfile.read(length).decode("utf-8"))
                result = _publish_blofin_v2_cycle_report(report)
                return self._send(200, json.dumps(result, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            except (ValueError, RuntimeError) as exc:
                return self._send(422, json.dumps({
                    "status": "REJECTED", "reason": str(exc)[:120],
                }).encode(), "application/json; charset=utf-8")
            except Exception as exc:
                return self._send(503, json.dumps({
                    "status": "UNAVAILABLE", "error_type": type(exc).__name__,
                }).encode(), "application/json; charset=utf-8")
        if route not in {"/scan", "/paper-bot/tick", "/futures-paper-bot/tick", "/api/ai/diagnostic", "/api/ai/dry-run", "/api/blofin/demo/execute-once", "/api/blofin/demo/smoke-test", "/api/blofin/demo/autopilot/control", "/api/blofin/demo/v2-autopilot/run-once"}:
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        origin = self.headers.get("Origin")
        request_host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc.lower() != request_host.lower():
            return self._send(403, b"Cross-origin scan requests are not allowed", "text/plain; charset=utf-8")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return self._send(400, b"Invalid Content-Length", "text/plain; charset=utf-8")
        if length <= 0 or length > 16_384:
            return self._send(413, b"Invalid request size", "text/plain; charset=utf-8")
        try:
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            if route == "/api/blofin/demo/v2-autopilot/run-once":
                if (
                    not origin
                    or urlparse(origin).netloc.lower() != request_host.lower()
                    or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}
                ):
                    return self._send(403, b'{"status":"FORBIDDEN"}', "application/json; charset=utf-8")
                expected_token = os.getenv("BLOFIN_DASHBOARD_TOKEN", "")
                if not _blofin_dashboard_token_valid(self.headers.get("Authorization", ""), expected_token):
                    return self._send(401, json.dumps({
                        "status": "UNAUTHORIZED", "message_fa": "توکن پنل معتبر نیست."
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if form.get("confirm", [""])[0] != "1":
                    return self._send(400, b'{"status":"CONFIRMATION_REQUIRED"}', "application/json; charset=utf-8")
                if os.getenv("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED", "").strip().lower() != "true" or not _enabled_v2_env("BLOFIN_DEMO_TRADING_ENABLED"):
                    return self._send(409, json.dumps({
                        "status": "BLOCKED", "blocker": "V2_DEMO_FLAGS_DISABLED",
                        "message_fa": "دو کلید ایمنی V2 و BloFin Demo باید در Render فعال باشند؛ هیچ چرخه‌ای آغاز نشد.",
                        "orders_submitted": False,
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if os.getenv("BLOFIN_DEMO_AUTOPILOT_ENABLED", "").strip().lower() != "false":
                    return self._send(409, json.dumps({
                        "status": "BLOCKED", "blocker": "LEGACY_AUTOPILOT_NOT_FALSE",
                        "message_fa": "اتوپایلوت قدیمی باید صریحاً خاموش باشد.",
                        "orders_submitted": False,
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if storage_backend() != "TURSO_CLOUD_SQLITE":
                    return self._send(409, json.dumps({
                        "status": "BLOCKED", "blocker": "DURABLE_TURSO_NOT_CONFIGURED",
                        "message_fa": "ذخیره‌سازی پایدار Turso آماده نیست.",
                        "orders_submitted": False,
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if os.getenv("BLOFIN_DEMO_AUTOPILOT_ROLE", "WEB").strip().upper() != "WEB":
                    return self._send(409, json.dumps({
                        "status": "ROLE_BLOCKED", "orders_submitted": False,
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if not V2_AUTOPILOT_RUN_LOCK.acquire(blocking=False):
                    with LOCK:
                        active_job_id = V2_AUTOPILOT_RUN_STATE.get("job_id")
                    return self._send(409, json.dumps({
                        "status": "ALREADY_RUNNING", "job_id": active_job_id,
                        "message_fa": "یک چرخه V2 در حال اجراست؛ همان چرخه را دنبال کن.",
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                job_id = uuid.uuid4().hex
                with LOCK:
                    V2_AUTOPILOT_RUN_STATE.update({
                        "status": "QUEUED", "job_id": job_id,
                        "started_at_utc": datetime.now(timezone.utc).isoformat(),
                        "finished_at_utc": None, "result": None, "error_type": None,
                    })
                worker = threading.Thread(
                    target=_run_challenge1_v2_dashboard_cycle,
                    args=(job_id,), daemon=True, name="challenge1-v2-dashboard-cycle",
                )
                try:
                    worker.start()
                except Exception:
                    V2_AUTOPILOT_RUN_LOCK.release()
                    with LOCK:
                        V2_AUTOPILOT_RUN_STATE.update({"status": "WORKER_START_FAILED", "error_type": "ThreadStartError"})
                    return self._send(503, b'{"status":"WORKER_START_FAILED"}', "application/json; charset=utf-8")
                return self._send(202, json.dumps({
                    "status": "QUEUED", "job_id": job_id,
                    "message_fa": "چرخه واقعی CHALLENGE_1_V2 در محیط BloFin Demo آغاز شد؛ ورود فقط با تأیید هر دو ناظر و کنترل‌های ریسک مجاز است.",
                }, ensure_ascii=False).encode(), "application/json; charset=utf-8")

            if route == "/api/blofin/demo/autopilot/control":
                if (
                    not origin
                    or urlparse(origin).netloc.lower() != request_host.lower()
                    or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}
                ):
                    return self._send(403, b'{"status":"FORBIDDEN"}', "application/json; charset=utf-8")
                expected_token = os.getenv("BLOFIN_DASHBOARD_TOKEN", "")
                if not _blofin_dashboard_token_valid(self.headers.get("Authorization", ""), expected_token):
                    return self._send(401, json.dumps({
                        "status": "UNAUTHORIZED",
                        "message_fa": "توکن پنل معتبر نیست یا در Environment سرویس تنظیم نشده است.",
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if form.get("confirm", [""])[0] != "1":
                    return self._send(400, b'{"status":"CONFIRMATION_REQUIRED"}', "application/json; charset=utf-8")
                action = str(form.get("action", [""])[0] or "").strip().lower()
                if action not in {"start", "stop"}:
                    return self._send(400, json.dumps({
                        "status": "INVALID_ACTION",
                        "message_fa": "فرمان اتوپایلوت باید start یا stop باشد.",
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")

                if action == "stop":
                    _set_blofin_demo_autopilot_runtime_override(False)
                    with LOCK:
                        BLOFIN_AUTOPILOT_STATE.update({
                            "enabled": False,
                            "status": "STOPPING",
                            "search_continues": False,
                            "next_cycle_scheduled": False,
                            "next_cycle_at_utc": None,
                            "next_cycle_in_seconds": None,
                            "last_message_fa": "فرمان توقف دریافت شد؛ چرخه جاری تکمیل می‌شود و چرخه جدیدی شروع نخواهد شد.",
                        })
                    _persist_blofin_autopilot_state()
                    return self._send(200, json.dumps({
                        "status": "STOPPING",
                        "message_fa": "توقف ثبت شد؛ چرخه جاری قطع نمی‌شود و پس از پایان آن چرخه جدیدی آغاز نخواهد شد.",
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")

                configured_profile = os.getenv("BLOFIN_DEMO_AUTOPILOT_PROFILE", "CHALLENGE_1").strip().upper()
                configured_profile = (
                    configured_profile
                    if configured_profile in {"CHALLENGE_1", "CHALLENGE_1_V2"}
                    else "CHALLENGE_1"
                )
                if configured_profile == "CHALLENGE_1_V2":
                    if os.getenv("BLOFIN_CHALLENGE1_V2_ORCHESTRATION_ENABLED", "").strip().lower() != "true":
                        return self._send(409, json.dumps({
                            "status": "BLOCKED",
                            "blocker": "V2_ORCHESTRATION_FEATURE_FLAG_DISABLED",
                            "message_fa": "پروفایل AutoPilot روی V2 تنظیم شده اما Feature Flag مسیر مستقل خاموش است.",
                            "orders_submitted": False,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    if storage_backend() != "TURSO_CLOUD_SQLITE":
                        return self._send(409, json.dumps({
                            "status": "BLOCKED",
                            "blocker": "DURABLE_TURSO_NOT_CONFIGURED",
                            "message_fa": "ذخیره‌سازی پایدار Turso برای چرخه V2 آماده نیست.",
                            "orders_submitted": False,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                else:
                    readiness = _blofin_demo_execution_readiness()
                    if readiness.get("status") != "READY":
                        return self._send(409, json.dumps({
                            "status": "BLOCKED",
                            "message_fa": readiness.get("message_fa"),
                            "blockers": readiness.get("blockers") or [],
                            "blockers_fa": readiness.get("blockers_fa") or [],
                            "readiness": readiness,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                role = os.getenv("BLOFIN_DEMO_AUTOPILOT_ROLE", "WEB").strip().upper() or "WEB"
                if role != "WEB":
                    return self._send(409, json.dumps({
                        "status": "ROLE_BLOCKED",
                        "message_fa": "این سرویس در نقش WEB نیست؛ اتوپایلوت از این پردازش شروع نمی‌شود.",
                        "runtime_role": role,
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                with LOCK:
                    active_jobs = [
                        item for item in BLOFIN_EXECUTION_JOBS.values()
                        if item.get("status") in {"queued", "running"}
                    ]
                    worker_alive = bool(
                        BLOFIN_AUTOPILOT_THREAD is not None
                        and BLOFIN_AUTOPILOT_THREAD.is_alive()
                    )
                if active_jobs:
                    return self._send(409, json.dumps({
                        "status": "EXECUTION_ALREADY_RUNNING",
                        "message_fa": "یک چرخه معامله در حال اجراست؛ پس از تعیین‌تکلیف همان چرخه اتوپایلوت را شروع کن.",
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                _set_blofin_demo_autopilot_runtime_override(True)
                _start_blofin_demo_autopilot()
                with LOCK:
                    worker_alive = bool(
                        BLOFIN_AUTOPILOT_THREAD is not None
                        and BLOFIN_AUTOPILOT_THREAD.is_alive()
                    )
                if not worker_alive:
                    _set_blofin_demo_autopilot_runtime_override(False)
                    return self._send(503, json.dumps({
                        "status": "WORKER_START_FAILED",
                        "message_fa": "کارگر اتوپایلوت شروع نشد؛ نقش سرویس و لاگ‌های Staging را بررسی کن.",
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return self._send(202, json.dumps({
                    "status": "STARTING",
                    "message_fa": "اتوپایلوت Challenge 1 فعال شد؛ پایش از چرخه بعدی و پس از تکمیل کندل ۵ دقیقه‌ای ادامه می‌یابد.",
                }, ensure_ascii=False).encode(), "application/json; charset=utf-8")

            if route == "/api/blofin/demo/smoke-test":
                if (
                    not origin
                    or urlparse(origin).netloc.lower() != request_host.lower()
                    or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}
                ):
                    return self._send(403, b'{"status":"FORBIDDEN"}', "application/json; charset=utf-8")
                # A separately managed smoke-test token lets an operator launch
                # this one-purpose Demo test without changing the dashboard token.
                expected_token = (
                    os.getenv("BLOFIN_DEMO_SMOKE_TEST_TOKEN", "").strip()
                    or os.getenv("BLOFIN_DASHBOARD_TOKEN", "")
                )
                if not _blofin_dashboard_token_valid(self.headers.get("Authorization", ""), expected_token):
                    return self._send(401, json.dumps({
                        "status": "UNAUTHORIZED", "message_fa": "مجوز تست مستقل معتبر نیست."
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if form.get("confirm", [""])[0] != "1":
                    return self._send(400, b'{"status":"CONFIRMATION_REQUIRED"}', "application/json; charset=utf-8")
                readiness = _blofin_demo_smoke_test_readiness()
                if readiness.get("status") != "READY":
                    return self._send(409, json.dumps({
                        "status": "BLOCKED", "message_fa": readiness.get("message_fa"),
                        "readiness": readiness,
                    }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                with BLOFIN_EXECUTION_GUARD:
                    with LOCK:
                        active = any(
                            item.get("status") in {"queued", "running"}
                            for item in BLOFIN_EXECUTION_JOBS.values()
                        )
                    if active:
                        return self._send(409, json.dumps({
                            "status": "ALREADY_RUNNING",
                            "message_fa": "یک چرخه اجرای Demo هم‌اکنون در حال اجراست."
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    job_id = uuid.uuid4().hex
                    _set_blofin_execution_job(
                        job_id, "queued", stage="QUEUED",
                        message_fa="تست ربات در محیط دمو صرافی در صف اجرای مستقل قرار گرفت؛ هنوز سفارشی ارسال نشده است.",
                        test_name="تست ربات در محیط دمو صرافی",
                    )
                    worker = threading.Thread(
                        target=_run_blofin_demo_smoke_test_job,
                        args=(job_id,), daemon=True, name="blofin-demo-smoke-test",
                    )
                    try:
                        worker.start()
                    except Exception:
                        _set_blofin_execution_job(
                            job_id, "failed", stage="WORKER_START_FAILED",
                            message_fa="کارگر تست مستقل شروع نشد.",
                        )
                        return self._send(503, b'{"status":"WORKER_START_FAILED"}', "application/json; charset=utf-8")
                return self._send(202, json.dumps({
                    "status": "QUEUED", "job_id": job_id,
                    "message_fa": "تست مستقل ربات در BloFin Demo آغاز شد.",
                }, ensure_ascii=False).encode(), "application/json; charset=utf-8")

            if route == "/api/blofin/demo/execute-once":
                if (
                    not origin
                    or urlparse(origin).netloc.lower() != request_host.lower()
                    or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}
                ):
                    return self._send(403, b'{"status":"FORBIDDEN"}', "application/json; charset=utf-8")
                expected_token = os.getenv("BLOFIN_DASHBOARD_TOKEN", "")
                if not _blofin_dashboard_token_valid(self.headers.get("Authorization", ""), expected_token):
                    return self._send(401, json.dumps({"status":"UNAUTHORIZED","message_fa":"توکن پنل معتبر نیست."}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                instrument_id = str(form.get("instrument", [""])[0] or "").strip().upper()
                execution_profile = str(form.get("profile", ["CHALLENGE_1"])[0] or "CHALLENGE_1").strip().upper()
                if (
                    form.get("confirm", [""])[0] != "1"
                    or not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", instrument_id)
                    or execution_profile not in {"CHALLENGE_1", "CHALLENGE_1_V2"}
                ):
                    return self._send(400, b'{"status":"CONFIRMATION_REQUIRED"}', "application/json; charset=utf-8")
                if execution_profile == "CHALLENGE_1_V2":
                    if os.getenv("BLOFIN_CHALLENGE1_V2_ORCHESTRATION_ENABLED", "").strip().lower() != "true":
                        return self._send(409, json.dumps({
                            "status": "BLOCKED",
                            "blocker": "V2_ORCHESTRATION_FEATURE_FLAG_DISABLED",
                            "message_fa": "مسیر مستقل V2 با Feature Flag خاموش است؛ هیچ سفارش یا چرخه‌ای شروع نشد.",
                            "orders_submitted": False,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    if storage_backend() != "TURSO_CLOUD_SQLITE":
                        return self._send(409, json.dumps({
                            "status": "BLOCKED",
                            "blocker": "DURABLE_TURSO_NOT_CONFIGURED",
                            "message_fa": "برای اجرای V2 فقط‌خواندنی، دفتر ثبت پایدار Turso باید قابل دسترسی باشد.",
                            "orders_submitted": False,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    if (
                        os.getenv("BLOFIN_DEMO_AUTOPILOT_ENABLED", "").strip().lower() != "false"
                        or _blofin_demo_autopilot_enabled()
                    ):
                        return self._send(409, json.dumps({
                            "status": "BLOCKED",
                            "blocker": "AUTOPILOT_MUST_REMAIN_DISABLED",
                            "message_fa": "برای اجرای دستی V2، AutoPilot باید در Render خاموش باشد؛ هیچ چرخه‌ای شروع نشد.",
                            "orders_submitted": False,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    required_flags = {
                        "SINGLE_SHOT_DEMO_TEST": os.getenv("SINGLE_SHOT_DEMO_TEST", "").strip().lower() == "true",
                        "BLOFIN_DEMO_TRADING_ENABLED": os.getenv("BLOFIN_DEMO_TRADING_ENABLED", "").strip().lower() == "true",
                        "CHALLENGE_1_V2_SINGLE_SHOT_WRITE": os.getenv("CHALLENGE_1_V2_SINGLE_SHOT_WRITE", "").strip().lower() == "true",
                    }
                    missing_flags = [key for key, enabled in required_flags.items() if not enabled]
                    if missing_flags:
                        return self._send(409, json.dumps({
                            "status": "BLOCKED",
                            "blocker": "V2_SINGLE_SHOT_FLAGS_INCOMPLETE",
                            "missing_flags": missing_flags,
                            "message_fa": "کلیدهای ایمنی اجرای یک‌باره V2 کامل نیستند؛ هیچ چرخه‌ای شروع نشد.",
                            "orders_submitted": False,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    lock_store = None
                    try:
                        lock_store = BloFinDemoEventStore()
                        single_shot_lock = lock_store.single_shot_lock_status()
                    except Exception:
                        single_shot_lock = None
                    finally:
                        if lock_store is not None:
                            lock_store.close()
                    if not isinstance(single_shot_lock, dict) or not isinstance(single_shot_lock.get("locked"), bool):
                        return self._send(503, json.dumps({
                            "status": "BLOCKED",
                            "blocker": "SINGLE_SHOT_LOCK_UNREADABLE",
                            "message_fa": "قفل دائمی Single-Shot قابل خواندن نیست؛ اجرای V2 متوقف شد.",
                            "orders_submitted": False,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    if single_shot_lock.get("locked") or int(single_shot_lock.get("consumed_orders") or 0) != 0:
                        return self._send(409, json.dumps({
                            "status": "BLOCKED",
                            "blocker": "SINGLE_SHOT_ALREADY_CONSUMED",
                            "single_shot_lock": single_shot_lock,
                            "message_fa": "سهمیه یک‌بارمصرف قبلاً مصرف شده است؛ اجرای مجدد مجاز نیست.",
                            "orders_submitted": False,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                else:
                    readiness = _blofin_demo_execution_readiness()
                    if readiness.get("status") != "READY":
                        return self._send(409, json.dumps({
                            "status": "BLOCKED", "message_fa": readiness.get("message_fa"),
                            "readiness": readiness,
                        }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                with BLOFIN_EXECUTION_GUARD:
                    with LOCK:
                        active = any(
                            item.get("status") in {"queued", "running"}
                            for item in BLOFIN_EXECUTION_JOBS.values()
                        )
                    if active:
                        return self._send(409, json.dumps({"status":"ALREADY_RUNNING","message_fa":"یک آزمایش دمو در حال اجراست."}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    job_id = uuid.uuid4().hex
                    _set_blofin_execution_job(
                        job_id, "queued", stage="QUEUED",
                        message_fa=f"چرخه Demo برای {instrument_id} با ربات {execution_profile} ثبت شد؛ هنوز سفارشی ارسال نشده است.",
                        trade_context={"instrument_id": instrument_id, "execution_profile": execution_profile},
                        selection_context={
                            "selection_method": "MANUAL_OPERATOR",
                            "selection_reason": "The operator explicitly selected this instrument for a Challenge 1 Demo cycle.",
                            "configured_universe": [],
                        },
                    )
                    worker = threading.Thread(
                        target=_run_blofin_demo_execution_job,
                        args=(job_id, instrument_id, execution_profile),
                        daemon=True, name="blofin-demo-manual-cycle",
                    )
                    try:
                        worker.start()
                    except Exception:
                        _set_blofin_execution_job(
                            job_id, "failed", stage="WORKER_START_FAILED",
                            message_fa="کارگر اجرای سفارش دمو شروع نشد.",
                        )
                        return self._send(503, b'{"status":"WORKER_START_FAILED"}', "application/json; charset=utf-8")
                return self._send(202, json.dumps({
                    "status": "QUEUED", "job_id": job_id,
                    "message_fa": "تحلیل Strategy 1 با الگوهای کندلی و نموداری در BloFin Demo آغاز شد.",
                }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            if route == "/api/ai/dry-run":
                if (
                    not origin
                    or urlparse(origin).netloc.lower() != request_host.lower()
                    or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}
                ):
                    return self._send(403, b'{"status":"FORBIDDEN","message":"Same-origin request required."}',
                                      "application/json; charset=utf-8")
                if form.get("confirm", [""])[0] != "1":
                    return self._send(400, b'{"status":"CONFIRMATION_REQUIRED"}',
                                      "application/json; charset=utf-8")
                payload = _run_ai_synthetic_dry_run_limited()
                status_code = _ai_dry_run_http_status(payload)
                if status_code >= 400:
                    # Structured, secret-free diagnostics make provider/configuration failures
                    # distinguishable in Render logs without persisting prompts or credentials.
                    print(
                        "CSR_AI_DRY_RUN_DIAGNOSTIC "
                        + json.dumps(_ai_dry_run_safe_diagnostic(payload), ensure_ascii=False, sort_keys=True),
                        flush=True,
                    )
                return self._send(status_code, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")

            if route == "/api/ai/diagnostic":
                if (
                    not origin
                    or urlparse(origin).netloc.lower() != request_host.lower()
                    or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}
                ):
                    return self._send(403, b'{"status":"FORBIDDEN","message":"Same-origin request required."}',
                                      "application/json; charset=utf-8")
                if form.get("confirm", [""])[0] != "1":
                    return self._send(400, b'{"status":"CONFIRMATION_REQUIRED"}',
                                      "application/json; charset=utf-8")
                payload = _run_ai_diagnostic_limited()
                result_status = str(payload.get("status") or "UNKNOWN")
                status_code = 200 if result_status == "CONNECTED" else (
                    429 if result_status in {"RATE_LIMITED", "DIAGNOSTIC_COOLDOWN", "DAILY_DIAGNOSTIC_LIMIT"} else
                    503 if result_status == "MISSING_API_KEY" else 502
                )
                return self._send(status_code, json.dumps(payload, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")

            asset = _parse_scan_asset(form.get("asset", [""])[0].strip())
            if route == "/paper-bot/tick" and (
                asset.market_type != "cex" or asset.symbol not in PAPER_ASSETS
            ):
                raise ValueError("Paper trading accepts only the configured major CEX spot universe.")
            if route == "/futures-paper-bot/tick" and (
                asset.market_type != "perp" or asset.symbol not in FUTURES_PAPER_ASSETS
            ):
                raise ValueError("Futures paper trading accepts only the configured major USDT-M perpetual universe.")
            headline = form.get("headline", [""])[0].strip() if route == "/scan" else ""
            social_post = form.get("social_post", [""])[0].strip() if route == "/scan" else ""
            global JOB_FENCE_COUNTER
            canonical_request = json.dumps(
                {"route": route, "asset": asset.raw, "headline": headline, "social_post": social_post},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            )
            idempotency_key = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
            job_id = uuid.uuid4().hex
            task = {"job_id": job_id, "route": route, "asset": asset,
                    "headline": headline, "social_post": social_post,
                    "idempotency_key": idempotency_key}
            _ensure_scan_worker()
            with LOCK:
                existing_id = ACTIVE_SCAN_KEYS.get(idempotency_key)
                existing = JOBS.get(existing_id) if existing_id else None
                if existing and existing.get("status") in {"queued", "running"}:
                    job_id = existing_id
                else:
                    if SCAN_QUEUE.full():
                        return self._send(429, _page("<h2>صف اسکن پر است</h2><p>۵۰ درخواست در صف هستند. کمی بعد دوباره تلاش کن.</p><a href='/'>بازگشت</a>"))
                    ACTIVE_SCAN_KEYS[idempotency_key] = job_id
                    JOB_FENCE_COUNTER += 1
                    created_at = datetime.now(timezone.utc)
                    JOBS[job_id] = {
                        "status": "queued",
                        "queue_position": SCAN_QUEUE.qsize() + 1,
                        "created_at_utc": created_at.isoformat(),
                        "valid_until_utc": (created_at + timedelta(seconds=JOB_LEASE_SECONDS)).isoformat(),
                        "idempotency_key": idempotency_key,
                        "fencing_token": JOB_FENCE_COUNTER,
                    }
                    task["fencing_token"] = JOBS[job_id]["fencing_token"]
                    while len(JOBS) > 100:
                        oldest = next(iter(JOBS))
                        if JOBS[oldest].get("status") in {"queued", "running"}:
                            break
                        JOBS.pop(oldest)
                    SCAN_QUEUE.put_nowait(task)
            if route == "/paper-bot/tick":
                body = (
                    "<h2>چرخهٔ ربات آزمایشی آغاز شد</h2><p>شناسه: <code>" + job_id + "</code></p>"
                    "<p id='state' aria-live='polite'>در حال ارزیابی دادهٔ بازار و مدیریت موقعیت کاغذی...</p>"
                    "<div id='result' aria-live='polite'><p>نتیجه پس از تکمیل چرخه به فارسی نمایش داده می‌شود.</p></div>"
                    "<p><a href='/'>بازگشت به داشبورد</a></p>"
                    + _paper_tick_page_script(job_id)
                )
                return self._send(202, _page(body, "Paper Bot Cycle"))
            if route == "/futures-paper-bot/tick":
                body = (
                    "<h2>چرخهٔ فیوچرز آزمایشی آغاز شد</h2><p>شناسه: <code>" + job_id + "</code></p>"
                    "<p id='futures-state' aria-live='polite'>در حال بررسی داده‌های بازار و شرایط ورود...</p>"
                    "<section class='card' id='futures-report' aria-live='polite'><p>گزارش فارسی پس از تکمیل چرخه اینجا نمایش داده می‌شود.</p></section>"
                    "<details class='card'><summary>جزئیات فنی و دادهٔ خام</summary><pre id='futures-result' style='white-space:pre-wrap;overflow-wrap:anywhere'></pre></details>"
                    "<p><a href='/'>بازگشت به داشبورد</a></p>"
                    "<script>(function(){const id='" + job_id + "',state=document.getElementById('futures-state'),report=document.getElementById('futures-report'),out=document.getElementById('futures-result');"
                    "function addLine(label,value){const row=document.createElement('p'),strong=document.createElement('strong'),span=document.createElement('span');strong.textContent=label+'： ';span.textContent=value===null||value===undefined||value===''?'—':String(value);row.appendChild(strong);row.appendChild(span);report.appendChild(row);}"
                    "async function poll(){try{const r=await fetch('/jobs/'+id,{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();"
                    "if(j.status==='completed'){const p=j.result||{},c=p.cycle||{},f=p.report_fa||{},s=p.futures_state||{},a=s.account||{},risk=s.risk_controls||{};"
                    "state.textContent='چرخه تکمیل شد؛ نتیجه در گزارش فارسی آمده است.';report.replaceChildren();"
                    "addLine('نتیجه',f.status||c.status_fa||'نامشخص');addLine('تصمیم',f.decision||c.decision_fa||'نامشخص');addLine('جهت',f.direction||c.direction_fa||'بدون معامله');"
                    "addLine('دلیل',f.reason||c.reason_fa||'دلیل ثبت نشده است.');if(f.technical_error_type||f.technical_detail)addLine('جزئیات فنی',String(f.technical_error_type||'')+(f.technical_detail?': '+f.technical_detail:''));"
                    "addLine('موجودی فرضی Paper',Number(a.balance_usd||0).toFixed(2)+' USD');addLine('ارزش فرضی حساب',Number(a.equity_usd||0).toFixed(2)+' USD');"
                    "addLine('موقعیت باز',s.open_position?'وجود دارد':'وجود ندارد');addLine('ترمز ریسک حساب',f.risk_circuit_breaker_fa||risk.risk_circuit_breaker_status_fa||'نامشخص');"
                    "addLine('وضعیت هوش مصنوعی',f.ai_status_fa||'در این چرخه تأیید نشده است.');addLine('محاسبه لیکوییدیشن',f.liquidation_model_status||'نامشخص');"
                    "addLine('سفارش واقعی','هیچ سفارشی ارسال نشد');addLine('حالت اجرا','فقط Paper و فقط‌خواندنی');out.textContent=JSON.stringify(p,null,2);return;}"
                    "if(j.status==='failed'){state.textContent='چرخه با خطا متوقف شد.';report.textContent='خطای چرخه: '+(j.error||'جزئیات در دسترس نیست');out.textContent=JSON.stringify(j,null,2);return;}"
                    "state.textContent=j.status==='queued'?'چرخه در صف اجراست.':j.status==='running'?'در حال اجرای چرخه؛ هنوز تصمیم نهایی صادر نشده است.':'وضعیت چرخه: '+j.status;"
                    "}catch(e){state.textContent='ارتباط موقتاً قطع شد؛ فقط همان شناسهٔ چرخه دوباره بررسی می‌شود.';}"
                    "setTimeout(poll,2500);}poll();})();</script>"
                )
                return self._send(202, _page(body, "Futures Paper Bot"))
            body = (
                "<h2>اسکن آغاز شد</h2><p>شناسه کار: <code>" + job_id + "</code></p>"
                "<p id='state'>در حال جمع‌آوری داده‌های بازار، نهنگ‌ها و احساسات...</p>"
                 "<!--RADAR_ASSET_SCAN-->"
                "<section class='card'><h3 id='diagnosis-title'>تشخیص نهایی</h3><div id='diagnosis-headline' class='metric'></div><div id='diagnosis-short' class='grid'></div><div id='diagnosis-long' class='grid'></div><div id='diagnosis-reasons'></div></section>"
                "<section class='card'><h3>کف و سقف در بازه‌های مختلف</h3><div id='timeframe-range-table'></div><p id='candle-age-note' class='muted'></p></section>"
                "<section class='card'><h3>نمای کلی بازار و نمودار</h3><div id='market-summary' class='grid'></div>"
                "<div id='range-table'></div><canvas id='candles' width='1000' height='320'></canvas></section>"
                "<section class='card'><h3>Market Intelligence · DEX و مشتقات</h3><div id='mi-summary' class='grid'></div><div id='mi-notes' class='muted'></div></section>"
                "<section class='card'><h3>۱۲ فیلتر</h3><div id='gate-table'></div></section>"
                "<section class='card'><h3>خلاصه نتیجه اسکن</h3><div id='scan-summary' class='grid'></div></section><p><a href='/'>بازگشت</a></p>"
                "<script>const job='" + job_id + "';"
                "function cell(tag,text){const e=document.createElement(tag);e.textContent=text??'UNKNOWN';return e;}"
                "function table(container,headers,rows){const t=document.createElement('table'),h=document.createElement('thead'),hr=document.createElement('tr');headers.forEach(v=>hr.appendChild(cell('th',v)));h.appendChild(hr);t.appendChild(h);const b=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(v=>tr.appendChild(cell('td',v)));b.appendChild(tr);});t.appendChild(b);container.replaceChildren(t);}"
                "function renderMarket(m){const box=document.getElementById('market-summary');"
                "const items=[['وضعیت دارایی',m.asset_regime],['رژیم معیار',m.benchmark_regime],['قیمت آخر',m.current_price],['سن کندل (ساعت)',m.candle_age_hours],['وضعیت کف/سقف',m.status],['الگوی هندسی',(m.chart_pattern||{}).pattern||'نامشخص'],['وضعیت الگو',(m.chart_pattern||{}).status||'UNKNOWN'],['تأیید شکست با حجم',(m.chart_pattern||{}).breakout_confirmation||'UNKNOWN'],['حجم شکست / میانه ۲۰ کندل',(m.chart_pattern||{}).breakout_volume_ratio||'UNKNOWN']];"
                "box.replaceChildren();items.forEach(([k,v])=>{const d=document.createElement('div');d.className='metric';d.appendChild(cell('span',k));const strong=cell('strong',v);d.appendChild(strong);box.appendChild(d);});"
                "const labels={'7':'۷ روز','30':'۳۰ روز','90':'۹۰ روز','365':'۳۶۵ روز'};"
                "const rows=Object.entries(m.ranges||{}).map(([k,v])=>[labels[k]||k,v.status,v.low,v.high,v.candle_count,v.coverage_pct]);"
                "table(document.getElementById('range-table'),['بازه','وضعیت','کف','سقف','تعداد کندل','پوشش %'],rows);"
                "const canvas=document.getElementById('candles'),ctx=canvas.getContext('2d'),bars=m.candles||[];ctx.clearRect(0,0,canvas.width,canvas.height);"
                "if(!bars.length){ctx.fillStyle='#f59e0b';ctx.fillText('تاریخچه OHLC برای نمودار موجود نیست',20,30);return;}"
                "const lo=Math.min(...bars.map(x=>x.low)),hi=Math.max(...bars.map(x=>x.high)),span=Math.max(hi-lo,1e-12),pad=20,w=(canvas.width-2*pad)/bars.length;"
                "bars.forEach((x,i)=>{const cx=pad+i*w+w/2,y=v=>canvas.height-pad-(v-lo)/span*(canvas.height-2*pad);const up=x.close>=x.open;ctx.strokeStyle=up?'#22c55e':'#ef4444';ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.moveTo(cx,y(x.high));ctx.lineTo(cx,y(x.low));ctx.stroke();const top=y(Math.max(x.open,x.close)),bottom=y(Math.min(x.open,x.close));ctx.fillRect(cx-w*.32,top,Math.max(1,w*.64),Math.max(1,bottom-top));});const shape=m.chart_pattern||{};if(shape.status==='FORMING'||shape.status==='BREAKOUT_UP'||shape.status==='BREAKOUT_DOWN'){const drawLine=(line,color)=>{if(!line)return;ctx.save();ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash([7,5]);ctx.beginPath();ctx.moveTo(pad+(line.start_index+.5)*w,y(line.start_price));ctx.lineTo(pad+(line.end_index+.5)*w,y(line.end_price));ctx.stroke();ctx.restore();};drawLine(shape.upper_line,'#ffc66d');drawLine(shape.lower_line,'#72a7ff');(shape.pivots||[]).forEach(p=>{ctx.fillStyle=p.kind==='HIGH'?'#ffc66d':'#72a7ff';ctx.beginPath();ctx.arc(pad+(p.index+.5)*w,y(p.price),4,0,Math.PI*2);ctx.fill();});ctx.fillStyle='#edf4fb';ctx.fillText('Triangle: '+(shape.status||'UNKNOWN')+' / '+(shape.breakout||'NO BREAKOUT'),pad,16);}}"
                "function renderTimeframes(rows){const labels={'1h':'۱ ساعت','6h':'۶ ساعت','12h':'۱۲ ساعت','1d':'۱ روز','1w':'۱ هفته','1mo':'۱ ماه'};const data=Object.entries(rows||{}).map(([k,v])=>[labels[k]||k,v.status==='KNOWN'?'کامل':v.status==='PARTIAL'?'ناقص':'ناموجود',v.low,v.high,v.bar_count+'/'+v.expected_bars,v.coverage_pct===undefined?'—':v.coverage_pct+'%',v.age_hours===null||v.age_hours===undefined?'—':v.age_hours+' ساعت']);table(document.getElementById('timeframe-range-table'),['بازه','وضعیت داده','کف','سقف','تعداد کندل','پوشش','سن آخرین کندل'],data);}"
                "function renderDiagnosis(d){const title=document.getElementById('diagnosis-title'),head=document.getElementById('diagnosis-headline');title.textContent='تشخیص نهایی · '+(d.coin_name||'رمزارز')+' ('+(d.symbol||'')+')';head.replaceChildren(cell('strong',d.final_recommendation||'تشخیص در دسترس نیست'),cell('div','وضعیت کلی: '+(d.overall_signal_fa||'خنثی')+' · '+(d.explanation||'')));const makeCard=(label,item)=>{const box=document.createElement('div');box.className='metric';box.appendChild(cell('strong',label));box.appendChild(cell('div',item.action_fa||'ناموجود'));(item.reasons||[]).forEach(reason=>{const p=cell('p','• '+reason);box.appendChild(p);});return box;};document.getElementById('diagnosis-short').replaceChildren(makeCard('تصمیم کوتاه‌مدت',d.short_term||{}));document.getElementById('diagnosis-long').replaceChildren(makeCard('تصمیم بلندمدت / نگهداری',d.long_term||{}));const list=document.createElement('ul');(d.blockers||[]).forEach(row=>{const li=cell('li',(row.name?row.name+' — ':'')+row.reason+' ['+row.status+']');list.appendChild(li);});const reasons=document.getElementById('diagnosis-reasons');reasons.replaceChildren(cell('h4','چرا خرید توصیه می‌شود یا نمی‌شود؟'),list);document.getElementById('candle-age-note').textContent=d.candle_age_text||'سن کندل در دسترس نیست.';}"
                "function renderMarketIntelligence(m){const d=m.dex_screener||{},c=m.coinglass||{},s=m.scoring||{};"
                "const w=c.whale_intelligence||{};const items=[['وضعیت کلی',m.status],['توکن DEX',(d.token_name||'')+' '+(d.token_symbol||'')],['نقدینگی DEX (USD)',d.liquidity_usd],['حجم DEX در ۲۴ ساعت (USD)',d.volume_24h_usd],['سهم تعداد تراکنش خرید %',d.buy_transaction_share_pct],['تعداد تراکنش ۲۴ ساعت',d.total_transactions_24h],['عمر جفت‌ارز (ساعت)',d.pair_age_hours],['وضعیت CoinGlass',c.status],['Funding میانه %',c.funding_median_pct],['تعداد صرافی‌های Funding',c.funding_exchange_count],['Open Interest (USD)',c.open_interest_usd],['تغییر OI در ۲۴ ساعت %',c.open_interest_change_24h_pct],['وضعیت Whale API',w.status],['پوزیشن‌های نهنگ Hyperliquid',w.position_count],['کیف‌پول‌های نهنگ',w.wallet_count],['ارزش پوزیشن لانگ (USD)',w.long_notional_usd],['ارزش پوزیشن شورت (USD)',w.short_notional_usd],['خالص مواجهه جهت‌دار (USD)',w.net_directional_notional_usd],['هشدارهای ورود',w.open_alert_count],['هشدارهای خروج',w.close_alert_count],['تعدیل امتیاز تأیید',s.confirmation_adjustment],['تعدیل امتیاز ریسک',s.risk_adjustment],['فشار DEX',s.dex_flow_bias],['ازدحام مشتقات',s.coinglass_crowding]];const box=document.getElementById('mi-summary');box.replaceChildren();items.forEach(([k,v])=>{const d=document.createElement('div');d.className='metric';d.appendChild(cell('span',k));d.appendChild(cell('strong',v===null||v===undefined?'ناموجود':v));box.appendChild(d);});const whaleWallets=(w.positions||[]).slice(0,5).map(x=>'نهنگ '+(x.wallet||'آدرس نامشخص')+' · '+x.direction+' · '+(x.position_value_usd??'—')+' USD · اهرم '+(x.leverage??'—')+'x · PnL '+(x.unrealized_pnl_usd??'—'));const whaleAlerts=(w.alerts||[]).slice(0,5).map(x=>'هشدار '+x.action+' '+x.direction+' · '+(x.wallet||'آدرس نامشخص')+' · '+(x.position_value_usd??'—')+' USD');const notes=[...(s.notes||[]),d.reason,c.reason,w.reason,w.coverage,...whaleWallets,...whaleAlerts,m.disclaimer].filter(Boolean);document.getElementById('mi-notes').textContent=notes.join(' · ')||'دادهٔ تکمیلی خاصی از منابع بیرونی گزارش نشده است.';}"
                "window.CryptoRadar.scanning('radar-asset-scan', 'در حال جمع‌آوری داده و اجرای فیلترهای تحلیلی...');"
                "let pollAttempts=0;const pollStarted=Date.now();async function poll(){try{const r=await fetch('/jobs/'+job,{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();pollAttempts=0;document.getElementById('state').textContent=j.status;"
                "if(j.status==='completed'){const s=j.result||{};window.CryptoRadar.update('radar-asset-scan',[{symbol:s.symbol||s.asset,name:s.coin_name,signal:s.signal}],{state:'complete',caption:'نتیجهٔ واقعی موتور تحلیل: '+String(s.signal||'UNKNOWN')+' · امتیاز تأیید به‌تنهایی مجوز ورود نیست.'});renderDiagnosis(j.result.diagnosis||{});renderTimeframes(j.result.timeframe_ranges||{});renderMarket(j.result.market_overview||{});renderMarketIntelligence(j.result.market_intelligence||{});renderGates(j.result.gates);const s=j.result,box=document.getElementById('scan-summary');const items=[['نام ارز',s.coin_name||s.asset],['نماد',s.symbol||'—'],['وضعیت',((s.diagnosis||{}).overall_signal_fa)||s.signal],['امتیاز خام تأیید (مجوز ورود نیست)',s.confirmation_score],['امتیاز ریسک',s.estimated_risk_score],['رتبه فرصت',s.opportunity_rank],['خلاصه',s.summary_fa]];box.replaceChildren();items.forEach(([k,v])=>{const d=document.createElement('div');d.className='metric';d.appendChild(cell('span',k));d.appendChild(cell('strong',v));box.appendChild(d);});return;}"
                "if(j.status==='failed'){document.getElementById('state').textContent='اسکن ناموفق بود: '+j.error;window.CryptoRadar.error('radar-asset-scan','اسکن کامل نشد؛ هیچ سیگنال خرید یا فروشی ثبت نشده است.');return;}"
                "if(Date.now()-pollStarted>=1800000){document.getElementById('state').textContent='مهلت پیگیری تمام شد؛ برای ادامه وضعیت همین شناسه را بررسی کنید: /jobs/'+job;return;}"
                "}catch(e){pollAttempts+=1;const elapsed=Date.now()-pollStarted;if(elapsed>=1800000){document.getElementById('state').textContent='مهلت بازیابی ارتباط تمام شد؛ شناسهٔ همان اسکن: '+job;return;}const delay=Math.min(30000,2500*Math.pow(2,Math.min(pollAttempts,4)));document.getElementById('state').textContent='ارتباط موقتاً قطع شد؛ تلاش مجدد خودکار برای همان شناسه در '+Math.ceil(delay/1000)+' ثانیه...';setTimeout(poll,delay);return;}"
                "setTimeout(poll,2500);}poll();</script>"
            )
            return self._send(202, _page(body, "Scan Running"))
        except Exception as exc:
            with LOCK:
                SCAN_RUNNING = False
            message = html.escape(f"{type(exc).__name__}: {exc}")
            return self._send(400, _page(f"<h2>خطا در ورودی</h2><p>{message}</p><a href='/'>بازگشت</a>"))

    def log_message(self, fmt, *args):
        super().log_message(fmt, *args)


def _blofin_autopilot_cycle_summary(job: dict) -> dict:
    """Normalize signal labels and classify completed no-entry Demo cycles safely."""
    job = job if isinstance(job, dict) else {}
    raw_result = job.get("result")
    result = raw_result if isinstance(raw_result, dict) else {}
    result_analysis = result.get("analysis")
    job_analysis = job.get("analysis")
    if isinstance(result_analysis, dict) and result_analysis:
        analysis = result_analysis
    elif isinstance(job_analysis, dict):
        analysis = job_analysis
    else:
        analysis = {}

    raw_signal = (
        (result_analysis.get("signal") if isinstance(result_analysis, dict) else None)
        or (job_analysis.get("signal") if isinstance(job_analysis, dict) else None)
        or result.get("signal")
        or job.get("signal")
        or result.get("status")
    )
    raw_signal = getattr(raw_signal, "value", raw_signal)
    signal_value = re.sub(r"_+", "_", re.sub(r"[\s-]+", "_", str(raw_signal or "").strip().upper())).strip("_")
    cycle_status = str(job.get("status") or "UNKNOWN").strip().lower()
    orders_submitted = bool(result.get("orders_submitted", job.get("orders_submitted", False)))
    trade_executed = bool(result.get("trade_executed", job.get("trade_executed", False)))
    no_entry = (
        cycle_status == "completed"
        and not orders_submitted
        and not trade_executed
        and signal_value in {"", "NONE", "NO_TRADE", "NO_SIGNAL", "HOLD", "NEUTRAL", "UNKNOWN"}
    )
    return {
        "analysis": analysis,
        "cycle_status": cycle_status,
        "signal": signal_value or None,
        "orders_submitted": orders_submitted,
        "trade_executed": trade_executed,
        "no_entry": no_entry,
    }


def _blofin_demo_next_5m_scan_window(now_utc: datetime | None = None) -> tuple[int, str]:
    """Return the delay just after the next completed 5-minute entry candle."""
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    next_boundary_epoch = (int(current.timestamp()) // 300 + 1) * 300 + 3
    next_at = datetime.fromtimestamp(next_boundary_epoch, timezone.utc)
    delay_seconds = max(5, int(math.ceil((next_at - current).total_seconds())))
    return delay_seconds, next_at.isoformat()

def _blofin_demo_next_scan_after_cycle(
    cycle_started_at_utc: datetime, *, now_utc: datetime | None = None,
) -> tuple[int, str]:
    """Schedule from cycle start so a scan overlapping a candle close catches it."""
    _, boundary_iso = _blofin_demo_next_5m_scan_window(cycle_started_at_utc)
    target = datetime.fromisoformat(boundary_iso)
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if target <= current:
        # The scan crossed the candle boundary. Re-check shortly rather than
        # skipping the just-closed candle and sleeping for another full interval.
        target = current + timedelta(seconds=5)
    delay_seconds = max(5, int(math.ceil((target - current).total_seconds())))
    return delay_seconds, target.isoformat()

def _blofin_demo_autopilot_worker(max_cycles: int | None = None) -> None:
    """Run Challenge 1 on BloFin Demo; max_cycles enables safe scheduled one-shot runs."""
    interval_seconds = 300
    raw_symbols = os.getenv(
        "BLOFIN_DEMO_AUTOPILOT_INSTRUMENTS",
        ",".join(DEFAULT_CHALLENGE1_INSTRUMENTS),
    )
    instrument_ids = tuple(dict.fromkeys(
        symbol.strip().upper() for symbol in raw_symbols.split(",")
        if re.fullmatch(r"[A-Z0-9]{2,20}-USDT", symbol.strip().upper())
    ))
    if not instrument_ids:
        instrument_ids = DEFAULT_CHALLENGE1_INSTRUMENTS
    configured_profile = os.getenv("BLOFIN_DEMO_AUTOPILOT_PROFILE", "CHALLENGE_1").strip().upper()
    execution_profile = (
        configured_profile
        if configured_profile in {"CHALLENGE_1", "CHALLENGE_1_V2"}
        else "CHALLENGE_1"
    )
    instrument_id = instrument_ids[0]
    completed_cycles = 0
    with LOCK:
        BLOFIN_AUTOPILOT_STATE.update({
            "enabled": True,
            "status": "STARTING",
            "runtime_role": "WORKER",
            "interval_seconds": interval_seconds,
            "instrument_id": instrument_id,
            "instrument_ids": list(instrument_ids),
            "execution_profile": execution_profile,
            "last_message_fa": "اتوپایلوت Challenge 1 فعال شد؛ بازارهای منتخب برای الگوهای کندلی/نموداری پایش می‌شوند.",
        })
    _persist_blofin_autopilot_state()
    while True:
        if not _blofin_demo_autopilot_enabled():
            with LOCK:
                BLOFIN_AUTOPILOT_STATE.update({
                    "enabled": False, "status": "DISABLED",
                    "search_continues": False,
                    "next_cycle_scheduled": False,
                    "next_cycle_at_utc": None,
                    "next_cycle_in_seconds": None,
                    "last_message_fa": "اتوپایلوت با تنظیمات محیطی غیرفعال شده است.",
                })
            _persist_blofin_autopilot_state()
            if max_cycles is not None:
                return
            time.sleep(30)
            continue
        if not BLOFIN_AUTOPILOT_LOCK.acquire(blocking=False):
            if max_cycles is not None:
                raise RuntimeError("Another Autopilot cycle already holds the worker lock.")
            time.sleep(10)
            continue
        try:
            if SCAN_RUNNING:
                with LOCK:
                    BLOFIN_AUTOPILOT_STATE.update({
                        "status": "WAITING_FOR_SCAN_QUEUE",
                        "last_message_fa": "اسکن دیگری در حال اجراست؛ برای جلوگیری از تداخل، چرخه Demo به تعویق افتاد.",
                    })
                _persist_blofin_autopilot_state()
                if max_cycles is not None:
                    return
                time.sleep(30)
                continue
            started = datetime.now(timezone.utc).isoformat()
            batch_results = []
            batch_jobs = []
            for scan_index, scan_instrument in enumerate(instrument_ids, start=1):
                instrument_id = scan_instrument
                job_id = uuid.uuid4().hex
                with LOCK:
                    BLOFIN_AUTOPILOT_STATE.update({
                        "enabled": True,
                        "status": "RUNNING",
                        "last_cycle_started_at_utc": started,
                        "last_stage": "CHALLENGE_1_MULTI_SYMBOL_SCAN",
                        "instrument_id": instrument_id,
                        "instrument_ids": list(instrument_ids),
                        "execution_profile": execution_profile,
                        "last_message_fa": f"موتور Challenge 1 در حال اسکن {instrument_id} از {len(instrument_ids)} بازار منتخب است.",
                        "cycles_started": int(BLOFIN_AUTOPILOT_STATE.get("cycles_started") or 0) + (1 if not batch_jobs else 0),
                    })
                _persist_blofin_autopilot_state()
                _set_blofin_execution_job(
                    job_id, "queued", stage="AUTOPILOT_CYCLE",
                    message_fa=f"چرخه خودکار Challenge 1 برای {instrument_id} آغاز شد.",
                    trade_context={"instrument_id": instrument_id, "execution_profile": execution_profile},
                    selection_context={
                        "selection_method": "CONFIGURED_UNIVERSE_ORDER",
                        "selection_reason": (
                            "The scheduler scans the configured instrument list sequentially; "
                            "this is not a performance-ranked asset selection."
                        ),
                        "configured_universe": list(instrument_ids),
                        "scan_index": scan_index, "scan_count": len(instrument_ids),
                    },
                )
                _run_blofin_demo_execution_job(job_id, instrument_id, execution_profile)
                with LOCK:
                    finished_job = copy.deepcopy(BLOFIN_EXECUTION_JOBS.get(job_id) or {})
                summary = _blofin_autopilot_cycle_summary(finished_job)
                finished_result = finished_job.get("result") if isinstance(finished_job.get("result"), dict) else {}
                ai_review_summary = _blofin_ai_review_summary(finished_job)
                batch_results.append({
                    "instrument_id": instrument_id,
                    "job_id": job_id,
                    "status": finished_job.get("status", "UNKNOWN"),
                    "signal": summary.get("signal"),
                    "orders_submitted": summary.get("orders_submitted"),
                    "trade_executed": summary.get("trade_executed"),
                    "reason_fa": finished_result.get("reason_fa") or finished_job.get("message_fa"),
                    "failure_stage": finished_job.get("stage"),
                    "error_type": (
                        (finished_job.get("error") or {}).get("type")
                        if isinstance(finished_job.get("error"), dict) else None
                    ),
                    "ai_review_summary": ai_review_summary,
                    "ai_consensus": finished_job.get("ai_consensus") or finished_result.get("ai_consensus"),
                })
                batch_jobs.append((job_id, instrument_id))
            job_id, instrument_id = batch_jobs[-1]
            exchange_monitor = {}
            monitor_store = None
            try:
                monitor_store = BloFinDemoEventStore()
                latest_local_order = monitor_store.latest_order()
                monitor_instrument_id = (
                    str(latest_local_order.get("instrument_id") or instrument_id).upper()
                    if isinstance(latest_local_order, dict) else instrument_id
                )
                snapshot = _blofin_demo_live_monitor_snapshot(monitor_instrument_id)
                live_positions = [
                    row for row in snapshot.get("positions", [])
                    if isinstance(row, dict) and abs(float(row.get("positions") or 0)) > 0
                ]
                open_tpsl_rows = [
                    row for row in snapshot.get("open_tpsl_orders", [])
                    if isinstance(row, dict)
                ]
                closed_rows = [
                    row for row in snapshot.get("closed_positions", [])
                    if isinstance(row, dict)
                ]
                def safe_exchange_timestamp(row):
                    try:
                        return int(row.get("updateTime") or 0)
                    except (TypeError, ValueError, OverflowError):
                        return 0
                closed_rows.sort(key=safe_exchange_timestamp, reverse=True)
                latest_closed = closed_rows[0] if closed_rows else {}
                try:
                    latest_realized_pnl = float(latest_closed.get("realizedPnl"))
                    if not math.isfinite(latest_realized_pnl):
                        latest_realized_pnl = None
                except (TypeError, ValueError, OverflowError):
                    latest_realized_pnl = None
                latest_closed_position_id = str(latest_closed.get("positionId") or "").strip()
                learning_outcome_update = (
                    monitor_store.record_closed_position_outcome(
                        instrument_id=monitor_instrument_id,
                        history_rows=closed_rows,
                        partial_rows=snapshot.get("partially_closed_positions", []),
                        position_id=latest_closed_position_id,
                    )
                    if latest_closed_position_id else
                    {"status": "NO_CLOSED_POSITION_ID_IN_HISTORY"}
                )
                learning_integrity = monitor_store.learning_memory.integrity_report()
                exchange_monitor = {
                    "exchange_monitor_status": snapshot.get("status"),
                    "exchange_data_completeness": snapshot.get("data_completeness"),
                    "monitored_order_instrument": monitor_instrument_id,
                    "exchange_open_position_count": len(live_positions),
                    "exchange_open_positions": [
                        {
                            "instId": row.get("instId"),
                            "positionSide": row.get("positionSide"),
                            "positions": row.get("positions"),
                            "averagePrice": row.get("averagePrice"),
                            "markPrice": row.get("markPrice"),
                            "unrealizedPnl": row.get("unrealizedPnl"),
                            "leverage": row.get("leverage"),
                        } for row in live_positions[:3]
                    ],
                    "exchange_open_tpsl_order_count": len(open_tpsl_rows),
                    "exchange_open_tpsl_orders": [
                        {
                            "instId": row.get("instId"),
                            "positionSide": row.get("positionSide"),
                            "side": row.get("side"),
                            "size": row.get("size"),
                            "actualSize": row.get("actualSize"),
                            "tpTriggerPrice": row.get("tpTriggerPrice"),
                            "slTriggerPrice": row.get("slTriggerPrice"),
                            "state": row.get("state"),
                            "reduceOnly": row.get("reduceOnly"),
                        } for row in open_tpsl_rows[:10]
                    ],
                    "latest_closed_position": (
                        {
                            "historyId": latest_closed.get("historyId"),
                            "instId": latest_closed.get("instId"),
                            "side": latest_closed.get("side"),
                            "openAveragePrice": latest_closed.get("openAveragePrice"),
                            "closeAveragePrice": latest_closed.get("closeAveragePrice"),
                            "realizedPnl": latest_closed.get("realizedPnl"),
                            "realizedPnlRatio": latest_closed.get("realizedPnlRatio"),
                            "fee": latest_closed.get("fee"),
                            "createTime": latest_closed.get("createTime"),
                            "updateTime": latest_closed.get("updateTime"),
                        } if latest_closed else None
                    ),
                    "exchange_closed_positions_observed": len(closed_rows),
                    "latest_closed_realized_pnl": latest_realized_pnl,
                    "latest_closed_profitable": (
                        latest_realized_pnl > 0 if latest_realized_pnl is not None else None
                    ),
                    "learning_outcome_update": learning_outcome_update,
                    "learning_memory_integrity": learning_integrity,
                    "exchange_read_errors": snapshot.get("read_errors", []),
                }
            except Exception as monitor_exc:
                exchange_monitor = {
                    "exchange_monitor_status": "UNKNOWN",
                    "exchange_monitor_error_type": type(monitor_exc).__name__,
                }
            finally:
                if monitor_store is not None:
                    try:
                        monitor_store.close()
                    except Exception:
                        pass
            with LOCK:
                job = copy.deepcopy(BLOFIN_EXECUTION_JOBS.get(job_id) or {})
                result = job.get("result") if isinstance(job.get("result"), dict) else {}
                cycle_summary = _blofin_autopilot_cycle_summary(job)
                analysis = cycle_summary["analysis"]
                cycle_status = cycle_summary["cycle_status"]
                orders_submitted = cycle_summary["orders_submitted"]
                trade_executed = cycle_summary["trade_executed"]
                signal_value = cycle_summary["signal"]
                no_entry = cycle_summary["no_entry"]
                # The scheduler keeps running even when a candle was already finalized,
                # a signal was rejected, or a position is being managed. Reflect that fact
                # instead of showing a false stopped state for duplicate-candle cycles.
                autopilot_enabled = _blofin_demo_autopilot_enabled()
                continue_search = bool(autopilot_enabled)
                autopilot_status = "SEARCHING_FOR_SIGNAL" if continue_search else "DISABLED"
                autopilot_message = (
                    f"چرخهٔ {job_id} پایان یافت؛ پایش خودکار ادامه دارد و چرخهٔ بعدی در صف زمان‌بندی است."
                    if continue_search else "اتوپایلوت غیرفعال شده؛ چرخهٔ بعدی زمان‌بندی نمی‌شود."
                )
                BLOFIN_AUTOPILOT_STATE.update({
                    "enabled": bool(autopilot_enabled),
                    "status": autopilot_status,
                    "last_cycle_status": job.get("status", "UNKNOWN"),
                    "last_cycle_finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    "last_stage": job.get("stage"),
                    "last_message_fa": autopilot_message,
                    "last_result_status": result.get("status") or job.get("stage"),
                    "last_job_id": job_id,
                    "search_continues": continue_search,
                    "next_cycle_scheduled": bool(autopilot_enabled),
                    "last_cycle_diagnostics": {
                        "instrument_id": instrument_id,
                        "instrument_ids": list(instrument_ids),
                        "symbols_scanned": len(instrument_ids),
                        "symbol_results": batch_results,
                        "execution_profile": execution_profile,
                        "signal": signal_value or None,
                        "gate_pass_count": analysis.get("gate_pass_count"),
                        "gate_fail_count": analysis.get("gate_fail_count"),
                        "gate_unknown_count": analysis.get("gate_unknown_count"),
                        "orders_submitted": orders_submitted,
                        "trade_executed": trade_executed,
                        "average_fill_price": result.get("average_fill_price"),
                        "filled_size": result.get("filled_size"),
                        "reason_fa": result.get("reason_fa") or job.get("message_fa"),
                        "error_type": (
                            (job.get("error") or {}).get("type")
                            if isinstance(job.get("error"), dict) else None
                        ),
                        "open_positions": len(result.get("positions") or []),
                        "closed_positions": len(result.get("closed_positions") or []),
                        **exchange_monitor,
                    },
                })
            _persist_blofin_autopilot_state()
        except Exception as exc:
            with LOCK:
                BLOFIN_AUTOPILOT_STATE.update({
                    "status": "ERROR",
                    "last_cycle_finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    "last_message_fa": "چرخه خودکار با خطای کنترل‌شده متوقف شد: " + type(exc).__name__,
                })
            _persist_blofin_autopilot_state()
        finally:
            BLOFIN_AUTOPILOT_LOCK.release()
        completed_cycles += 1
        if max_cycles is not None and completed_cycles >= max_cycles:
            return
        try:
            cycle_started = datetime.fromisoformat(started)
            next_delay_seconds, next_cycle_at_utc = _blofin_demo_next_scan_after_cycle(cycle_started)
        except Exception:
            next_delay_seconds, next_cycle_at_utc = _blofin_demo_next_5m_scan_window()
        autopilot_enabled = _blofin_demo_autopilot_enabled()
        with LOCK:
            BLOFIN_AUTOPILOT_STATE.update({
                "status": "WAITING_FOR_NEXT_5M_CANDLE" if autopilot_enabled else "DISABLED",
                "interval_seconds": interval_seconds,
                "next_cycle_at_utc": next_cycle_at_utc if autopilot_enabled else None,
                "next_cycle_in_seconds": next_delay_seconds if autopilot_enabled else None,
                "next_cycle_scheduled": bool(autopilot_enabled),
            })
        _persist_blofin_autopilot_state()
        time.sleep(next_delay_seconds)


def _reconcile_blofin_demo_orders_on_startup() -> dict:
    """Probe durable Turso and reconcile all unresolved Demo orders using READ only."""
    global BLOFIN_STARTUP_RECONCILE_DONE
    with BLOFIN_STARTUP_RECONCILE_LOCK:
        if BLOFIN_STARTUP_RECONCILE_DONE:
            with LOCK:
                previous = copy.deepcopy(BLOFIN_AUTOPILOT_STATE.get("startup_reconciliation") or {})
            return previous
        BLOFIN_STARTUP_RECONCILE_DONE = True

    store = None
    result: dict[str, Any] = {
        "status": "SKIPPED",
        "reason": "DURABLE_TURSO_NOT_CONFIGURED",
        "orders_submitted": False,
        "trigger": "PROCESS_STARTUP",
    }
    try:
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            raise RuntimeError("DURABLE_TURSO_NOT_CONFIGURED")
        store = BloFinDemoEventStore()
        storage_probe = store.verify_durable_round_trip(connections=4)
        result["storage_probe"] = storage_probe
        if not storage_probe.get("verified"):
            result.update({
                "status": "BLOCKED",
                "reason": "TURSO_MULTI_CONNECTION_PROBE_FAILED",
            })
        else:
            reconciler = BloFinDemoExecutor(
                client=BloFinDemoTradeClient(timeout=10.0),
                store=store,
                policy=BloFinDemoRiskPolicy(max_leverage=1),
                trading_enabled=False,
            )
            sweep = reconciler.reconcile_unresolved_orders(limit=100)
            result.update(sweep)
            result["trigger"] = "PROCESS_STARTUP"
            result["storage_probe"] = storage_probe
            if result.get("orders_submitted") is not False:
                result["status"] = "BLOCKED"
                result["orders_submitted"] = False
                result["reason"] = "READ_ONLY_RECONCILIATION_REPORTED_UNSAFE_WRITE_STATE"
            elif (
                result.get("status") == "RECONCILED"
                and int(result.get("remaining_unresolved") or 0) == 0
            ):
                result["reason"] = (
                    "DURABLE_TURSO_PROBE_AND_READ_ONLY_RECONCILIATION_PASSED"
                )
            else:
                # Do not retain the pre-probe DURABLE_TURSO_NOT_CONFIGURED
                # placeholder after a successful durable storage probe.
                existing_reason = result.get("reason")
                if existing_reason == "DURABLE_TURSO_NOT_CONFIGURED":
                    existing_reason = None
                result["reason"] = existing_reason or (
                    "DURABLE_TURSO_PROBE_PASSED; "
                    "READ_ONLY_RECONCILIATION_REQUIRES_ATTENTION"
                )
    except Exception as exc:
        result.update({
            "status": "UNAVAILABLE",
            "reason": "STARTUP_RECONCILIATION_UNAVAILABLE",
            "error_type": type(exc).__name__,
            "orders_submitted": False,
        })
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    with LOCK:
        BLOFIN_AUTOPILOT_STATE["startup_reconciliation"] = copy.deepcopy(result)
        BLOFIN_AUTOPILOT_STATE["startup_reconciliation_at_utc"] = datetime.now(timezone.utc).isoformat()
        BLOFIN_AUTOPILOT_STATE["startup_reconciliation_orders_submitted"] = False
    safe_log = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "attempted": result.get("attempted"),
        "remaining_unresolved": result.get("remaining_unresolved"),
        "orders_submitted": False,
        "storage_probe_verified": bool(
            isinstance(result.get("storage_probe"), dict)
            and result["storage_probe"].get("verified") is True
        ),
        "storage_probe_connections": (
            result.get("storage_probe", {}).get("connections")
            if isinstance(result.get("storage_probe"), dict) else None
        ),
        "error_type": result.get("error_type"),
    }
    print("BLOFIN_DEMO_STARTUP_RECOVERY " + json.dumps(
        safe_log, sort_keys=True, separators=(",", ":")
    ))
    _persist_blofin_autopilot_state()
    return result


def _schedule_blofin_demo_startup_recovery() -> None:
    """Run startup Turso probing/recovery off the HTTP listener thread."""
    global BLOFIN_STARTUP_RECONCILE_THREAD
    with BLOFIN_STARTUP_RECONCILE_LOCK:
        if BLOFIN_STARTUP_RECONCILE_DONE:
            return
        if BLOFIN_STARTUP_RECONCILE_THREAD is not None and BLOFIN_STARTUP_RECONCILE_THREAD.is_alive():
            return
        BLOFIN_STARTUP_RECONCILE_THREAD = threading.Thread(
            target=_reconcile_blofin_demo_orders_on_startup,
            daemon=True,
            name="blofin-demo-startup-recovery",
        )
        BLOFIN_STARTUP_RECONCILE_THREAD.start()


def _start_blofin_demo_autopilot() -> None:
    """Start the in-web worker only in WEB role; API and WORKER roles stay separate."""
    global BLOFIN_AUTOPILOT_THREAD
    role = os.getenv("BLOFIN_DEMO_AUTOPILOT_ROLE", "WEB").strip().upper() or "WEB"
    if role not in {"WEB", "API", "WORKER"}:
        with LOCK:
            BLOFIN_AUTOPILOT_STATE.update({
                "enabled": False, "status": "ROLE_BLOCKED",
                "runtime_role": role,
                "last_message_fa": "نقش اجرای Autopilot نامعتبر است؛ هیچ چرخه‌ای شروع نشد.",
            })
        return
    if role != "WEB":
        with LOCK:
            BLOFIN_AUTOPILOT_STATE.update({
                "enabled": False, "status": "DISABLED",
                "runtime_role": role,
                "last_message_fa": (
                    "این پردازش فقط نقش API دارد؛ اجرای Worker در Runner مستقل انجام می‌شود."
                    if role == "API" else
                    "این پردازش در نقش Worker زمان‌بندی‌شده است؛ حلقهٔ دائمی وب اجرا نمی‌شود."
                ),
            })
        return
    _restore_blofin_autopilot_state()
    # Recovery is independent of the trading scheduler and remains READ-only.
    # Schedule it off the HTTP listener thread so a slow Turso network never
    # prevents the dashboard from binding and serving /health.
    _schedule_blofin_demo_startup_recovery()
    if not _blofin_demo_autopilot_enabled():
        with LOCK:
            BLOFIN_AUTOPILOT_STATE.update({
                "enabled": False, "status": "DISABLED",
                "search_continues": False,
                "next_cycle_scheduled": False,
                "next_cycle_at_utc": None,
                "next_cycle_in_seconds": None,
                "last_message_fa": "BLOFIN_DEMO_AUTOPILOT_ENABLED فعال نیست.",
            })
        _persist_blofin_autopilot_state()
        return
    with BLOFIN_AUTOPILOT_START_LOCK:
        if BLOFIN_AUTOPILOT_THREAD is not None and BLOFIN_AUTOPILOT_THREAD.is_alive():
            return
        BLOFIN_AUTOPILOT_THREAD = threading.Thread(
            target=_blofin_demo_autopilot_worker,
            daemon=True,
            name="blofin-demo-autopilot",
        )
        BLOFIN_AUTOPILOT_THREAD.start()



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

def _blofin_ai_review_summary(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose reviewer verification metadata without leaking prompts or credentials."""
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    review_rows = job.get("ai_reviews")
    if not isinstance(review_rows, list):
        review_rows = result.get("ai_reviews")
    summary: list[dict[str, Any]] = []
    for review_row in review_rows if isinstance(review_rows, list) else []:
        if not isinstance(review_row, dict):
            continue
        review_payload = review_row.get("review")
        review_payload = review_payload if isinstance(review_payload, dict) else review_row
        summary.append({
            "reviewer_id": review_row.get("reviewer_id") or review_payload.get("reviewer_id"),
            "provider": review_payload.get("provider"),
            "model": review_payload.get("model") or review_payload.get("configured_model"),
            "returned_model": review_payload.get("returned_model"),
            "status": review_payload.get("status") or "UNKNOWN",
            "request_attempted": review_payload.get("request_attempted"),
            "response_source": review_payload.get("response_source"),
            "response_validated": review_payload.get("response_validated"),
            "model_verified": review_payload.get("model_verified"),
            "requested_at_utc": review_payload.get("requested_at_utc") or review_payload.get("request_started_at_utc"),
            "completed_at_utc": review_payload.get("completed_at_utc"),
            "http_status": review_payload.get("http_status"),
            "latency_ms": review_payload.get("latency_ms"),
            "error_type": review_payload.get("error_type"),
            "error_code": review_payload.get("error_code"),
        })
    return summary


def _queue_blofin_completed_job_reports(job_id: str, instrument_id: str) -> dict[str, Any]:
    """Queue the final Demo event and activity projections for manual and AutoPilot jobs."""
    with LOCK:
        job = copy.deepcopy(BLOFIN_EXECUTION_JOBS.get(job_id) or {})
    if str(job.get("status") or "").lower() not in {"completed", "failed"}:
        return {"status": "NOT_FINAL"}
    notion = _queue_blofin_job_notion_events(job_id, instrument_id, job)
    activity = _queue_blofin_activity_journal(job_id, instrument_id, job)
    return {"status": "QUEUED", "notion": notion, "activity": activity}

def _run_challenge1_supervisor_reviews(
    analysis: Any, asset: AssetSpec, *, signal_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper around the shared two-reviewer approval contract."""
    return run_challenge1_v2_supervisor_reviews(
        analysis, asset, signal_id=signal_id,
        primary_reviewer=run_free_ai_second_opinion,
        second_reviewer=run_independent_ai_supervisor,
    )

def _blofin_manual_decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BloFinDemoExecutionBlocked(f"{field_name} is not a valid decimal value.") from exc
    if not result.is_finite():
        raise BloFinDemoExecutionBlocked(f"{field_name} must be finite.")
    return result

def _blofin_manual_write_client() -> BloFinDemoTradeClient:
    """Resolve and verify the Demo-only Trade key before any manual write."""
    readiness = _blofin_demo_execution_readiness()
    if readiness.get("status") != "READY":
        raise BloFinDemoExecutionBlocked(
            str(readiness.get("message_fa") or "پیش‌نیازهای امن حساب Demo کامل نیستند.")
        )
    client = BloFinDemoTradeClient(timeout=10.0)
    if client.base_url != DEMO_REST_URL:
        raise BloFinDemoExecutionBlocked("Manual trading is pinned to the BloFin Demo API host.")
    client.verify_trade_permission()
    return client

def _blofin_manual_close_position(
    *, instrument_id: str, position_id: str, position_side_hint: str = "",
    action_source: str = "MANUAL", audit_id: str | None = None,
    exit_reason: str | None = None,
) -> dict[str, Any]:
    """Close one freshly revalidated Demo position; loss-making positions are not blocked."""
    instrument_id = str(instrument_id or "").strip().upper()
    position_id = str(position_id or "").strip()
    position_side_hint = str(position_side_hint or "").strip().lower()
    if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", instrument_id):
        raise BloFinDemoExecutionBlocked("نماد پوزیشن معتبر نیست.")
    if position_id and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", position_id):
        raise BloFinDemoExecutionBlocked("شناسه پوزیشن معتبر نیست.")
    if position_side_hint and position_side_hint not in {"net", "long", "short"}:
        raise BloFinDemoExecutionBlocked("جهت پوزیشن معتبر نیست.")

    client = _blofin_manual_write_client()
    positions = client.get_positions(inst_id=instrument_id)
    if not isinstance(positions, list):
        raise BloFinDemoExecutionBlocked("فهرست تازه پوزیشن‌ها قابل تفسیر نیست.")
    active = []
    for row in positions:
        if not isinstance(row, dict):
            raise BloFinDemoExecutionBlocked("ردیف پوزیشن موجود قابل تفسیر نیست.")
        if str(row.get("instId") or "").strip().upper() != instrument_id:
            continue
        raw_size = row.get("positions")
        quantity = _blofin_manual_decimal(raw_size, "position_size")
        if abs(quantity) > 0:
            active.append(row)

    if position_id:
        matches = [
            row for row in active
            if str(row.get("positionId") or "").strip() == position_id
        ]
    else:
        matches = [
            row for row in active
            if str(row.get("positionSide") or "").strip().lower() == position_side_hint
        ]
    if len(matches) != 1:
        raise BloFinDemoExecutionBlocked(
            "پوزیشن دقیق و یکتا در وضعیت زنده حساب پیدا نشد؛ شناسه Position ID را دوباره از مانیتور تازه انتخاب کن."
        )
    row = matches[0]
    actual_position_id = str(row.get("positionId") or "").strip()
    if position_id and actual_position_id != position_id:
        raise BloFinDemoExecutionBlocked("Position ID با پوزیشن زنده مطابقت ندارد.")
    position_side = str(row.get("positionSide") or "").strip().lower()
    margin_mode = str(row.get("marginMode") or "").strip().lower()
    if position_side not in {"net", "long", "short"} or margin_mode not in {"isolated", "cross"}:
        raise BloFinDemoExecutionBlocked("Margin Mode یا Position Side این پوزیشن کامل نیست.")
    pnl_value = row.get("unrealizedPnl")
    try:
        pnl = _blofin_manual_decimal(pnl_value, "unrealized_pnl") if pnl_value not in (None, "") else None
    except BloFinDemoExecutionBlocked:
        pnl = None

    action_source = str(action_source or "MANUAL").strip().upper()
    if action_source not in {"MANUAL", "AUTOPILOT", "RISK_OVERRIDE"}:
        raise BloFinDemoExecutionBlocked("منبع فرمان بستن پوزیشن معتبر نیست.")
    if action_source == "AUTOPILOT":
        try:
            minimum_profit = max(
                0.01, min(float(os.getenv("BLOFIN_DEMO_EXIT_MIN_PROFIT_USDT", "0.02")), 100_000.0)
            )
        except (TypeError, ValueError):
            minimum_profit = 0.02
        if pnl is None or pnl < Decimal(str(minimum_profit)):
            raise BloFinDemoExecutionBlocked(
                "سود شناور پوزیشن در بازبینی نهایی به حداقل محافظه‌کارانه نرسید؛ خروج خودکار ارسال نشد."
            )
    action_id = "CLOSE" + uuid.uuid4().hex[:20]
    audit_event_id = str(audit_id or action_id).strip()
    if not re.fullmatch(r"[A-Za-z0-9]{1,64}", audit_event_id):
        raise BloFinDemoExecutionBlocked("شناسه ثبت گزارش بستن پوزیشن معتبر نیست.")
    event_prefix = "AUTO" if action_source == "AUTOPILOT" else (
        "RISK_OVERRIDE" if action_source == "RISK_OVERRIDE" else "MANUAL"
    )
    requested_event = f"{event_prefix}_POSITION_CLOSE_REQUESTED"
    accepted_event = f"{event_prefix}_POSITION_CLOSE_ACCEPTED"
    unknown_event = f"{event_prefix}_POSITION_CLOSE_UNKNOWN"
    store = None
    try:
        store = BloFinDemoEventStore()
        audit = {
            "instrument_id": instrument_id,
            "position_id": actual_position_id or None,
            "position_side": position_side,
            "margin_mode": margin_mode,
            "unrealized_pnl_usdt": str(pnl) if pnl is not None else None,
            "loss_making_position": bool(pnl is not None and pnl < 0),
            "action_source": action_source,
            "exit_reason": exit_reason,
            "exchange_close_client_order_id": action_id,
        }
        store.record_manual_action(audit_event_id, requested_event, audit)
        try:
            with BLOFIN_ORDER_SUBMISSION_LOCK:
                response = client.close_futures_position(
                    inst_id=instrument_id,
                    margin_mode=margin_mode,
                    position_side=position_side,
                    client_order_id=action_id,
                    position_id=actual_position_id or None,
                )
        except Exception as exc:
            store.record_manual_action(audit_event_id, unknown_event, {
                **audit,
                "submission_outcome": "UNKNOWN",
                "error_type": type(exc).__name__,
                "retry_allowed": False,
            })
            raise
        store.record_manual_action(audit_event_id, accepted_event, {
            **audit, "submission_outcome": "ACCEPTED",
        })
    finally:
        if store is not None:
            store.close()

    return {
        "status": "CLOSE_ACCEPTED",
        "environment": "BLOFIN_DEMO",
        "instrument_id": instrument_id,
        "position_id": actual_position_id or None,
        "position_side": position_side,
        "unrealized_pnl_usdt_at_confirmation": str(pnl) if pnl is not None else None,
        "loss_making_position": bool(pnl is not None and pnl < 0),
        "exchange_response": response.get("data"),
        "message_fa": (
            "درخواست بستن همین پوزیشن به قیمت بازار به BloFin Demo ارسال شد. "
            "منفی بودن PnL مانع بستن نبود؛ قیمت اجرای واقعی و PnL نهایی پس از Fill مشخص می‌شود."
        ),
    }

def _blofin_demo_profit_exit_decision(
    order: dict[str, Any], position: dict[str, Any], mark_price: Any,
    *, now: int | None = None, minimum_profit_usdt: float = 0.50,
    maximum_hold_seconds: int = 86_400,
) -> str | None:
    """Return a profit-only exit reason for one exact robot-linked Demo position."""
    if not isinstance(order, dict) or not isinstance(position, dict):
        return None
    instrument = str(order.get("instrument_id") or "").strip().upper()
    if instrument != str(position.get("instId") or "").strip().upper():
        return None
    linked_id = str(order.get("position_id") or "").strip()
    active_id = str(position.get("positionId") or "").strip()
    if not linked_id or linked_id != active_id:
        return None
    try:
        quantity = float(position.get("positions") or 0)
        pnl = float(position.get("unrealizedPnl"))
        current_mark = float(mark_price)
        target = float(order.get("take_profit"))
        opened_at = int(order.get("created_at") or 0)
        current_time = int(time.time()) if now is None else int(now)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) for value in (quantity, pnl, current_mark, target)):
        return None
    if abs(quantity) <= 0 or current_mark <= 0 or target <= 0:
        return None
    # Never call a losing or fee-fragile position a profitable exit.
    if pnl < max(0.01, float(minimum_profit_usdt)):
        return None
    side = str(order.get("side") or "").strip().upper()
    if side not in {"LONG", "SHORT"}:
        return None
    target_reached = (
        current_mark >= target if side == "LONG" else current_mark <= target
    )
    if target_reached:
        return "TAKE_PROFIT_LEVEL_REACHED"
    age_seconds = max(0, current_time - opened_at) if opened_at > 0 else 0
    if opened_at > 0 and age_seconds >= max(900, int(maximum_hold_seconds)):
        return "MAX_HOLD_PROFIT_ONLY"
    return None


def _blofin_demo_stop_loss_triggered(side: Any, mark_price: Any, stop_loss: Any) -> bool:
    """Report whether mark has crossed the stored SL threshold; never sends an order."""
    direction = str(side or "").strip().upper()
    try:
        mark = float(mark_price)
        stop = float(stop_loss)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(mark) or not math.isfinite(stop) or mark <= 0 or stop <= 0:
        return False
    if direction == "LONG":
        return mark <= stop
    if direction == "SHORT":
        return mark >= stop
    return False


def _blofin_demo_position_market_evidence(reader: Any, instrument_id: str) -> dict[str, Any]:
    """Summarize only recent, exchange-confirmed closed 5m candles for an open Demo position."""
    now_ms = int(time.time() * 1000)
    interval_ms = 300_000
    try:
        raw = reader.get_candles(inst_id=instrument_id, bar="5m", limit=8)
    except Exception as exc:
        return {
            "status": "READ_FAILED",
            "direction": "UNKNOWN",
            "source": "BLOFIN_CONFIRMED_5M_CANDLES",
            "error_type": type(exc).__name__,
            "candles": [],
        }
    if not isinstance(raw, list):
        return {
            "status": "INVALID_RESPONSE",
            "direction": "UNKNOWN",
            "source": "BLOFIN_CONFIRMED_5M_CANDLES",
            "candles": [],
        }
    parsed = []
    for item in raw:
        try:
            if isinstance(item, dict):
                stamp = int(float(item.get("ts", item.get("timestamp"))))
                opened, high, low, close = (
                    float(item.get("open", item.get("o"))),
                    float(item.get("high", item.get("h"))),
                    float(item.get("low", item.get("l"))),
                    float(item.get("close", item.get("c"))),
                )
                confirmed = str(item.get("confirm", item.get("confirmed", ""))).strip().lower()
                volume = float(item.get("volume", item.get("vol", item.get("v", 0))) or 0)
            elif isinstance(item, (list, tuple)) and len(item) >= 9:
                stamp = int(float(item[0]))
                opened, high, low, close = (float(item[1]), float(item[2]), float(item[3]), float(item[4]))
                volume = float(item[5] or 0)
                confirmed = str(item[8]).strip().lower()
            else:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        values = (opened, high, low, close, volume)
        if not all(math.isfinite(value) for value in values):
            continue
        if min(opened, high, low, close) <= 0 or volume < 0 or high < max(opened, close, low) or low > min(opened, close, high):
            continue
        if confirmed not in {"1", "true", "yes"}:
            continue
        if stamp <= 0 or stamp + interval_ms > now_ms:
            continue
        parsed.append({
            "timestamp_ms": stamp,
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
    parsed.sort(key=lambda row: row["timestamp_ms"])
    deduped = {}
    for row in parsed:
        deduped[row["timestamp_ms"]] = row
    rows = list(deduped.values())[-4:]
    if not rows:
        return {
            "status": "NO_CONFIRMED_CANDLES",
            "direction": "UNKNOWN",
            "source": "BLOFIN_CONFIRMED_5M_CANDLES",
            "candles": [],
        }
    if now_ms - (rows[-1]["timestamp_ms"] + interval_ms) > interval_ms + 60_000:
        return {
            "status": "STALE_CANDLES",
            "direction": "UNKNOWN",
            "source": "BLOFIN_CONFIRMED_5M_CANDLES",
            "latest_closed_candle_timestamp_ms": rows[-1]["timestamp_ms"],
            "candles": rows,
        }
    if len(rows) < 3 or any(
        rows[index]["timestamp_ms"] - rows[index - 1]["timestamp_ms"] != interval_ms
        for index in range(1, len(rows))
    ):
        return {
            "status": "INSUFFICIENT_OR_DISCONTINUOUS_CANDLES",
            "direction": "UNKNOWN",
            "source": "BLOFIN_CONFIRMED_5M_CANDLES",
            "latest_closed_candle_timestamp_ms": rows[-1]["timestamp_ms"],
            "candles": rows,
        }
    closes = [float(row["close"]) for row in rows[-3:]]
    if closes[0] < closes[1] < closes[2]:
        direction = "UP"
    elif closes[0] > closes[1] > closes[2]:
        direction = "DOWN"
    else:
        direction = "SIDEWAYS"
    return {
        "status": "VERIFIED",
        "direction": direction,
        "source": "BLOFIN_CONFIRMED_5M_CANDLES",
        "latest_closed_candle_timestamp_ms": rows[-1]["timestamp_ms"],
        "candles": rows[-3:],
    }


def _blofin_demo_protective_order_evidence(
    reader: Any, instrument_id: str, order: dict[str, Any], position: dict[str, Any],
) -> dict[str, Any]:
    """Verify pending exchange TP/SL only by exact positionId or exact originating clientOrderId."""
    position_id = str(position.get("positionId") or "").strip()
    client_order_id = str(order.get("client_order_id") or "").strip()
    side = str(order.get("side") or "").strip().lower()
    try:
        rows = reader.get_open_tpsl_orders(inst_id=instrument_id, limit=100)
    except Exception as exc:
        return {
            "status": "READ_FAILED",
            "error_type": type(exc).__name__,
            "source": "BLOFIN_PENDING_TPSL",
            "matched_by": None,
            "orders": [],
        }
    if not isinstance(rows, list):
        return {
            "status": "INVALID_RESPONSE",
            "source": "BLOFIN_PENDING_TPSL",
            "matched_by": None,
            "orders": [],
        }
    matched = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("instId") or "").strip().upper() != instrument_id:
            continue
        row_position_id = str(row.get("positionId") or "").strip()
        row_client_id = str(row.get("clientOrderId") or "").strip()
        row_position_side = str(row.get("positionSide") or "").strip().lower()
        if row_position_side and row_position_side not in {side, "net"}:
            continue
        if position_id and row_position_id == position_id:
            matched_by = "EXACT_POSITION_ID"
        elif client_order_id and row_client_id == client_order_id:
            matched_by = "EXACT_CLIENT_ORDER_ID"
        else:
            continue
        matched.append({
            "matched_by": matched_by,
            "tpsl_id": str(row.get("tpslId") or ""),
            "client_order_id": row_client_id,
            "position_id": row_position_id,
            "inst_id": str(row.get("instId") or ""),
            "position_side": row_position_side,
            "state": str(row.get("state") or ""),
            "tp_trigger_price": row.get("tpTriggerPrice"),
            "sl_trigger_price": row.get("slTriggerPrice"),
            "tp_order_price": row.get("tpOrderPrice"),
            "sl_order_price": row.get("slOrderPrice"),
            "actual_size": row.get("actualSize"),
            "size": row.get("size"),
        })
    if not matched:
        return {
            "status": "NOT_VERIFIED_NO_EXACT_POSITION_LINK",
            "source": "BLOFIN_PENDING_TPSL",
            "matched_by": None,
            "orders": [],
        }
    has_sl = any(
        _is_positive_finite_number(item.get("sl_trigger_price")) for item in matched
    )
    has_tp = any(
        _is_positive_finite_number(item.get("tp_trigger_price")) for item in matched
    )
    if has_sl and has_tp:
        status = "BOTH_TP_AND_SL_VISIBLE"
    elif has_sl:
        status = "SL_ONLY_VISIBLE"
    elif has_tp:
        status = "TP_ONLY_VISIBLE"
    else:
        status = "MATCHED_ORDER_WITHOUT_VALID_TP_SL_PRICES"
    return {
        "status": status,
        "source": "BLOFIN_PENDING_TPSL",
        "matched_by": sorted({item["matched_by"] for item in matched}),
        "orders": matched,
    }


def _is_positive_finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0


def _blofin_demo_position_manager_cycle() -> dict[str, Any]:
    """Manage only exchange positions linked by exact ID to the robot's Demo orders."""
    store = None
    try:
        readiness = _blofin_demo_execution_readiness()
        if readiness.get("status") != "READY":
            readiness_blockers: list[str] = []
            if not readiness.get("trade_credentials_configured"):
                readiness_blockers.append("TRADE_CREDENTIALS_NOT_CONFIGURED")
            if readiness.get("trade_permission_status") == "READ_ONLY":
                readiness_blockers.append("TRADE_PERMISSION_READ_ONLY")
            elif not readiness.get("trade_permission_verified"):
                readiness_blockers.append("TRADE_PERMISSION_NOT_VERIFIED")
            if not readiness.get("trading_enabled"):
                readiness_blockers.append("DEMO_TRADING_DISABLED")
            if not readiness.get("dashboard_auth_configured"):
                readiness_blockers.append("DASHBOARD_TOKEN_NOT_CONFIGURED")
            if not readiness.get("durable_storage"):
                readiness_blockers.append("DURABLE_STORAGE_UNAVAILABLE")
            if not readiness.get("event_store_readable"):
                readiness_blockers.append("EVENT_STORE_UNREADABLE")
            if not readiness_blockers:
                readiness_blockers.append("EXECUTION_READINESS_NOT_READY")

            if "TRADE_CREDENTIALS_NOT_CONFIGURED" in readiness_blockers:
                message_fa = "اعتبارنامه‌های Trade در Environment کامل نیستند؛ مدیر خروج هیچ اقدامی انجام نداد."
            elif "TRADE_PERMISSION_READ_ONLY" in readiness_blockers:
                message_fa = "مجوز API فقط‌خواندنی است؛ مدیر خروج مجوز تغییر پوزیشن ندارد و هیچ اقدامی انجام نداد."
            elif "TRADE_PERMISSION_NOT_VERIFIED" in readiness_blockers:
                message_fa = "مجوز Trade تأیید نشد؛ مدیر خروج متوقف ماند و هیچ پوزیشنی تغییر نکرد."
            else:
                reasons = []
                if "DEMO_TRADING_DISABLED" in readiness_blockers:
                    reasons.append("فعال‌سازی معاملات Demo خاموش است")
                if "DASHBOARD_TOKEN_NOT_CONFIGURED" in readiness_blockers:
                    reasons.append("توکن پنل تنظیم نشده است")
                if "DURABLE_STORAGE_UNAVAILABLE" in readiness_blockers:
                    reasons.append("حافظه پایدار در دسترس نیست")
                if "EVENT_STORE_UNREADABLE" in readiness_blockers:
                    reasons.append("دفتر رویداد قابل خواندن نیست")
                message_fa = (
                    "مدیر خروج به‌دلیل پیش‌نیازهای مسدودشده اقدام نکرد: "
                    + "؛ ".join(reasons or ["آمادگی اجرای Demo تأیید نشده است"])
                    + ". هیچ پوزیشنی تغییر نکرد."
                )
            return {
                "status": "EXECUTION_READINESS_BLOCKED",
                "readiness_status": readiness.get("status"),
                "readiness_blockers": readiness_blockers,
                "trade_permission_status": readiness.get("trade_permission_status"),
                "credential_source": readiness.get("credential_source"),
                "trading_enabled": bool(readiness.get("trading_enabled")),
                "dashboard_auth_configured": bool(readiness.get("dashboard_auth_configured")),
                "durable_storage": bool(readiness.get("durable_storage")),
                "event_store_readable": bool(readiness.get("event_store_readable")),
                "message_fa": message_fa,
            }
        raw_min_profit = os.getenv("BLOFIN_DEMO_EXIT_MIN_PROFIT_USDT", "0.02")
        raw_max_hold = os.getenv("BLOFIN_DEMO_MAX_HOLD_SECONDS", "86400")
        try:
            minimum_profit = max(0.01, min(float(raw_min_profit), 100_000.0))
        except (TypeError, ValueError):
            minimum_profit = 0.02
        try:
            maximum_hold = max(900, min(int(raw_max_hold), 604_800))
        except (TypeError, ValueError):
            maximum_hold = 86_400

        store = BloFinDemoEventStore()
        pending_orders = store.orders_pending_learning_outcome(limit=50)
        if not pending_orders:
            return {
                "status": "IDLE",
                "managed_positions": 0,
                "message_fa": "پوزیشن رباتیِ لینک‌شده‌ای برای مدیریت خروج وجود ندارد.",
            }

        reader = BloFinDemoReadOnly(timeout=8.0)
        positions = reader.get_positions()
        if not isinstance(positions, list):
            raise BloFinDemoResponseError("BloFin positions response is not a list.")

        active_rows = []
        for row in positions:
            if not isinstance(row, dict):
                raise BloFinDemoResponseError("BloFin returned an unreadable position row.")
            try:
                size = float(row.get("positions") or 0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise BloFinDemoResponseError("BloFin position size is invalid.") from exc
            if math.isfinite(size) and abs(size) > 0:
                active_rows.append(row)

        actions: list[dict[str, Any]] = []
        managed = 0
        for order in pending_orders:
            instrument_id = str(order.get("instrument_id") or "").strip().upper()
            position_id = str(order.get("position_id") or "").strip()
            client_order_id = str(order.get("client_order_id") or "").strip()
            if not instrument_id or not position_id or not client_order_id:
                actions.append({"status": "UNLINKED_POSITION_ID", "instrument_id": instrument_id})
                continue

            # Exchange finalization is reconciled against exact positionId, not
            # the newest history row or the dashboard's selected symbol.
            matching_active = [
                row for row in active_rows
                if str(row.get("instId") or "").strip().upper() == instrument_id
                and str(row.get("positionId") or "").strip() == position_id
            ]
            if len(matching_active) > 1:
                actions.append({
                    "status": "AMBIGUOUS_POSITION_ID",
                    "instrument_id": instrument_id,
                    "position_id": position_id,
                })
                continue
            if not matching_active:
                try:
                    closed_rows = reader.get_positions_history(
                        inst_id=instrument_id, state="closed", limit=100,
                    )
                    partial_rows = reader.get_positions_history(
                        inst_id=instrument_id, state="partially_closed", limit=100,
                    )
                    outcome = store.record_closed_position_outcome(
                        instrument_id=instrument_id,
                        history_rows=closed_rows if isinstance(closed_rows, list) else [],
                        partial_rows=partial_rows if isinstance(partial_rows, list) else [],
                        client_order_id=client_order_id,
                        position_id=position_id,
                    )
                    if outcome:
                        activity_update = _update_blofin_activity_after_close(
                            outcome, instrument_id, position_id,
                            closed_rows if isinstance(closed_rows, list) else [],
                            partial_rows if isinstance(partial_rows, list) else [],
                        )
                        actions.append({
                            "status": outcome.get("status"),
                            "instrument_id": instrument_id,
                            "position_id": position_id,
                            "realized_result": outcome.get("realized_result"),
                            "realized_pnl_after_reported_trading_fees": outcome.get(
                                "realized_pnl_after_reported_trading_fees"
                            ),
                            "activity_report_sync": activity_update,
                        })
                except Exception as exc:
                    actions.append({
                        "status": "CLOSE_RECONCILIATION_UNKNOWN",
                        "instrument_id": instrument_id,
                        "position_id": position_id,
                        "error_type": type(exc).__name__,
                    })
                continue

            position = matching_active[0]
            managed += 1
            # A previously accepted/unknown close is not resent blindly.
            prior_events = store.recent_events(client_order_id, limit=100)
            prior_close = [
                event for event in prior_events
                if str(event.get("event_type") or "").upper() in {
                    "AUTO_POSITION_CLOSE_ACCEPTED", "AUTO_POSITION_CLOSE_UNKNOWN",
                    "RISK_OVERRIDE_POSITION_CLOSE_REQUESTED",
                    "RISK_OVERRIDE_POSITION_CLOSE_ACCEPTED",
                    "RISK_OVERRIDE_POSITION_CLOSE_UNKNOWN",
                }
            ]
            if prior_close:
                actions.append({
                    "status": "CLOSE_AWAITING_EXCHANGE_RECONCILIATION",
                    "instrument_id": instrument_id,
                    "position_id": position_id,
                })
                continue

            mark_rows = reader.get_mark_price(inst_id=instrument_id)
            mark_row = (
                mark_rows[0] if isinstance(mark_rows, list) and mark_rows
                and isinstance(mark_rows[0], dict)
                else mark_rows if isinstance(mark_rows, dict) else None
            )
            if not isinstance(mark_row, dict):
                actions.append({
                    "status": "MARK_PRICE_UNKNOWN",
                    "instrument_id": instrument_id,
                    "position_id": position_id,
                })
                continue
            reason = _blofin_demo_profit_exit_decision(
                order, position, mark_row.get("markPrice"),
                minimum_profit_usdt=minimum_profit,
                maximum_hold_seconds=maximum_hold,
            )
            experience_id = str(order.get("experience_id") or "").strip() or None
            audit_position_id = str(position.get("positionId") or position_id)
            try:
                mark_price_value = float(mark_row.get("markPrice"))
            except (TypeError, ValueError, OverflowError):
                mark_price_value = None
            try:
                pnl_value = float(position.get("unrealizedPnl"))
            except (TypeError, ValueError, OverflowError):
                pnl_value = None
            try:
                opened_epoch = float(order.get("created_at") or 0)
            except (TypeError, ValueError, OverflowError):
                opened_epoch = 0.0
            holding_seconds = max(0, int(time.time() - opened_epoch)) if opened_epoch > 0 else None
            try:
                target_value = float(order.get("take_profit") or 0)
            except (TypeError, ValueError, OverflowError):
                target_value = 0.0
            try:
                stop_value = float(order.get("stop_loss") or 0)
            except (TypeError, ValueError, OverflowError):
                stop_value = 0.0
            side_value = str(order.get("side") or "").upper()
            take_profit_reached = (
                mark_price_value is not None and target_value > 0
                and ((mark_price_value >= target_value) if side_value == "LONG"
                     else (mark_price_value <= target_value) if side_value == "SHORT" else False)
            )
            stop_loss_threshold_reached = _blofin_demo_stop_loss_triggered(
                side_value, mark_price_value, stop_value,
            )
            cycle_epoch_ms = int(time.time() * 1000)
            monitor_cycle_id = f"{audit_position_id}:{cycle_epoch_ms}"
            market_evidence = _blofin_demo_position_market_evidence(reader, instrument_id)
            protective_evidence = _blofin_demo_protective_order_evidence(
                reader, instrument_id, order, position,
            )

            entry_snapshot = None
            snapshot_read_error = None
            if experience_id:
                activity_journal = None
                try:
                    activity_journal = NotionActivityJournal()
                    entry_snapshot = activity_journal.get_snapshot(experience_id)
                except Exception as snapshot_exc:
                    snapshot_read_error = type(snapshot_exc).__name__
                finally:
                    if activity_journal is not None:
                        activity_journal.close()

            snapshot_evidence = (
                entry_snapshot.get("evidence")
                if isinstance(entry_snapshot, dict) and isinstance(entry_snapshot.get("evidence"), dict)
                else None
            )
            reviewer_states: dict[str, dict[str, Any]] = {}
            review_record = None
            if not experience_id:
                skip_code = "EXPERIENCE_ID_MISSING"
                skip_reason = "The local Demo order is not linked to a verified Challenge 1 experience."
            elif snapshot_read_error:
                skip_code = "ENTRY_SNAPSHOT_READ_FAILED"
                skip_reason = f"Could not read the persisted entry evidence: {snapshot_read_error}"
            elif not isinstance(snapshot_evidence, dict) or not snapshot_evidence:
                skip_code = "ENTRY_SNAPSHOT_EVIDENCE_MISSING"
                skip_reason = "The persisted entry journal has no Challenge 1 evidence for this experience."
            else:
                skip_code = ""
                skip_reason = ""
                review_record = {
                    "instrument_id": instrument_id,
                    "signal": side_value if side_value in {"LONG", "SHORT"} else "UNKNOWN",
                    "reason_fa": str(entry_snapshot.get("reason_fa") or ""),
                    "entry_price_usd": order.get("entry_price"),
                    "stop_loss_usd": order.get("stop_loss"),
                    "take_profit_usd": order.get("take_profit"),
                    "challenge1_evidence": snapshot_evidence,
                }
                cycle_context = {
                    "cycle_id": monitor_cycle_id,
                    "instrument_id": instrument_id,
                    "position": position,
                    "average_fill_price": (
                        entry_snapshot.get("actual_entry_price")
                        if isinstance(entry_snapshot, dict) and entry_snapshot.get("actual_entry_price") is not None
                        else position.get("averagePrice")
                    ),
                    "filled_size": position.get("positions"),
                    "execution_status": "OPEN_POSITION_MATCHED",
                    "robot_decision": "EXIT_ELIGIBLE" if reason else "HOLD",
                    "robot_reason": reason or "Profit exit conditions are not met.",
                    "holding_seconds": holding_seconds,
                    "market_direction": market_evidence.get("direction", "UNKNOWN"),
                    "market_direction_source": market_evidence.get("source", "UNKNOWN"),
                    "market_direction_evidence": market_evidence.get("candles", []),
                    "protective_orders": protective_evidence,
                    "risk_change": "NONE_BY_POSITION_MANAGER_CYCLE",
                }
                try:
                    reviewer_states = schedule_position_observer_review(
                        challenge_record=review_record,
                        cycle_context=cycle_context,
                        experience_id=experience_id,
                        position_id=audit_position_id,
                        order_id=str(order.get("exchange_order_id") or client_order_id),
                        symbol=instrument_id,
                        signal=side_value if side_value in {"LONG", "SHORT"} else "UNKNOWN",
                    )
                except Exception as observer_exc:
                    skip_code = "OBSERVER_SCHEDULER_FAILED"
                    skip_reason = f"Observer scheduler failed: {type(observer_exc).__name__}"
                    reviewer_states = {}

            if skip_code:
                reviewer_states = {
                    reviewer_id: {
                        "status": "NOT_CALLED",
                        "reason_code": skip_code,
                        "request_attempted": False,
                        "reason": skip_reason,
                    }
                    for reviewer_id in ("nemotron", "second_ai")
                }

            skip_audit_errors = []
            for reviewer_id in ("nemotron", "second_ai"):
                state = reviewer_states.get(reviewer_id)
                state = state if isinstance(state, dict) else {
                    "status": "NOT_CALLED",
                    "reason_code": "REVIEWER_STATE_MISSING",
                    "request_attempted": False,
                }
                reviewer_states[reviewer_id] = state
                if str(state.get("status") or "").upper() in {"NOT_CALLED", "PENDING"}:
                    try:
                        record_position_observer_skip(
                            experience_id=experience_id,
                            position_id=audit_position_id,
                            reviewer_id=reviewer_id,
                            cycle_id=monitor_cycle_id,
                            reason_code=str(state.get("reason_code") or "NOT_CALLED"),
                            reason=str(state.get("reason") or state.get("reason_code") or "Observer was not called this cycle."),
                            last_result=state,
                        )
                    except Exception as skip_exc:
                        skip_audit_errors.append({
                            "reviewer_id": reviewer_id,
                            "error_type": type(skip_exc).__name__,
                        })

            exit_basis = {
                "instrument_id": instrument_id, "position_id": audit_position_id,
                "experience_id": experience_id,
                "monitor_cycle_id": monitor_cycle_id,
                "heartbeat_at_utc": datetime.now(timezone.utc).isoformat(),
                "heartbeat_status": "ALIVE",
                "manager_interval_seconds": 15,
                "decision": "EXIT_ELIGIBLE" if reason else "HOLD",
                "rule_engine": "BLOFIN_DEMO_PROFIT_EXIT_RULES",
                "exit_reason": reason, "side": side_value,
                "mark_price": mark_price_value,
                "unrealized_pnl_usdt": pnl_value,
                "loss_making_position": pnl_value is not None and pnl_value < 0,
                "minimum_profit_usdt": minimum_profit,
                "minimum_profit_passed": pnl_value is not None and pnl_value >= minimum_profit,
                "stop_loss_price": stop_value,
                "stop_loss_threshold_reached": stop_loss_threshold_reached,
                "stop_loss_management": "EXCHANGE_PROTECTION_RECONCILIATION_ONLY_NO_MANUAL_CLOSE",
                "take_profit": target_value,
                "take_profit_reached": take_profit_reached,
                "holding_seconds": holding_seconds,
                "maximum_hold_seconds": maximum_hold,
                "maximum_hold_reached": holding_seconds is not None and holding_seconds >= maximum_hold,
                "market_direction": market_evidence.get("direction", "UNKNOWN"),
                "market_evidence": market_evidence,
                "protective_orders": protective_evidence,
                "reviewer_states": reviewer_states,
                "reviewer_skip_audit_errors": skip_audit_errors,
                "observer_review_policy": "TWO_INDEPENDENT_POST_DECISION_AUDIT_REVIEWERS",
                "observer_execution_effect": "NONE",
                "observer_evidence_source": "PERSISTED_NOTION_ACTIVITY_JOURNAL",
            }
            try:
                audit_result = record_position_monitor_cycle(
                    experience_id=experience_id,
                    position_id=audit_position_id,
                    order_id=str(order.get("exchange_order_id") or client_order_id),
                    symbol=instrument_id,
                    signal=side_value if side_value in {"LONG", "SHORT"} else "UNKNOWN",
                    cycle_details=exit_basis,
                    cycle_epoch_ms=cycle_epoch_ms,
                )
                if audit_result.get("status") != "RECORDED":
                    raise RuntimeError("POSITION_CYCLE_AUDIT_NOT_RECORDED")
                if skip_audit_errors:
                    raise RuntimeError("POSITION_OBSERVER_SKIP_AUDIT_INCOMPLETE")
            except Exception as audit_exc:
                actions.append({
                    "status": "AUDIT_WRITE_FAILED", "instrument_id": instrument_id,
                    "position_id": position_id, "error_type": type(audit_exc).__name__,
                    "message_fa": "ثبت کامل چرخه پایش یا وضعیت عدم فراخوانی ناظرها تأیید نشد؛ درخواست خروج جدید ارسال نشد.",
                })
                continue
            if not reason:
                if stop_loss_threshold_reached:
                    protection_status = str(protective_evidence.get("status") or "").upper()
                    verified_sl_present = protection_status in {
                        "BOTH_TP_AND_SL_VISIBLE", "SL_ONLY_VISIBLE",
                    }
                    verified_sl_absent = protection_status in {
                        "NOT_VERIFIED_NO_EXACT_POSITION_LINK",
                        "TP_ONLY_VISIBLE",
                        "MATCHED_ORDER_WITHOUT_VALID_TP_SL_PRICES",
                    }
                    if verified_sl_absent:
                        try:
                            _record_challenge1_audit(
                                experience_id or position_id, "RISK_OVERRIDE_EXIT_AUTHORIZED",
                                {
                                    **exit_basis,
                                    "decision": "CLOSE_DEMO_POSITION",
                                    "precondition": (
                                        "Exact linked position matched, mark crossed stored stop, "
                                        "and no active exchange SL could be verified."
                                    ),
                                    "protective_order_status": protection_status,
                                    "loss_exit_permitted": True,
                                },
                                event_key=f"risk-override-exit:{audit_position_id}:{cycle_epoch_ms}",
                                experience_id=experience_id, position_id=audit_position_id,
                                status="WARNING",
                                reason="Stop threshold crossed without verifiable exchange SL; exact-position risk override requested.",
                            )
                            close_result = _blofin_manual_close_position(
                                instrument_id=instrument_id,
                                position_id=position_id,
                                action_source="RISK_OVERRIDE",
                                audit_id=client_order_id,
                                exit_reason="STOP_LOSS_CROSSED_WITHOUT_VERIFIED_SL",
                            )
                            actions.append({
                                "status": "RISK_OVERRIDE_CLOSE_REQUESTED",
                                "instrument_id": instrument_id,
                                "position_id": position_id,
                                "stop_loss_price": stop_value,
                                "mark_price": mark_price_value,
                                "protective_orders": protective_evidence,
                                "exchange_response": close_result.get("exchange_response"),
                                "position_cycle_audit": audit_result,
                            })
                        except Exception as close_exc:
                            actions.append({
                                "status": "RISK_OVERRIDE_CLOSE_NOT_CONFIRMED",
                                "instrument_id": instrument_id,
                                "position_id": position_id,
                                "error_type": type(close_exc).__name__,
                                "protective_orders": protective_evidence,
                                "position_cycle_audit": audit_result,
                            })
                        continue
                    actions.append({
                        "status": (
                            "STOP_LOSS_LEVEL_CROSSED_AWAIT_EXCHANGE_SL"
                            if verified_sl_present
                            else "STOP_LOSS_LEVEL_CROSSED_PROTECTION_STATE_UNKNOWN"
                        ),
                        "instrument_id": instrument_id,
                        "position_id": position_id,
                        "stop_loss_price": stop_value,
                        "mark_price": mark_price_value,
                        "protective_orders": protective_evidence,
                        "position_cycle_audit": audit_result,
                    })
                    continue
                actions.append({
                    "status": "HOLD_UNTIL_PROFIT_EXIT",
                    "instrument_id": instrument_id,
                    "position_id": position_id,
                    "unrealized_pnl_usdt": position.get("unrealizedPnl"),
                    "minimum_profit_usdt": minimum_profit,
                    "position_cycle_audit": audit_result,
                })
                continue

            try:
                _record_challenge1_audit(
                    experience_id or position_id, "EXIT_REQUEST_AUTHORIZED",
                    {
                        **exit_basis, "decision": "CLOSE_DEMO_POSITION",
                        "precondition": "Exact linked position ID matched and minimum profit rule passed.",
                    },
                    event_key=f"exit-request:{audit_position_id}:{reason}",
                    experience_id=experience_id, position_id=audit_position_id,
                    status="SUCCESS", reason=f"Automatic Demo close request authorized by exit rule: {reason}",
                )
                close_result = _blofin_manual_close_position(
                    instrument_id=instrument_id,
                    position_id=position_id,
                    action_source="AUTOPILOT",
                    audit_id=client_order_id,
                    exit_reason=reason,
                )
                actions.append({
                    "status": close_result.get("status"),
                    "instrument_id": instrument_id,
                    "position_id": position_id,
                    "exit_reason": reason,
                    "unrealized_pnl_usdt_at_request": close_result.get(
                        "unrealized_pnl_usdt_at_confirmation"
                    ),
                    "exchange_response": close_result.get("exchange_response"),
                    "position_cycle_audit": audit_result,
                })
            except BloFinDemoExecutionBlocked as exc:
                try:
                    _record_challenge1_audit(
                        experience_id or position_id,
                        "EXIT_REQUEST_REJECTED",
                        {
                            **exit_basis,
                            "decision": "EXIT_NOT_SUBMITTED",
                            "rejection_reason_fa": str(exc)[:1000],
                            "orders_submitted": False,
                        },
                        event_key=f"exit-request-rejected:{audit_position_id}:{reason}",
                        experience_id=experience_id,
                        position_id=audit_position_id,
                        status="WARNING",
                        reason=str(exc)[:1200],
                    )
                except Exception:
                    pass
                actions.append({
                    "status": "AUTO_EXIT_NOT_SUBMITTED",
                    "instrument_id": instrument_id,
                    "position_id": position_id,
                    "exit_reason": reason,
                    "reason_fa": str(exc),
                })

        return {
            "status": "PASS",
            "managed_positions": managed,
            "actions": actions,
            "minimum_profit_usdt": minimum_profit,
            "maximum_hold_seconds": maximum_hold,
            "message_fa": (
                "مدیر خروج فقط پوزیشن‌های دقیقاً لینک‌شده به سفارش ربات را بررسی می‌کند؛ "
                "خروج خودکار فقط با رسیدن به حد سود یا پایان زمان نگهداری و سود شناور کافی انجام می‌شود."
            ),
        }
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

def _blofin_demo_position_manager_worker() -> None:
    """Persistent Demo-only profit manager; independent from the new-entry AutoPilot switch."""
    while True:
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            result = _blofin_demo_position_manager_cycle()
            with LOCK:
                BLOFIN_POSITION_MANAGER_STATE.update({
                    "status": result.get("status", "UNKNOWN"),
                    "last_check_at_utc": checked_at,
                    "last_action": result.get("actions", []),
                    "last_message_fa": result.get("message_fa", "مدیر خروج بررسی را انجام داد."),
                    "readiness_status": result.get("readiness_status"),
                    "readiness_blockers": list(result.get("readiness_blockers") or []),
                    "trade_permission_status": result.get("trade_permission_status", "UNKNOWN"),
                    "credential_source": result.get("credential_source"),
                    "trading_enabled": result.get("trading_enabled"),
                    "dashboard_auth_configured": result.get("dashboard_auth_configured"),
                    "durable_storage": result.get("durable_storage"),
                    "event_store_readable": result.get("event_store_readable"),
                })
        except Exception as exc:
            with LOCK:
                BLOFIN_POSITION_MANAGER_STATE.update({
                    "status": "ERROR",
                    "last_check_at_utc": checked_at,
                    "last_error_type": type(exc).__name__,
                    "last_message_fa": "مدیر خروج با خطای کنترل‌شده روبه‌رو شد؛ هیچ خروج تأییدنشده‌ای فرض نمی‌شود.",
                })
        time.sleep(15)

def _start_blofin_demo_position_manager() -> None:
    """Start at most one position manager loop in this Render process."""
    global BLOFIN_POSITION_MANAGER_THREAD
    with BLOFIN_POSITION_MANAGER_START_LOCK:
        if BLOFIN_POSITION_MANAGER_THREAD is not None and BLOFIN_POSITION_MANAGER_THREAD.is_alive():
            return
        BLOFIN_POSITION_MANAGER_THREAD = threading.Thread(
            target=_blofin_demo_position_manager_worker,
            daemon=True,
            name="blofin-demo-profit-manager",
        )
        BLOFIN_POSITION_MANAGER_THREAD.start()



def _blofin_activity_journal_snapshot(job_id: str, instrument_id: str, job: dict) -> dict:
    """Build one auditable Notion activity row from a real Challenge 1 symbol job."""
    current = job if isinstance(job, dict) else {}
    raw_result = current.get("result")
    result = raw_result if isinstance(raw_result, dict) else {}
    raw_analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else (
        current.get("analysis") if isinstance(current.get("analysis"), dict) else {}
    )
    evidence = result.get("challenge1_evidence")
    if not isinstance(evidence, dict):
        evidence = raw_analysis.get("challenge1_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    experience_id = (
        result.get("learning_experience_id")
        or current.get("learning_experience_id")
        or raw_analysis.get("learning_experience_id")
    )
    if not experience_id:
        # Data-error jobs still get a unique audit row, but are not mislabelled
        # as a verified LearningExperienceMemory record.
        experience_id = "job-" + str(job_id)
    signal = str(
        result.get("signal")
        or evidence.get("decision")
        or raw_analysis.get("signal")
        or "UNKNOWN"
    ).upper()
    if signal not in {"LONG", "SHORT", "NO_TRADE"}:
        signal = "UNKNOWN"
    positions = result.get("positions") if isinstance(result.get("positions"), list) else []
    position = next(
        (row for row in positions if isinstance(row, dict)
         and str(row.get("instId") or "").upper() == str(instrument_id).upper()),
        {},
    )

    # The successful execution result keeps a compact primary ai_review for the UI,
    # while the full two-reviewer council stays at the job root. Prefer the complete
    # reviewer records and preserve both independently in the activity journal.
    raw_reviews = result.get("ai_reviews") if isinstance(result.get("ai_reviews"), list) else (
        current.get("ai_reviews") if isinstance(current.get("ai_reviews"), list) else []
    )
    if not raw_reviews:
        fallback = result.get("ai_review") if isinstance(result.get("ai_review"), dict) else (
            current.get("ai_review") if isinstance(current.get("ai_review"), dict) else {}
        )
        if fallback:
            raw_reviews = [{
                "reviewer_id": fallback.get("reviewer_id") or "nemotron",
                "actor": "Nemotron Supervisor",
                "review": fallback,
            }]

    ai_reviews = []
    for item in raw_reviews[:2]:
        if not isinstance(item, dict):
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else item
        reviewer_id = str(item.get("reviewer_id") or review.get("reviewer_id") or "")
        actor = str(item.get("actor") or (
            "Nemotron Supervisor" if reviewer_id == "nemotron" else
            "Second AI Supervisor" if reviewer_id == "second_ai" else
            "AI Supervisor"
        ))
        request_attempted = review.get("request_attempted")
        review_status = str(review.get("status") or "UNKNOWN").upper()
        if request_attempted is None and review_status in {"NOT_CALLED", "NOT_CONFIGURED"}:
            request_attempted = False
        ai_reviews.append({
            "reviewer_id": reviewer_id,
            "actor": actor,
            "status": review_status,
            "provider": review.get("provider") or "",
            "model": review.get("model") or "",
            "returned_model": review.get("returned_model") or "",
            "request_attempted": request_attempted,
            "response_source": review.get("response_source") or "NONE",
            "response_validated": review.get("response_validated") is True,
            "model_verified": review.get("model_verified") is True,
            "paper_entry_eligible": review.get("paper_entry_eligible") is True,
            "skip_reason_code": review.get("skip_reason_code") or "",
            "http_status": review.get("http_status"),
            "latency_ms": review.get("latency_ms"),
            "reason": (
                review.get("rationale_fa") or review.get("reason")
                or review.get("status_fa") or ""
            ),
        })

    present_reviewers = {item["reviewer_id"] for item in ai_reviews if item.get("reviewer_id")}
    for reviewer_id, actor in (
        ("nemotron", "Nemotron Supervisor"),
        ("second_ai", "Second AI Supervisor"),
    ):
        if reviewer_id not in present_reviewers:
            ai_reviews.append({
                "reviewer_id": reviewer_id,
                "actor": actor,
                "status": "NOT_CALLED",
                "provider": "",
                "model": "",
                "returned_model": "",
                "request_attempted": False,
                "response_source": "NONE",
                "response_validated": False,
                "model_verified": False,
                "paper_entry_eligible": False,
                "skip_reason_code": "UPSTREAM_STAGE_BLOCKED",
                "reason": (
                    "این ناظر در این چرخه فراخوانی نشد؛ ربات پیش از مرحله بازبینی مستقل متوقف شد."
                ),
            })

    raw_consensus = result.get("ai_consensus") if isinstance(result.get("ai_consensus"), dict) else (
        current.get("ai_consensus") if isinstance(current.get("ai_consensus"), dict) else {}
    )
    consensus = {
        key: raw_consensus.get(key)
        for key in (
            "status", "primary_status", "second_status",
            "primary_verified", "second_verified",
            "primary_approved", "second_approved",
            "two_reviewer_approval", "decision",
        )
        if key in raw_consensus
    }
    all_not_called = all(item.get("request_attempted") is False for item in ai_reviews)
    reviewer_consensus = str(
        consensus.get("status") or ("NOT_CALLED" if all_not_called else "NOT_COMPARABLE")
    ).upper()
    ai_status = (
        "APPROVED_BY_BOTH"
        if consensus.get("two_reviewer_approval") is True
        and consensus.get("primary_verified") is True
        and consensus.get("second_verified") is True
        else "NOT_CALLED" if all_not_called
        else reviewer_consensus
    )
    ai_model = " | ".join(
        f"{item['reviewer_id'] or item['actor']}:{item['returned_model'] or item['model'] or 'UNKNOWN'}"
        for item in ai_reviews
    )[:300]
    ai_reason = " | ".join(
        f"{item['reviewer_id'] or item['actor']} {item['status']}: {item['reason']}"
        for item in ai_reviews
    )[:1600]

    return {
        "experience_id": str(experience_id),
        "job_id": str(job_id),
        "symbol": str(instrument_id).upper(),
        "signal": signal,
        "status": str(result.get("status") or current.get("stage") or current.get("status") or "UNKNOWN"),
        "reason_fa": result.get("reason_fa") or current.get("message_fa") or "",
        "analysis_time_utc": current.get("completed_at_utc") or current.get("updated_at_utc") or datetime.now(timezone.utc).isoformat(),
        "signal_bar_open_utc": result.get("signal_bar_open_utc") or evidence.get("confirmation_candle", {}).get("open_time_utc") or evidence.get("confirmation_candle", {}).get("time_utc"),
        "engine_version": evidence.get("decision_engine_version") or "CHALLENGE_1_UNKNOWN",
        "patterns": evidence.get("detected_patterns") if isinstance(evidence.get("detected_patterns"), list) else [],
        "chart_structure": json.dumps(evidence.get("chart_structure") or {}, ensure_ascii=False, sort_keys=True),
        "volume_confirmation": evidence.get("volume_confirmation") if isinstance(evidence.get("volume_confirmation"), dict) else {},
        "evidence": evidence,
        "candles_5m": evidence.get("candles_5m") if isinstance(evidence.get("candles_5m"), list) else [],
        "entry_price": result.get("entry_price_usd") or result.get("entry_price") or evidence.get("entry_price"),
        "stop_loss": result.get("stop_loss_usd") or result.get("stop_loss"),
        "take_profit": result.get("take_profit_usd") or result.get("take_profit"),
        "actual_entry_price": result.get("average_fill_price") if result.get("trade_executed") is True else None,
        "actual_entry_time_utc": result.get("filled_at_utc") if result.get("trade_executed") is True else None,
        "actual_exit_price": None,
        "actual_exit_time_utc": None,
        "realized_net_pnl_usdt": None,
        "reported_trading_fees_usdt": None,
        "order_id": result.get("exchange_order_id") or result.get("order_id") or "",
        "position_id": result.get("position_id") or position.get("positionId") or "",
        "trade_executed": result.get("trade_executed") is True,
        "orders_submitted": result.get("orders_submitted") is True,
        "closed_confirmed": False,
        "ai_status": ai_status or "NOT_CALLED",
        "ai_model": ai_model,
        "ai_reason": ai_reason,
        "reviewer_consensus": reviewer_consensus,
        "ai_consensus": consensus,
        "ai_reviews": ai_reviews,
        "source_url": (os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/") + "/api/blofin/demo/autopilot/status") if os.getenv("RENDER_EXTERNAL_URL", "").strip() else "",
    }

def _queue_blofin_activity_journal(job_id: str, instrument_id: str, job: dict) -> dict:
    """Persist the activity/market snapshot to Turso before Notion network sync."""
    journal = None
    try:
        journal = NotionActivityJournal()
        snapshot = _blofin_activity_journal_snapshot(job_id, instrument_id, job)
        queued = journal.enqueue(snapshot)
        return {"status": "QUEUED" if queued else "UNCHANGED", "experience_id": snapshot["experience_id"]}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
    finally:
        if journal is not None:
            journal.close()

def _update_blofin_activity_after_close(
    outcome: dict, instrument_id: str, position_id: str,
    closed_rows: list, partial_rows: list,
) -> dict:
    """Update the same activity row only after full-size exchange history is reconciled."""
    if not isinstance(outcome, dict) or outcome.get("status") != "RECORDED":
        return {"status": "NOT_FINAL"}
    experience_id = str(outcome.get("experience_id") or "")
    journal = None
    try:
        journal = NotionActivityJournal()
        snapshot = journal.get_snapshot(experience_id)
        if not isinstance(snapshot, dict):
            return {"status": "ACTIVITY_ROW_NOT_FOUND"}
        matching = []
        def finite_number(value):
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if math.isfinite(number) else None

        for row in list(partial_rows or []) + list(closed_rows or []):
            if not isinstance(row, dict):
                continue
            if (
                str(row.get("positionId") or "") != str(position_id)
                or str(row.get("instId") or "").upper() != str(instrument_id).upper()
            ):
                continue
            quantity = finite_number(row.get("closePositions"))
            price = finite_number(row.get("closeAveragePrice"))
            if quantity is None or quantity <= 0 or price is None or price <= 0:
                continue
            matching.append((row, quantity, price))
        total_quantity = sum(item[1] for item in matching)
        if not matching or total_quantity <= 0:
            return {"status": "EXIT_PRICE_UNAVAILABLE"}
        weighted_exit = sum(quantity * price for _, quantity, price in matching) / total_quantity
        exit_ms = max(
            (int(row.get("updateTime") or row.get("createTime") or 0) for row, _, _ in matching),
            default=0,
        )
        fee_total = 0.0
        fee_values = [_blofin_safe_float(row.get("fee")) for row, _, _ in matching]
        if fee_values and all(value is not None for value in fee_values):
            fee_total = sum(value for value in fee_values if value is not None)
        snapshot.update({
            "actual_exit_price": weighted_exit,
            "actual_exit_time_utc": (
                datetime.fromtimestamp(exit_ms / 1000, tz=timezone.utc).isoformat()
                if exit_ms > 0 else None
            ),
            "realized_net_pnl_usdt": outcome.get("realized_pnl_after_reported_trading_fees"),
            "reported_trading_fees_usdt": fee_total,
            "closed_confirmed": True,
            "status": "CLOSED_RECONCILED",
        })
        _record_challenge1_audit(
            experience_id, "EXIT_RECONCILED",
            {
                "instrument_id": instrument_id, "position_id": position_id,
                "decision": "CLOSED_CONFIRMED_BY_BLOFIN_DEMO_HISTORY",
                "actual_exit_price": weighted_exit,
                "actual_exit_time_utc": snapshot.get("actual_exit_time_utc"),
                "realized_net_pnl_usdt": snapshot.get("realized_net_pnl_usdt"),
                "reported_trading_fees_usdt": fee_total,
                "closed_confirmed": True, "exchange_outcome": outcome,
                "reviewers_exit_status": "NOT_RUN_RULE_BASED_EXIT_MANAGER",
            },
            event_key=f"exit-reconciled:{position_id}:{exit_ms}",
            experience_id=experience_id, position_id=position_id,
            status="SUCCESS",
            reason="Final exit price, exchange history and realized PnL were reconciled.",
        )
        journal.enqueue(snapshot)
        return {"status": "QUEUED", "experience_id": experience_id}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
    finally:
        if journal is not None:
            journal.close()

def _blofin_job_notion_events(job_id: str, instrument_id: str, job: dict) -> list[dict]:
    """Build one robot record and one independently attributable record per AI reviewer."""
    current = job if isinstance(job, dict) else {}
    result = current.get("result") if isinstance(current.get("result"), dict) else {}
    summary = _blofin_autopilot_cycle_summary(current)
    signal = summary.get("signal") or "UNKNOWN"
    no_entry = bool(summary.get("no_entry"))
    orders_submitted = bool(summary.get("orders_submitted"))
    trade_executed = bool(summary.get("trade_executed"))
    status = "SUCCESS" if str(current.get("status") or "").lower() == "completed" else "FAILED"
    reason = result.get("reason_fa") or current.get("message_fa") or "No additional reason was supplied."
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else (
        current.get("analysis") if isinstance(current.get("analysis"), dict) else {}
    )
    reviews = result.get("ai_reviews") if isinstance(result.get("ai_reviews"), list) else (
        current.get("ai_reviews") if isinstance(current.get("ai_reviews"), list) else []
    )
    if not reviews:
        single = result.get("ai_review") if isinstance(result.get("ai_review"), dict) else (
            current.get("ai_review") if isinstance(current.get("ai_review"), dict) else {}
        )
        if single:
            reviews = [{"reviewer_id": single.get("reviewer_id") or "nemotron",
                        "actor": "Nemotron Supervisor", "review": single}]
    if reviews:
        present_reviewers = {
            str(item.get("reviewer_id") or (item.get("review") or {}).get("reviewer_id") or "").lower()
            for item in reviews if isinstance(item, dict)
        }
        not_called_reason = (
            "این ناظر در این چرخه فراخوانی نشد؛ نتیجهٔ عدم فراخوانی صریحاً ثبت شده است."
        )
        if "nemotron" not in present_reviewers:
            reviews.append({
                "reviewer_id": "nemotron", "actor": "Nemotron Supervisor",
                "review": {
                    "reviewer_id": "nemotron", "status": "NOT_CALLED",
                    "request_attempted": False, "skip_reason_code": "REVIEWER_NOT_PRESENT",
                    "response_source": "NONE", "response_validated": False,
                    "model_verified": False, "paper_entry_eligible": False,
                    "reason": not_called_reason,
                },
            })
        if not ({"second_ai", "second"} & present_reviewers):
            reviews.append({
                "reviewer_id": "second_ai", "actor": "Second AI Supervisor",
                "review": {
                    "reviewer_id": "second_ai", "status": "NOT_CALLED",
                    "request_attempted": False, "skip_reason_code": "REVIEWER_NOT_PRESENT",
                    "response_source": "NONE", "response_validated": False,
                    "model_verified": False, "paper_entry_eligible": False,
                    "reason": not_called_reason,
                },
            })

    if not reviews:
        blocked_stage = str(current.get("stage") or result.get("status") or "UNKNOWN")
        blocked_reason = (
            "هیچ فراخوانی خارجی برای این چرخه انجام نشد؛ ربات پیش از مرحله بازبینی AI "
            f"در مرحله {blocked_stage} متوقف شد."
        )
        reviews = [
            {
                "reviewer_id": "nemotron",
                "actor": "Nemotron Supervisor",
                "review": {
                    "reviewer_id": "nemotron", "status": "NOT_CALLED",
                    "request_attempted": False, "skip_reason_code": "UPSTREAM_STAGE_BLOCKED",
                    "response_source": "NONE", "response_validated": False,
                    "model_verified": False, "paper_entry_eligible": False,
                    "reason": blocked_reason,
                },
            },
            {
                "reviewer_id": "second_ai",
                "actor": "Second AI Supervisor",
                "review": {
                    "reviewer_id": "second_ai", "status": "NOT_CALLED",
                    "request_attempted": False, "skip_reason_code": "UPSTREAM_STAGE_BLOCKED",
                    "response_source": "NONE", "response_validated": False,
                    "model_verified": False, "paper_entry_eligible": False,
                    "reason": blocked_reason,
                },
            },
        ]

    consensus = result.get("ai_consensus") if isinstance(result.get("ai_consensus"), dict) else (
        current.get("ai_consensus") if isinstance(current.get("ai_consensus"), dict) else {}
    )
    consensus_status = str(consensus.get("status") or "NOT_COMPARABLE").upper()
    if not consensus:
        verified = [
            item.get("review", {}) for item in reviews
            if isinstance(item, dict) and isinstance(item.get("review"), dict)
            and item["review"].get("request_attempted") is True
            and item["review"].get("response_validated") is True
            and item["review"].get("model_verified") is True
        ]
        all_not_called = bool(reviews) and all(
            isinstance(item, dict)
            and isinstance(item.get("review"), dict)
            and item["review"].get("request_attempted") is False
            for item in reviews
        )
        consensus_status = (
            "AGREEMENT" if len(verified) >= 2 and len({
                str(item.get("status") or "").upper() for item in verified
            }) == 1 else
            "SINGLE_REVIEWER" if len(verified) == 1 else
            "NOT_CALLED" if all_not_called else
            "NOT_COMPARABLE"
        )

    reviewer_decisions = []
    for item in reviews:
        review = item.get("review") if isinstance(item, dict) and isinstance(item.get("review"), dict) else {}
        verified = (
            review.get("request_attempted") is True
            and review.get("response_source") == "LIVE_API"
            and review.get("response_validated") is True
            and review.get("model_verified") is True
            and bool(str(review.get("returned_model") or "").strip())
        )
        review_status = str(review.get("status") or "UNKNOWN").upper()
        request_attempted = review.get("request_attempted")
        if request_attempted is None and review_status in {"NOT_CALLED", "NOT_CONFIGURED"}:
            request_attempted = False
        if verified and review_status == "APPROVED" and review.get("paper_entry_eligible") is True:
            reviewer_decisions.append("APPROVE")
        elif verified and review_status in {"VETO", "REJECT"}:
            reviewer_decisions.append("REJECT")
        elif request_attempted is False:
            reviewer_decisions.append("NOT_CALLED")
        else:
            reviewer_decisions.append("OBSERVE_ONLY")

    if len(reviewer_decisions) >= 2 and all(x == "APPROVE" for x in reviewer_decisions):
        ai_decision = "APPROVE"
    elif "REJECT" in reviewer_decisions:
        ai_decision = "REJECT"
    elif reviewer_decisions and all(x == "NOT_CALLED" for x in reviewer_decisions):
        ai_decision = "NOT_CALLED"
    else:
        ai_decision = "OBSERVE_ONLY"

    order = result.get("order") if isinstance(result.get("order"), dict) else {}
    order_id = (
        result.get("exchange_order_id") or result.get("order_id")
        or order.get("exchange_order_id") or order.get("order_id") or ""
    )
    positions = result.get("positions") if isinstance(result.get("positions"), list) else []
    first_position = positions[0] if positions and isinstance(positions[0], dict) else {}
    position_id = (
        result.get("position_id") or first_position.get("positionId")
        or first_position.get("position_id") or ""
    )
    experience_id = (
        result.get("learning_experience_id")
        or current.get("learning_experience_id")
        or analysis.get("learning_experience_id")
        or ""
    )
    error_record = current.get("error") if isinstance(current.get("error"), dict) else {}
    details = {
        "job_id": str(job_id),
        "stage": current.get("stage"),
        "job_status": current.get("status"),
        "signal": signal,
        "orders_submitted": orders_submitted,
        "trade_executed": trade_executed,
        "gate_pass_count": analysis.get("gate_pass_count"),
        "gate_fail_count": analysis.get("gate_fail_count"),
        "gate_unknown_count": analysis.get("gate_unknown_count"),
        "result_status": result.get("status"),
        "input_data_type": "LIVE_MARKET",
        "reason": str(reason)[:1200],
        "error_type": error_record.get("error_type") or error_record.get("type") or "",
        "error_message": str(error_record.get("error_message") or "")[:1200],
        "failed_operation": str(error_record.get("failed_operation") or "")[:160],
        "execution_stage": str(error_record.get("execution_stage") or current.get("stage") or "")[:128],
        "database_backend": str(error_record.get("database_backend") or "")[:64],
        "error_trace_reference": str(error_record.get("error_trace_reference") or "")[:256],
        "error_timestamp_utc": str(error_record.get("timestamp_utc") or "")[:64],
    }
    common = {
        "cycle_id": str(job_id),
        "occurred_at_utc": (
            current.get("completed_at_utc") or current.get("updated_at_utc")
            or datetime.now(timezone.utc).isoformat()
        ),
        "market": "Futures",
        "symbol": str(instrument_id).upper(),
        "timeframe": "5m",
        "signal": signal if signal in {"LONG", "SHORT", "NO_TRADE"} else "UNKNOWN",
        "experience_id": experience_id,
        "order_id": order_id,
        "position_id": position_id,
        "source_url": (os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/") + "/api/blofin/demo/autopilot/status") if os.getenv("RENDER_EXTERNAL_URL", "").strip() else "",
        "input_data_type": "LIVE_MARKET",
        "reviewer_consensus": consensus_status,
    }
    main_type = "NO_TRADE" if no_entry else "ANALYSIS_COMPLETED"
    events = [{
        **common,
        "event_id": f"{job_id}:ROBOT:{main_type}",
        "event_type": main_type,
        "actor": "Robot",
        "status": status,
        "reason": str(reason)[:1600],
        "details": json.dumps(details, ensure_ascii=False, sort_keys=True),
        "ai_decision": ai_decision,
    }]
    if orders_submitted:
        events.append({
            **common, "event_id": f"{job_id}:ROBOT:ORDER_SUBMITTED",
            "event_type": "ORDER_SUBMITTED", "actor": "Robot", "status": "SUCCESS",
            "reason": "BloFin Demo accepted an order submission; exchange fill must be verified separately.",
            "details": json.dumps({"job_id": job_id, "order_id": order_id}, ensure_ascii=False),
            "ai_decision": ai_decision,
        })
    if trade_executed:
        events.append({
            **common, "event_id": f"{job_id}:BLOFIN:ORDER_FILLED",
            "event_type": "ORDER_FILLED", "actor": "BloFin Demo", "status": "SUCCESS",
            "reason": "A Demo fill was confirmed by the execution worker.",
            "details": json.dumps({
                "job_id": job_id,
                "average_fill_price": result.get("average_fill_price"),
                "filled_size": result.get("filled_size"),
            }, ensure_ascii=False),
            "ai_decision": ai_decision,
        })

    for index, item in enumerate(reviews):
        if not isinstance(item, dict):
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else {}
        reviewer_id = str(item.get("reviewer_id") or review.get("reviewer_id") or f"reviewer_{index + 1}")
        actor = str(item.get("actor") or (
            "Nemotron Supervisor" if reviewer_id.lower() == "nemotron" else
            "Second AI Supervisor" if reviewer_id.lower() in {"second_ai", "second"} else
            "AI Supervisor"
        ))
        review_status = str(review.get("status") or "UNKNOWN").upper()
        request_attempted = review.get("request_attempted")
        if review_status == "PENDING" and request_attempted is None:
            # Scheduled async review is not an actual NOT_CALLED response.
            continue
        if request_attempted is None and review_status in {"NOT_CALLED", "NOT_CONFIGURED"}:
            request_attempted = False
        response_source = str(review.get("response_source") or "NONE").upper()
        response_validated = review.get("response_validated") is True
        model_verified = review.get("model_verified") is True
        verified_response = (
            request_attempted is True and response_source == "LIVE_API"
            and response_validated and model_verified
            and bool(str(review.get("returned_model") or "").strip())
        )
        if verified_response and review_status == "APPROVED" and review.get("paper_entry_eligible") is True:
            reviewer_decision = "APPROVE"
            event_type = "AI_APPROVAL"
        elif verified_response and review_status in {"VETO", "REJECT"}:
            reviewer_decision = "REJECT"
            event_type = "AI_VETO"
        elif request_attempted is False:
            reviewer_decision = "NOT_CALLED"
            event_type = "AI_REVIEW_REQUESTED"
        else:
            reviewer_decision = "OBSERVE_ONLY"
            event_type = "AI_RECOMMENDATION"

        call_status = (
            "CALLED" if request_attempted is True else
            "NOT_CONFIGURED" if review_status == "NOT_CONFIGURED" else
            "NOT_CALLED" if request_attempted is False else
            "ERROR" if review_status == "ERROR" else "UNKNOWN"
        )
        validation_status = (
            "VALID" if verified_response else
            "INVALID" if request_attempted is True and response_source == "LIVE_API" else
            "NOT_RUN" if request_attempted is False else "UNKNOWN"
        )
        actual_model = str(review.get("returned_model") or "") if verified_response else ""
        review_reason = review.get("reason") or review.get("status_fa") or reason
        event_details = {
            "job_id": str(job_id),
            "reviewer_id": reviewer_id,
            "reviewer_status": review_status,
            "configured_model": review.get("model") or review.get("configured_model"),
            "returned_model": review.get("returned_model"),
            "request_attempted": request_attempted,
            "skip_reason_code": review.get("skip_reason_code"),
            "response_source": response_source,
            "response_validated": response_validated,
            "model_verified": model_verified,
            "http_status": review.get("http_status"),
            "latency_ms": review.get("latency_ms"),
            "total_tokens": review.get("total_tokens"),
            "paper_entry_eligible": review.get("paper_entry_eligible"),
            "native_council": review.get("native_council"),
            "error_type": review.get("error_type"),
            "error_code": review.get("error_code"),
            "consensus": consensus,
        }
        events.append({
            **common,
            "event_id": f"{job_id}:AI:{reviewer_id}:{event_type}",
            "event_type": event_type,
            "actor": actor,
            "status": "SUCCESS" if verified_response else "WARNING",
            "ai_decision": reviewer_decision,
            "ai_model": actual_model,
            "reviewer_id": reviewer_id,
            "ai_provider": review.get("provider") or "",
            "ai_prompt_version": review.get("prompt_version") or "",
            "ai_requested_at_utc": review.get("requested_at_utc") or review.get("request_started_at_utc"),
            "ai_completed_at_utc": review.get("completed_at_utc"),
            "ai_call_status": call_status,
            "ai_validation": validation_status,
            "ai_analysis": review.get("rationale_fa") or review.get("reason") or "",
            "ai_suggestion": review.get("final_rating") or review.get("status_fa") or review_status,
            "reason": str(review_reason)[:1600],
            "details": json.dumps(event_details, ensure_ascii=False, sort_keys=True, default=str),
        })

    if consensus_status == "DISAGREEMENT":
        events.append({
            **common,
            "event_id": f"{job_id}:AI:COUNCIL:AI_DISAGREEMENT",
            "event_type": "AI_DISAGREEMENT",
            "actor": "AI Supervisor",
            "status": "WARNING",
            "ai_decision": "OBSERVE_ONLY",
            "reviewer_id": "COUNCIL",
            "reviewer_consensus": "DISAGREEMENT",
            "ai_call_status": "CALLED",
            "ai_validation": "VALID",
            "input_data_type": "LIVE_MARKET",
            "reason": "دو ناظر مستقل به نتیجه یکسان نرسیدند؛ این گزارش صرفاً نظارتی است و مانع ورود یا مدیریت پوزیشن Challenge 1 نمی‌شود.",
            "details": json.dumps({
                "job_id": str(job_id),
                "nemotron_status": consensus.get("primary_status"),
                "second_ai_status": consensus.get("second_status"),
                "decision": "OBSERVE_ONLY",
                "orders_submitted": orders_submitted,
                "trade_executed": trade_executed,
            }, ensure_ascii=False, sort_keys=True),
        })
    return events

def _queue_blofin_job_notion_events(job_id: str, instrument_id: str, job: dict) -> dict:
    """Persist the event/outbox before attempting any non-critical Notion network I/O."""
    outbox = None
    queued = 0
    try:
        outbox = NotionEventOutbox()
        for event in _blofin_job_notion_events(job_id, instrument_id, job):
            queued += int(outbox.enqueue(event))
        return {"status": "QUEUED", "queued": queued}
    except Exception as exc:
        # Notion observability is intentionally fail-open for the trading engine.
        return {"status": "UNAVAILABLE", "queued": queued, "error_type": type(exc).__name__}
    finally:
        if outbox is not None:
            outbox.close()


NOTION_SYNC_THREAD = None
NOTION_SYNC_START_LOCK = threading.Lock()
NOTION_EVENT_SYNC_STATE = {
    "status": "STARTING",
    "pending": 0,
    "synced": 0,
    "failed": 0,
    "retrying": 0,
    "last_error_type": None,
    "last_http_status": None,
    "recent_errors": [],
    "last_worker_error_type": None,
    "last_checked_at_utc": None,
}
NOTION_ACTIVITY_SYNC_STATE = {
    "status": "STARTING",
    "pending": 0,
    "synced": 0,
    "failed": 0,
    "last_success_utc": None,
    "latest_experience_id": None,
    "last_error_type": None,
    "last_http_status": None,
    "last_checked_at_utc": None,
}

def _record_notion_event_sync_state(
    result: dict | None = None, *, error_type: str | None = None,
) -> dict:
    """Publish outbox retry details and worker errors without exposing credentials."""
    with LOCK:
        NOTION_EVENT_SYNC_STATE["last_checked_at_utc"] = datetime.now(timezone.utc).isoformat()
        if isinstance(result, dict):
            NOTION_EVENT_SYNC_STATE.update(copy.deepcopy(result))
            NOTION_EVENT_SYNC_STATE["last_worker_error_type"] = None
        elif error_type:
            NOTION_EVENT_SYNC_STATE.update({
                "status": "WORKER_ERROR",
                "last_worker_error_type": str(error_type)[:100],
            })
        return copy.deepcopy(NOTION_EVENT_SYNC_STATE)


def _notion_sync_worker() -> None:
    """Flush pending outbox events while the Demo web process is awake."""
    try:
        interval = int(os.getenv("NOTION_SYNC_INTERVAL_SECONDS", "30"))
    except (TypeError, ValueError):
        interval = 30
    interval = max(15, min(interval, 300))
    while True:
        outbox = None
        activity_journal = None
        event_sync_result = None
        event_sync_error = None
        try:
            outbox = NotionEventOutbox()
            event_sync_result = outbox.flush_pending(limit=10)
        except Exception as exc:
            # A temporary Notion/Turso outage must not stop trading, but it must
            # remain visible in the operational status endpoint.
            event_sync_error = type(exc).__name__
        finally:
            if outbox is not None:
                outbox.close()
        _record_notion_event_sync_state(event_sync_result, error_type=event_sync_error)
        activity_sync_result = None
        activity_sync_error = None
        try:
            activity_journal = NotionActivityJournal()
            activity_sync_result = activity_journal.flush_pending(limit=3)
        except Exception as exc:
            # Chart/file sync is retryable and must never interrupt market analysis.
            activity_sync_error = type(exc).__name__
        finally:
            if activity_journal is not None:
                activity_journal.close()
        with LOCK:
            NOTION_ACTIVITY_SYNC_STATE["last_checked_at_utc"] = datetime.now(timezone.utc).isoformat()
            if isinstance(activity_sync_result, dict):
                NOTION_ACTIVITY_SYNC_STATE.update(activity_sync_result)
                NOTION_ACTIVITY_SYNC_STATE["last_worker_error_type"] = None
            elif activity_sync_error:
                NOTION_ACTIVITY_SYNC_STATE["status"] = "WORKER_ERROR"
                NOTION_ACTIVITY_SYNC_STATE["last_worker_error_type"] = activity_sync_error
        time.sleep(interval)



def _start_notion_sync_worker() -> None:
    """Start a single best-effort outbox flusher for this web process."""
    global NOTION_SYNC_THREAD
    with NOTION_SYNC_START_LOCK:
        if NOTION_SYNC_THREAD is not None and NOTION_SYNC_THREAD.is_alive():
            return
        NOTION_SYNC_THREAD = threading.Thread(
            target=_notion_sync_worker,
            daemon=True,
            name="notion-event-outbox-sync",
        )
        NOTION_SYNC_THREAD.start()


def main():
    port = int(os.getenv("PORT", "10000"))
    host = os.getenv("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    role = os.getenv("BLOFIN_DEMO_AUTOPILOT_ROLE", "WEB").strip().upper() or "WEB"
    if role == "WEB":
        _start_notion_sync_worker()
        _start_blofin_demo_position_manager()
    _start_blofin_demo_autopilot()
    print(f"CRYPTO SUPER RADAR listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
