"""Conservative Solana/Base DEX candidate discovery for Gem Hunter.

This module discovers pools; it does not certify token safety or authorize trades.
GeckoTerminal new/trending lists are attention/discovery inputs, not endorsements.
"""
from __future__ import annotations

import copy
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

BASE_URL = "https://api.geckoterminal.com/api/v2"
NETWORKS = frozenset({"solana", "base"})
INCLUDE = "base_token,quote_token,dex"
MAX_CANDIDATES = 50
MAX_POOL_AGE_HOURS = 168.0

# Cache all GeckoTerminal new/trending pool feeds, not only trending lists.
# Successful payloads live for five minutes; failed requests enter a two-minute
# cooldown. This reduces repeated 429s without treating failed/cooldown sources
# as complete evidence or allowing fallback data to upgrade GeckoTerminal status.
_GECKOTERMINAL_SOURCE_CACHE: dict[str, dict[str, Any]] = {}
# Backward-compatible alias used by existing tests and diagnostics.
_TRENDING_SOURCE_CACHE = _GECKOTERMINAL_SOURCE_CACHE
_TRENDING_CACHE_TTL_SECONDS = 300.0
_TRENDING_ERROR_COOLDOWN_SECONDS = 120.0


def _get_cached_geckoterminal_payload(http, url: str, params: dict[str, Any], source: str,
                                      now: datetime) -> tuple[Any, dict[str, Any]]:
    current = time.monotonic()
    cached = _GECKOTERMINAL_SOURCE_CACHE.get(source)
    if cached and cached.get("expires_at", 0.0) > current:
        age = max(0.0, (now - cached["fetched_at"]).total_seconds())
        if cached.get("status") == "PASS":
            return copy.deepcopy(cached["payload"]), {
                "cached": True, "data_age_seconds": round(age, 1)
            }
        raise RuntimeError(
            f"Cached rate-limit cooldown: {cached.get('error', 'source unavailable')}"
        )
    try:
        payload = http.get(url, params=params)
    except Exception as exc:
        _GECKOTERMINAL_SOURCE_CACHE[source] = {
            "status": "UNKNOWN",
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            "fetched_at": now,
            "expires_at": current + _TRENDING_ERROR_COOLDOWN_SECONDS,
        }
        raise
    _GECKOTERMINAL_SOURCE_CACHE[source] = {
        "status": "PASS",
        "payload": copy.deepcopy(payload),
        "fetched_at": now,
        "expires_at": current + _TRENDING_CACHE_TTL_SECONDS,
    }
    return payload, {"cached": False, "data_age_seconds": 0.0}


# Keep the old private helper name available for callers that used it.
_get_trending_payload = _get_cached_geckoterminal_payload


# Only canonical native/wrapped assets are excluded. Symbols alone are not trusted.
BASE_QUOTE_ADDRESSES = {
    "solana": {
        "So11111111111111111111111111111111111111112",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    },
    "base": {
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "0x0000000000000000000000000000000000000000",
    },
}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_address(chain: str, address: Any) -> bool:
    if not isinstance(address, str):
        return False
    if chain == "base":
        return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", address))
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address))


def _score(age_hours: Optional[float], liquidity: Optional[float],
           volume_24h: Optional[float], buys: Optional[float],
           sells: Optional[float], buyers: Optional[float],
           sellers: Optional[float], price_change_1h: Optional[float],
           volume_to_liquidity: Optional[float]) -> Optional[float]:
    """Bounded research-ordering heuristic with explicit risk penalties; not a signal."""
    if age_hours is None or liquidity is None or liquidity <= 0 or volume_24h is None:
        return None
    # Research ordering must not reward extreme novelty: very young pools
    # have almost no operating history and therefore receive the fewest points.
    age_points = 3 if age_hours <= 1 else 8 if age_hours <= 6 else 12 if age_hours <= 24 else 18 if age_hours <= 72 else 25
    liq_points = 2 if liquidity < 5_000 else 5 if liquidity < 25_000 else 10 if liquidity < 100_000 else 15 if liquidity < 250_000 else 20 if liquidity < 1_000_000 else 25
    vol_points = 0 if volume_24h < 1_000 else 4 if volume_24h < 10_000 else 8 if volume_24h < 50_000 else 12 if volume_24h < 250_000 else 16 if volume_24h < 1_000_000 else 20
    tx_total = (buys or 0) + (sells or 0)
    activity_points = 0 if tx_total < 5 else 4 if tx_total < 20 else 8 if tx_total < 75 else 12 if tx_total < 250 else 15
    # Transaction counts and unique-wallet counts do not reveal notional flow.
    # Keep this component neutral until dollar-denominated buy/sell volume exists.
    pressure_points = 7
    penalty = 0
    if liquidity < 25_000:
        penalty += 10
    if volume_to_liquidity is not None and volume_to_liquidity > 10:
        penalty += 20
    if price_change_1h is not None:
        if price_change_1h <= -20:
            penalty += 20
        elif price_change_1h <= -10:
            penalty += 10
        elif price_change_1h >= 100:
            penalty += 15
        elif price_change_1h >= 50:
            penalty += 10
    base_score = min(100.0, age_points + liq_points + vol_points + activity_points + pressure_points)
    return round(max(0.0, base_score - penalty), 2)


def _rows(payload: Any, source: str, now: datetime) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("GeckoTerminal pool response has an invalid schema")
    included = {
        item.get("id"): item.get("attributes") or {}
        for item in payload.get("included", [])
        if isinstance(item, dict) and item.get("id")
    }
    candidates = []
    for pool in payload["data"]:
        if not isinstance(pool, dict):
            continue
        pool_id = str(pool.get("id") or "")
        chain = pool_id.split("_", 1)[0].lower()
        if chain not in NETWORKS:
            continue
        attrs = pool.get("attributes") or {}
        relationships = pool.get("relationships") or {}
        base_ref = ((relationships.get("base_token") or {}).get("data") or {})
        quote_ref = ((relationships.get("quote_token") or {}).get("data") or {})
        base_id = base_ref.get("id")
        quote_id = quote_ref.get("id")
        token = included.get(base_id, {})
        quote = included.get(quote_id, {})
        token_address = token.get("address") or (str(base_id).split("_", 1)[1] if isinstance(base_id, str) and "_" in base_id else None)
        if not _valid_address(chain, token_address):
            continue
        if token_address in BASE_QUOTE_ADDRESSES[chain]:
            continue
        pool_address = attrs.get("address")
        if not isinstance(pool_address, str) or not pool_address.strip():
            continue
        created = _timestamp(attrs.get("pool_created_at"))
        age = (now - created).total_seconds() / 3600.0 if created else None
        if age is not None and age < -5:
            age = None
        if age is not None and age > MAX_POOL_AGE_HOURS:
            continue
        price = _number(attrs.get("base_token_price_usd"))
        liquidity = _number(attrs.get("reserve_in_usd"))
        volumes = attrs.get("volume_usd") or {}
        volume_24h = _number(volumes.get("h24")) if isinstance(volumes, dict) else None
        transactions = attrs.get("transactions") or {}
        h1 = transactions.get("h1") or {} if isinstance(transactions, dict) else {}
        h24 = transactions.get("h24") or {} if isinstance(transactions, dict) else {}
        buys = _number(h1.get("buys")) if isinstance(h1, dict) else None
        sells = _number(h1.get("sells")) if isinstance(h1, dict) else None
        buyers = _number(h1.get("buyers")) if isinstance(h1, dict) else None
        sellers = _number(h1.get("sellers")) if isinstance(h1, dict) else None
        buys_24h = _number(h24.get("buys")) if isinstance(h24, dict) else None
        sells_24h = _number(h24.get("sells")) if isinstance(h24, dict) else None
        price_change = attrs.get("price_change_percentage") or {}
        change_1h = _number(price_change.get("h1")) if isinstance(price_change, dict) else None
        dex_ref = ((relationships.get("dex") or {}).get("data") or {})
        dex_attrs = included.get(dex_ref.get("id"), {})
        flags = []
        if price is None or price <= 0:
            flags.append("PRICE_UNAVAILABLE")
        if liquidity is None or liquidity <= 0:
            flags.append("LIQUIDITY_UNAVAILABLE")
        elif liquidity < 25_000:
            flags.append("LOW_LIQUIDITY")
        if volume_24h is None:
            flags.append("VOLUME_UNAVAILABLE")
        elif volume_24h < 10_000:
            flags.append("LOW_24H_VOLUME")
        if age is None:
            flags.append("POOL_AGE_UNKNOWN")
        tx_total = (buys or 0) + (sells or 0)
        if tx_total < 20:
            flags.append("LOW_1H_ACTIVITY")
        if buys is not None and sells is not None and sells > buys:
            flags.append("SELLS_EXCEED_BUYS")
        ratio = volume_24h / liquidity if volume_24h is not None and liquidity and liquidity > 0 else None
        if ratio is not None and ratio > 10:
            flags.append("EXTREME_VOLUME_LIQUIDITY_RATIO")
        if change_1h is not None and change_1h <= -20:
            flags.append("SHARP_1H_DRAWDOWN")
        elif change_1h is not None and change_1h >= 50:
            flags.append("SHARP_1H_PUMP")
        score = _score(
            age, liquidity, volume_24h, buys, sells, buyers, sellers,
            change_1h, ratio,
        )
        candidates.append({
            "asset": f"{chain}:{token_address}",
            "chain": chain,
            "token_address": token_address,
            "token_name": token.get("name"),
            "token_symbol": token.get("symbol"),
            "token_decimals": token.get("decimals"),
            "pool_address": pool_address,
            "pool_name": attrs.get("name"),
            "dex": dex_attrs.get("name") or dex_ref.get("id"),
            "price_usd": price if price is not None and price > 0 else None,
            "liquidity_usd": liquidity if liquidity is not None and liquidity >= 0 else None,
            "volume_1h_usd": _number(volumes.get("h1")) if isinstance(volumes, dict) else None,
            "volume_24h_usd": volume_24h,
            "volume_to_liquidity_24h": round(ratio, 4) if ratio is not None else None,
            "buys_1h": buys,
            "sells_1h": sells,
            "unique_buyers_1h": buyers,
            "unique_sellers_1h": sellers,
            "buys_24h": buys_24h,
            "sells_24h": sells_24h,
            "price_change_1h_pct": change_1h,
            "pool_created_at_utc": created.isoformat().replace("+00:00", "Z") if created else None,
            "pool_age_hours": round(age, 2) if age is not None else None,
            "source": source,
            "sources": [source],
            "discovery_priority_score": score,
            "score_semantics": "Bounded research-ordering heuristic; pool maturity earns more than novelty, transaction-count ratios do not substitute for notional buy flow, and penalties cover low liquidity, extreme turnover/liquidity and sharp 1h moves; not a probability, safety score, or trade signal.",
            "candidate_status": "UNSCREENED" if score is not None and price is not None and price > 0 and liquidity is not None and liquidity > 0 else "UNKNOWN",
            "trade_authorization": "NO_TRADE",
            "risk_flags": flags,
            "evidence_caveat": "DEX pool aggregates and transaction counts can be manipulated; no contract-security, holder-distribution, beneficial-ownership, or execution checks have run.",
        })
    return candidates



def _dexpaprika_recovery_rows(http, network: str, now: datetime, *, limit: int = 2) -> tuple[list[dict[str, Any]], int]:
    """Read a small recovery sample; this never substitutes for complete new-pool coverage."""
    if network not in NETWORKS:
        raise ValueError(f"Unsupported recovery network: {network}")
    base = "https://api.dexpaprika.com"
    payload = http.get(
        f"{base}/networks/{network}/pools/search",
        params={"order_by": "created_at", "sort": "desc", "limit": max(1, min(limit, 2))},
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError(f"DexPaprika {network} pool-search response has an invalid schema")

    candidates: list[dict[str, Any]] = []
    detail_errors = 0
    source = f"DEXPAPRIKA_{network.upper()}_RECOVERY"
    for pool in payload["results"][:2]:
        if not isinstance(pool, dict) or str(pool.get("chain", "")).lower() != network:
            continue
        pool_id = pool.get("id")
        # DexPaprika pool identifiers may be an EVM pair address (20 bytes)
        # or a bytes32 identifier (e.g. some V4 pools). Keep the latter as an
        # identifier; it is not automatically a DexScreener/Gecko pool address.
        valid_pool_id = (
            bool(re.fullmatch(r"0x(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", pool_id))
            if network == "base" and isinstance(pool_id, str)
            else _valid_address(network, pool_id)
        )
        if not valid_pool_id:
            continue
        try:
            detail = http.get(f"{base}/networks/{network}/pools/{pool_id}")
        except Exception:
            detail_errors += 1
            continue
        if not isinstance(detail, dict) or str(detail.get("chain", "")).lower() != network:
            detail_errors += 1
            continue
        detail_pool_id = detail.get("id")
        if detail_pool_id is not None and str(detail_pool_id) != str(pool_id):
            detail_errors += 1
            continue
        base_token_id = detail.get("base_token_id")
        tokens = detail.get("tokens")
        if not isinstance(tokens, list):
            detail_errors += 1
            continue
        quote_addresses = {item.lower() for item in BASE_QUOTE_ADDRESSES[network]}
        token = next((item for item in tokens
                      if isinstance(item, dict)
                      and str(item.get("id", "")).lower() not in quote_addresses), None)
        if not isinstance(token, dict):
            continue
        token_address = token.get("id")
        if not _valid_address(network, token_address):
            continue

        created = _timestamp(detail.get("created_at") or pool.get("created_at"))
        age = (now - created).total_seconds() / 3600.0 if created else None
        if age is not None and age < -5:
            age = None
        if age is not None and age > MAX_POOL_AGE_HOURS:
            continue

        price = _number(detail.get("last_price_usd")) if str(token_address).lower() == str(base_token_id or "").lower() else None
        if price is None:
            reserves = detail.get("token_reserves")
            if isinstance(reserves, list):
                reserve_row = next((item for item in reserves
                                    if isinstance(item, dict)
                                    and str(item.get("token_id", "")).lower() == str(token_address).lower()), None)
                if isinstance(reserve_row, dict):
                    raw_reserve = _number(reserve_row.get("reserve"))
                    reserve_usd = _number(reserve_row.get("reserve_usd"))
                    try:
                        decimals = int(token.get("decimals"))
                    except (TypeError, ValueError, OverflowError):
                        decimals = -1
                    if (raw_reserve is not None and raw_reserve > 0 and reserve_usd is not None
                            and reserve_usd > 0 and 0 <= decimals <= 36):
                        token_units = raw_reserve / (10 ** decimals)
                        if token_units > 0:
                            price = reserve_usd / token_units
        if price is None or price <= 0:
            price = _number(pool.get("price_usd")) if str(token_address).lower() == str(base_token_id or "").lower() else None
        liquidity = _number(detail.get("liquidity_usd"))
        h1 = detail.get("1h") or {}
        h24 = detail.get("24h") or {}
        if not isinstance(h1, dict) or not isinstance(h24, dict):
            detail_errors += 1
            continue
        volume_24h = _number(h24.get("volume_usd"))
        buys = _number(h1.get("buys"))
        sells = _number(h1.get("sells"))
        buys_24h = _number(h24.get("buys"))
        sells_24h = _number(h24.get("sells"))
        change_1h = _number(h1.get("last_price_usd_change")) if str(token_address).lower() == str(base_token_id or "").lower() else None
        if liquidity is None:
            liquidity = _number(pool.get("liquidity_usd"))
        if volume_24h is None:
            volume_24h = _number(pool.get("volume_usd_24h"))

        flags = []
        if price is None or price <= 0:
            flags.append("PRICE_UNAVAILABLE")
        if liquidity is None or liquidity <= 0:
            flags.append("LIQUIDITY_UNAVAILABLE")
        elif liquidity < 25_000:
            flags.append("LOW_LIQUIDITY")
        if volume_24h is None:
            flags.append("VOLUME_UNAVAILABLE")
        elif volume_24h < 10_000:
            flags.append("LOW_24H_VOLUME")
        if age is None:
            flags.append("POOL_AGE_UNKNOWN")
        if (buys or 0) + (sells or 0) < 20:
            flags.append("LOW_1H_ACTIVITY")
        if buys is not None and sells is not None and sells > buys:
            flags.append("SELLS_EXCEED_BUYS")
        ratio = volume_24h / liquidity if volume_24h is not None and liquidity and liquidity > 0 else None
        if ratio is not None and ratio > 10:
            flags.append("EXTREME_VOLUME_LIQUIDITY_RATIO")
        if change_1h is not None and change_1h <= -20:
            flags.append("SHARP_1H_DRAWDOWN")
        elif change_1h is not None and change_1h >= 50:
            flags.append("SHARP_1H_PUMP")
        score = _score(age, liquidity, volume_24h, buys, sells, None, None, change_1h, ratio)
        pool_identifier = str(detail.get("id") or pool_id)
        reported_pool_address = detail.get("pool_address")
        pool_address = (
            reported_pool_address
            if _valid_address(network, reported_pool_address)
            else pool_identifier if _valid_address(network, pool_identifier) else None
        )
        candidates.append({
            "asset": f"{network}:{token_address}",
            "chain": network,
            "token_address": token_address,
            "token_name": token.get("name"),
            "token_symbol": token.get("symbol"),
            "token_decimals": token.get("decimals"),
            "pool_address": pool_address,
            "pool_id": pool_identifier,
            "pool_identity_status": "ADDRESS_VERIFIED" if pool_address else "IDENTIFIER_ONLY",
            "pool_name": f"{token.get('symbol') or token_address} / {network} pair",
            "dex": detail.get("dex_name") or pool.get("dex_name"),
            "price_usd": price if price is not None and price > 0 else None,
            "liquidity_usd": liquidity if liquidity is not None and liquidity >= 0 else None,
            "volume_1h_usd": _number(h1.get("volume_usd")),
            "volume_24h_usd": volume_24h,
            "volume_to_liquidity_24h": round(ratio, 4) if ratio is not None else None,
            "buys_1h": buys,
            "sells_1h": sells,
            "unique_buyers_1h": None,
            "unique_sellers_1h": None,
            "buys_24h": buys_24h,
            "sells_24h": sells_24h,
            "price_change_1h_pct": change_1h,
            "pool_created_at_utc": created.isoformat().replace("+00:00", "Z") if created else None,
            "pool_age_hours": round(age, 2) if age is not None else None,
            "source": source,
            "sources": [source],
            "discovery_priority_score": score,
            "score_semantics": "Limited secondary-source discovery ordering only; not a probability, safety score, or trade signal.",
            "candidate_status": "UNSCREENED" if score is not None and price is not None and price > 0 and liquidity is not None and liquidity > 0 else "UNKNOWN",
            "trade_authorization": "NO_TRADE",
            "risk_flags": flags,
            "evidence_caveat": "Limited DexPaprika recovery sample; keyless coverage may omit pools younger than one hour and the sample is not complete new-pool coverage. Independent contract, holder and execution checks have not run.",
        })
    return candidates, detail_errors

def discover_gem_candidates(http, *, limit: int = 20,
                            now: Optional[datetime] = None) -> dict[str, Any]:
    """Fetch bounded new/trending pools and return normalized Solana/Base candidates."""
    limit = max(1, min(int(limit), MAX_CANDIDATES))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    sources: dict[str, dict[str, Any]] = {}
    merged: dict[str, dict[str, Any]] = {}
    if http is None or not callable(getattr(http, "get", None)):
        return {
            "status": "UNKNOWN", "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
            "candidate_count": 0, "candidates": [], "sources": {},
            "trade_authorization": "NO_TRADE",
            "reason": "Market data HTTP client is unavailable.",
        }
    for network in sorted(NETWORKS):
        for source_suffix, endpoint in (
            ("NEW_POOLS", "new_pools"),
            ("TRENDING_POOLS", "trending_pools"),
        ):
            source = f"GECKOTERMINAL_{source_suffix}_{network.upper()}"
            url = f"{BASE_URL}/networks/{network}/{endpoint}"
            try:
                params = {"page": 1, "include": INCLUDE}
                payload, cache_metadata = _get_cached_geckoterminal_payload(
                    http, url, params, source, now
                )
                rows = _rows(payload, source, now)
                sources[source] = {"status": "PASS", "candidate_rows": len(rows), **cache_metadata}
                for row in rows:
                    key = row["asset"].lower()
                    existing = merged.get(key)
                    if existing is None:
                        merged[key] = row
                        continue
                    combined_sources = sorted(set(existing["sources"] + row["sources"]))
                    old_liq = existing.get("liquidity_usd") or 0.0
                    new_liq = row.get("liquidity_usd") or 0.0
                    if new_liq > old_liq:
                        row["sources"] = combined_sources
                        merged[key] = row
                    else:
                        existing["sources"] = combined_sources
            except Exception as exc:
                sources[source] = {
                    "status": "UNKNOWN",
                    "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                }
    # Recover limited coverage on whichever chain has a missing GeckoTerminal feed.
    # A secondary feed never upgrades the primary GeckoTerminal completeness state.
    for network in sorted(NETWORKS):
        gecko_sources = (
            f"GECKOTERMINAL_NEW_POOLS_{network.upper()}",
            f"GECKOTERMINAL_TRENDING_POOLS_{network.upper()}",
        )
        if all(sources.get(name, {}).get("status") == "PASS" for name in gecko_sources):
            continue
        fallback_source = f"DEXPAPRIKA_{network.upper()}_RECOVERY"
        try:
            fallback_rows, detail_errors = _dexpaprika_recovery_rows(http, network, now, limit=2)
            sources[fallback_source] = {
                "status": "UNKNOWN" if detail_errors else "PASS",
                "candidate_rows": len(fallback_rows),
                "detail_errors": detail_errors,
                "detail_limit": 2,
                "coverage": "LIMITED_TOP_2_RECOVERY_NOT_COMPLETE_NEW_POOL_COVERAGE",
                "freshness_caveat": "Keyless coverage may omit pools younger than one hour.",
            }
            for row in fallback_rows:
                key = row["asset"].lower()
                existing = merged.get(key)
                if existing is None:
                    merged[key] = row
                else:
                    existing["sources"] = sorted(set(existing.get("sources", []) + row["sources"]))
                    if (row.get("liquidity_usd") or 0) > (existing.get("liquidity_usd") or 0):
                        row["sources"] = existing["sources"]
                        merged[key] = row
        except Exception as exc:
            sources[fallback_source] = {
                "status": "UNKNOWN",
                "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                "coverage": "LIMITED_RECOVERY_NOT_COMPLETE_NEW_POOL_COVERAGE",
            }

    statuses = [item["status"] for item in sources.values()]
    expected_sources = len(NETWORKS) * 2
    gecko_statuses = [status for name, status in (
        (name, item["status"]) for name, item in sources.items()
        if name.startswith("GECKOTERMINAL_")
    )]
    gecko_complete = len(gecko_statuses) == expected_sources and all(status == "PASS" for status in gecko_statuses)
    overall = "PASS" if gecko_complete else "PARTIAL" if any(s == "PASS" for s in statuses) else "UNKNOWN"
    candidates = sorted(
        merged.values(),
        key=lambda item: (
            item.get("discovery_priority_score") is not None,
            item.get("discovery_priority_score") or -1,
            item.get("liquidity_usd") or -1,
        ),
        reverse=True,
    )[:limit]
    return {
        "status": overall,
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "networks": sorted(NETWORKS),
        "source_count": len(sources),
        "sources": sources,
        "gecko_terminal_complete": gecko_complete,
        "fallback_coverage_note": "DexPaprika is a limited recovery sample only; it never replaces complete GeckoTerminal coverage or authorizes trading." if any(name.startswith("DEXPAPRIKA_") for name in sources) else None,
        "candidate_counts_by_network": {
            network: sum(1 for item in merged.values() if item.get("chain") == network)
            for network in sorted(NETWORKS)
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
        "trade_authorization": "NO_TRADE",
        "score_semantics": "Risk-adjusted discovery ordering only; candidates require independent market validation and security gates before any paper evaluation.",
    }
