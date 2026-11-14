"""Public, read-only fundamental data adapters with explicit provenance.

Only evidence with a source timestamp is classified as fresh. Raw observations
are descriptive; this module deliberately does not manufacture module scores.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional


class FundamentalProviderError(RuntimeError):
    pass


def _finite_nonnegative(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 and number < float("inf") else None


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _last_chart_timestamp(rows: Any) -> Optional[float]:
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[-1]
    if not isinstance(row, (list, tuple)) or not row:
        return None
    try:
        stamp = float(row[0])
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp if stamp > 0 and stamp < float("inf") else None


def evidence_record(
    *, value: Any, source: str, source_url: str,
    fetched_at: datetime, observed_at: Optional[float],
    unit: str, quality: str = "VALID",
) -> dict[str, Any]:
    """Attach source, retrieval time, observation time, freshness and quality."""
    numeric = _finite_nonnegative(value)
    if numeric is None:
        quality = "INCOMPLETE"
    age_seconds = None
    observed_iso = None
    freshness = "UNKNOWN"
    if observed_at is not None:
        observed_iso = _utc_iso(observed_at)
        age_seconds = max(0.0, (fetched_at.timestamp() - observed_at))
        freshness = "FRESH" if age_seconds <= 48 * 3600 else (
            "STALE" if age_seconds <= 7 * 86400 else "EXPIRED"
        )
    if quality == "CONFLICT":
        freshness = "CONFLICT"
    return {
        "value": numeric,
        "unit": unit,
        "source": source,
        "source_url": source_url,
        "fetched_at_utc": fetched_at.isoformat(),
        "observed_at_utc": observed_iso,
        "age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
        "freshness": freshness,
        "quality": quality,
        "status": "KNOWN" if numeric is not None and quality == "VALID" and freshness == "FRESH" else "UNKNOWN",
    }


class DefiLlamaFundamentalProvider:
    """Free DefiLlama protocol TVL/fee adapter; protocol slug must be explicit."""

    BASE = "https://api.llama.fi"
    SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

    def __init__(self, http: Any, clock=None):
        self.http = http
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self, protocol_slug: str) -> dict[str, Any]:
        slug = str(protocol_slug or "").strip().lower()
        if not self.SLUG_RE.fullmatch(slug):
            raise FundamentalProviderError("A valid, explicitly mapped DefiLlama protocol slug is required")
        fetched_at = self.clock()
        protocol_url = f"{self.BASE}/protocol/{slug}"
        fees_url = f"{self.BASE}/summary/fees/{slug}"
        protocol = self.http.get(protocol_url)
        fees = self.http.get(fees_url)
        if not isinstance(protocol, dict) or not isinstance(fees, dict):
            raise FundamentalProviderError("DefiLlama returned an invalid protocol or fee response schema")

        tvl_rows = protocol.get("tvl")
        if not isinstance(tvl_rows, list) or not tvl_rows or not isinstance(tvl_rows[-1], dict):
            tvl_value, tvl_timestamp = None, None
        else:
            last_tvl = tvl_rows[-1]
            tvl_value = _finite_nonnegative(last_tvl.get("totalLiquidityUSD"))
            try:
                tvl_timestamp = float(last_tvl.get("date"))
            except (TypeError, ValueError, OverflowError):
                tvl_timestamp = None

        fee_chart = fees.get("totalDataChart")
        fee_timestamp = _last_chart_timestamp(fee_chart)
        fee_24h = _finite_nonnegative(fees.get("total24h"))
        revenue_24h = _finite_nonnegative(fees.get("totalRevenue24h"))
        if revenue_24h is None:
            revenue_24h = _finite_nonnegative(fees.get("totalRevenue24h"))

        tvl_record = evidence_record(
            value=tvl_value, source="DefiLlama protocol TVL",
            source_url=protocol_url, fetched_at=fetched_at,
            observed_at=tvl_timestamp, unit="USD",
        )
        fees_record = evidence_record(
            value=fee_24h, source="DefiLlama protocol fees",
            source_url=fees_url, fetched_at=fetched_at,
            observed_at=fee_timestamp, unit="USD/24h",
        )
        revenue_record = evidence_record(
            value=revenue_24h, source="DefiLlama protocol revenue",
            source_url=fees_url, fetched_at=fetched_at,
            observed_at=fee_timestamp, unit="USD/24h",
        )
        metadata = {
            "name": protocol.get("name"),
            "category": protocol.get("category"),
            "symbol": protocol.get("symbol"),
            "chains": protocol.get("chains") if isinstance(protocol.get("chains"), list) else [],
            "website": protocol.get("url"),
            "github": protocol.get("github") if isinstance(protocol.get("github"), list) else [],
            "gecko_id": protocol.get("gecko_id"),
            "token_rights": protocol.get("tokenRights"),
        }
        return {
            "project": {
                "status": "UNKNOWN", "score": None,
                "evidence": {
                    "provider": "DefiLlama", "protocol_slug": slug,
                    "metadata": metadata,
                    "data_points": {"tvl_usd": tvl_record, "fees_24h_usd": fees_record,
                                    "revenue_24h_usd": revenue_record},
                    "note": "Raw protocol observations only; no project-health score is inferred.",
                },
            },
            "industry": {
                "evidence": {
                    "provider": "DefiLlama", "category": metadata["category"],
                    "note": "Category metadata is not a scored industry analysis.",
                },
            },
            "dcf": {
                "evidence": {
                    "status": "UNKNOWN",
                    "reason": "DCF requires verified forecast cashflows, discount assumptions, net treasury value and explicit token value capture.",
                },
            },
            "comparables": {
                "evidence": {
                    "status": "UNKNOWN",
                    "category": metadata["category"],
                    "reason": "A peer set and comparable market-cap/revenue/TVL denominators are not established by this response.",
                },
            },
            "provider_metadata": {
                "provider": "DefiLlama",
                "protocol_slug": slug,
                "fetched_at_utc": fetched_at.isoformat(),
                "source_urls": [protocol_url, fees_url],
                "data_quality": "PARTIAL" if any(
                    item["status"] != "KNOWN" for item in (tvl_record, fees_record, revenue_record)
                ) else "VALID",
            },
        }


__all__ = ["FundamentalProviderError", "DefiLlamaFundamentalProvider", "evidence_record"]
