"""Bounded, read-only fast path for manually submitted DEX token scans."""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .gem_hunter_v3 import process_gem_hunter_v3_candidates


DEX_CHAIN_IDS = {"solana": "solana", "base": "base", "eth": "ethereum", "ethereum": "ethereum"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _fetch_json(url: str, timeout: float = 4.5) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "CryptoSuperRadar/1.0"})
    with urlopen(request, timeout=max(0.5, min(float(timeout), 5.0))) as response:
        return json.loads(response.read(1_000_000).decode("utf-8"))


def build_fast_asset_scan_report(
    asset: Any,
    *,
    fetch_json: Callable[[str], Any] | None = None,
    v3_processor: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Fetch one token's DEX pair data and score it with Gem Hunter V3; never trades."""
    started = time.monotonic()
    chain = str(getattr(asset, "chain", "") or "").lower()
    address = str(getattr(asset, "address", "") or "").strip()
    dex_chain = DEX_CHAIN_IDS.get(chain)
    if not dex_chain or not address:
        raise ValueError("شبکه یا آدرس قرارداد برای اسکن سریع DEX معتبر نیست.")

    fetch = fetch_json or _fetch_json
    url = f"https://api.dexscreener.com/token-pairs/v1/{dex_chain}/{address}"
    try:
        response = fetch(url)
    except (TimeoutError, HTTPError, URLError, OSError) as exc:
        return _limited_report(asset, "منبع DEX پاسخ نداد؛ دادهٔ بازار قابل تأیید نیست.", type(exc).__name__, started)
    except Exception as exc:
        return _limited_report(asset, "دریافت دادهٔ DEX با خطا روبه‌رو شد.", type(exc).__name__, started)

    pairs = response if isinstance(response, list) else []
    if not pairs:
        return _limited_report(asset, "برای این آدرس جفت‌ارز معتبری در DEX Screener پیدا نشد.", "NO_PAIR_DATA", started)

    pair = max(
        (item for item in pairs if isinstance(item, dict)),
        key=lambda item: _number((item.get("liquidity") or {}).get("usd")) or 0.0,
        default=None,
    )
    if pair is None:
        return _limited_report(asset, "ساختار پاسخ جفت‌ارز معتبر نبود.", "INVALID_PAIR_SCHEMA", started)

    base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    quote = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
    liquidity = _number((pair.get("liquidity") or {}).get("usd"))
    volume = _number((pair.get("volume") or {}).get("h24"))
    txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    tx24 = txns.get("h24") if isinstance(txns.get("h24"), dict) else {}
    tx1 = txns.get("h1") if isinstance(txns.get("h1"), dict) else {}
    buys = _number(tx24.get("buys"))
    sells = _number(tx24.get("sells"))
    buys_1h = _number(tx1.get("buys"))
    sells_1h = _number(tx1.get("sells"))
    pair_created_ms = _number(pair.get("pairCreatedAt"))
    pool_created_at_utc = (
        datetime.fromtimestamp(pair_created_ms / 1000.0, tz=timezone.utc).isoformat()
        if pair_created_ms is not None and pair_created_ms > 0 else None
    )
    price = _number(pair.get("priceUsd"))
    changes = pair.get("priceChange") if isinstance(pair.get("priceChange"), dict) else {}

    candidate = {
        "candidate_id": f"{dex_chain}:{address}",
        "chain": dex_chain,
        "token_address": address,
        "token_name": base.get("name") or "نامشخص",
        "token_symbol": base.get("symbol") or "UNKNOWN",
        "price_usd": price,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "buys_24h": buys,
        "sells_24h": sells,
        "buys_1h": buys_1h,
        "sells_1h": sells_1h,
        "pool_created_at_utc": pool_created_at_utc,
        "price_change_5m_pct": _number(changes.get("m5")),
        "price_change_1h_pct": _number(changes.get("h1")),
        "price_change_6h_pct": _number(changes.get("h6")),
        "price_change_24h_pct": _number(changes.get("h24")),
        "market_cap_usd": _number(pair.get("marketCap")),
        "fdv_usd": _number(pair.get("fdv")),
        "dex_id": pair.get("dexId"),
        "pair_address": pair.get("pairAddress"),
        "pair_url": pair.get("url"),
        "quote_symbol": quote.get("symbol"),
        "market_reconciliation": {
            "status": "UNKNOWN",
            "verified_liquidity_usd": None,
            "verified_volume_24h_usd": None,
            "reason": "دادهٔ سریع از یک منبع DEX دریافت شده و تطبیق مستقل انجام نشده است.",
        },
        "security_evidence": {"status": "UNKNOWN", "reason": "ممیزی امنیتی مستقل در مسیر سریع اجرا نشده است."},
        "technical_evidence": {"coverage_status": "UNKNOWN", "filters": {}},
        "whale_evidence": {"status": "NOT_AVAILABLE", "reason": "جریان کیف‌پول‌های نهنگ برای این جفت‌ارز در منبع سریع ارائه نشده است."},
        "trade_authorization": "NO_TRADE",
    }
    processor = v3_processor or process_gem_hunter_v3_candidates
    try:
        processed = processor([candidate], notify=False)
        scored = processed[0] if processed and isinstance(processed[0], dict) else candidate
    except Exception as exc:
        scored = candidate
        scored["gem_hunter_v3_error"] = type(exc).__name__

    whale = scored.get("whale_evidence") or candidate["whale_evidence"]
    risk_score = _number(scored.get("risk_score"))
    opportunity_score = _number(scored.get("opportunity_score"))
    return {
        "status": "COMPLETE" if price is not None else "LIMITED",
        "message_fa": "تحلیل سریع Gem Hunter V3 تکمیل شد؛ داده‌های تک‌منبعی به‌عنوان تأیید مستقل معرفی نمی‌شوند.",
        "asset": getattr(asset, "raw", ""),
        "chain": dex_chain,
        "contract_address": address,
        "token_name": scored.get("token_name") or base.get("name") or "نامشخص",
        "symbol": scored.get("token_symbol") or base.get("symbol") or "UNKNOWN",
        "price_usd": price,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "buys_24h": buys,
        "sells_24h": sells,
        "price_change_pct": {key: _number(changes.get(key)) for key in ("m5", "h1", "h6", "h24")},
        "market_cap_usd": candidate["market_cap_usd"],
        "fdv_usd": candidate["fdv_usd"],
        "opportunity_score": opportunity_score,
        "risk_score": risk_score,
        "pump_phase": scored.get("pump_phase"),
        "pump_phase_fa": scored.get("pump_phase_fa") or "فاز پامپ/دامپ با شواهد فعلی نامشخص است.",
        "risk_invalidation_price_usd": scored.get("risk_invalidation_price_usd"),
        "red_flag_status": (scored.get("security_evidence") or {}).get("status", "UNKNOWN"),
        "red_flag_fa": "خط قرمز امنیتی: نامشخص؛ تا بررسی مستقل، توکن امن فرض نمی‌شود.",
        "whale_flow": {
            "status": whale.get("status", "NOT_AVAILABLE"),
            "message_fa": whale.get("reason") or "دادهٔ مستقیم جریان نهنگ‌ها در منبع سریع موجود نیست.",
        },
        "dex": {"name": pair.get("dexId"), "pair_address": pair.get("pairAddress"), "url": pair.get("url")},
        "trade_authorization": "NO_TRADE",
        "orders_submitted": False,
        "transactions_signed": False,
        "read_only": True,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "gem_hunter_v3": {key: scored.get(key) for key in ("gem_hunter_v3_status", "momentum_score", "momentum_coverage_pct", "risk_score", "opportunity_score")},
    }


def _limited_report(asset: Any, message: str, error_type: str, started: float) -> dict[str, Any]:
    return {
        "status": "LIMITED",
        "message_fa": message,
        "error_type": error_type,
        "asset": getattr(asset, "raw", ""),
        "opportunity_score": None,
        "risk_score": None,
        "pump_phase_fa": "قابل تعیین نیست؛ دادهٔ معتبر کافی نیست.",
        "red_flag_status": "UNKNOWN",
        "red_flag_fa": "خط قرمز امنیتی نامشخص است؛ خرید توصیه نمی‌شود.",
        "whale_flow": {"status": "NOT_AVAILABLE", "message_fa": "جریان نهنگ‌ها قابل دریافت نبود."},
        "trade_authorization": "NO_TRADE",
        "orders_submitted": False,
        "transactions_signed": False,
        "read_only": True,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def run_with_hard_timeout(
    function: Callable[[], Any], timeout_seconds: float = 15.0
) -> tuple[bool, Any]:
    """Run blocking work on a daemon thread and return without waiting past deadline."""
    finished = threading.Event()
    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = function()
        except Exception as exc:
            result["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=target, daemon=True, name="fast-asset-scan-timebox")
    thread.start()
    if not finished.wait(max(0.01, float(timeout_seconds))):
        return False, None
    if "error" in result:
        raise result["error"]
    return True, result.get("value")
