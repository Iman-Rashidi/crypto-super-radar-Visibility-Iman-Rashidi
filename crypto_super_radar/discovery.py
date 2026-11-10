"""Broad public-market discovery for liquid CEX altcoins and small-cap DEX tokens.

Discovery is a candidate-generation layer, not a buy signal. Every candidate still
requires a full 12-gate scan; missing fields remain UNKNOWN.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .providers import DataProviderError, NETWORKS, _positive_float, _safe_float

STABLE_BASES = {
    "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDE", "USDS",
    "USDD", "PYUSD", "EURC", "USDP", "USD1",
}
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def discover_cex_altcoins(http: Any, limit: int = 100,
                           min_quote_volume_usd: float = 1_000_000.0) -> dict:
    """Discover liquid Binance USDT spot pairs; selected scans cross-check providers."""
    exchange = http.get("https://data-api.binance.vision/api/v3/exchangeInfo")
    tickers = http.get("https://data-api.binance.vision/api/v3/ticker/24hr")
    if not isinstance(exchange, dict) or not isinstance(exchange.get("symbols"), list):
        raise DataProviderError("Binance exchangeInfo returned an invalid schema")
    if not isinstance(tickers, list):
        raise DataProviderError("Binance 24h ticker returned an invalid schema")
    eligible = {
        item.get("symbol") for item in exchange["symbols"]
        if isinstance(item, dict)
        and item.get("status") == "TRADING"
        and item.get("quoteAsset") == "USDT"
        and item.get("isSpotTradingAllowed", True)
    }
    rows = []
    for item in tickers:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        if symbol not in eligible or symbol.endswith(LEVERAGED_SUFFIXES):
            continue
        base = symbol[:-4]
        if base in STABLE_BASES:
            continue
        price = _positive_float(item.get("lastPrice"))
        volume = _safe_float(item.get("quoteVolume"))
        change = _finite(item.get("priceChangePercent"))
        trades = _safe_float(item.get("count"))
        if price is None or volume is None or volume < min_quote_volume_usd:
            continue
        rows.append({
            "symbol": symbol, "base_asset": base, "quote_asset": "USDT",
            "price_usd": price, "volume_24h_usd": volume,
            "change_24h_pct": change, "trades_24h": int(trades or 0),
            "source": "Binance Spot discovery; cross-validation occurs on full scan",
            "scan_asset": "cex:" + symbol,
        })
    rows.sort(key=lambda row: row["volume_24h_usd"], reverse=True)
    return {
        "status": "PASS" if rows else "UNKNOWN",
        "source": "Binance Spot public market data",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "min_quote_volume_usd": min_quote_volume_usd,
        "count": min(len(rows), max(1, min(int(limit), 250))),
        "items": rows[:max(1, min(int(limit), 250))],
    }


def _finite(value: Any):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def discover_dex_microcaps(http: Any, chain: str, limit: int = 30) -> dict:
    """Discover new pools and return contract addresses plus explicit risk flags."""
    if chain not in NETWORKS:
        raise ValueError("Unsupported chain")
    url = f"https://api.geckoterminal.com/api/v2/networks/{NETWORKS[chain]}/new_pools"
    payload = http.get(url, params={"page": 1, "include": "base_token,quote_token"})
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise DataProviderError("GeckoTerminal new-pools response has an invalid schema")
    included = {}
    for token in payload.get("included") or []:
        if isinstance(token, dict):
            included[token.get("id")] = token.get("attributes") or {}
    candidates = []
    for pool in payload["data"]:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes") or {}
        rel = pool.get("relationships") or {}
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id")
        quote_id = ((rel.get("quote_token") or {}).get("data") or {}).get("id")
        base = included.get(base_id, {})
        quote = included.get(quote_id, {})
        address = str(base.get("address") or "").strip()
        symbol = str(base.get("symbol") or "").strip()
        name = str(base.get("name") or "").strip()
        quote_symbol = str(quote.get("symbol") or "").upper()
        # Keep common USD-quoted pools only; do not mistake a quote token for target.
        if not address or quote_symbol not in STABLE_BASES:
            continue
        price = _positive_float(attrs.get("base_token_price_usd"))
        liquidity = _safe_float(attrs.get("reserve_in_usd"))
        volume_data = attrs.get("volume_usd") or {}
        volume_24h = _safe_float(volume_data.get("h24"))
        volume_1h = _safe_float(volume_data.get("h1"))
        txns = attrs.get("transactions") or {}
        tx1h = txns.get("h1") or {}
        buys = _safe_float(tx1h.get("buys")) or 0.0
        sells = _safe_float(tx1h.get("sells")) or 0.0
        market_cap = _safe_float(attrs.get("market_cap_usd"))
        fdv = _safe_float(attrs.get("fdv_usd"))
        created = attrs.get("pool_created_at")
        pool_address = attrs.get("address")
        flags = []
        if liquidity is None or liquidity < 25_000:
            flags.append("نقدینگی کم یا نامشخص")
        if volume_24h is None:
            flags.append("حجم ۲۴ساعته نامشخص")
        elif liquidity and volume_24h / max(liquidity, 1.0) > 5:
            flags.append("گردش حجم غیرعادی نسبت به نقدینگی")
        if fdv and liquidity and fdv / max(liquidity, 1.0) > 100:
            flags.append("FDV بسیار بزرگ نسبت به نقدینگی")
        if buys + sells < 10:
            flags.append("تعداد معاملات یک‌ساعته پایین")
        candidates.append({
            "chain": chain, "name": name, "symbol": symbol,
            "contract_address": address, "pool_address": pool_address,
            "price_usd": price, "liquidity_usd": liquidity,
            "volume_1h_usd": volume_1h, "volume_24h_usd": volume_24h,
            "market_cap_usd": market_cap, "fdv_usd": fdv,
            "buys_1h": int(buys), "sells_1h": int(sells),
            "pool_created_at": created, "risk_flags": flags,
            "scan_asset": f"{chain}:{address}",
            "explorer_url": (
                f"https://solscan.io/token/{address}" if chain == "solana"
                else f"https://basescan.org/token/{address}" if chain == "base"
                else f"https://etherscan.io/token/{address}"
            ),
            "dexscreener_url": f"https://dexscreener.com/{'ethereum' if chain == 'eth' else chain}/{pool_address or address}",
            "discovery_source": "GeckoTerminal new pools",
            "security_status": "NOT_CHECKED",
        })
    candidates.sort(
        key=lambda row: (
            row["liquidity_usd"] or 0,
            row["volume_24h_usd"] or 0,
        ), reverse=True
    )
    cap = max(1, min(int(limit), 100))
    return {
        "status": "PASS" if candidates else "UNKNOWN",
        "chain": chain,
        "source": "GeckoTerminal public new-pool feed",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "count": min(len(candidates), cap),
        "items": candidates[:cap],
        "notice": "Discovery only: contract safety, honeypot behavior and holder concentration require a full scan; missing evidence is not a pass.",
    }
