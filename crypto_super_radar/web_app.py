"""Dependency-light Persian web dashboard for on-demand research scans."""
from __future__ import annotations

import html
import hashlib
import hmac
import copy
import concurrent.futures
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .integrated_engine import WhaleAwareConfirmationEngine
from .indicators import observed_price_ranges
from .models import AssetSpec, GateStatus, Signal
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
from .providers import _ohlcv_frame
from .feed_health import build_feed_health_report
from .global_market_data import GlobalMarketBoard
from .live_market_radar import BYBIT_INTERVAL, INTERVAL_MS, build_live_market_radar_payload
from .blofin_demo_readonly import BloFinDemoAuthError, BloFinDemoError, BloFinDemoReadOnly, BloFinDemoResponseError
from .ai_second_opinion import ai_connection_status, run_openrouter_connection_diagnostic

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
BLOFIN_STATUS_LOCK = threading.Lock()

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


def _blofin_demo_connection_status() -> dict:
    """Return a rate-limited, non-sensitive read-only BloFin Demo connectivity result."""
    with BLOFIN_STATUS_LOCK:
        now = time.monotonic()
        cached = BLOFIN_STATUS_CACHE.get("payload")
        if isinstance(cached, dict) and BLOFIN_STATUS_CACHE.get("expires_at", 0.0) > now:
            return copy.deepcopy(cached)

        credential_names = (
            "BLOFIN_DEMO_API_KEY",
            "BLOFIN_DEMO_API_SECRET",
            "BLOFIN_DEMO_PASSPHRASE",
        )
        configured = all(bool(os.getenv(name, "").strip()) for name in credential_names)
        stage = "ENVIRONMENT_CHECK"
        market_data = "NOT_CHECKED"
        private_api = "NOT_CHECKED"
        if not configured:
            payload = {
                "status": "NOT_CONFIGURED",
                "environment": "BLOFIN_DEMO",
                "permission_mode": "READ_ONLY",
                "market_data": market_data,
                "private_api": private_api,
                "orders_enabled": False,
                "message_fa": "سه متغیر API Key، Secret Key و Passphrase باید در Environment همین سرویس تنظیم شوند.",
            }
            cache_seconds = 15
        else:
            try:
                client = BloFinDemoReadOnly(timeout=8.0)

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
                    "permission_mode": "READ_ONLY",
                    "market_data": market_data,
                    "private_api": private_api,
                    "orders_enabled": False,
                    "message_fa": "اتصال Demo و احراز هویت API موفق است. ربات هنوز فقط اطلاعات را می‌خواند و هیچ سفارشی ارسال نمی‌کند.",
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
                    "permission_mode": "READ_ONLY",
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
                    "permission_mode": "READ_ONLY",
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
                    "permission_mode": "READ_ONLY",
                    "market_data": market_data,
                    "private_api": private_api,
                    "orders_enabled": False,
                    "failed_stage": stage,
                    "error_type": type(exc).__name__,
                    "message_fa": "آزمون اتصال با خطای پیش‌بینی‌نشده متوقف شد؛ اطلاعات محرمانه در پاسخ عمومی نمایش داده نمی‌شود.",
                }
                cache_seconds = 20

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


def _blofin_demo_account_snapshot() -> dict:
    """Fetch a deliberately whitelisted read-only account snapshot from BloFin Demo."""
    try:
        client = BloFinDemoReadOnly(timeout=8.0)
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
            "instId", "marginMode", "positionSide", "leverage", "positions",
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
            "permission_mode": "READ_ONLY",
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
            "permission_mode": "READ_ONLY",
            "orders_enabled": False,
            "message_fa": "BloFin احراز هویت درخواست خواندن حساب را رد کرد.",
        }
    except BloFinDemoError as exc:
        return {
            "status": "READ_FAILED",
            "environment": "BLOFIN_DEMO",
            "permission_mode": "READ_ONLY",
            "orders_enabled": False,
            "error_type": type(exc).__name__,
            "message_fa": "خواندن اطلاعات حساب از BloFin Demo کامل نشد.",
        }
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "environment": "BLOFIN_DEMO",
            "permission_mode": "READ_ONLY",
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
            "networks": copy.deepcopy(result.get("networks") or ["base", "solana"]),
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
        "networks": ["base", "solana"],
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


def _gem_hunter_api_payload(limit: int = 20, *, include_diagnostics: bool = False) -> dict:
    """Return a cached Gem Hunter result immediately and refresh expired data in a worker."""
    limit = max(1, min(int(limit), 50))
    now = time.monotonic()
    with GEM_HUNTER_RESULT_CACHE_LOCK:
        cached = copy.deepcopy(GEM_HUNTER_RESULT_CACHE.get("payload"))
        expires_at = GEM_HUNTER_RESULT_CACHE.get("expires_at", 0.0)
        refreshing = GEM_HUNTER_RESULT_CACHE.get("refreshing", False)

    if (cached is None or expires_at <= now) and not refreshing:
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
    status = (
        "PASS" if complete and all(item.get("status") == "PASS" for item in items[:total])
        else "PARTIAL" if complete else "LOADING"
    )
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
        "freshness": "FRESH" if complete else "PARTIAL",
        "updated_at_utc": datetime.now(timezone.utc).isoformat() if items else None,
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
    if refreshing and cached.get("completed_items", 0) < cached.get("total_items", len(CRYPTO_MARKET_UNIVERSE)):
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

def _run_futures_paper_tick(job_id: str, asset: AssetSpec) -> None:
    """Run one futures-only paper cycle and publish its directional risk/account state."""
    global SCAN_RUNNING
    try:
        cycle = FUTURES_PAPER_BOT.run_once(asset)
        state = FUTURES_PAPER_BOT.state()
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


class Handler(BaseHTTPRequestHandler):
    server_version = "CryptoSuperRadar/0.2"

    def _send(self, status: int, content: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

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
            payload = ai_connection_status()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/blofin/demo/status":
            payload = _blofin_demo_connection_status()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
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
            payload = _gem_hunter_api_payload(limit=limit, include_diagnostics=True)
            payload["report_mode"] = "RESEARCH_ONLY_NOT_A_TRADE_SIGNAL"
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
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
            <p class="muted">تحلیل کمی و Paper Research؛ بدون اتصال کیف‌پول یا ارسال سفارش</p>
          </div>
          <span class="mode-pill"><span class="mode-dot"></span>فقط خواندنی</span>
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
          <h2>BloFin Demo Futures · اتصال و حساب فقط‌خواندنی</h2>
          <p class="muted">اتصال API به‌صورت خودکار بررسی می‌شود. برای دیدن موجودی، پوزیشن‌ها و سفارش‌های باز، توکن مستقل پنل را وارد کن. این توکن در مرورگر ذخیره نمی‌شود.</p>
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
          <p class="muted" id="blofin-demo-safety">محیط: Demo · مجوز: فقط‌خواندنی · ارسال سفارش: غیرفعال</p>
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
            if(!checkButton||!status||!accountButton||!clearButton||!tokenInput||!accountStatus||!accountView||!safety)return;

            checkButton.addEventListener('click',async function(){
              checkButton.disabled=true;
              status.textContent='در حال بررسی اتصال امن به BloFin Demo...';
              try{
                const response=await fetch('/api/blofin/demo/status',{cache:'no-store'});
                if(!response.ok)throw new Error('HTTP '+response.status);
                const result=await response.json();
                const messages={
                  CONNECTED_READ_ONLY:'اتصال موفق است؛ بازار، موجودی Futures، پوزیشن‌ها و سفارش‌های باز با مجوز فقط‌خواندنی تأیید شدند.',
                  NOT_CONFIGURED:'متغیرهای API در Environment همین سرویس تنظیم نشده‌اند.',
                  AUTH_FAILED:'احراز هویت ناموفق بود؛ مقادیر و مجوز READ را بررسی کن.',
                  CONNECTION_FAILED:'اتصال به BloFin Demo برقرار نشد؛ شبکه یا محدودیت IP را بررسی کن.',
                  UNKNOWN:'نتیجه آزمون نامشخص است؛ آزمون را بعداً تکرار کن.'
                };
                status.textContent=messages[result.status]||messages.UNKNOWN;
                safety.textContent='محیط: Demo · مجوز: فقط‌خواندنی · ارسال سفارش: غیرفعال';
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
              [['ارزش کل حساب (USD)',balance.totalEquity],['ارزش مارجین ایزوله',balance.isolatedEquity],
               ['تعداد پوزیشن‌ها',(result.positions||[]).length],['سفارش‌های باز',(result.open_orders||[]).length]]
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
                accountStatus.textContent='اطلاعات حساب Demo دریافت شد. زمان دریافت: '+new Date().toLocaleString();
                safety.textContent='محیط: Demo · مجوز: فقط‌خواندنی · ارسال سفارش: غیرفعال';
              }catch(error){
                accountStatus.textContent='ارتباط با پنل حساب برقرار نشد.';
              }finally{
                accountButton.disabled=false;
              }
            });
            clearButton.addEventListener('click',function(){
              tokenInput.value='';
              accountView.replaceChildren();
              accountStatus.textContent='توکن و اطلاعات نمایش‌داده‌شده از صفحه پاک شدند.';
            });
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
          <p class="muted">توکن‌های تازهٔ Solana و Base را در مرحلهٔ کشف شناسایی می‌کند. فقط BUYهایی نمایش داده می‌شوند که پروندهٔ ارزیابی نسخه‌دار، حداقل ۹ فیلتر از ۱۲، عبور همهٔ ۴ گیت حیاتی و شواهد تازه و قابل‌ردیابی بازار و Swapهای واقعی نهنگ را داشته باشند. نامزدهای کشف‌شده نیز جداگانه در فهرست پژوهشی نمایش داده می‌شوند؛ حتی اگر هنوز ارزیابی نشده، رد شده یا دارای دادهٔ ناقص باشند. این فهرست سیگنال خرید نیست و دادهٔ قدیمی با برچسب مشخص می‌شود. در هر نوبت حداکثر ۱۰ استخر و ۵ قرارداد به‌صورت چرخشی بررسی می‌شوند؛ بقیه در صف بررسی نوبت‌های بعدی می‌مانند.</p>
          <div class="grid" style="align-items:center;margin:12px 0">
            <button id="gem-hunter-run" type="button">شروع کشف کاندیداها</button>
            <span id="gem-hunter-status" class="muted" aria-live="polite">هنوز اسکن کشف اجرا نشده است.</span>
          </div>
          <div id="gem-hunter-results" style="overflow-x:auto">
            <p class="muted">برای دریافت فهرست اولیهٔ کاندیداها دکمهٔ کشف را بزنید.</p>
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
            trend:'روند', momentum:'شتاب', relative_strength:'قدرت نسبی',
            volume_confirmation:'تأیید حجم', buy_sell_pressure:'فشار خرید/فروش',
            capital_flow:'جریان سرمایه', whale_activity:'فعالیت نهنگ',
            liquidity_depth:'عمق نقدینگی', market_integrity:'سلامت معاملات',
            contract_security:'امنیت قرارداد', holder_distribution:'توزیع هولدرها',
            market_regime_validation:'تأیید رژیم بازار'
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
              if (item.token_address && ['solana','base'].includes(item.chain)) addLink(links, 'اکسپلورر ↗', explorerBase(item.chain) + '/token/' + encodeURIComponent(item.token_address));
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
            card.appendChild(el('p', (item.chain || 'unknown').toUpperCase() + ' · نامزد مرحلهٔ کشف · ' + (item.candidate_status || 'UNKNOWN') + ' · ' + (item.trade_authorization || 'NO_TRADE'), 'candidate-meta'));
            card.appendChild(el('p', 'امتیاز اولویت پژوهشی، نه احتمال موفقیت و نه سیگنال خرید: ' + (item.discovery_priority_score ?? 'ناموجود'), 'candidate-meta'));
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
            if (item.pool_address && ['solana','base'].includes(item.chain)) addLink(links, 'بازار در DEX Screener ↗', poolUrl(item));
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
            details.appendChild(el('summary', 'گزارش کامل ۱۲ فیلتر و ۴ گیت'));
            details.appendChild(el('h4', '۱۲ فیلتر مستقل'));
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
                const response = await fetch('/api/gem-hunter/report?limit=50', {
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
                  /^HTTP (429|5\d\d)$/.test(message);
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
            button.disabled = true; results.replaceChildren(el('p', 'در حال دریافت نامزدها و ساخت گزارش فارسی ۱۲ فیلتر و ۴ گیت...'));
             window.CryptoRadar.scanning('radar-gem-hunter', 'در حال کشف و ارزیابی نامزدهای Solana و Base...');
            status.textContent = 'در حال کشف و اعتبارسنجی Solana و Base...';
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
              summary.appendChild(el('p', 'کشف: ' + discovered + ' | دارای پروندهٔ ارزیابی: ' +
                Number(data.evaluation_record_count || 0) + ' | ساختار کامل پرونده: ' +
                Number(data.complete_evaluation_count || 0) + ' | ارزیابی ناقص: ' +
                Number(data.incomplete_evaluation_count || 0) + ' | ارزیابی‌نشده: ' +
                Number(data.unassessed_candidate_count || 0), 'muted'));
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
              candidates.forEach(item => list.appendChild(renderCandidate(item)));
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
          <p class="muted">LIVE در این بخش فقط به معنی تازه‌بودن قیمت‌های منتخب و تأیید آن‌ها توسط Binance و Bybit است؛ این وضعیت به معنی پوشش کل بازار یا اتصال WebSocket نیست.</p>
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
          <p class="muted">این گزارش فقط معاملات بسته‌شدهٔ Paper Trading را می‌سنجد. تا ثبت حداقل ۳۰ معامله، نمونه برای ارزیابی کافی نیست؛ حتی پس از آن نیز عملکرد گذشته تضمین سود آینده نیست. لغزش قیمت جداگانه از قیمت اجرای شبیه‌سازی‌شده تفکیک نشده است.</p>
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
              const received=new Date(data.updated_at_utc).toLocaleString();
              status.textContent='منابع: '+(data.sources||[]).join(' و ')+' · دریافت: '+received+
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
            <p class="live-radar-note">این بخش فعلاً هشدار تحلیلی است و به Paper Bot سفارش نمی‌دهد. هیچ الگوریتمی کف یا سقف مطلق را تضمین نمی‌کند. کندل در حال تشکیل صرفاً برای نمایش است و ممکن است تغییر کند.</p>
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
          <p class="muted">ربات فقط LONG در بازار Spot را با همان ۱۲ فیلتر ارزیابی می‌کند؛ هر بار یک چرخه اجرا می‌شود. ریسک هر موقعیت حداکثر ۰٫۵٪ سرمایه، ارزش موقعیت حداکثر ۱۰٪، بدون اهرم و بدون سفارش واقعی است. کارمزد و لغزش اعمال می‌شوند؛ حد سربه‌سر متحرک، خروج زمان‌دار، سقف زیان روزانه، افت سرمایه و وقفه پس از زیان هم ورودهای تازه را کنترل می‌کنند.</p>
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
          <script>(async function(){const box=document.getElementById('futures-paper-status');try{const r=await fetch('/futures-paper-bot/status',{cache:'no-store'});const s=await r.json();const a=s.account||{},p=s.open_position||{},risk=s.risk_controls||{};box.textContent='حالت: '+s.mode+' · موجودی: '+Number(a.balance_usd||0).toFixed(2)+' USD · ارزش حساب: '+Number(a.equity_usd||0).toFixed(2)+' USD · موقعیت: '+(p.direction||'ندارد')+' '+(p.asset||'')+' · حد ضرر: '+(p.stop_loss||'—')+' · حد سود: '+(p.take_profit||'—')+' · ترمز ریسک: '+(risk.entry_allowed?'ورود مجاز':'ورود متوقف')+' · سفارش واقعی: '+(s.orders_submitted?'فعال':'غیرفعال');}catch(e){box.textContent='وضعیت حساب فیوچرز در دسترس نیست.';}})();</script>
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
        if route not in {"/scan", "/paper-bot/tick", "/futures-paper-bot/tick", "/api/ai/diagnostic"}:
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
                    "<p id='futures-state' aria-live='polite'>در حال ارزیابی دادهٔ Perpetual و مدیریت موقعیت LONG/SHORT...</p>"
                    "<pre id='futures-result' style='white-space:pre-wrap;overflow-wrap:anywhere'></pre>"
                    "<p><a href='/'>بازگشت به داشبورد</a></p>"
                    "<script>(function(){const id='" + job_id + "',state=document.getElementById('futures-state'),out=document.getElementById('futures-result');"
                    "async function poll(){try{const r=await fetch('/jobs/'+id,{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();"
                    "state.textContent='وضعیت: '+j.status;if(j.status==='completed'){const p=j.result||{},c=p.cycle||{},a=(p.futures_state||{}).account||{};"
                    "state.textContent='نتیجه: '+(c.status||'UNKNOWN')+' · جهت: '+(c.direction||'بدون معامله')+' · موجودی: '+Number(a.balance_usd||0).toFixed(2)+' USD · ارزش حساب: '+Number(a.equity_usd||0).toFixed(2)+' USD';"
                    "out.textContent=JSON.stringify(p,null,2);return;}if(j.status==='failed'){out.textContent=j.error||'چرخه ناموفق بود';return;}}catch(e){state.textContent='خطای ارتباط؛ تلاش مجدد برای همان چرخه';}"
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


def main():
    port = int(os.getenv("PORT", "10000"))
    host = os.getenv("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"CRYPTO SUPER RADAR listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()