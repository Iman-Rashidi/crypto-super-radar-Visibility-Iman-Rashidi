"""Conservative analysis of GeckoTerminal's bounded recent Pool swap feed.

The endpoint exposes only a bounded recent-trade sample (up to 300 records in
its documented window). A capped response is therefore PARTIAL, never treated
as complete 24h history. Net swap flow is a Pool-side buy-minus-sell measure,
not project-wide capital inflow.
"""
from __future__ import annotations

import math
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

CACHE_TTL_SECONDS = 60.0
MAX_TRADES_PER_RESPONSE = 300
MIN_TRADES_FOR_PRESSURE_CLASSIFICATION = 30
MAX_CANDIDATES_PER_SCAN = 1
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.RLock()
_SCAN_OFFSET = 0


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _wallet_key(chain: str, address: Any) -> str | None:
    if not isinstance(address, str) or not address.strip():
        return None
    value = address.strip()
    return value if chain == "solana" else value.lower()


def analyze_pool_trade_evidence(
    trades: Any, *, chain: str, token_address: str, pool_address: str,
    liquidity_usd: float | None, now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and aggregate recent trades for one requested token/Pool."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    result: dict[str, Any] = {
        "schema_version": "gem-hunter-pool-trades-v1",
        "status": "UNKNOWN",
        "coverage_status": "UNKNOWN",
        "chain": chain,
        "token_address": token_address,
        "pool_address": pool_address,
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "source": "GECKOTERMINAL",
        "window_hours": 24,
        "sample_cap": MAX_TRADES_PER_RESPONSE,
        "trade_count": 0,
        "unique_trader_count": None,
        "buy_count": 0,
        "sell_count": 0,
        "buy_volume_usd": None,
        "sell_volume_usd": None,
        "total_volume_usd": None,
        "buy_volume_share_pct": None,
        "net_swap_flow_usd": None,
        "repeat_trader_share_pct": None,
        "round_trip_wallet_count": None,
        "whale_threshold_usd": None,
        "whale_buy_count": 0,
        "whale_sell_count": 0,
        "whale_events": [],
        "wash_trading_signal": "UNKNOWN",
        "reason_codes": [],
        "reason_fa": [],
    }
    if chain not in {"base", "solana"} or not token_address or not pool_address:
        result["reason_codes"].append("INVALID_POOL_CONTEXT")
        result["reason_fa"].append("هویت شبکه، توکن یا Pool برای تحلیل معاملات معتبر نیست.")
        return result
    if not isinstance(trades, list) or len(trades) > MAX_TRADES_PER_RESPONSE:
        result["reason_codes"].append("INVALID_TRADE_RESPONSE")
        result["reason_fa"].append("ساختار یا تعداد رکوردهای پاسخ معاملات با قرارداد منبع سازگار نیست.")
        return result

    cutoff = now - timedelta(hours=24)
    parsed: list[dict[str, Any]] = []
    invalid_rows: list[str] = []
    seen_observations: set[tuple] = set()
    for index, row in enumerate(trades):
        if not isinstance(row, dict):
            invalid_rows.append("ROW_SCHEMA")
            continue
        kind = str(row.get("kind") or "").lower()
        amount = _finite(row.get("volume_in_usd"))
        stamp = _timestamp(row.get("block_timestamp"))
        tx_hash = row.get("tx_hash")
        wallet = _wallet_key(chain, row.get("tx_from_address"))
        if kind not in {"buy", "sell"}:
            invalid_rows.append("TRADE_SIDE")
            continue
        if amount is None or amount <= 0:
            invalid_rows.append("TRADE_NOTIONAL")
            continue
        if stamp is None or stamp < cutoff or stamp > now + timedelta(minutes=2):
            invalid_rows.append("TRADE_TIMESTAMP")
            continue
        if not isinstance(tx_hash, str) or not tx_hash.strip():
            invalid_rows.append("TRADE_HASH")
            continue
        if wallet is not None:
            # EVM wallets are normalized; Solana base58 addresses remain case-sensitive.
            wallet = wallet if chain == "solana" else wallet.lower()
        provider_trade_id = row.get("__trade_id")
        identity = (
            ("provider-id", provider_trade_id) if isinstance(provider_trade_id, str) and provider_trade_id
            else ("observation", tx_hash.strip().lower() if chain != "solana" else tx_hash.strip(),
                  stamp.isoformat(), kind, amount, wallet)
        )
        if identity in seen_observations:
            invalid_rows.append("DUPLICATE_TRADE_OBSERVATION")
            continue
        seen_observations.add(identity)
        parsed.append({
            "kind": kind,
            "amount_usd": amount,
            "timestamp": stamp,
            "tx_hash": tx_hash.strip(),
            "wallet": wallet,
        })

    if invalid_rows:
        result["reason_codes"].extend(sorted(set(invalid_rows)))
        result["reason_fa"].append("بخشی از رکوردهای معاملات ناقص، تکراری یا خارج از پنجرهٔ ۲۴ساعته بود؛ تجمیع کامل تأیید نمی‌شود.")

    # A 300-row response may be truncated by the provider. Never infer that
    # a full 24h population is present merely because the endpoint succeeded.
    capped = len(trades) >= MAX_TRADES_PER_RESPONSE
    complete = not invalid_rows and not capped
    result["status"] = "COMPLETE" if complete else "PARTIAL" if parsed or trades else "COMPLETE"
    result["coverage_status"] = result["status"]
    result["trade_count"] = len(parsed)
    buys = [row for row in parsed if row["kind"] == "buy"]
    sells = [row for row in parsed if row["kind"] == "sell"]
    buy_volume = sum(row["amount_usd"] for row in buys)
    sell_volume = sum(row["amount_usd"] for row in sells)
    total_volume = buy_volume + sell_volume
    result.update({
        "buy_count": len(buys),
        "sell_count": len(sells),
        "buy_volume_usd": round(buy_volume, 8),
        "sell_volume_usd": round(sell_volume, 8),
        "total_volume_usd": round(total_volume, 8),
        "buy_volume_share_pct": round(buy_volume / total_volume * 100.0, 4) if total_volume > 0 else None,
        "net_swap_flow_usd": round(buy_volume - sell_volume, 8),
    })

    traders: dict[str, list[str]] = defaultdict(list)
    for row in parsed:
        if row["wallet"]:
            traders[row["wallet"]].append(row["kind"])
    result["unique_trader_count"] = len(traders) if parsed and len(traders) == len(parsed) else (
        len(traders) if traders else None
    )
    if traders:
        repeat_events = sum(len(kinds) for kinds in traders.values() if len(kinds) > 1)
        round_trips = sum(1 for kinds in traders.values() if "buy" in kinds and "sell" in kinds)
        wallet_observations = sum(len(kinds) for kinds in traders.values())
        result["repeat_trader_share_pct"] = round(
            repeat_events / wallet_observations * 100.0, 4
        ) if wallet_observations else None
        result["round_trip_wallet_count"] = round_trips

    liquidity = _finite(liquidity_usd)
    whale_threshold = max(1000.0, liquidity * 0.02) if liquidity is not None and liquidity > 0 else 1000.0
    result["whale_threshold_usd"] = whale_threshold
    for row in parsed:
        if row["amount_usd"] < whale_threshold:
            continue
        if row["kind"] == "buy":
            result["whale_buy_count"] += 1
        else:
            result["whale_sell_count"] += 1
        if len(result["whale_events"]) < 25:
            result["whale_events"].append({
                "event_type": "SWAP_BUY" if row["kind"] == "buy" else "SWAP_SELL",
                "amount_usd": round(row["amount_usd"], 8),
                "timestamp_utc": row["timestamp"].isoformat().replace("+00:00", "Z"),
                "tx_hash": row["tx_hash"],
                "wallet_address": row["wallet"],
            })

    if capped:
        result["reason_codes"].append("PROVIDER_SAMPLE_CAPPED")
        result["reason_fa"].append("منبع ۳۰۰ معاملهٔ اخیر را برگردانده است؛ احتمال حذف رکوردهای بیشتر وجود دارد و جمع ۲۴ساعته کامل نیست.")
    elif invalid_rows:
        result["reason_codes"].append("TRADE_COVERAGE_INVALID")
    else:
        result["reason_codes"].append("RECENT_POOL_TRADES_VALIDATED")
        result["reason_fa"].append("رکوردهای معاملاتی در پنجرهٔ ۲۴ساعته از نظر نوع، مبلغ دلاری، زمان و شناسه تراکنش معتبرند.")

    wallets_complete = len(parsed) > 0 and all(row["wallet"] for row in parsed)
    result["wallet_attribution_status"] = (
        "COMPLETE_FOR_RETURNED_SAMPLE" if wallets_complete else "PARTIAL"
    )
    if wallets_complete and len(parsed) >= 30:
        unique_ratio = len(traders) / len(parsed)
        repeat_share = result["repeat_trader_share_pct"] or 0.0
        round_trips = result["round_trip_wallet_count"] or 0
        # This is a warning heuristic, not proof of wash trading or common ownership.
        if unique_ratio < 0.20 and repeat_share >= 80 and round_trips >= 5:
            result["wash_trading_signal"] = "SUSPECTED"
            result["reason_codes"].append("CONCENTRATED_REPEAT_TRADER_PATTERN")
            result["reason_fa"].append("الگوی تکرار و تمرکز معامله‌گر مشکوک است؛ این شاخص به‌تنهایی اثبات Wash Trading نیست.")
        else:
            result["wash_trading_signal"] = "NO_STRONG_PATTERN_IN_SAMPLE"
    if complete and len(parsed) >= MIN_TRADES_FOR_PRESSURE_CLASSIFICATION and total_volume > 0:
        share = result["buy_volume_share_pct"]
        result["buy_sell_pressure_status"] = (
            "PASS" if share >= 55.0 else "FAIL" if share <= 45.0 else "UNKNOWN"
        )
    else:
        result["buy_sell_pressure_status"] = "UNKNOWN"
    result["reason_codes"].append(
        "TRADE_WINDOW_COMPLETE" if complete else "TRADE_WINDOW_PARTIAL"
    )
    return result


def enrich_discovery_candidates_with_pool_trades(
    candidates: Any, data_service, *, limit: int = MAX_CANDIDATES_PER_SCAN,
) -> list[dict]:
    """Fetch at most one exact-pool trade sample per scan and cache complete windows."""
    if not isinstance(candidates, list):
        return []
    rows = [dict(item) for item in candidates if isinstance(item, dict)]
    eligible = [
        index for index, row in enumerate(rows)
        if (row.get("market_reconciliation") or {}).get("status") == "PASS"
        and not isinstance(row.get("qualification"), dict)
        and isinstance(row.get("token_address"), str)
        and isinstance(row.get("pool_address"), str)
        and row.get("chain") in {"base", "solana"}
    ]
    if not eligible:
        return rows
    bounded = max(0, min(int(limit), MAX_CANDIDATES_PER_SCAN, len(eligible)))
    global _SCAN_OFFSET
    with _CACHE_LOCK:
        start = _SCAN_OFFSET % len(eligible) if bounded else 0
        if bounded:
            _SCAN_OFFSET = (start + bounded) % len(eligible)
    selected = {eligible[(start + offset) % len(eligible)] for offset in range(bounded)}

    for index, row in enumerate(rows):
        if index not in selected:
            row["pool_trade_evidence"] = {
                "status": "NOT_CHECKED_BUDGET",
                "coverage_status": "NOT_CHECKED_BUDGET",
                "reason_codes": ["POOL_TRADE_SCAN_BUDGET_EXHAUSTED"],
            }
            continue
        chain, token, pool = row["chain"], row["token_address"], row["pool_address"]
        key = f"{chain}:{token if chain == 'solana' else token.lower()}:{pool if chain == 'solana' else pool.lower()}"
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and cached[0] > time.monotonic():
                row["pool_trade_evidence"] = dict(cached[1])
                continue
        try:
            gecko = getattr(data_service, "gecko", None)
            if gecko is None or not callable(getattr(gecko, "trades", None)):
                raise RuntimeError("GeckoTerminal Pool trades adapter is unavailable")
            trade_rows = gecko.trades(chain, pool, token)
            rec = row.get("market_reconciliation") or {}
            evidence = analyze_pool_trade_evidence(
                trade_rows, chain=chain, token_address=token, pool_address=pool,
                liquidity_usd=_finite(rec.get("verified_liquidity_usd")),
            )
        except Exception as exc:
            evidence = {
                "schema_version": "gem-hunter-pool-trades-v1",
                "status": "UNKNOWN",
                "coverage_status": "UNKNOWN",
                "chain": chain, "token_address": token, "pool_address": pool,
                "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source": "GECKOTERMINAL",
                "reason_codes": ["POOL_TRADE_PROVIDER_UNAVAILABLE"],
                "source_errors": [type(exc).__name__],
            }
        row["pool_trade_evidence"] = evidence
        if evidence.get("coverage_status") == "COMPLETE":
            with _CACHE_LOCK:
                _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, dict(evidence))
    return rows
