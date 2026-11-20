from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone

import requests

from crypto_super_radar.market_replay import (
    DEMO_REST_URL,
    FUNDING_PATH,
    ReplayDownloadError,
    _json_get,
    _subtract_calendar_months,
    _utc_ms,
)

ALLOWED_HOURS = {1, 2, 4, 8}


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
            valid.append({
                "fundingTime": ts,
                "fundingRate": rate,
                "fundingKind": str(row.get("fundingKind", "MISSING")),
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

    def summarize(deltas: list[int]) -> dict:
        hours = [delta / 3_600_000 for delta in deltas]
        counter = Counter(round(h, 6) for h in hours)
        return {
            "count": len(deltas),
            "interval_hours_distribution": dict(sorted(counter.items())),
            "median_hours": statistics.median(hours) if hours else None,
            "non_whole_hour_intervals": sum(abs(h - round(h)) > 1e-8 for h in hours),
            "outside_1_2_4_8_hours": [
                {"hours": round(h, 6), "count": n}
                for h, n in sorted(counter.items())
                if not any(abs(h - allowed) < 1e-8 for allowed in ALLOWED_HOURS)
            ],
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

    return {
        "symbol": symbol,
        "window_start_utc": datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
        "window_end_utc": now.isoformat(),
        "pages": pages,
        "event_count": len(ordered),
        "event_kind_counts": dict(Counter(x["fundingKind"] for x in ordered)),
        "invalid_rows": invalid_rows,
        "all_event_intervals": summarize(all_deltas),
        "regular_kind_0_intervals": summarize(regular_deltas),
        "current_funding_interval": current_interval,
        "current_interval_error_type": current_error,
        "history_endpoint_note": (
            "History rows expose fundingKind but not a per-event fundingInterval. "
            "Current GET Funding Rate interval cannot establish historical interval regimes."
        ),
        "historical_schedule_completeness": (
            "UNKNOWN_WHEN_MULTIPLE_INTERVALS_OR_UNDOCUMENTED_GAPS_EXIST"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--months", type=int, default=7)
    args = parser.parse_args()
    result = inspect_symbol(args.symbol, args.months)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
