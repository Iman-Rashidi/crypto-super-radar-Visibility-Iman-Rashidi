"""Freshness and coverage classification for the curated cross-venue market board."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_feed_health_report(
    board: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Report only the freshness/coverage that the current market board can prove.

    LIVE means the curated symbols were recently cross-validated by both
    configured spot providers. It does not mean a continuous WebSocket feed.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    board = board if isinstance(board, dict) else {}

    observed_at = _parse_utc(board.get("updated_at_utc"))
    try:
        ttl = max(1, int(board.get("cache_ttl_seconds", 60)))
    except (TypeError, ValueError, OverflowError):
        ttl = 60
    items = board.get("items") if isinstance(board.get("items"), list) else []
    try:
        total = max(0, int(board.get("total_items", 0)))
    except (TypeError, ValueError, OverflowError):
        total = 0
    verified = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "PASS")
    age = max(0.0, (now - observed_at).total_seconds()) if observed_at else None

    if observed_at is None:
        status = "UNKNOWN"
        reason = "No valid market-board observation timestamp is available."
    elif (now - observed_at).total_seconds() < -60:
        status = "DRIFT"
        reason = "The market-board timestamp is unexpectedly in the future."
    elif age is not None and age > ttl:
        status = "STALE"
        reason = "The cached cross-venue observation exceeded its freshness window."
    elif total > 0 and verified >= total and len(items) >= total and board.get("status") == "PASS":
        status = "LIVE"
        reason = "All curated symbols passed the two-provider spot-price validation within the cache window."
    elif verified > 0:
        status = "PARTIAL"
        reason = "Only part of the curated symbol set passed cross-provider validation."
    else:
        status = "UNKNOWN"
        reason = "No curated symbol currently has a verified two-provider observation."

    return {
        "status": status,
        "observed_at_utc": observed_at.isoformat().replace("+00:00", "Z") if observed_at else None,
        "age_seconds": round(age, 2) if age is not None else None,
        "stale_after_seconds": ttl,
        "verified_symbols": verified,
        "observed_symbols": len(items),
        "expected_symbols": total,
        "providers": board.get("sources") or ["Binance Spot", "Bybit Spot"],
        "reason": reason,
        "scope": "Curated spot-market board only; not a continuous feed or full-market coverage.",
        "external_feeds": {
            "CoinGlass": "ON_DEMAND_NOT_MONITORED_BY_THIS_ENDPOINT",
            "DEX_Screener": "ON_DEMAND_NOT_MONITORED_BY_THIS_ENDPOINT",
        },
    }
