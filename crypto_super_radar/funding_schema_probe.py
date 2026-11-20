"""Read-only probe of BloFin Demo public funding response schema.

No authentication, no account endpoints, and no trading operations are used.
The probe records whether historical FundingKind is actually present and
compares the current funding interval/time response to a UTC settlement boundary.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import requests

BASE_URL = "https://demo-trading-openapi.blofin.com"
SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
HISTORY_PATH = "/api/v1/market/funding-rate-history"
CURRENT_PATH = "/api/v1/market/funding-rate"
TOLERANCE_MS = 10_000


def _safe_payload(response: requests.Response) -> tuple[dict[str, Any] | None, str | None]:
    if response.status_code != 200:
        return None, f"HTTP_{response.status_code}"
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return None, "INVALID_JSON"
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        code = payload.get("code") if isinstance(payload, dict) else "NON_OBJECT"
        return None, f"API_CODE_{code}"
    return payload, None


def summarize_history_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return {"status": "INVALID_DATA_SHAPE", "sample_count": 0, "kind_present_count": 0}
    valid_rows = [row for row in rows if isinstance(row, Mapping)]
    present = [row for row in valid_rows if "fundingKind" in row and row.get("fundingKind") not in (None, "")]
    kinds = Counter(str(row.get("fundingKind")) for row in present)
    events = []
    for row in valid_rows:
        try:
            stamp = int(row.get("fundingTime"))
        except (TypeError, ValueError, OverflowError):
            continue
        events.append({
            "funding_time_ms": stamp,
            "funding_time_utc": datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat(),
            "funding_kind_present": "fundingKind" in row and row.get("fundingKind") not in (None, ""),
            "funding_kind": str(row.get("fundingKind")) if row.get("fundingKind") not in (None, "") else None,
            "response_keys": sorted(str(key) for key in row.keys()),
        })
    return {
        "status": "OK" if valid_rows else "EMPTY",
        "sample_count": len(valid_rows),
        "kind_present_count": len(present),
        "kind_missing_count": len(valid_rows) - len(present),
        "kind_values": dict(kinds),
        "events": events,
    }


def summarize_current_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        return {"status": "INVALID_DATA_SHAPE"}
    row = rows[0]
    try:
        value = float(row.get("fundingInterval"))
        unit = str(row.get("fundingIntervalUnit") or "").lower()
        stamp = int(row.get("fundingTime"))
        if not math.isfinite(value) or value <= 0:
            raise ValueError("invalid interval")
        multiplier = {"minute": 60_000, "hour": 3_600_000}.get(unit)
        if multiplier is None:
            raise ValueError("unsupported interval unit")
        interval_ms = int(value * multiplier)
        remainder = stamp % interval_ms
        boundary_ok = min(remainder, interval_ms - remainder) <= TOLERANCE_MS
    except (TypeError, ValueError, OverflowError):
        return {
            "status": "INTERVAL_METADATA_MISSING_OR_INVALID",
            "response_keys": sorted(str(key) for key in row.keys()),
            "funding_interval": row.get("fundingInterval"),
            "funding_interval_unit": row.get("fundingIntervalUnit"),
            "funding_time": row.get("fundingTime"),
        }
    return {
        "status": "OK" if boundary_ok else "OFF_SCHEDULE",
        "funding_interval": value,
        "funding_interval_unit": unit,
        "funding_interval_ms": interval_ms,
        "funding_time_ms": stamp,
        "funding_time_utc": datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat(),
        "utc_boundary_tolerance_ms": TOLERANCE_MS,
        "utc_boundary_verified": boundary_ok,
        "response_keys": sorted(str(key) for key in row.keys()),
    }


def probe_symbol(session: requests.Session, symbol: str, base_url: str = BASE_URL) -> dict[str, Any]:
    result: dict[str, Any] = {"symbol": symbol}
    for name, path in (("history", HISTORY_PATH), ("current", CURRENT_PATH)):
        try:
            response = session.get(
                base_url.rstrip("/") + path,
                params={"instId": symbol, "limit": 5} if name == "history" else {"instId": symbol},
                timeout=(5, 15),
            )
            payload, error = _safe_payload(response)
            if error:
                result[name] = {"status": error}
            elif name == "history":
                result[name] = summarize_history_payload(payload)
            else:
                result[name] = summarize_current_payload(payload)
        except requests.RequestException as exc:
            result[name] = {"status": f"REQUEST_ERROR: {type(exc).__name__}"}
    return result


def run_probe(symbols: tuple[str, ...] = SYMBOLS) -> dict[str, Any]:
    session = requests.Session()
    session.headers.update({"User-Agent": "CRYPTO-SUPER-RADAR-Funding-Schema-Probe/1.0"})
    results = [probe_symbol(session, symbol) for symbol in symbols]
    history_rows = [row.get("history", {}) for row in results]
    return {
        "probe_version": "BLOFIN_PUBLIC_FUNDING_SCHEMA_V1",
        "environment": "BLOFIN_DEMO_PUBLIC_MARKET_DATA",
        "base_url": BASE_URL,
        "authenticated": False,
        "order_writes": False,
        "symbols": results,
        "aggregate": {
            "history_sample_count": sum(int(row.get("sample_count", 0)) for row in history_rows),
            "funding_kind_present_count": sum(int(row.get("kind_present_count", 0)) for row in history_rows),
            "funding_kind_missing_count": sum(int(row.get("kind_missing_count", 0)) for row in history_rows),
            "all_history_requests_ok": all(row.get("status") in {"OK", "EMPTY"} for row in history_rows),
        },
        "interpretation": (
            "This is a schema/metadata probe only. Current interval metadata does not backfill "
            "historical fundingKind and is not substituted for per-event history fields."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/funding_schema_probe.json")
    args = parser.parse_args(argv)
    report = run_probe()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["aggregate"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
