"""Primary DEX Screener exact-pool market-data validation for Gem Hunter.

DEX Screener is the hot-path source. GeckoTerminal is not called here; a 429
from that optional provider cannot block the read-only evaluation pipeline.
Single-source market validation is explicitly labelled as such.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from typing import Any

DEX_BASE = "https://api.dexscreener.com"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
MAX_SOURCE_RATIO = 1.05  # Backward-compatible strict default for price comparisons.
MAX_PRICE_DISAGREEMENT_RATIO = 1.05
MAX_LIQUIDITY_DISAGREEMENT_RATIO = 1.25
MAX_VOLUME_DISAGREEMENT_RATIO = 1.50
CACHE_TTL_SECONDS = 120.0
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


def _same_address(left: Any, right: Any, chain: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    # EVM addresses are case-insensitive; Solana base58 addresses are case-sensitive.
    return left == right if chain == "solana" else left.lower() == right.lower()


def _dex_snapshot_from_payload(payload: Any, chain: str, token: str, pool: str) -> dict[str, Any]:
    chain_id = {"base": "base", "solana": "solana", "ethereum": "ethereum"}.get(chain)
    if not chain_id:
        raise ValueError("Unsupported chain for exact-pool reconciliation")
    if isinstance(payload, dict):
        payload = payload.get("pairs") or []
    if not isinstance(payload, list):
        raise ValueError("DexScreener pair response schema is invalid")
    matches = []
    for pair in payload:
        if not isinstance(pair, dict) or not _same_address(pair.get("pairAddress"), pool, chain):
            continue
        if str(pair.get("chainId") or "").lower() != chain_id:
            continue
        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}
        if not isinstance(base, dict) or not isinstance(quote, dict):
            continue
        if not (_same_address(base.get("address"), token, chain) or _same_address(quote.get("address"), token, chain)):
            continue
        liquidity = _number((pair.get("liquidity") or {}).get("usd"))
        volume = _number((pair.get("volume") or {}).get("h24"))
        base_is_token = _same_address(base.get("address"), token, chain)
        quote_is_token = _same_address(quote.get("address"), token, chain)
        price = None
        price_source = None
        if base_is_token:
            candidate_price = _number(pair.get("priceUsd"))
            if candidate_price is not None and candidate_price > 0:
                price = candidate_price
                price_source = "BASE_PRICE_USD"
        elif quote_is_token:
            base_price_usd = _number(pair.get("priceUsd"))
            base_per_quote = _number(pair.get("priceNative"))
            if (base_price_usd is not None and base_price_usd > 0
                    and base_per_quote is not None and base_per_quote > 0):
                derived_price = base_price_usd / base_per_quote
                if math.isfinite(derived_price) and derived_price > 0:
                    price = derived_price
                    price_source = "DERIVED_QUOTE_PRICE_USD"
        matches.append({
            "provider": "DEXSCREENER", "pool_address": pair.get("pairAddress"),
            "token_identity_verified": True, "liquidity_usd": liquidity,
            "volume_24h_usd": volume, "price_usd": price, "price_source": price_source,
            "dex_id": pair.get("dexId"), "url": pair.get("url"),
        })
    if not matches:
        raise LookupError("DexScreener did not return the exact pool with matching token identity")
    return matches[0]


def _dex_snapshot(http, chain: str, token: str, pool: str) -> dict[str, Any]:
    chain_id = {"base": "base", "solana": "solana", "ethereum": "ethereum"}.get(chain)
    if not chain_id:
        raise ValueError("Unsupported chain for exact-pool reconciliation")
    payload = http.get(f"{DEX_BASE}/token-pairs/v1/{chain_id}/{token}")
    return _dex_snapshot_from_payload(payload, chain, token, pool)


def _gecko_snapshot(http, chain: str, token: str, pool: str) -> dict[str, Any]:
    if chain not in {"base", "solana", "ethereum"}:
        raise ValueError("Unsupported chain for GeckoTerminal reconciliation")
    payload = http.get(
        f"{GECKO_BASE}/networks/{'eth' if chain == 'ethereum' else chain}/pools/{pool}",
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
    if not (_same_address(base_address, token, chain) or _same_address(quote_address, token, chain)):
        raise LookupError("GeckoTerminal pool does not prove the requested token identity")
    returned_pool = attrs.get("address")
    if not _same_address(returned_pool, pool, chain):
        raise LookupError("GeckoTerminal did not prove the exact requested pool address")
    price_key = "base_token_price_usd" if _same_address(base_address, token, chain) else "quote_token_price_usd"
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


def _ratio_conflict(values: list[float], max_ratio: float = MAX_SOURCE_RATIO) -> bool:
    positive = [value for value in values if value > 0]
    if len(positive) != len(values):
        return len(set(values)) > 1
    return max(positive) / min(positive) > max_ratio


def reconcile_candidate_market_data(candidate: dict, http, *, dex_snapshot: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Validate exact-pool DEX Screener primary evidence without mutating discovery values."""
    chain = str(candidate.get("chain") or "").lower()
    token = candidate.get("token_address")
    pool = candidate.get("pool_address")
    reported_price = _number(candidate.get("price_usd"))
    reported_liquidity = _number(candidate.get("liquidity_usd"))
    reported_volume = _number(candidate.get("volume_24h_usd"))
    base = {
        "status": "UNKNOWN",
        "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pool_address": pool,
        "token_address": token,
        "chain": chain,
        "reported_price_usd": reported_price,
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

    cache_token = token if chain == "solana" else token.lower()
    cache_pool = pool if chain == "solana" else pool.lower()
    key = f"{chain}:{cache_token}:{cache_pool}"
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])

    # DEX Screener is the primary and sufficient market-data provider. GeckoTerminal
    # is intentionally not called in the hot path; its 429s cannot block evaluation.
    try:
        snapshot = dict(dex_snapshot) if isinstance(dex_snapshot, dict) else _dex_snapshot(http, chain, token, pool)
    except Exception as exc:
        base["source_errors"] = [f"{type(exc).__name__}: {str(exc)[:160]}"]
        base["reason_codes"].append("DEXSCREENER_PRIMARY_UNAVAILABLE")
        base["reason_fa"].append("منبع اصلی DEX Screener برای همین توکن/استخر پاسخ معتبر نداد.")
        result = base
    else:
        base["sources"] = [snapshot]
        base["source_count"] = 1
        base["verification_level"] = "DEXSCREENER_PRIMARY_SINGLE_SOURCE"
        base["independent_confirmation"] = "NOT_PERFORMED"
        price = snapshot.get("price_usd")
        liquidity = snapshot.get("liquidity_usd")
        volume = snapshot.get("volume_24h_usd")
        base["verified_price_usd"] = price
        base["verified_liquidity_usd"] = liquidity
        base["verified_volume_24h_usd"] = volume
        conflicts = []
        if price is None or price <= 0:
            conflicts.append("SOURCE_PRICE_MISSING")
        elif reported_price is not None and reported_price > 0 and _ratio_conflict([reported_price, price], MAX_PRICE_DISAGREEMENT_RATIO):
            conflicts.append("MARKET_PRICE_MISMATCH")
        if liquidity is None or liquidity < 0:
            conflicts.append("SOURCE_LIQUIDITY_MISSING")
        elif reported_liquidity is not None and reported_liquidity >= 0 and _ratio_conflict([reported_liquidity, liquidity], MAX_LIQUIDITY_DISAGREEMENT_RATIO):
            conflicts.append("MARKET_LIQUIDITY_MISMATCH")
        if volume is None or volume < 0:
            conflicts.append("SOURCE_VOLUME_MISSING")
        elif reported_volume is not None and reported_volume >= 0 and _ratio_conflict([reported_volume, volume], MAX_VOLUME_DISAGREEMENT_RATIO):
            conflicts.append("MARKET_VOLUME_MISMATCH")
        if conflicts:
            base["status"] = "FAIL"
            base["reason_codes"].extend(conflicts)
            base["reason_fa"].append("دادهٔ DEX Screener با دادهٔ کشف یا الزامات پایه سازگار نیست.")
        elif liquidity < 50_000:
            base["status"] = "FAIL"
            base["reason_codes"].append("PRIMARY_LIQUIDITY_BELOW_FLOOR")
            base["reason_fa"].append("نقدینگی گزارش‌شده توسط منبع اصلی کمتر از کف ۵۰ هزار دلار است.")
        elif volume <= 0:
            base["status"] = "FAIL"
            base["reason_codes"].append("PRIMARY_VOLUME_NOT_POSITIVE")
            base["reason_fa"].append("حجم ۲۴ساعته معتبر و مثبت نیست.")
        else:
            base["status"] = "PASS"
            base["reason_codes"].append("DEXSCREENER_PRIMARY_EXACT_POOL_MATCH")
            base["reason_fa"].append("هویت توکن و استخر و معیارهای بازار از منبع اصلی DEX Screener تطبیق داده شد؛ تأیید مستقل منبع دوم انجام نشده است.")
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
    selected_rows = [valid[index] for index in sorted(selected)]
    batch_snapshots: dict[str, dict[str, Any]] = {}
    # Fetch exact token/pool market rows once per chain (up to 30 token addresses
    # per request) rather than issuing ten serial token-pairs requests.
    by_chain: dict[str, list[dict[str, Any]]] = {}
    for row in selected_rows:
        by_chain.setdefault(str(row.get("chain") or "").lower(), []).append(row)
    for chain, chain_rows in by_chain.items():
        addresses = []
        for row in chain_rows:
            token = row.get("token_address")
            if isinstance(token, str) and token and token not in addresses:
                addresses.append(token)
        if not addresses:
            continue
        try:
            payload = http.get(
                f"{DEX_BASE}/tokens/v1/{chain}/" + ",".join(addresses[:30])
            )
            for row in chain_rows:
                token, pool = row.get("token_address"), row.get("pool_address")
                if not isinstance(token, str) or not isinstance(pool, str):
                    continue
                try:
                    snapshot = _dex_snapshot_from_payload(payload, chain, token, pool)
                    cache_token = token if chain == "solana" else token.lower()
                    cache_pool = pool if chain == "solana" else pool.lower()
                    batch_snapshots[f"{chain}:{cache_token}:{cache_pool}"] = snapshot
                except Exception:
                    continue
        except Exception:
            # Individual exact-token fallback below is DEX Screener only.
            pass
    for index, row in enumerate(valid):
        if index in selected:
            token, pool = row.get("token_address"), row.get("pool_address")
            cache_token = token if str(row.get("chain") or "").lower() == "solana" else str(token or "").lower()
            cache_pool = pool if str(row.get("chain") or "").lower() == "solana" else str(pool or "").lower()
            cache_key = f"{str(row.get('chain') or '').lower()}:{cache_token}:{cache_pool}"
            row["market_reconciliation"] = reconcile_candidate_market_data(
                row, http, dex_snapshot=batch_snapshots.get(cache_key)
            )
        else:
            row["market_reconciliation"] = {
                "status": "NOT_CHECKED_BUDGET",
                "reason_codes": ["SCAN_BUDGET_EXHAUSTED"],
                "reason_fa": ["این نامزد در این نوبت به‌دلیل محدودیت درخواست API در صف بررسی بعدی قرار گرفت."],
                "source_count": 0,
                "sources": [],
            }
    return valid
