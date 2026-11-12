"""Per-venue, event-time order-flow aggregation with fail-closed coverage.

This module is deliberately transport-agnostic. Adapters must normalize each
trade to base quantity and supply venue-native trade IDs plus exchange event
timestamps. It never combines CVD across venues when one venue is unhealthy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True, slots=True)
class _Trade:
    trade_id: str
    event_time_ms: int
    price: float
    base_quantity: float
    side: str
    source: str


class VenueOrderFlow:
    """A single venue's bounded CVD window and feed-health state.

    CVD is usable only when the venue remained connected for the configured
    coverage threshold, has a recent receive/heartbeat, and has valid trades.
    Invalid payloads freeze this venue until an explicit reconnect. Event time
    determines window membership; receive time is used only for freshness.
    """

    def __init__(
        self,
        venue: str,
        *,
        window_ms: int = 300_000,
        min_coverage_pct: float = 80.0,
        stale_after_ms: int = 15_000,
        max_events: int = 50_000,
        future_skew_ms: int = 5_000,
    ) -> None:
        if not isinstance(venue, str) or not venue.strip():
            raise ValueError("venue is required")
        for name, value in (
            ("window_ms", window_ms),
            ("stale_after_ms", stale_after_ms),
            ("max_events", max_events),
            ("future_skew_ms", future_skew_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if window_ms <= 0 or stale_after_ms <= 0 or max_events <= 0 or future_skew_ms < 0:
            raise ValueError("window, stale threshold and capacity must be positive; skew cannot be negative")
        coverage = _finite(min_coverage_pct)
        if coverage is None or not 0.0 < coverage <= 100.0:
            raise ValueError("min_coverage_pct must be greater than 0 and at most 100")

        self.venue = venue.strip()
        self.window_ms = window_ms
        self.min_coverage_pct = coverage
        self.stale_after_ms = stale_after_ms
        self.max_events = max_events
        self.future_skew_ms = future_skew_ms
        self._connected = False
        self._connected_at_ms: int | None = None
        self._last_received_at_ms: int | None = None
        self._frozen_reason: str | None = None
        self._events: dict[str, _Trade] = {}
        self._rejected_count = 0
        self._duplicate_count = 0

    def connect(self, connected_at_ms: int) -> None:
        timestamp = _timestamp_ms(connected_at_ms)
        if timestamp is None:
            raise ValueError("connected_at_ms must be a non-negative integer timestamp")
        self._connected = True
        self._connected_at_ms = timestamp
        self._last_received_at_ms = timestamp
        self._frozen_reason = None

    def disconnect(self) -> None:
        self._connected = False

    def heartbeat(self, received_at_ms: int) -> bool:
        """Record a live transport heartbeat; it is not a market trade."""
        timestamp = _timestamp_ms(received_at_ms)
        if not self._connected or self._frozen_reason:
            return False
        if timestamp is None:
            self._freeze("INVALID_RECEIVE_TIMESTAMP")
            return False
        self._last_received_at_ms = max(self._last_received_at_ms or timestamp, timestamp)
        return True

    def _freeze(self, reason: str) -> None:
        self._frozen_reason = reason
        self._rejected_count += 1

    def _prune(self, now_ms: int) -> None:
        cutoff = now_ms - self.window_ms
        expired = [
            trade_id for trade_id, event in self._events.items()
            if event.event_time_ms < cutoff
        ]
        for trade_id in expired:
            del self._events[trade_id]

    def ingest_trade(
        self,
        *,
        trade_id: str,
        event_time_ms: int,
        received_at_ms: int,
        price: Any,
        base_quantity: Any,
        aggressor_side: str,
        source: str,
    ) -> dict[str, Any]:
        """Ingest one provider-normalized trade.

        base_quantity must already account for the provider's contract
        multiplier. If the adapter cannot establish base quantity, it must not
        pass contract count as though it were base quantity.
        """
        if not self._connected:
            return {"accepted": False, "reason": "DISCONNECTED"}
        if self._frozen_reason:
            return {"accepted": False, "reason": "FROZEN", "detail": self._frozen_reason}

        receive_ts = _timestamp_ms(received_at_ms)
        if receive_ts is None:
            self._freeze("INVALID_RECEIVE_TIMESTAMP")
            return {"accepted": False, "reason": "INVALID_RECEIVE_TIMESTAMP"}
        self._last_received_at_ms = max(self._last_received_at_ms or receive_ts, receive_ts)

        identifier = trade_id.strip() if isinstance(trade_id, str) else ""
        if not identifier:
            self._freeze("MISSING_TRADE_ID")
            return {"accepted": False, "reason": "MISSING_TRADE_ID"}
        if identifier in self._events:
            self._duplicate_count += 1
            return {"accepted": False, "reason": "DUPLICATE_TRADE_ID"}

        event_ts = _timestamp_ms(event_time_ms)
        normalized_price = _finite(price)
        normalized_quantity = _finite(base_quantity)
        side = aggressor_side.upper().strip() if isinstance(aggressor_side, str) else ""
        source_name = source.strip() if isinstance(source, str) else ""
        if event_ts is None:
            self._freeze("INVALID_EVENT_TIMESTAMP")
            return {"accepted": False, "reason": "INVALID_EVENT_TIMESTAMP"}
        if normalized_price is None or normalized_price <= 0:
            self._freeze("INVALID_PRICE")
            return {"accepted": False, "reason": "INVALID_PRICE"}
        if normalized_quantity is None or normalized_quantity <= 0:
            self._freeze("UNKNOWN_OR_INVALID_BASE_QUANTITY")
            return {"accepted": False, "reason": "UNKNOWN_OR_INVALID_BASE_QUANTITY"}
        if side not in {"BUY", "SELL"}:
            self._freeze("UNKNOWN_AGGRESSOR_SIDE")
            return {"accepted": False, "reason": "UNKNOWN_AGGRESSOR_SIDE"}
        if not source_name:
            self._freeze("MISSING_SOURCE_PROVENANCE")
            return {"accepted": False, "reason": "MISSING_SOURCE_PROVENANCE"}
        if event_ts > receive_ts + self.future_skew_ms:
            self._freeze("FUTURE_EVENT_TIMESTAMP")
            return {"accepted": False, "reason": "FUTURE_EVENT_TIMESTAMP"}

        self._prune(receive_ts)
        if event_ts < receive_ts - self.window_ms:
            return {"accepted": False, "reason": "OUTSIDE_CURRENT_WINDOW"}
        if len(self._events) >= self.max_events:
            self._freeze("EVENT_BUFFER_CAPACITY_EXCEEDED")
            return {"accepted": False, "reason": "EVENT_BUFFER_CAPACITY_EXCEEDED"}

        self._events[identifier] = _Trade(
            trade_id=identifier,
            event_time_ms=event_ts,
            price=normalized_price,
            base_quantity=normalized_quantity,
            side=side,
            source=source_name,
        )
        return {"accepted": True, "reason": "ACCEPTED"}

    def snapshot(self, now_ms: int) -> dict[str, Any]:
        now = _timestamp_ms(now_ms)
        if now is None:
            raise ValueError("now_ms must be a non-negative integer timestamp")
        self._prune(now)
        window_start = max(0, now - self.window_ms)
        connected_at = self._connected_at_ms
        coverage_start = max(window_start, connected_at or window_start)
        coverage_ms = max(0, now - coverage_start) if connected_at is not None else 0
        coverage_pct = min(100.0, coverage_ms / self.window_ms * 100.0)
        receive_age = (
            now - self._last_received_at_ms
            if self._last_received_at_ms is not None else None
        )

        if not self._connected:
            status = "DISCONNECTED"
        elif self._frozen_reason:
            status = "FROZEN"
        elif receive_age is None:
            status = "STALE"
        elif receive_age < -self.future_skew_ms:
            status = "DRIFT"
        elif receive_age > self.stale_after_ms:
            status = "STALE"
        elif coverage_pct < self.min_coverage_pct:
            status = "WARMING_UP"
        elif not self._events:
            status = "SILENT"
        else:
            status = "LIVE"

        ordered = sorted(self._events.values(), key=lambda event: (event.event_time_ms, event.trade_id))
        buy = [event for event in ordered if event.side == "BUY"]
        sell = [event for event in ordered if event.side == "SELL"]
        observed_buy_base = sum(event.base_quantity for event in buy)
        observed_sell_base = sum(event.base_quantity for event in sell)
        observed_cvd_base = observed_buy_base - observed_sell_base
        observed_cvd_quote = sum(
            event.base_quantity * event.price * (1.0 if event.side == "BUY" else -1.0)
            for event in ordered
        )
        usable = status == "LIVE"

        return {
            "venue": self.venue,
            "status": status,
            "window_start_ms": window_start,
            "window_end_ms": now,
            "window_ms": self.window_ms,
            "coverage_pct": round(coverage_pct, 4),
            "minimum_coverage_pct": self.min_coverage_pct,
            "last_receive_age_ms": receive_age,
            "event_count": len(ordered),
            "buy_trade_count": len(buy),
            "sell_trade_count": len(sell),
            "first_event_time_ms": ordered[0].event_time_ms if ordered else None,
            "last_event_time_ms": ordered[-1].event_time_ms if ordered else None,
            "observed_buy_base_quantity": round(observed_buy_base, 12),
            "observed_sell_base_quantity": round(observed_sell_base, 12),
            "observed_cvd_base": round(observed_cvd_base, 12),
            "observed_cvd_quote_notional": round(observed_cvd_quote, 8),
            "cvd_base": round(observed_cvd_base, 12) if usable else None,
            "cvd_quote_notional": round(observed_cvd_quote, 8) if usable else None,
            "sources": sorted({event.source for event in ordered}),
            "duplicate_count": self._duplicate_count,
            "rejected_count": self._rejected_count,
            "frozen_reason": self._frozen_reason,
        }


def build_multi_venue_order_flow_report(
    venues: Mapping[str, VenueOrderFlow],
    *,
    now_ms: int,
) -> dict[str, Any]:
    """Report venue health and directional agreement without hiding dead venues."""
    snapshots = {
        name: tracker.snapshot(now_ms)
        for name, tracker in sorted(venues.items())
    }
    live = [item for item in snapshots.values() if item["status"] == "LIVE"]
    all_live = bool(snapshots) and len(live) == len(snapshots)
    if not all_live:
        alignment = "UNKNOWN"
        reason = "Directional agreement is unavailable until every configured venue is LIVE."
    else:
        signs = {
            1 if item["cvd_base"] > 0 else -1 if item["cvd_base"] < 0 else 0
            for item in live
        }
        if signs == {1}:
            alignment, reason = "BUY_DOMINANT", "All configured venues show positive base-quantity CVD."
        elif signs == {-1}:
            alignment, reason = "SELL_DOMINANT", "All configured venues show negative base-quantity CVD."
        elif signs == {0}:
            alignment, reason = "NEUTRAL", "All configured venues show zero net base-quantity CVD."
        else:
            alignment, reason = "DISAGREEMENT", "Configured venues disagree on CVD direction."

    return {
        "status": "LIVE" if all_live else "PARTIAL",
        "directional_alignment": alignment,
        "reason": reason,
        "configured_venue_count": len(snapshots),
        "live_venue_count": len(live),
        "venues": snapshots,
        "aggregate_cvd": None,
        "aggregate_policy": "PER_VENUE_ONLY; no aggregate is emitted when venue coverage is incomplete.",
    }


def summarize_liquidation_events(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Separate confirmed liquidations from estimated large-print proxies.

    Only records explicitly classified as CONFIRMED_LIQUIDATION contribute to
    the confirmed liquidation total. Large prints never become confirmed
    liquidations by inference.
    """
    seen: set[tuple[str, str]] = set()
    confirmed: list[float] = []
    estimated: list[float] = []
    invalid_rows = 0
    unknown_evidence_rows = 0
    duplicate_rows = 0

    for event in events:
        venue = event.get("venue")
        event_id = event.get("event_id")
        timestamp = _timestamp_ms(event.get("event_time_ms"))
        source = event.get("source")
        notional = _finite(event.get("notional_usd"))
        evidence = event.get("evidence_class")
        if (
            not isinstance(venue, str) or not venue.strip()
            or not isinstance(event_id, str) or not event_id.strip()
            or timestamp is None
            or not isinstance(source, str) or not source.strip()
            or notional is None or notional <= 0
        ):
            invalid_rows += 1
            continue
        key = (venue.strip(), event_id.strip())
        if key in seen:
            duplicate_rows += 1
            continue
        seen.add(key)
        if evidence == "CONFIRMED_LIQUIDATION":
            confirmed.append(notional)
        elif evidence == "ESTIMATED_LARGE_PRINT":
            estimated.append(notional)
        else:
            unknown_evidence_rows += 1

    valid_count = len(confirmed) + len(estimated)
    status = (
        "NO_DATA" if not seen
        else "PARTIAL" if invalid_rows or unknown_evidence_rows
        else "OBSERVED"
    )
    return {
        "status": status,
        "confirmed_liquidation_count": len(confirmed),
        "confirmed_liquidation_notional_usd": round(sum(confirmed), 8) if confirmed else None,
        "estimated_large_print_count": len(estimated),
        "estimated_large_print_notional_usd": round(sum(estimated), 8) if estimated else None,
        "unknown_evidence_rows": unknown_evidence_rows,
        "invalid_rows": invalid_rows,
        "duplicate_rows": duplicate_rows,
        "valid_classified_rows": valid_count,
        "interpretation": "Estimated large prints are not included in confirmed liquidation totals.",
    }
