"""Conservative Solana/Base DEX candidate discovery for Gem Hunter.

This module discovers pools; it does not certify token safety or authorize trades.
GeckoTerminal new/trending lists are attention/discovery inputs, not endorsements.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

BASE_URL = "https://api.geckoterminal.com/api/v2"
NETWORKS = frozenset({"solana", "base"})
INCLUDE = "base_token,quote_token,dex"
MAX_CANDIDATES = 50
MAX_POOL_AGE_HOURS = 168.0

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
           sellers: Optional[float]) -> Optional[float]:
    """Bounded discovery-priority heuristic; not a probability or trade signal."""
    if age_hours is None or liquidity is None or liquidity <= 0 or volume_24h is None:
        return None
    age_points = 25 if age_hours <= 1 else 20 if age_hours <= 6 else 15 if age_hours <= 24 else 8 if age_hours <= 72 else 3
    liq_points = 2 if liquidity < 5_000 else 5 if liquidity < 25_000 else 10 if liquidity < 100_000 else 15 if liquidity < 250_000 else 20 if liquidity < 1_000_000 else 25
    vol_points = 0 if volume_24h < 1_000 else 4 if volume_24h < 10_000 else 8 if volume_24h < 50_000 else 12 if volume_24h < 250_000 else 16 if volume_24h < 1_000_000 else 20
    tx_total = (buys or 0) + (sells or 0)
    activity_points = 0 if tx_total < 5 else 4 if tx_total < 20 else 8 if tx_total < 75 else 12 if tx_total < 250 else 15
    unique_total = (buyers or 0) + (sellers or 0)
    buy_share = (buys / tx_total) if tx_total > 0 else None
    unique_share = (buyers / unique_total) if unique_total > 0 else None
    pressure = buy_share if buy_share is not None else unique_share
    pressure_points = 7 if pressure is None else 0 if pressure < 0.35 else 4 if pressure < 0.45 else 8 if pressure < 0.55 else 12 if pressure < 0.65 else 15
    return round(min(100.0, age_points + liq_points + vol_points + activity_points + pressure_points), 2)


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
        score = _score(age, liquidity, volume_24h, buys, sells, buyers, sellers)
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
            "score_semantics": "Bounded heuristic for research ordering only; not a probability, safety score, or trade signal.",
            "candidate_status": "UNSCREENED" if score is not None and price is not None and price > 0 and liquidity is not None and liquidity > 0 else "UNKNOWN",
            "trade_authorization": "NO_TRADE",
            "risk_flags": flags,
            "evidence_caveat": "DEX pool aggregates and transaction counts can be manipulated; no contract-security, holder-distribution, beneficial-ownership, or execution checks have run.",
        })
    return candidates


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
    for source, endpoint in (
        ("GECKOTERMINAL_NEW_POOLS", "new_pools"),
        ("GECKOTERMINAL_TRENDING_POOLS", "trending_pools"),
    ):
        url = f"{BASE_URL}/networks/{endpoint}"
        try:
            payload = http.get(url, params={"page": 1, "include": INCLUDE})
            rows = _rows(payload, source, now)
            sources[source] = {"status": "PASS", "candidate_rows": len(rows)}
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
    statuses = [item["status"] for item in sources.values()]
    overall = "PASS" if len(statuses) == 2 and all(s == "PASS" for s in statuses) else "PARTIAL" if any(s == "PASS" for s in statuses) else "UNKNOWN"
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
        "candidate_count": len(candidates),
        "candidates": candidates,
        "trade_authorization": "NO_TRADE",
        "score_semantics": "Discovery ordering only; candidates require independent market validation and security gates before any paper evaluation.",
    }
