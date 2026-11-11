"""Independent, exact-pool market-data reconciliation for Gem Hunter.

A discovery aggregate is never trusted as executable liquidity. This module
compares the exact token/pool identity and pool-level liquidity across
DexScreener and GeckoTerminal. It is deliberately read-only and fail-closed.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

DEX_BASE = "https://api.dexscreener.com"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
MAX_SOURCE_RATIO = 2.0
CACHE_TTL_SECONDS = 45.0
MAX_CANDIDATES_PER_SCAN = 10
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.RLock()
_SCAN_OFFSET = 0


def _number(value: Any):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _same_address(left: Any, right: Any) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


def _dex_snapshot(http, chain: str, token: str, pool: str) -> dict[str, Any]:
    chain_id = {"base": "base", "solana": "solana"}.get(chain)
    if not chain_id:
        raise ValueError("Unsupported chain for exact-pool reconciliation")
    payload = http.get(f"{DEX_BASE}/token-pairs/v1/{chain_id}/{token}")
    if isinstance(payload, dict):
        payload = payload.get("pairs") or []
    if not isinstance(payload, list):
        raise ValueError("DexScreener pair response schema is invalid")
    matches = []
    for pair in payload:
        if not isinstance(pair, dict) or not _same_address(pair.get("pairAddress"), pool):
            continue
        if str(pair.get("chainId") or "").lower() != chain_id:
            continue
        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}
        if not isinstance(base, dict) or not isinstance(quote, dict):
            continue
        if not (_same_address(base.get("address"), token) or _same_address(quote.get("address"), token)):
            continue
        liquidity = _number((pair.get("liquidity") or {}).get("usd"))
        volume = _number((pair.get("volume") or {}).get("h24"))
        price = _number(pair.get("priceUsd")) if _same_address(base.get("address"), token) else None
        matches.append({
            "provider": "DEXSCREENER",
            "pool_address": pair.get("pairAddress"),
            "token_identity_verified": True,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume,
            "price_usd": price if price is not None and price > 0 else None,
            "dex_id": pair.get("dexId"),
            "url": pair.get("url"),
        })
    if not matches:
        raise LookupError("DexScreener did not return the exact pool with matching token identity")
    return matches[0]


def _gecko_snapshot(http, chain: str, token: str, pool: str) -> dict[str, Any]:
    if chain not in {"base", "solana"}:
        raise ValueError("Unsupported chain for GeckoTerminal reconciliation")
    payload = http.get(
        f"{GECKO_BASE}/networks/{chain}/pools/{pool}",
        params={"include": "base_token,quote_token"},
    )
    if not isinstance(payload, dict):
        raise ValueError("GeckoTerminal pool response schema is invalid")
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    if not isinstance(attrs, dict):
        raise ValueError("GeckoTerminal pool attributes are invalid")
    included = {
        row.get("id"): row.get("attributes") or {}
        for row in payload.get("included", [])
        if isinstance(row, dict) and row.get("id")
    }
    relationships = data.get("relationships") or {}
    base_id = ((relationships.get("base_token") or {}).get("data") or {}).get("id")
    quote_id = ((relationships.get("quote_token") or {}).get("data") or {}).get("id")
    base = included.get(base_id, {})
    quote = included.get(quote_id, {})
    base_address = base.get("address")
    quote_address = quote.get("address")
    if not (_same_address(base_address, token) or _same_address(quote_address, token)):
        raise LookupError("GeckoTerminal pool does not prove the requested token identity")
    returned_pool = attrs.get("address")
    if not _same_address(returned_pool, pool):
        raise LookupError("GeckoTerminal did not prove the exact requested pool address")
    price_key = "base_token_price_usd" if _same_address(base_address, token) else "quote_token_price_usd"
    price = _number(attrs.get(price_key))
    return {
        "provider": "GECKOTERMINAL",
        "pool_address": returned_pool or pool,
        "token_identity_verified": True,
        "liquidity_usd": _number(attrs.get("reserve_in_usd")),
        "volume_24h_usd": _number((attrs.get("volume_usd") or {}).get("h24")),
        "price_usd": price if price is not None and price > 0 else None,
        "dex_id": attrs.get("name"),
        "url": None,
    }


def _ratio_conflict(values: list[float]) -> bool:
    positive = [value for value in values if value > 0]
    if len(positive) != len(values):
        return len(set(values)) > 1
    return max(positive) / min(positive) > MAX_SOURCE_RATIO


def reconcile_candidate_market_data(candidate: dict, http) -> dict[str, Any]:
    """Return exact-pool cross-source evidence without mutating discovery values."""
    chain = str(candidate.get("chain") or "").lower()
    token = candidate.get("token_address")
    pool = candidate.get("pool_address")
    reported_liquidity = _number(candidate.get("liquidity_usd"))
    reported_volume = _number(candidate.get("volume_24h_usd"))
    base = {
        "status": "UNKNOWN",
        "pool_address": pool,
        "token_address": token,
        "chain": chain,
        "reported_liquidity_usd": reported_liquidity,
        "reported_volume_24h_usd": reported_volume,
        "verified_liquidity_usd": None,
        "verified_volume_24h_usd": None,
        "verified_price_usd": None,
        "source_count": 0,
        "sources": [],
        "reason_codes": [],
        "reason_fa": [],
        "max_source_disagreement_ratio": None,
    }
    if not http or not isinstance(token, str) or not isinstance(pool, str) or not pool.strip():
        base["reason_codes"].append("IDENTITY_OR_HTTP_UNAVAILABLE")
        base["reason_fa"].append("آدرس Pool یا اتصال منبع داده برای تطبیق مستقل موجود نیست.")
        return base

    key = f"{chain}:{token.lower()}:{pool.lower()}"
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])

    snapshots = []
    errors = []
    for loader in (_dex_snapshot, _gecko_snapshot):
        try:
            snapshots.append(loader(http, chain, token, pool))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}")
    base["sources"] = snapshots
    base["source_count"] = len(snapshots)

    if len(snapshots) < 2:
        base["reason_codes"].append("CROSS_SOURCE_CONFIRMATION_MISSING")
        base["reason_fa"].append("تطبیق هم‌زمان همان Pool در دو منبع مستقل کامل نشد؛ دادهٔ نقدینگی تأییدنشده است.")
        if errors:
            base["source_errors"] = errors
        result = base
    else:
        liquidities = [row["liquidity_usd"] for row in snapshots]
        volumes = [row["volume_24h_usd"] for row in snapshots]
        if any(value is None or value < 0 for value in liquidities):
            base["reason_codes"].append("SOURCE_LIQUIDITY_MISSING")
            base["reason_fa"].append("حداقل یکی از منابع نقدینگی معتبر برای Pool ارائه نکرده است.")
            result = base
        elif any(value is None or value < 0 for value in volumes):
            base["reason_codes"].append("SOURCE_VOLUME_MISSING")
            base["reason_fa"].append("حجم ۲۴ساعتهٔ هر دو منبع برای مقایسه موجود نیست.")
            result = base
        else:
            liquidity_conflict = _ratio_conflict(liquidities)
            volume_conflict = _ratio_conflict(volumes)
            reported_volume_conflict = (
                reported_volume is not None
                and reported_volume >= 0
                and _ratio_conflict([reported_volume, *volumes])
            )
            reported_conflict = (
                reported_liquidity is not None
                and reported_liquidity >= 0
                and _ratio_conflict([reported_liquidity, *liquidities])
            )
            ratios = []
            for values in ([*liquidities, reported_liquidity] if reported_liquidity is not None else liquidities,):
                positives = [value for value in values if value > 0]
                if len(positives) == len(values) and positives:
                    ratios.append(max(positives) / min(positives))
                elif len(set(values)) > 1:
                    ratios.append(float("inf"))
            base["max_source_disagreement_ratio"] = max(ratios) if ratios else 1.0
            base["verified_liquidity_usd"] = min(liquidities)
            base["verified_volume_24h_usd"] = min(volumes)
            prices = [row["price_usd"] for row in snapshots if row["price_usd"] is not None]
            base["verified_price_usd"] = min(prices) if prices else None
            if liquidity_conflict or volume_conflict or reported_conflict or reported_volume_conflict:
                base["status"] = "FAIL"
                base["reason_codes"].append("MARKET_DATA_DISAGREEMENT")
                base["reason_fa"].append("نقدینگی یا حجم گزارش کشف با منابع مستقل همان استخر اختلاف بیش از حد مجاز دارد.")
            elif min(liquidities) < 50_000:
                base["status"] = "FAIL"
                base["reason_codes"].append("VERIFIED_LIQUIDITY_BELOW_FLOOR")
                base["reason_fa"].append("نقدینگی تأییدشدهٔ Pool از حداقل ۵۰ هزار دلار کمتر است.")
            else:
                base["status"] = "PASS"
                base["reason_codes"].append("EXACT_POOL_CROSS_SOURCE_MATCH")
                base["reason_fa"].append("هویت توکن و Pool و دادهٔ نقدینگی/حجم در دو منبع مستقل تطبیق داده شد.")
            result = base

    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, dict(result))
    return result


def reconcile_discovery_candidates(candidates: Any, http, *, limit: int = MAX_CANDIDATES_PER_SCAN) -> list[dict]:
    """Reconcile a bounded research shortlist; explicitly label the unprocessed tail."""
    if not isinstance(candidates, list):
        return []
    valid = [dict(row) for row in candidates if isinstance(row, dict)]
    valid.sort(
        key=lambda row: _number(row.get("discovery_priority_score")) or 0.0,
        reverse=True,
    )
    bounded = max(0, min(int(limit), MAX_CANDIDATES_PER_SCAN, len(valid)))
    global _SCAN_OFFSET
    with _CACHE_LOCK:
        start = (_SCAN_OFFSET % len(valid)) if valid and bounded else 0
        if valid and bounded:
            _SCAN_OFFSET = (start + bounded) % len(valid)
    selected = {(start + offset) % len(valid) for offset in range(bounded)} if valid else set()
    for index, row in enumerate(valid):
        if index in selected:
            row["market_reconciliation"] = reconcile_candidate_market_data(row, http)
        else:
            row["market_reconciliation"] = {
                "status": "NOT_CHECKED_BUDGET",
                "reason_codes": ["SCAN_BUDGET_EXHAUSTED"],
                "reason_fa": ["این نامزد در این نوبت به‌دلیل محدودیت درخواست API در صف بررسی بعدی قرار گرفت."],
                "source_count": 0,
                "sources": [],
            }
    return valid
