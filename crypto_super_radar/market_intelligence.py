"""Market Intelligence enrichment from DEX Screener and CoinGlass.

External data is advisory. Missing fields remain None and never become zero.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from statistics import median
from typing import Any, Optional

DEX_BASE = "https://api.dexscreener.com"
COINGLASS_BASE = "https://open-api-v4.coinglass.com"
SUPPORTED_CHAINS = {"ethereum": "eth", "base": "base", "solana": "solana"}
_CACHE_LOCK = threading.RLock()
_DEX_CACHE: dict[str, tuple[float, Any]] = {}
_CG_OI_CACHE: dict[str, tuple[float, Any]] = {}
_CG_FUNDING_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _positive(value: Any) -> Optional[float]:
    result = _number(value)
    return result if result is not None and result > 0 else None


def _cached(cache: dict, key: str, ttl: float, loader):
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = cache.get(key)
        if entry and entry[0] > now:
            return entry[1]
    value = loader()
    with _CACHE_LOCK:
        cache[key] = (time.monotonic() + ttl, value)
    return value


def _dex_pairs_for_token(asset, http) -> dict:
    if asset.market_type != "dex" or not asset.chain or not asset.address:
        return {"status": "NOT_APPLICABLE", "reason": "DEX token-pool data applies only to contract-address assets."}
    chain_id = {"eth": "ethereum", "base": "base", "solana": "solana"}.get(asset.chain)
    if not chain_id:
        return {"status": "UNSUPPORTED", "reason": "Unsupported DEX network."}

    key = f"{chain_id}:{asset.address.lower()}"
    def load():
        url = f"{DEX_BASE}/token-pairs/v1/{chain_id}/{asset.address}"
        rows = http.get(url)
        if isinstance(rows, dict):
            rows = rows.get("pairs") or []
        if not isinstance(rows, list):
            raise ValueError("DEX Screener returned an invalid token-pair schema.")
        selected = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            token = row.get("baseToken") or {}
            if not isinstance(token, dict) or str(token.get("address") or "").lower() != asset.address.lower():
                continue
            liquidity = _number((row.get("liquidity") or {}).get("usd"))
            price = _positive(row.get("priceUsd"))
            volume = _number((row.get("volume") or {}).get("h24"))
            if liquidity is None or price is None or volume is None:
                continue
            txns = (row.get("txns") or {}).get("h24") or {}
            buys = _number(txns.get("buys"))
            sells = _number(txns.get("sells"))
            total = (buys + sells) if buys is not None and sells is not None else None
            buy_share = (buys / total * 100.0) if total and total > 0 else None
            created_ms = _number(row.get("pairCreatedAt"))
            age_hours = None
            if created_ms is not None and created_ms > 0:
                age_hours = max(0.0, (time.time() * 1000.0 - created_ms) / 3_600_000.0)
            selected.append({
                "status": "KNOWN",
                "chain_id": chain_id,
                "dex_id": row.get("dexId"),
                "token_name": token.get("name"),
                "token_symbol": token.get("symbol"),
                "token_address": asset.address,
                "pair_address": row.get("pairAddress"),
                "pair_url": row.get("url"),
                "price_usd": price,
                "liquidity_usd": liquidity,
                "volume_24h_usd": volume,
                "price_change_24h_pct": _number((row.get("priceChange") or {}).get("h24")),
                "buys_24h": buys,
                "sells_24h": sells,
                "buy_transaction_share_pct": buy_share,
                "total_transactions_24h": total,
                "volume_to_liquidity_24h": volume / liquidity if liquidity > 0 else None,
                "pair_created_at_ms": created_ms,
                "pair_age_hours": age_hours,
                "scope_note": "Buy/sell share is transaction count, not buy/sell notional volume.",
            })
        if not selected:
            raise ValueError("No verifiable DEX pair with the requested token as base asset was returned.")
        return max(selected, key=lambda item: item["liquidity_usd"])

    try:
        return _cached(_DEX_CACHE, key, 20.0, load)
    except Exception as exc:
        return {"status": "UNKNOWN", "reason": f"DEX Screener unavailable: {type(exc).__name__}."}


def _coinglass_symbol(asset, dex_data: dict) -> Optional[str]:
    if asset.market_type in {"cex", "perp"}:
        symbol = (asset.symbol or "").upper()
    else:
        symbol = str(dex_data.get("token_symbol") or "").upper()
    for quote in ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "USD", "EUR", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            symbol = symbol[:-len(quote)]
            break
    return symbol if symbol and symbol.isalnum() else None


def _coinglass_data(symbol: Optional[str], http) -> dict:
    api_key = os.getenv("COINGLASS_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "NOT_CONFIGURED",
            "reason": "COINGLASS_API_KEY is not configured on the server.",
            "funding_median_pct": None,
            "funding_exchange_count": None,
            "open_interest_usd": None,
            "open_interest_change_24h_pct": None,
        }
    if not symbol:
        return {"status": "NO_SYMBOL", "reason": "No unambiguous derivative-market symbol was available."}

    headers = {"CG-API-KEY": api_key, "accept": "application/json"}

    def request(path: str, params: Optional[dict] = None):
        payload = http.request_json(
            "GET", f"{COINGLASS_BASE}{path}", params=params, headers=headers,
        )
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            code = payload.get("code") if isinstance(payload, dict) else "invalid-schema"
            raise ValueError(f"CoinGlass API business code {code}")
        data = payload.get("data")
        return data

    try:
        oi_rows = _cached(
            _CG_OI_CACHE,
            symbol,
            15.0,
            lambda: request("/api/futures/open-interest/exchange-list", {"symbol": symbol}),
        )
        if not isinstance(oi_rows, list):
            oi_rows = []
        total_row = next(
            (row for row in oi_rows if isinstance(row, dict) and str(row.get("exchange", "")).lower() == "all"),
            None,
        )
        if total_row is None:
            total_row = next((row for row in oi_rows if isinstance(row, dict)), {})
        oi_usd = _number(total_row.get("open_interest_usd")) if total_row else None
        oi_change = _number(total_row.get("open_interest_change_percent_24h")) if total_row else None

        now = time.monotonic()
        with _CACHE_LOCK:
            funding_payload = _CG_FUNDING_CACHE.get("payload")
            funding_valid = _CG_FUNDING_CACHE.get("expires_at", 0.0) > now
        if not funding_valid:
            funding_payload = request("/api/futures/funding-rate/exchange-list")
            with _CACHE_LOCK:
                _CG_FUNDING_CACHE["payload"] = funding_payload
                _CG_FUNDING_CACHE["expires_at"] = time.monotonic() + 60.0
        if not isinstance(funding_payload, list):
            funding_payload = []
        coin = next(
            (row for row in funding_payload if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol),
            None,
        )
        funding_rows = (coin or {}).get("stablecoin_margin_list") or []
        rates = [
            rate for row in funding_rows if isinstance(row, dict)
            for rate in [_number(row.get("funding_rate"))] if rate is not None
        ]
        funding_median = median(rates) if rates else None
        known = oi_usd is not None or funding_median is not None or oi_change is not None
        return {
            "status": "KNOWN" if known else "NO_DATA",
            "symbol": symbol,
            "funding_median_pct": float(funding_median) if funding_median is not None else None,
            "funding_exchange_count": len(rates) if rates else 0,
            "open_interest_usd": oi_usd,
            "open_interest_change_24h_pct": oi_change,
            "funding_scope": "Stablecoin-margined futures; median across exchanges with an actual numeric value.",
            "open_interest_scope": "Market-wide All row when supplied by CoinGlass; changes are already percentages.",
            "reason": None if known else "CoinGlass returned no derivatives data for this symbol.",
        }
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "reason": f"CoinGlass request unavailable: {type(exc).__name__}.",
            "symbol": symbol,
            "funding_median_pct": None,
            "funding_exchange_count": None,
            "open_interest_usd": None,
            "open_interest_change_24h_pct": None,
        }


def _market_scoring(direction: str, dex: dict, cg: dict, min_dex_liquidity: float) -> dict:
    direction = str(direction or "NONE").upper()
    confirmation = 0.0
    risk = 0.0
    opportunity = 0.0
    confidence_penalty = 0.0
    dex_bias = "UNKNOWN"
    cg_crowding = "UNKNOWN"
    notes = []

    if dex.get("status") == "KNOWN":
        liquidity = _number(dex.get("liquidity_usd"))
        if liquidity is not None and liquidity < min_dex_liquidity:
            confirmation -= 3.0
            risk += 8.0
            notes.append("DEX liquidity is below the configured minimum.")
        age = _number(dex.get("pair_age_hours"))
        if age is not None and age < 24.0:
            confirmation -= 2.0
            risk += 5.0
            notes.append("DEX pair is less than 24 hours old; launch-stage execution risk is elevated.")
        turnover = _number(dex.get("volume_to_liquidity_24h"))
        if turnover is not None and turnover > 5.0:
            confirmation -= 1.0
            risk += 5.0
            notes.append("24-hour volume is unusually high relative to reported pool liquidity.")
        buy_share = _number(dex.get("buy_transaction_share_pct"))
        tx_count = _number(dex.get("total_transactions_24h"))
        if buy_share is not None and tx_count is not None and tx_count >= 30:
            if buy_share >= 58.0:
                dex_bias = "BUY_PRESSURE"
            elif buy_share <= 42.0:
                dex_bias = "SELL_PRESSURE"
            else:
                dex_bias = "BALANCED"
            aligned = (direction == "LONG" and dex_bias == "BUY_PRESSURE") or (
                direction == "SHORT" and dex_bias == "SELL_PRESSURE"
            )
            opposed = (direction == "LONG" and dex_bias == "SELL_PRESSURE") or (
                direction == "SHORT" and dex_bias == "BUY_PRESSURE"
            )
            if aligned:
                confirmation += 2.0
                opportunity += 1.5
                notes.append("DEX buy/sell transaction-count pressure aligns with the model direction.")
            elif opposed:
                confirmation -= 3.0
                risk += 5.0
                opportunity -= 2.0
                confidence_penalty += 4.0
                notes.append("DEX buy/sell transaction-count pressure conflicts with the model direction.")

    funding = _number(cg.get("funding_median_pct"))
    oi_change = _number(cg.get("open_interest_change_24h_pct"))
    if funding is not None:
        if funding >= 0.05:
            cg_crowding = "LONG_CROWDED"
        elif funding <= -0.05:
            cg_crowding = "SHORT_CROWDED"
        else:
            cg_crowding = "NOT_EXTREME"
        crowded_with_direction = (direction == "LONG" and cg_crowding == "LONG_CROWDED") or (
            direction == "SHORT" and cg_crowding == "SHORT_CROWDED"
        )
        if crowded_with_direction:
            confirmation -= 3.0
            risk += 5.0
            opportunity -= 2.0
            confidence_penalty += 4.0
            notes.append("CoinGlass funding indicates crowded positioning in the model direction.")
        if oi_change is not None and oi_change >= 20.0 and cg_crowding in {"LONG_CROWDED", "SHORT_CROWDED"}:
            risk += 3.0
            notes.append("Open interest is expanding rapidly while funding is stretched.")
    if oi_change is not None and oi_change <= -20.0:
        risk += 3.0
        notes.append("Open interest is contracting sharply; deleveraging volatility may be elevated.")

    return {
        "confirmation_adjustment": round(max(-8.0, min(4.0, confirmation)), 2),
        "risk_adjustment": round(max(0.0, min(20.0, risk)), 2),
        "opportunity_adjustment": round(max(-5.0, min(3.0, opportunity)), 2),
        "confidence_penalty": round(max(0.0, min(12.0, confidence_penalty)), 2),
        "dex_flow_bias": dex_bias,
        "coinglass_crowding": cg_crowding,
        "notes": notes,
        "methodology": "Conservative heuristic overlays; DEX buy/sell values are transaction counts, not notional volume. No adjustment is made for missing metrics.",
    }


def fetch_market_intelligence(asset, direction: str, snapshot=None, http=None, min_dex_liquidity_usd: float = 250000.0) -> dict:
    """Fetch cached DEX and optional CoinGlass evidence, then derive bounded overlays."""
    if http is None:
        return {
            "status": "UNKNOWN",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "dex_screener": {"status": "UNKNOWN", "reason": "Public HTTP client unavailable."},
            "coinglass": {"status": "NOT_CONFIGURED", "reason": "CoinGlass client unavailable."},
            "scoring": _market_scoring(direction, {}, {}, min_dex_liquidity_usd),
        }
    dex = _dex_pairs_for_token(asset, http)
    symbol = _coinglass_symbol(asset, dex)
    cg = _coinglass_data(symbol, http)
    scoring = _market_scoring(direction, dex, cg, min_dex_liquidity_usd)
    source_statuses = [
        dex.get("status") == "KNOWN",
        cg.get("status") == "KNOWN",
    ]
    status = "KNOWN" if all(source_statuses) else "PARTIAL" if any(source_statuses) else "UNKNOWN"
    return {
        "status": status,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "asset": asset.raw,
        "direction_context": direction,
        "dex_screener": dex,
        "coinglass": cg,
        "scoring": scoring,
        "disclaimer": "Market Intelligence is supplementary, not a standalone buy/sell signal. Missing CoinGlass or DEX data is excluded, never treated as zero.",
    }


def search_dex_tokens(http, query: str, limit: int = 12) -> dict:
    """Search supported DEX networks and return explicit contract choices."""
    text = str(query or "").strip()[:100]
    if len(text) < 2:
        return {"status": "UNKNOWN", "query": text, "results": [], "reason": "Enter at least two characters."}
    try:
        payload = http.get(f"{DEX_BASE}/latest/dex/search", params={"q": text})
        pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if not isinstance(pairs, list):
            raise ValueError("DEX Screener search response has an invalid schema.")
        query_lower = text.casefold()
        chosen = {}
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            chain_id = str(pair.get("chainId") or "").lower()
            prefix = SUPPORTED_CHAINS.get(chain_id)
            token = pair.get("baseToken") or {}
            if not prefix or not isinstance(token, dict):
                continue
            name = str(token.get("name") or "")
            symbol = str(token.get("symbol") or "")
            address = str(token.get("address") or "")
            matches = (
                query_lower in name.casefold()
                or query_lower in symbol.casefold()
                or query_lower == address.casefold()
            )
            if not matches or len(address) < 10:
                continue
            liquidity = _number((pair.get("liquidity") or {}).get("usd"))
            volume = _number((pair.get("volume") or {}).get("h24"))
            price = _positive(pair.get("priceUsd"))
            if liquidity is None or price is None:
                continue
            created_ms = _number(pair.get("pairCreatedAt"))
            key = f"{prefix}:{address.lower()}"
            item = {
                "asset": f"{prefix}:{address}",
                "chain": chain_id,
                "name": name,
                "symbol": symbol,
                "address": address,
                "dex": pair.get("dexId"),
                "pair_address": pair.get("pairAddress"),
                "price_usd": price,
                "liquidity_usd": liquidity,
                "volume_24h_usd": volume,
                "pair_created_at_ms": created_ms,
                "pair_url": pair.get("url"),
            }
            if key not in chosen or liquidity > chosen[key]["liquidity_usd"]:
                chosen[key] = item
        results = sorted(chosen.values(), key=lambda item: item["liquidity_usd"], reverse=True)[:max(1, min(limit, 20))]
        return {
            "status": "KNOWN" if results else "UNKNOWN",
            "query": text,
            "result_count": len(results),
            "results": results,
            "supported_networks": list(SUPPORTED_CHAINS.keys()),
            "reason": None if results else "No matching token was found on the supported DEX networks.",
        }
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "query": text,
            "results": [],
            "reason": f"DEX Screener search unavailable: {type(exc).__name__}.",
        }
