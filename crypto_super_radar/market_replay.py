"""Download public BloFin Demo historical data into a local SQLite replay store.

Only public market-data endpoints are used. No API key, no order-write method,
and no Live account endpoint is included. The public candle API allows up to
1,440 rows per page; pagination walks backward using the after timestamp.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import requests

DEMO_REST_URL = "https://demo-trading-openapi.blofin.com"
CANDLE_PATH = "/api/v1/market/candles"
MARK_CANDLE_PATH = "/api/v1/market/mark-price-candles"
FUNDING_PATH = "/api/v1/market/funding-rate-history"
INSTRUMENTS_PATH = "/api/v1/market/instruments"
DEFAULT_SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
BAR_MS = {"15m": 15 * 60_000, "1H": 60 * 60_000, "4H": 4 * 60 * 60_000}

# Evidence-backed price-candle blackout observed in BloFin public Demo data.
# Candle timestamps are open times; end_exclusive_ms is the close boundary of
# the final absent 15m candle. Only this exact LAST-price gap is allowlisted.
OBSERVED_PRICE_CANDLE_BLACKOUTS = ({
    "instrument_id": "BTC-USDT",
    "bar": "15m",
    "source": "LAST",
    "missing_from_utc": "2026-03-09T07:45:00+00:00",
    "missing_to_utc": "2026-03-09T12:30:00+00:00",
    "missing_intervals": 20,
    "start_ms": 1773042300000,
    "last_missing_ms": 1773059400000,
    "end_exclusive_ms": 1773060300000,
    "left_candle_utc": "2026-03-09T07:30:00+00:00",
    "right_candle_utc": "2026-03-09T12:45:00+00:00",
    "evidence": "BloFin public Demo historical downloader Run #114 artifact 14711734625, downloader.log",
}, {
    "instrument_id": "BTC-USDT",
    "bar": "1H",
    "source": "LAST",
    "missing_from_utc": "2026-03-09T07:00:00+00:00",
    "missing_to_utc": "2026-03-09T11:00:00+00:00",
    "missing_intervals": 5,
    "start_ms": 1773039600000,
    "last_missing_ms": 1773054000000,
    "end_exclusive_ms": 1773057600000,
    "left_candle_utc": "2026-03-09T06:00:00+00:00",
    "right_candle_utc": "2026-03-09T12:00:00+00:00",
    "evidence": "BloFin public Demo historical downloader Run #115 artifact 14713645678, downloader.log",
}, {
    "instrument_id": "BTC-USDT",
    "bar": "4H",
    "source": "LAST",
    "missing_from_utc": "2026-03-09T04:00:00+00:00",
    "missing_to_utc": "2026-03-09T08:00:00+00:00",
    "missing_intervals": 2,
    "start_ms": 1773028800000,
    "last_missing_ms": 1773043200000,
    "end_exclusive_ms": 1773057600000,
    "left_candle_utc": "2026-03-09T00:00:00+00:00",
    "right_candle_utc": "2026-03-09T12:00:00+00:00",
    "evidence": "BloFin public Demo historical downloader Run #44920261175 artifact 14713811121, downloader.log",
},)

# Challenge 1 accepts missing fundingKind only on the fixed UTC
# 00:00 / 08:00 / 16:00 regular schedule. Never infer a dynamic cadence.
REGULAR_FUNDING_INTERVAL_MS = 8 * 60 * 60_000
UTC_DAY_MS = 24 * 60 * 60_000
FUNDING_SCHEDULE_TOLERANCE_MS = 10_000
OBSERVED_FUNDING_BLACKOUTS = ({
    "start_ms": 1782403200000,  # 2026-06-25T16:00:00Z
    "end_ms": 1782489600000,    # 2026-06-26T16:00:00Z
    "label": "EXCHANGE_DATA_GAP_BLACKOUT",
    "evidence": "Common 24-hour timestamp gap across all five approved symbols in Historical Replay Run #60",
    "outage_confirmed": False,
},)


def _observed_price_candle_gap_matches(
    *, symbol: str, bar: str, first_missing_ms: int, last_missing_ms: int,
    missing_count: int, mark_price: bool,
) -> bool:
    source = "MARK" if mark_price else "LAST"
    return any(
        str(window["instrument_id"]) == str(symbol).upper()
        and str(window["bar"]) == str(bar)
        and str(window["source"]) == source
        and int(window["start_ms"]) == int(first_missing_ms)
        and int(window["last_missing_ms"]) == int(last_missing_ms)
        and int(window["missing_intervals"]) == int(missing_count)
        for window in OBSERVED_PRICE_CANDLE_BLACKOUTS
    )


def _is_price_candle_blackout(timestamp_ms: int) -> dict[str, Any] | None:
    stamp = int(timestamp_ms)
    return next((
        dict(window) for window in OBSERVED_PRICE_CANDLE_BLACKOUTS
        if int(window["start_ms"]) <= stamp < int(window["end_exclusive_ms"])
    ), None)


def _is_regular_utc_funding_boundary(timestamp_ms: int):
    """Return True only within ±10s of 00:00, 08:00 or 16:00 UTC."""
    day_offset = int(timestamp_ms) % UTC_DAY_MS
    boundaries = (0, 8 * 60 * 60_000, 16 * 60 * 60_000)
    return min(
        min((day_offset - boundary) % UTC_DAY_MS,
            (boundary - day_offset) % UTC_DAY_MS)
        for boundary in boundaries
    ) <= FUNDING_SCHEDULE_TOLERANCE_MS


def _is_observed_blackout_gap(left_ms: int, right_ms: int) -> bool:
    return any(
        abs(int(left_ms) - int(window["start_ms"])) <= FUNDING_SCHEDULE_TOLERANCE_MS
        and abs(int(right_ms) - int(window["end_ms"])) <= FUNDING_SCHEDULE_TOLERANCE_MS
        for window in OBSERVED_FUNDING_BLACKOUTS
    )


def _validate_common_funding_blackout(funding_by_symbol: Mapping[str, list[tuple[Any, ...]]]) -> None:
    expected = (
        int(OBSERVED_FUNDING_BLACKOUTS[0]["start_ms"]),
        int(OBSERVED_FUNDING_BLACKOUTS[0]["end_ms"]),
    )
    if set(funding_by_symbol) != set(DEFAULT_SYMBOLS):
        raise ReplayDownloadError("common blackout validation requires the exact five-symbol approved basket")
    for symbol, items in funding_by_symbol.items():
        timestamps = [int(item[1]) for item in items]
        observed = {
            (left, right) for left, right in zip(timestamps, timestamps[1:])
            if abs((right - left) - (expected[1] - expected[0])) <= FUNDING_SCHEDULE_TOLERANCE_MS
        }
        if observed != {expected}:
            raise ReplayDownloadError(
                f"{symbol} does not contain exactly the declared common funding-data blackout"
            )


class ReplayDownloadError(RuntimeError):
    pass


def _record_unique_timestamp(
    seen: dict[int, tuple[Any, ...]],
    timestamp: int,
    item: tuple[Any, ...],
    *,
    series: str,
) -> None:
    """Reject duplicate source timestamps instead of silently overwriting them."""
    if timestamp in seen:
        raise ReplayDownloadError(f"duplicate timestamp in {series}: {timestamp}")
    seen[timestamp] = item


def _utc_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _subtract_calendar_months(value: datetime, months: int) -> datetime:
    """Subtract calendar months without turning six months into 180 days."""
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _json_get(session: requests.Session, url: str, params: Mapping[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            response = session.get(url, params=params, timeout=timeout, allow_redirects=False)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = max(float(retry_after or 0), 0.5 * (2 ** attempt))
                except ValueError:
                    delay = 0.5 * (2 ** attempt)
                time.sleep(min(delay, 20.0))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                raise ReplayDownloadError(f"BloFin public API rejected request: {payload!r}")
            return payload
        except (requests.RequestException, ValueError, ReplayDownloadError) as exc:
            last_error = exc
            if isinstance(exc, ReplayDownloadError):
                break
            if attempt < 4:
                time.sleep(min(0.5 * (2 ** attempt), 8.0))
    raise ReplayDownloadError(f"BloFin public data request failed: {type(last_error).__name__ if last_error else 'unknown'}")


def _parse_candle(row: Any, *, inst_id: str, bar: str, source: str) -> tuple[Any, ...] | None:
    minimum_columns = 6 if source == "MARK" else 9
    if not isinstance(row, (list, tuple)) or len(row) < minimum_columns:
        return None
    try:
        ts = int(row[0])
        values = [float(row[i]) for i in range(1, 5)]
        volume = float(row[5]) if source != "MARK" else 0.0
        confirm = str(row[8] if source != "MARK" else row[5])
    except (TypeError, ValueError, OverflowError):
        return None
    open_, high, low, close = values
    if (
        confirm != "1" or ts <= 0
        or any(not math_is_finite(value) for value in (*values, volume))
        or min(open_, high, low, close) <= 0 or volume < 0
    ):
        return None
    if high < max(open_, close, low) or low > min(open_, close, high):
        return None
    return (inst_id, bar, ts, open_, high, low, close, volume, confirm, source, json.dumps(list(row), separators=(",", ":")))


def download_candles(
    inst_id: str,
    bar: str,
    *,
    months: int = 6,
    mark_price: bool = False,
    session: requests.Session | None = None,
    base_url: str = DEMO_REST_URL,
    page_size: int = 1440,
) -> list[tuple[Any, ...]]:
    """Fetch confirmed bars from the requested lookback window.

    BloFin returns descending data. Duplicate timestamps are collapsed, and
    only confirmed bars are retained. If pagination stops making progress,
    the function fails instead of pretending the requested range is complete.
    """
    symbol = str(inst_id or "").strip().upper()
    if symbol not in DEFAULT_SYMBOLS:
        raise ValueError("instrument is not in the approved Challenge 1 watchlist")
    if bar not in BAR_MS or not 1 <= months <= 13 or not 1 <= page_size <= 1440:
        raise ValueError("unsupported bar/months/page_size")
    sess = session or requests.Session()
    now = datetime.now(timezone.utc)
    now_ms = _utc_ms(now)
    start_ms = _utc_ms(_subtract_calendar_months(now, months))
    path = MARK_CANDLE_PATH if mark_price else CANDLE_PATH
    url = base_url.rstrip("/") + path
    cursor: int | None = None
    seen: dict[int, tuple[Any, ...]] = {}
    previous_oldest: int | None = None
    for _ in range(200):
        params: dict[str, Any] = {"instId": symbol, "bar": bar, "limit": page_size}
        if cursor is not None:
            params["after"] = cursor
        payload = _json_get(sess, url, params)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ReplayDownloadError("candle endpoint returned a non-list data field")
        parsed = [item for raw in rows if (item := _parse_candle(raw, inst_id=symbol, bar=bar, source="MARK" if mark_price else "LAST"))]
        if not parsed:
            break
        for item in parsed:
            if item[2] >= start_ms:
                _record_unique_timestamp(
                    seen, int(item[2]), item, series=f"{symbol} {bar}"
                )
        oldest = min(int(item[2]) for item in parsed)
        if oldest <= start_ms:
            break
        if previous_oldest is not None and oldest >= previous_oldest:
            raise ReplayDownloadError("candle pagination did not move backward; refusing incomplete replay data")
        previous_oldest = oldest
        cursor = oldest
        if len(rows) < page_size:
            break
    else:
        raise ReplayDownloadError("candle pagination exceeded the safety page limit")
    if not seen:
        raise ReplayDownloadError(f"no completed candles downloaded for {symbol} {bar}")
    result = [seen[key] for key in sorted(seen)]
    interval = BAR_MS[bar]
    gaps = [
        (int(a[2]), int(b[2]), int((b[2] - a[2]) // interval) - 1)
        for a, b in zip(result, result[1:])
        if int(b[2]) - int(a[2]) != interval
    ]
    if gaps:
        unregistered = []
        for left_ms, right_ms, missing_count in gaps:
            first_missing_ms = left_ms + interval
            last_missing_ms = right_ms - interval
            if _observed_price_candle_gap_matches(
                symbol=symbol, bar=bar, first_missing_ms=first_missing_ms,
                last_missing_ms=last_missing_ms, missing_count=missing_count,
                mark_price=mark_price,
            ):
                continue
            first_missing = datetime.fromtimestamp(first_missing_ms / 1000, timezone.utc).isoformat()
            last_missing = datetime.fromtimestamp(last_missing_ms / 1000, timezone.utc).isoformat()
            unregistered.append(
                f"{symbol} {bar}: missing_from_utc={first_missing}, "
                f"missing_to_utc={last_missing}, missing_intervals={missing_count}, "
                f"left_candle_utc={datetime.fromtimestamp(left_ms / 1000, timezone.utc).isoformat()}, "
                f"right_candle_utc={datetime.fromtimestamp(right_ms / 1000, timezone.utc).isoformat()}"
            )
        if unregistered:
            raise ReplayDownloadError(
                f"historical candle series contains {len(unregistered)} unregistered timestamp gap(s): "
                + " | ".join(unregistered)
            )
    if result[0][2] > start_ms + interval:
        raise ReplayDownloadError(
            f"historical candle series for {symbol} {bar} does not cover the requested start"
        )
    latest_close_ms = int(result[-1][2]) + interval
    if latest_close_ms < now_ms - 2 * interval:
        raise ReplayDownloadError(
            f"historical candle series for {symbol} {bar} is stale at the end of the requested window"
        )
    return result


def download_instrument_rules(
    inst_id: str, *, session: requests.Session | None = None,
    base_url: str = DEMO_REST_URL,
) -> dict[str, Any]:
    symbol = str(inst_id or "").strip().upper()
    if symbol not in DEFAULT_SYMBOLS:
        raise ValueError("instrument is not in the approved watchlist")
    sess = session or requests.Session()
    payload = _json_get(sess, base_url.rstrip("/") + INSTRUMENTS_PATH, {"instId": symbol})
    rows = payload.get("data")
    row = next((item for item in rows if isinstance(item, Mapping) and item.get("instId") == symbol), None) if isinstance(rows, list) else None
    if row is None:
        raise ReplayDownloadError(f"instrument metadata unavailable for {symbol}")
    required = ("contractValue", "minSize", "lotSize", "tickSize", "contractType", "settleCurrency", "state")
    missing = [key for key in required if row.get(key) in (None, "")]
    if missing:
        raise ReplayDownloadError(f"instrument metadata is incomplete for {symbol}: {missing}")
    if str(row.get("contractType")).lower() != "linear" or str(row.get("settleCurrency")).upper() != "USDT" or str(row.get("state")).lower() != "live":
        raise ReplayDownloadError(f"instrument {symbol} is not a live USDT linear contract")
    return dict(row)


def _parse_funding_event(row: Any, *, symbol: str) -> tuple[str, int, float, str, str] | None:
    """Keep a missing Kind explicit in provenance; do not silently assume API kind 0."""
    if not isinstance(row, Mapping):
        return None
    try:
        ts, rate = int(row["fundingTime"]), float(row["fundingRate"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if ts <= 0 or not math_is_finite(rate):
        return None
    raw_kind = row.get("fundingKind")
    if raw_kind is None or not str(raw_kind).strip():
        return (symbol, ts, rate, "MISSING", "MISSING_API_FIELD")
    kind = str(raw_kind).strip()
    if kind not in {"0", "1"}:
        raise ReplayDownloadError(f"funding history has unsupported fundingKind={kind!r} for {symbol}")
    return (symbol, ts, rate, kind, "EXPLICIT_API")


def _classify_funding_schedule_events(
    events: list[tuple[str, int, float, str, str]], *, symbol: str,
) -> list[tuple[str, int, float, str, str, int]]:
    """Classify regular funding without inferring unsupported dynamic schedules.

    Explicit non-zero fundingKind is rejected. When fundingKind is absent, only
    00:00:00, 08:00:00 and 16:00:00 UTC (±10 seconds) are accepted. The known
    24-hour observed data gap is preserved as a blackout, not treated as cadence.
    """
    ordered = sorted(events, key=lambda item: int(item[1]))
    if len(ordered) < 3:
        raise ReplayDownloadError(f"funding history is too short to verify cadence for {symbol}")
    timestamps = [int(item[1]) for item in ordered]
    if len(set(timestamps)) != len(timestamps):
        raise ReplayDownloadError(f"funding history contains duplicate timestamps for {symbol}")

    # Validate each missing-Kind timestamp before pairwise cadence checks, so an
    # off-boundary record reports the actual schedule violation (e.g. 04:00 UTC).
    for _event_symbol, timestamp, _rate, raw_kind, raw_source in ordered:
        if raw_kind == "MISSING" and raw_source == "MISSING_API_FIELD":
            if not _is_regular_utc_funding_boundary(int(timestamp)):
                raise ReplayDownloadError(
                    f"missing fundingKind timestamp for {symbol} is outside "
                    "the 00:00/08:00/16:00 UTC schedule tolerance"
                )

    for index, (left, right) in enumerate(zip(timestamps, timestamps[1:])):
        if _is_observed_blackout_gap(left, right):
            continue
        delta_ms = right - left
        if delta_ms <= 0:
            raise ReplayDownloadError(f"funding timestamps are not strictly increasing for {symbol}")
        # Only infer cadence across a pair where both records lack the API kind.
        # Explicit API Kind=0 remains authoritative and may follow a dynamic venue cadence.
        left_event, right_event = ordered[index], ordered[index + 1]
        both_missing = (
            left_event[3] == "MISSING" and left_event[4] == "MISSING_API_FIELD"
            and right_event[3] == "MISSING" and right_event[4] == "MISSING_API_FIELD"
        )
        if both_missing and abs(delta_ms - REGULAR_FUNDING_INTERVAL_MS) > 2 * FUNDING_SCHEDULE_TOLERANCE_MS:
            raise ReplayDownloadError(
                f"funding history for {symbol} skips or breaks the fixed 8-hour UTC cadence "
                f"(delta_ms={delta_ms})"
            )

    classified = []
    for event_symbol, timestamp, rate, raw_kind, raw_source in ordered:
        if raw_kind not in {"0", "1", "MISSING"}:
            raise ReplayDownloadError(f"funding kind provenance is invalid for {symbol}")
        if raw_kind != "MISSING" and raw_kind != "0":
            raise ReplayDownloadError(
                f"stock-dividend or non-regular fundingKind={raw_kind!r} "
                f"is not supported by Challenge 1 replay for {symbol}"
            )
        if raw_kind == "0" and raw_source == "EXPLICIT_API":
            kind, source = "0", "EXPLICIT_API"
        elif raw_kind == "MISSING" and raw_source == "MISSING_API_FIELD":
            if not _is_regular_utc_funding_boundary(int(timestamp)):
                raise ReplayDownloadError(
                    f"missing fundingKind timestamp for {symbol} is outside "
                    "the 00:00/08:00/16:00 UTC schedule tolerance"
                )
            kind, source = "0", "SCHEDULE_INFERRED_UTC_8H_BOUNDARY"
        elif raw_kind == "0" and raw_source == "SCHEDULE_INFERRED_UTC_8H_BOUNDARY":
            if not _is_regular_utc_funding_boundary(int(timestamp)):
                raise ReplayDownloadError(
                    f"inferred funding timestamp for {symbol} is outside "
                    "the 00:00/08:00/16:00 UTC schedule tolerance"
                )
            kind, source = "0", "SCHEDULE_INFERRED_UTC_8H_BOUNDARY"
        else:
            raise ReplayDownloadError(f"funding kind provenance is invalid for {symbol}")
        classified.append((
            event_symbol, int(timestamp), float(rate), kind, source,
            REGULAR_FUNDING_INTERVAL_MS,
        ))
    return classified

def download_funding_rates(
    inst_id: str,
    *,
    months: int = 6,
    session: requests.Session | None = None,
    base_url: str = DEMO_REST_URL,
    page_size: int = 100,
) -> list[tuple[Any, ...]]:
    symbol = str(inst_id or "").strip().upper()
    if symbol not in DEFAULT_SYMBOLS or not 1 <= months <= 13 or not 1 <= page_size <= 100:
        raise ValueError("unsupported symbol/months/page_size")
    sess = session or requests.Session()
    now = datetime.now(timezone.utc)
    now_ms = _utc_ms(now)
    start_ms = _utc_ms(_subtract_calendar_months(now, months))
    url = base_url.rstrip("/") + FUNDING_PATH
    cursor: int | None = None
    seen: dict[int, tuple[Any, ...]] = {}
    previous_oldest: int | None = None
    for _ in range(100):
        params: dict[str, Any] = {"instId": symbol, "limit": page_size}
        if cursor is not None:
            params["after"] = cursor
        payload = _json_get(sess, url, params)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ReplayDownloadError("funding endpoint returned a non-list data field")
        valid = []
        for row in rows:
            parsed = _parse_funding_event(row, symbol=symbol)
            if parsed is not None:
                valid.append(parsed)
        if not valid:
            break
        for item in valid:
            if item[1] >= start_ms:
                _record_unique_timestamp(seen, int(item[1]), item, series=f"{symbol} funding")
        oldest = min(item[1] for item in valid)
        if oldest <= start_ms:
            break
        if previous_oldest is not None and oldest >= previous_oldest:
            raise ReplayDownloadError("funding pagination did not move backward")
        previous_oldest, cursor = oldest, oldest
        if len(rows) < page_size:
            break
    else:
        raise ReplayDownloadError("funding pagination exceeded the safety page limit")

    raw_events = [seen[key] for key in sorted(seen)]
    if len(raw_events) < 3:
        raise ReplayDownloadError(f"funding history is too short for {symbol}")
    classified = _classify_funding_schedule_events(raw_events, symbol=symbol)
    normal_intervals = [
        int(right[1]) - int(left[1])
        for left, right in zip(classified, classified[1:])
        if not _is_observed_blackout_gap(int(left[1]), int(right[1]))
    ]
    if not normal_intervals:
        raise ReplayDownloadError(f"funding history has no verified cadence for {symbol}")
    first_max_age = int(classified[0][5] * 1.5)
    last_max_age = int(classified[-1][5] * 1.5)
    funding_times = [int(item[1]) for item in classified]
    if funding_times[0] - start_ms > first_max_age or now_ms - funding_times[-1] > last_max_age:
        raise ReplayDownloadError(f"funding history does not cover the requested window for {symbol}")
    return classified

def math_is_finite(value: float) -> bool:
    return value == value and abs(value) != float("inf")


def initialise_store(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS replay_candles (
            instrument_id TEXT NOT NULL, bar TEXT NOT NULL, ts INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
            volume REAL NOT NULL, confirm TEXT NOT NULL, source TEXT NOT NULL, raw_json TEXT NOT NULL,
            PRIMARY KEY(instrument_id,bar,ts,source)
        );
        CREATE TABLE IF NOT EXISTS replay_instruments (
            instrument_id TEXT PRIMARY KEY, fetched_at_utc TEXT NOT NULL, raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS replay_funding (
            instrument_id TEXT NOT NULL, funding_time INTEGER NOT NULL,
            funding_rate REAL NOT NULL, funding_kind TEXT NOT NULL,
            funding_kind_source TEXT NOT NULL DEFAULT 'EXPLICIT_API',
            funding_interval_ms INTEGER,
            PRIMARY KEY(instrument_id,funding_time)
        );
        CREATE TABLE IF NOT EXISTS replay_price_blackouts (
            instrument_id TEXT NOT NULL, bar TEXT NOT NULL, source TEXT NOT NULL,
            start_ms INTEGER NOT NULL, last_missing_ms INTEGER NOT NULL,
            end_exclusive_ms INTEGER NOT NULL, missing_intervals INTEGER NOT NULL,
            evidence TEXT NOT NULL,
            PRIMARY KEY(instrument_id,bar,source,start_ms,last_missing_ms)
        );
    """)
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(replay_funding)").fetchall()
    }
    if "funding_kind_source" not in columns:
        connection.execute(
            "ALTER TABLE replay_funding ADD COLUMN funding_kind_source TEXT NOT NULL DEFAULT 'EXPLICIT_API'"
        )
    if "funding_interval_ms" not in columns:
        connection.execute("ALTER TABLE replay_funding ADD COLUMN funding_interval_ms INTEGER")
    connection.executemany(
        """INSERT OR REPLACE INTO replay_price_blackouts
           (instrument_id,bar,source,start_ms,last_missing_ms,end_exclusive_ms,missing_intervals,evidence)
           VALUES (?,?,?,?,?,?,?,?)""",
        [
            (
                window["instrument_id"], window["bar"], window["source"],
                int(window["start_ms"]), int(window["last_missing_ms"]),
                int(window["end_exclusive_ms"]), int(window["missing_intervals"]),
                window["evidence"],
            )
            for window in OBSERVED_PRICE_CANDLE_BLACKOUTS
        ],
    )
    connection.commit()


def save_replay(connection: sqlite3.Connection, candles: list[tuple[Any, ...]], funding: list[tuple[Any, ...]], instrument: Mapping[str, Any] | None = None) -> None:
    with connection:
        connection.executemany(
            """INSERT OR REPLACE INTO replay_candles
               (instrument_id,bar,ts,open,high,low,close,volume,confirm,source,raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""", candles,
        )
        if instrument is not None:
            connection.execute(
                "INSERT OR REPLACE INTO replay_instruments(instrument_id,fetched_at_utc,raw_json) VALUES(?,?,?)",
                (str(instrument["instId"]), datetime.now(timezone.utc).isoformat(), json.dumps(dict(instrument), sort_keys=True)),
            )
        normalised = []
        for item in funding:
            if len(item) == 6:
                symbol, timestamp, rate, kind, source, interval_ms = item
            elif len(item) == 4:
                symbol, timestamp, rate, kind = item
                source, interval_ms = "EXPLICIT_API", None
            else:
                raise ReplayDownloadError("unsupported funding persistence tuple")
            normalised.append((symbol, timestamp, rate, kind, source, interval_ms))
        connection.executemany(
            """INSERT OR REPLACE INTO replay_funding
               (instrument_id,funding_time,funding_rate,funding_kind,funding_kind_source,funding_interval_ms)
               VALUES (?,?,?,?,?,?)""", normalised,
        )


def download_watchlist(*, db_path: str, months: int = 7, symbols: tuple[str, ...] = DEFAULT_SYMBOLS) -> dict[str, Any]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    initialise_store(connection)
    report: dict[str, Any] = {
        "db_path": str(path), "environment": "BLOFIN_DEMO_PUBLIC_DATA",
        "symbols": {},
        "funding_kind_policy": "EXPLICIT_API_OR_PROVENANCE_TAGGED_SCHEDULE_INFERENCE",
        "schedule_inference_is_not_explicit_fundingKind": True,
        "blackout_windows": [dict(window) for window in OBSERVED_FUNDING_BLACKOUTS],
        "price_candle_blackouts": [dict(window) for window in OBSERVED_PRICE_CANDLE_BLACKOUTS],
        "price_blackout_policy": "Only exact evidence-backed gaps are allowlisted; no candle is fabricated, interpolated or forward-filled.",
    }
    funding_by_symbol: dict[str, list[tuple[Any, ...]]] = {}
    try:
        for symbol in symbols:
            result: dict[str, Any] = {}
            all_candles: list[tuple[Any, ...]] = []
            for bar in ("15m", "1H", "4H"):
                rows = download_candles(symbol, bar, months=months)
                all_candles.extend(rows)
                result[bar] = len(rows)
            marks = download_candles(symbol, "15m", months=months, mark_price=True)
            all_candles.extend(marks)
            funding = download_funding_rates(symbol, months=months)
            funding_by_symbol[symbol] = funding
            instrument = download_instrument_rules(symbol)
            save_replay(connection, all_candles, funding, instrument)
            result["MARK_15m"] = len(marks)
            result["funding"] = len(funding)
            result["funding_kind_explicit"] = sum(1 for item in funding if item[4] == "EXPLICIT_API")
            result["funding_kind_schedule_inferred"] = sum(
                1 for item in funding if item[4] == "SCHEDULE_INFERRED_UTC_8H_BOUNDARY"
            )
            report["symbols"][symbol] = result
        _validate_common_funding_blackout(funding_by_symbol)
        report["common_blackout_verified_for_all_symbols"] = True
    finally:
        connection.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Download BloFin Demo public replay data; no credentials and no order writes")
    parser.add_argument("--months", type=int, default=7, help="Calendar months to fetch (maximum 13); one extra month provides indicator warm-up for the six-month walk-forward window.")
    parser.add_argument("--db", default="data/blofin_replay.sqlite3")
    args = parser.parse_args()
    print(json.dumps(download_watchlist(db_path=args.db, months=args.months), indent=2))


if __name__ == "__main__":
    main()
