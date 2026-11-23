"""Isolated, read-only BloFin market-data ingestion for Challenge 1 V2.

The module has no order-writing capability. It requires a single exchange-derived
as-of timestamp, completed candles, a classified regular funding event, and exact
contract metadata before it calls the V2 analysis engine.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping

import pandas as pd

from types import SimpleNamespace

from .models import AssetSpec, Signal
from .ai_supervisor import run_challenge1_v2_supervisor_reviews
from .challenge1_v2 import (
    analyze_signal, build_fast_trigger_signal, build_early_wave3_signal,
    challenge1_v2_execution_evidence_ready,
)
from .challenge1_strategy import analyze_blofin_challenge1

BAR_INTERVAL_MS = {"5m": 5 * 60_000, "15m": 15 * 60_000, "1H": 60 * 60_000, "4H": 4 * 60 * 60_000}
MINIMUM_ROWS = {"5m": 60, "15m": 70, "1H": 205, "4H": 205, "BTC_15m": 40}
FUNDING_MAX_AGE_MS = 9 * 60 * 60_000
MAX_QUOTE_AGE_MS = 60_000


class Challenge1V2DataBlocked(RuntimeError):
    """V2 market data cannot be proven complete, fresh, and correctly classified."""


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or value is None:
        raise Challenge1V2DataBlocked(f"{field} is missing or invalid")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise Challenge1V2DataBlocked(f"{field} is missing or invalid") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise Challenge1V2DataBlocked(f"{field} is missing or invalid")
    return number


def _contract_specs(client: Any, instrument_id: str) -> dict[str, Any]:
    instrument = client.get_tradable_instrument(instrument_id)
    if not isinstance(instrument, Mapping):
        raise Challenge1V2DataBlocked("Exact linear USDT contract specifications are unavailable")
    if (
        instrument.get("instId") != instrument_id
        or instrument.get("apiPermission") != "full"
        or str(instrument.get("state") or "").lower() != "live"
        or str(instrument.get("contractType") or "").lower() != "linear"
        or str(instrument.get("settleCurrency") or "").upper() != "USDT"
    ):
        raise Challenge1V2DataBlocked("Contract identity or trading eligibility is not verified")
    specs = {}
    for field in ("contractValue", "minSize", "lotSize", "tickSize"):
        raw = instrument.get(field)
        value = _finite(raw, f"contract.{field}", positive=True)
        specs[field] = str(raw).strip()
        if not specs[field]:
            raise Challenge1V2DataBlocked(f"contract.{field} is missing")
    return {**dict(instrument), **specs}


def _market_quote(client: Any, instrument_id: str, local_now_ms: int) -> dict[str, Any]:
    tickers = client.get_tickers(inst_id=instrument_id)
    marks = client.get_mark_price(inst_id=instrument_id)
    if not isinstance(tickers, list) or not tickers or not isinstance(tickers[0], Mapping):
        raise Challenge1V2DataBlocked("Fresh ticker with best bid/ask is unavailable")
    if not isinstance(marks, list) or not marks or not isinstance(marks[0], Mapping):
        raise Challenge1V2DataBlocked("Fresh mark-price quote is unavailable")
    ticker, mark = tickers[0], marks[0]
    try:
        ticker_ts = int(ticker.get("ts"))
        mark_ts = int(mark.get("ts"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise Challenge1V2DataBlocked("Ticker or mark-price timestamp is missing") from exc
    if ticker_ts <= 0 or mark_ts <= 0:
        raise Challenge1V2DataBlocked("Ticker or mark-price timestamp is invalid")
    if abs(local_now_ms - ticker_ts) > MAX_QUOTE_AGE_MS:
        raise Challenge1V2DataBlocked("Exchange ticker is stale or its clock differs from local time")
    if abs(local_now_ms - mark_ts) > MAX_QUOTE_AGE_MS:
        raise Challenge1V2DataBlocked("Exchange mark price is stale or its clock differs from local time")
    last = _finite(ticker.get("last"), "ticker.last", positive=True)
    bid = _finite(ticker.get("bidPrice"), "ticker.bidPrice", positive=True)
    ask = _finite(ticker.get("askPrice"), "ticker.askPrice", positive=True)
    mark_price = _finite(mark.get("markPrice"), "mark.markPrice", positive=True)
    if ask < bid:
        raise Challenge1V2DataBlocked("Ticker bid/ask is crossed")
    mid = (bid + ask) / 2.0
    spread_fraction = (ask - bid) / mid
    if not math.isfinite(spread_fraction) or spread_fraction < 0 or spread_fraction > 0.0004:
        raise Challenge1V2DataBlocked("Ticker spread is outside the V2 execution guard")
    if abs(mark_price - last) / last > 0.005:
        raise Challenge1V2DataBlocked("Mark price materially diverges from the last trade")
    return {
        "instrument_id": instrument_id,
        "ticker_ts_ms": ticker_ts,
        "mark_ts_ms": mark_ts,
        "asof_ts_ms": ticker_ts,
        "last": last,
        "bidPrice": bid,
        "askPrice": ask,
        "markPrice": mark_price,
        "spread_fraction": spread_fraction,
        "quote_age_ms": max(0, local_now_ms - ticker_ts),
    }


def get_fresh_execution_quote(
    client: Any, instrument_id: str, *, local_now_ms: int | None = None,
) -> dict[str, Any]:
    """Return a timestamp-validated bid/ask/mark snapshot for dry-run sizing."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) if local_now_ms is None else int(local_now_ms)
    return _market_quote(client, instrument_id, now_ms)


def _closed_frame(
    rows: Any, *, bar: str, asof_ms: int, minimum_rows: int,
) -> pd.DataFrame:
    if not isinstance(rows, list):
        raise Challenge1V2DataBlocked(f"{bar} candle response is not a list")
    interval = BAR_INTERVAL_MS[bar]
    indexed: dict[int, tuple[float, float, float, float, float]] = {}
    for position, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            raise Challenge1V2DataBlocked(f"{bar} candle row {position} has an invalid schema")
        confirmation = str(row[8]).strip()
        if confirmation == "0":
            continue
        if confirmation != "1":
            raise Challenge1V2DataBlocked(f"{bar} candle confirmation flag is unsupported")
        try:
            timestamp = int(row[0])
        except (TypeError, ValueError, OverflowError) as exc:
            raise Challenge1V2DataBlocked(f"{bar} candle timestamp is invalid") from exc
        if timestamp <= 0:
            raise Challenge1V2DataBlocked(f"{bar} candle timestamp is invalid")
        close_ts = timestamp + interval
        if close_ts > asof_ms:
            # A provider row marked complete but beyond the shared as-of is never used.
            continue
        try:
            opened, high, low, close, volume = (_finite(row[i], f"{bar}.ohlcv[{i}]") for i in range(1, 6))
        except Challenge1V2DataBlocked:
            raise
        if min(opened, high, low, close) <= 0 or volume < 0:
            raise Challenge1V2DataBlocked(f"{bar} candle has non-positive prices or negative volume")
        if high < max(opened, low, close) or low > min(opened, high, close):
            raise Challenge1V2DataBlocked(f"{bar} candle OHLC range is inconsistent")
        if timestamp in indexed:
            raise Challenge1V2DataBlocked(f"{bar} candle timestamps contain duplicates")
        indexed[timestamp] = (opened, high, low, close, volume)

    stamps = sorted(indexed)
    if len(stamps) < minimum_rows:
        raise Challenge1V2DataBlocked(f"{bar} needs {minimum_rows} confirmed candles; received {len(stamps)}")
    tail_stamps = stamps[-minimum_rows:]
    if any(right - left != interval for left, right in zip(tail_stamps, tail_stamps[1:])):
        raise Challenge1V2DataBlocked(f"{bar} confirmed candle history has a gap")
    latest_close_ms = tail_stamps[-1] + interval
    latest_age = asof_ms - latest_close_ms
    if latest_age < 0 or latest_age > interval:
        raise Challenge1V2DataBlocked(f"{bar} latest confirmed candle is stale")
    values = [indexed[stamp] for stamp in stamps]
    frame = pd.DataFrame(
        values, columns=["open", "high", "low", "close", "volume"],
        index=pd.to_datetime(stamps, unit="ms", utc=True),
    )
    frame.index.name = "timestamp"
    return frame.astype(float)


STANDARD_CRYPTO_PERPETUALS = frozenset({"BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT"})


def _regular_funding(
    client: Any, instrument_id: str, *, asof_ms: int,
) -> dict[str, Any]:
    """Validate funding history; infer the standard class only for exact known crypto perps."""
    rows = client.get_funding_rate_history(inst_id=instrument_id, limit=100)
    if not isinstance(rows, list) or not rows:
        raise Challenge1V2DataBlocked("Funding history is unavailable")
    parsed = []
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise Challenge1V2DataBlocked("Funding history contains a malformed event")
        if str(row.get("instId") or "").strip().upper() != instrument_id:
            raise Challenge1V2DataBlocked("Funding event instrument identity is missing or mismatched")
        try:
            timestamp = int(row["fundingTime"])
            rate = _finite(row["fundingRate"], "fundingRate")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise Challenge1V2DataBlocked("Funding event timestamp or rate is missing") from exc
        raw_kind = row.get("fundingKind")
        if raw_kind is None or not str(raw_kind).strip():
            if instrument_id not in STANDARD_CRYPTO_PERPETUALS:
                raise Challenge1V2DataBlocked("FundingKind is missing for a non-standard instrument")
            kind = "0"
            classification = "STANDARD_CRYPTO_FUNDING"
        else:
            kind = str(raw_kind).strip()
            if kind not in {"0", "1"}:
                raise Challenge1V2DataBlocked("FundingKind is unsupported")
            classification = "STANDARD_CRYPTO_FUNDING" if kind == "0" else "NON_STANDARD_FUNDING_EVENT"
        if timestamp <= 0 or timestamp in seen:
            raise Challenge1V2DataBlocked("Funding history has an invalid or duplicate timestamp")
        seen.add(timestamp)
        parsed.append((timestamp, rate, kind, classification))
    latest = max(parsed, key=lambda item: item[0])
    timestamp, rate, kind, classification = latest
    age_ms = asof_ms - timestamp
    if age_ms < 0:
        raise Challenge1V2DataBlocked("Latest funding event is future-dated")
    if age_ms > FUNDING_MAX_AGE_MS:
        raise Challenge1V2DataBlocked("Latest funding event is stale")
    if kind != "0":
        raise Challenge1V2DataBlocked("Latest funding event is not regular fundingKind=0")
    return {
        "funding_rate": rate,
        "funding_kind": kind,
        "funding_classification": classification,
        "funding_event_time_ms": timestamp,
        "funding_max_age_ms": FUNDING_MAX_AGE_MS,
        "funding_age_ms": age_ms,
    }


def analyze_challenge1_v2_market(
    client: Any, instrument_id: str, *, local_now_ms: int | None = None,
    last_three_results: tuple[int, int, int] = (-1, -1, -1),
) -> dict[str, Any]:
    """Fetch the entire V2 evidence set and invoke the strategy at one shared as-of."""
    symbol = str(instrument_id or "").strip().upper()
    if not symbol.endswith("-USDT") or len(symbol) <= 5:
        raise Challenge1V2DataBlocked("V2 instrument must be a USDT linear perpetual")
    local_now = int(datetime.now(timezone.utc).timestamp() * 1000) if local_now_ms is None else int(local_now_ms)
    contract = _contract_specs(client, symbol)
    quote = _market_quote(client, symbol, local_now)
    asof_ms = int(quote["asof_ts_ms"])
    asof = pd.Timestamp(datetime.fromtimestamp(asof_ms / 1000.0, tz=timezone.utc))
    frame_1h = _closed_frame(
        client.get_candles(symbol, bar="1H", limit=300),
        bar="1H", asof_ms=asof_ms, minimum_rows=MINIMUM_ROWS["1H"],
    )
    frame_4h = _closed_frame(
        client.get_candles(symbol, bar="4H", limit=300),
        bar="4H", asof_ms=asof_ms, minimum_rows=MINIMUM_ROWS["4H"],
    )
    frame_5m = _closed_frame(
        client.get_candles(symbol, bar="5m", limit=300),
        bar="5m", asof_ms=asof_ms, minimum_rows=MINIMUM_ROWS["5m"],
    )
    frame_15m = _closed_frame(
        client.get_candles(symbol, bar="15m", limit=300),
        bar="15m", asof_ms=asof_ms, minimum_rows=MINIMUM_ROWS["15m"],
    )
    btc_15m = frame_15m if symbol == "BTC-USDT" else _closed_frame(
        client.get_candles("BTC-USDT", bar="15m", limit=300),
        bar="15m", asof_ms=asof_ms, minimum_rows=MINIMUM_ROWS["BTC_15m"],
    )
    funding = _regular_funding(client, symbol, asof_ms=asof_ms)
    if not (frame_1h.index[-1] + pd.Timedelta(hours=1) <= asof):
        raise Challenge1V2DataBlocked("1H frame exceeds the common as-of boundary")
    if not (frame_4h.index[-1] + pd.Timedelta(hours=4) <= asof):
        raise Challenge1V2DataBlocked("4H frame exceeds the common as-of boundary")
    if not (frame_5m.index[-1] + pd.Timedelta(minutes=5) <= asof):
        raise Challenge1V2DataBlocked("5m frame exceeds the common as-of boundary")
    if not (frame_15m.index[-1] + pd.Timedelta(minutes=15) <= asof):
        raise Challenge1V2DataBlocked("15m frame exceeds the common as-of boundary")
    record = analyze_signal(
        instrument_id=symbol,
        frame_1h=frame_1h,
        frame_4h=frame_4h,
        frame_15m=frame_15m,
        btc_15m=btc_15m,
        asof=asof,
        funding_rate=funding["funding_rate"],
        funding_kind=funding["funding_kind"],
        funding_event_time_ms=funding["funding_event_time_ms"],
        funding_max_age_ms=funding["funding_max_age_ms"],
        spread_fraction=quote["spread_fraction"],
        last_three_results=last_three_results,
    )
    # Run the dashboard's existing non-repainting 5m candle/Elliott engine
    # against the same exchange-derived as-of boundary. 15m trend alignment
    # is checked by the V2 fast-path builder before any AI reviewer is called.
    fast_trigger_record = analyze_blofin_challenge1(
        client, symbol, now_ms=asof_ms,
    )
    early_w3_path = build_early_wave3_signal(
        instrument_id=symbol, frame_5m=frame_5m, frame_15m=frame_15m,
        asof=asof, entry_price=quote["last"],
        funding_rate=funding["funding_rate"], funding_kind=funding["funding_kind"],
        funding_event_time_ms=funding["funding_event_time_ms"],
        funding_max_age_ms=funding["funding_max_age_ms"],
        spread_fraction=quote["spread_fraction"],
    )
    if early_w3_path.get("signal") in {"LONG", "SHORT"}:
        record = early_w3_path
    fast_path = None
    if not record.get("early_w3_active") and fast_trigger_record.get("signal") in {"LONG", "SHORT"}:
        fast_path = build_fast_trigger_signal(
            instrument_id=symbol,
            frame_1h=frame_1h,
            frame_4h=frame_4h,
            frame_15m=frame_15m,
            btc_15m=btc_15m,
            asof=asof,
            funding_rate=funding["funding_rate"],
            funding_kind=funding["funding_kind"],
            funding_event_time_ms=funding["funding_event_time_ms"],
            funding_max_age_ms=funding["funding_max_age_ms"],
            spread_fraction=quote["spread_fraction"],
            fast_trigger_record=fast_trigger_record,
        )
        if fast_path.get("signal") in {"LONG", "SHORT"}:
            record = fast_path
    record = dict(record)
    record["early_w3_diagnostic"] = {"signal": early_w3_path.get("signal"), "reason": early_w3_path.get("reason"), "selected": bool(early_w3_path.get("early_w3_active"))}
    record["fast_trigger_diagnostic"] = {
        "engine": "CHALLENGE_1_LOCAL_RULES_V5_5M_ELLIOTT_AWARE",
        "signal": fast_trigger_record.get("signal"),
        "signal_id": fast_trigger_record.get("signal_id"),
        "decision_reason_fa": fast_trigger_record.get("reason_fa"),
        "fast_path_selected": bool(fast_path and fast_path.get("signal") in {"LONG", "SHORT"}),
        "fast_path_rejection": (
            fast_path.get("reason") if isinstance(fast_path, Mapping)
            and fast_path.get("signal") == "NO_TRADE" else None
        ),
    }
    record = dict(record)
    record["execution_profile"] = "CHALLENGE_1_V2"
    record["contract_specs"] = dict(contract)
    record["market_quote"] = quote
    record["market_data_proof"] = {
        "source": "BLOFIN_DEMO_REST",
        "asof_utc": asof.isoformat(),
        "signal_rule": ("CLOSED_5M_15M_EARLY_W3_BREAKOUT" if record.get("early_w3_active") else ("DASHBOARD_CONFIRMED_5M_ELLIOTT_PLUS_COMPLETED_15M_EMA21_ALIGNMENT" if record.get("fast_trigger_active") else "COMPLETED_1H_ADX14_GTE_22_AND_CLOSE_VS_EMA21")),
        "signal_timeframe": "5m" if (record.get("fast_trigger_active") or record.get("early_w3_active")) else "1H",
        "entry_execution_timeframe": "15m",
        "timeframes": {
            "1H": {"rows": len(frame_1h), "latest_open_utc": frame_1h.index[-1].isoformat()},
            "4H": {"rows": len(frame_4h), "latest_open_utc": frame_4h.index[-1].isoformat()},
            "15m": {"rows": len(frame_15m), "latest_open_utc": frame_15m.index[-1].isoformat()},
            "5m": {"rows": len(frame_5m), "latest_open_utc": frame_5m.index[-1].isoformat(), "source": "BLOFIN_DEMO_REST_AND_LOCAL_WAVE3", "signal_id": (fast_trigger_record.get("signal_id") if fast_trigger_record.get("signal") in {"LONG", "SHORT"} else None), "selected": bool(record.get("fast_trigger_active") or record.get("early_w3_active"))},
            "BTC_15m": {"rows": len(btc_15m), "latest_open_utc": btc_15m.index[-1].isoformat()},
        },
        "funding": funding,
        "funding_classification": funding["funding_classification"],
        "contract_specs_verified": True,
        "common_asof": True,
    }
    if record.get("signal") not in {"LONG", "SHORT"}:
        record.update({
            "signal": "NO_TRADE",
            "status": "NO_TRADE",
            "reason": record.get("reason") or "V2 analysis did not produce a directional setup",
            "orders_submitted": False,
        })
        return record
    regime = record.get("regime")
    if not isinstance(regime, Mapping):
        record.update({
            "signal": "NO_TRADE",
            "status": "NO_TRADE",
            "reason": "1H trend regime evidence is missing",
            "orders_submitted": False,
        })
        return record
    ready, reason = challenge1_v2_execution_evidence_ready(record)
    if not ready:
        raise Challenge1V2DataBlocked("V2 execution evidence failed closed: " + reason)
    record["status"] = "ANALYSIS_READY"
    record["orders_submitted"] = False
    asset = AssetSpec.parse("perp:" + symbol.replace("-", ""))
    analysis = SimpleNamespace(
        signal=Signal(str(record["signal"])), asset=asset.raw,
        metrics={"execution_profile":"CHALLENGE_1_V2",
                 "challenge1_v2_evidence":record.get("challenge1_v2_evidence"),
                 "market_data_proof":record.get("market_data_proof")},
        entry_price=record.get("entry"), stop_loss=record.get("stop_loss"),
        take_profit=record.get("take_profit"),
        confirmation_score=record.get("confidence_score"),
        estimated_risk_score=None, to_record=lambda:record,
    )
    reviewers = run_challenge1_v2_supervisor_reviews(
        analysis, asset, signal_id=str(record.get("signal_id") or ""),
    )
    record.update({
        "ai_review":reviewers.get("ai_review"),
        "second_ai_review":reviewers.get("second_ai_review"),
        "ai_reviews":reviewers.get("ai_reviews") or [],
        "ai_consensus":reviewers.get("ai_consensus") or {},
        "ai_status":reviewers.get("status") or "UNKNOWN",
        "ai_review_reason":reviewers.get("reason_fa"),
        "orders_submitted":False,
    })
    if reviewers.get("approved") is not True:
        record["status"]="AI_REVIEW_BLOCKED"
        record["reason"]=reviewers.get("reason_fa") or "Two independent live AI approvals were not received"
    return record
