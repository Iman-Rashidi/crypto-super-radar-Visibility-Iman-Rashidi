from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone

import requests

from crypto_super_radar.market_replay import (
    DEFAULT_SYMBOLS,
    DEMO_REST_URL,
    FUNDING_PATH,
    ReplayDownloadError,
    _json_get,
    _subtract_calendar_months,
    _utc_ms,
)

ALLOWED_HOURS = {1, 2, 4, 8}


PRODUCTION_REST_URL = "https://openapi.blofin.com"
OBSERVED_GAP_START_MS = 1782403200000  # 2026-06-25T16:00:00Z
OBSERVED_GAP_END_MS = 1782489600000    # 2026-06-26T16:00:00Z


def summarize_schema_rows(rows: list, *, environment: str, query: str) -> dict:
    """Summarize event schema without exposing rates or mixing environments."""
    if not isinstance(rows, list):
        raise ReplayDownloadError("funding schema probe returned a non-list data field")
    objects = [row for row in rows if isinstance(row, dict)]
    kinds = Counter("MISSING" if row.get("fundingKind") is None else str(row.get("fundingKind")) for row in objects)
    timestamps = []
    for row in objects:
        try:
            ts = int(row.get("fundingTime"))
        except (TypeError, ValueError, OverflowError):
            continue
        if ts > 0:
            timestamps.append(ts)
    return {
        "environment": environment,
        "query": query,
        "row_count": len(objects),
        "row_key_sets": sorted({",".join(sorted(row.keys())) for row in objects}),
        "funding_kind_distribution": dict(sorted(kinds.items())),
        "funding_kind_field_present_for_all_rows": bool(objects) and all("fundingKind" in row for row in objects),
        "funding_kind_values_supported": bool(objects) and all(str(row.get("fundingKind")) in {"0", "1"} for row in objects),
        "funding_time_min_utc": _utc(min(timestamps)) if timestamps else None,
        "funding_time_max_utc": _utc(max(timestamps)) if timestamps else None,
    }


def summarize_cursor_semantics(rows: list, *, cursor_ms: int, direction: str) -> dict:
    """Check documented strict before/after pagination without exposing funding rates."""
    if direction not in {"after", "before"}:
        raise ValueError("direction must be 'after' or 'before'")
    timestamps = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            timestamp = int(row.get("fundingTime"))
        except (TypeError, ValueError, OverflowError):
            continue
        if timestamp > 0:
            timestamps.append(timestamp)
    if not timestamps:
        return {
            "status": "INCONCLUSIVE",
            "direction": direction,
            "cursor_ms": int(cursor_ms),
            "row_count_with_timestamp": 0,
            "returned_min_ms": None,
            "returned_max_ms": None,
        }
    passed = (
        max(timestamps) < cursor_ms
        if direction == "after"
        else min(timestamps) > cursor_ms
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "direction": direction,
        "cursor_ms": int(cursor_ms),
        "row_count_with_timestamp": len(timestamps),
        "returned_min_ms": min(timestamps),
        "returned_max_ms": max(timestamps),
    }


def _probe_environment_schema(session: requests.Session, *, base_url: str, environment: str,
                              symbol: str, query_name: str, cursor_params: dict) -> dict:
    params = {"instId": symbol, "limit": 5, **cursor_params}
    try:
        payload = _json_get(session, base_url.rstrip("/") + FUNDING_PATH, params)
        rows = payload.get("data")
        summary = summarize_schema_rows(rows, environment=environment, query=query_name)
        direction = next((key for key in ("after", "before") if key in cursor_params), None)
        if direction is not None:
            summary["pagination_contract"] = summarize_cursor_semantics(
                rows, cursor_ms=int(cursor_params[direction]), direction=direction
            )
        summary.update({"symbol": symbol, "status": "OK"})
        return summary
    except Exception as exc:
        return {"environment": environment, "symbol": symbol, "query": query_name,
                "status": "ERROR", "row_count": 0, "error_type": type(exc).__name__}


def compare_environment_schemas(symbols: tuple[str, ...]) -> dict:
    """Compare Demo and Production response shape only; Production is never replay data."""
    queries = (
        ("latest_page", {}),
        ("older_side_of_observed_gap", {"after": str(OBSERVED_GAP_END_MS)}),
        ("newer_side_of_observed_gap", {"before": str(OBSERVED_GAP_START_MS)}),
    )
    environments = (
        ("BLOFIN_DEMO_REPLAY_SOURCE", DEMO_REST_URL),
        ("BLOFIN_PRODUCTION_SCHEMA_ONLY_NOT_FOR_REPLAY", PRODUCTION_REST_URL),
    )
    probes = []
    for environment, base_url in environments:
        session = requests.Session()
        try:
            for symbol in symbols:
                for query_name, cursor_params in queries:
                    probes.append(_probe_environment_schema(
                        session, base_url=base_url, environment=environment,
                        symbol=symbol, query_name=query_name, cursor_params=cursor_params,
                    ))
        finally:
            session.close()
    demo_rows = [p for p in probes if p["environment"] == "BLOFIN_DEMO_REPLAY_SOURCE"
                 and p["status"] == "OK" and p["row_count"] > 0]
    prod_rows = [p for p in probes if p["environment"] == "BLOFIN_PRODUCTION_SCHEMA_ONLY_NOT_FOR_REPLAY"
                 and p["status"] == "OK" and p["row_count"] > 0]
    demo_all_missing = bool(demo_rows) and all(
        p["funding_kind_distribution"].get("MISSING", 0) == p["row_count"] for p in demo_rows
    )
    prod_has_kind = any(p["funding_kind_values_supported"] for p in prod_rows)
    demo_has_kind = any(p["funding_kind_values_supported"] for p in demo_rows)
    if demo_all_missing and prod_has_kind:
        finding = "DEMO_RESPONSE_SCHEMA_DIFFERS_FROM_PRODUCTION_CONTROL"
    elif demo_has_kind:
        finding = "FUNDING_KIND_PRESENT_IN_DEMO_SAMPLES"
    elif prod_rows and not prod_has_kind:
        finding = "FUNDING_KIND_NOT_OBSERVED_IN_EITHER_ENVIRONMENT_SAMPLE"
    else:
        finding = "INCONCLUSIVE_SCHEMA_COMPARISON"
    return {
        "purpose": "SCHEMA_DIAGNOSTIC_ONLY_NOT_A_REPLAY_DATASET",
        "symbols": list(symbols),
        "finding": finding,
        "documented_pagination_contract": {
            "after": "returns records with fundingTime earlier than the cursor",
            "before": "returns records with fundingTime later than the cursor",
            "cursor_checks_are_strict": True,
        },
        "pagination_contract_summary": {
            "pass": sum(1 for item in probes if item.get("pagination_contract", {}).get("status") == "PASS"),
            "fail": sum(1 for item in probes if item.get("pagination_contract", {}).get("status") == "FAIL"),
            "inconclusive": sum(1 for item in probes if item.get("pagination_contract", {}).get("status") == "INCONCLUSIVE"),
        },
        "production_rows_may_be_used_for_demo_replay": False,
        "gap_interval_utc": {"start": _utc(OBSERVED_GAP_START_MS), "end": _utc(OBSERVED_GAP_END_MS)},
        "probes": probes,
        "interpretation": (
            "Production is a separately labelled schema control only. Its rates, timestamps and "
            "records are not copied into, substituted for, or used to complete the Demo dataset."
        ),
    }




def _utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).isoformat()


def inspect_symbol(symbol: str, months: int = 7) -> dict:
    session = requests.Session()
    now = datetime.now(timezone.utc)
    start_ms = _utc_ms(_subtract_calendar_months(now, months))
    cursor = None
    previous_oldest = None
    events: dict[int, dict] = {}
    pages = 0
    invalid_rows = 0

    for _ in range(100):
        params = {"instId": symbol, "limit": 100}
        if cursor is not None:
            params["after"] = str(cursor)
        payload = _json_get(
            session,
            DEMO_REST_URL.rstrip("/") + FUNDING_PATH,
            params,
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ReplayDownloadError("funding endpoint returned a non-list data field")
        pages += 1
        valid = []
        for row in rows:
            if not isinstance(row, dict):
                invalid_rows += 1
                continue
            try:
                ts = int(row["fundingTime"])
                rate = float(row["fundingRate"])
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid_rows += 1
                continue
            if ts <= 0 or not math.isfinite(rate):
                invalid_rows += 1
                continue
            kind = row.get("fundingKind")
            valid.append({
                "fundingTime": ts,
                "fundingRate": rate,
                "fundingKind": str(kind) if kind is not None else "MISSING",
            })
        if not valid:
            break
        for item in valid:
            if item["fundingTime"] >= start_ms:
                if item["fundingTime"] in events:
                    raise ReplayDownloadError(
                        f"duplicate timestamp observed in diagnostic for {symbol}"
                    )
                events[item["fundingTime"]] = item
        oldest = min(x["fundingTime"] for x in valid)
        if oldest <= start_ms:
            break
        if previous_oldest is not None and oldest >= previous_oldest:
            raise ReplayDownloadError("funding pagination did not move backward")
        previous_oldest = oldest
        cursor = oldest
        if len(rows) < 100:
            break
    else:
        raise ReplayDownloadError("funding pagination exceeded safety page limit")

    ordered = [events[k] for k in sorted(events)]
    all_deltas = [
        b["fundingTime"] - a["fundingTime"]
        for a, b in zip(ordered, ordered[1:])
    ]
    regular = [x for x in ordered if x["fundingKind"] == "0"]
    regular_deltas = [
        b["fundingTime"] - a["fundingTime"]
        for a, b in zip(regular, regular[1:])
    ]

    def summarize(deltas: list[int], source: list[dict]) -> dict:
        hours = [delta / 3_600_000 for delta in deltas]
        counter = Counter(round(h, 6) for h in hours)
        anomalies = []
        for idx, h in enumerate(hours):
            if not any(abs(h - allowed) < 1e-8 for allowed in ALLOWED_HOURS):
                anomalies.append({
                    "from_utc": _utc(source[idx]["fundingTime"]),
                    "to_utc": _utc(source[idx + 1]["fundingTime"]),
                    "gap_hours": round(h, 6),
                    "from_kind": source[idx]["fundingKind"],
                    "to_kind": source[idx + 1]["fundingKind"],
                })
        return {
            "count": len(deltas),
            "interval_hours_distribution": dict(sorted(counter.items())),
            "median_hours": statistics.median(hours) if hours else None,
            "non_whole_hour_intervals": sum(abs(h - round(h)) > 1e-8 for h in hours),
            "outside_supported_1_2_4_8_hour_cadences": anomalies,
        }

    current_interval = None
    current_error = None
    try:
        current = _json_get(
            session,
            DEMO_REST_URL.rstrip("/") + "/api/v1/market/funding-rate",
            {"instId": symbol},
        )
        rows = current.get("data")
        if isinstance(rows, list) and rows:
            item = rows[0]
            current_interval = {
                "fundingInterval": item.get("fundingInterval"),
                "fundingIntervalUnit": item.get("fundingIntervalUnit"),
                "fundingTime": item.get("fundingTime"),
            }
    except Exception as exc:
        current_error = type(exc).__name__

    kinds = Counter(x["fundingKind"] for x in ordered)
    history_kind_missing = kinds.get("MISSING", 0) == len(ordered) and bool(ordered)
    all_summary = summarize(all_deltas, ordered)
    regular_summary = summarize(regular_deltas, regular)
    anomalies = all_summary["outside_supported_1_2_4_8_hour_cadences"]

    return {
        "symbol": symbol,
        "source_environment": "BLOFIN_DEMO_PUBLIC_API",
        "window_start_utc": _utc(start_ms),
        "window_end_utc": now.isoformat(),
        "pages": pages,
        "event_count": len(ordered),
        "event_kind_counts": dict(kinds),
        "funding_kind_missing_for_all_rows": history_kind_missing,
        "invalid_rows": invalid_rows,
        "all_event_intervals": all_summary,
        "regular_kind_0_intervals": regular_summary,
        "unexplained_interval_count": len(anomalies),
        "current_funding_interval": current_interval,
        "current_interval_error_type": current_error,
        "history_endpoint_note": (
            "History endpoint response observed here does not include a per-event fundingInterval; "
            "the current endpoint only establishes the current interval."
        ),
        "historical_schedule_completeness": (
            "BLOCKED" if anomalies or history_kind_missing else "UNKNOWN"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--months", type=int, default=7)
    parser.add_argument("--compare-production-schema", action="store_true",
                        help="Compare Production response schema as a separate read-only control; never use its rows for replay.")
    args = parser.parse_args()
    symbols = DEFAULT_SYMBOLS if args.all else (args.symbol,)
    results = [inspect_symbol(symbol, args.months) for symbol in symbols]
    report = {
        "status": "DIAGNOSTIC_ONLY_NOT_A_DATASET_ACCEPTANCE",
        "symbols": results,
        "interpretation": (
            "No data was synthesized. Unexplained intervals and missing event-kind fields "
            "must be resolved before a valid historical replay."
        ),
    }
    if args.compare_production_schema:
        report["cross_environment_schema_control"] = compare_environment_schemas(symbols)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
