"""Deterministic, closed-candle Challenge 1 strategy for BloFin Demo.

Only completed 5m candles create setups. No higher timeframe is fetched or used.
All entry decisions are local, pattern-based, and reproducible.
"""
from __future__ import annotations

import math
import time
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .chart_patterns import monitor_chart_patterns
from .elliott_waves import monitor_elliott_waves

DEFAULT_CHALLENGE1_INSTRUMENTS = (
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "LINK-USDT",
)

_INTERVAL_MS = {"5m": 5 * 60 * 1000}
_ENGINE_VERSION = "CHALLENGE_1_LOCAL_RULES_V5_5M_ELLIOTT_AWARE"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _completed_frame(
    client: Any,
    *,
    inst_id: str,
    bar: str,
    now_ms: int | None = None,
    minimum_rows: int = 60,
) -> pd.DataFrame:
    """Fetch, validate, sort, and retain only exchange-confirmed closed candles."""
    interval_ms = _INTERVAL_MS.get(str(bar))
    if interval_ms is None:
        raise ValueError("Unsupported Challenge 1 candle interval.")
    if isinstance(minimum_rows, bool) or int(minimum_rows) < 30:
        raise ValueError("minimum_rows must be at least 30.")
    now_value = int(now_ms if now_ms is not None else time.time() * 1000)
    raw = client.get_candles(inst_id, bar=bar, limit=min(300, max(100, int(minimum_rows) + 20)))
    if not isinstance(raw, list):
        raise ValueError("BloFin candle response is not a list.")

    rows: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            timestamp = item.get("ts", item.get("timestamp"))
            opened = item.get("open", item.get("o"))
            high = item.get("high", item.get("h"))
            low = item.get("low", item.get("l"))
            close = item.get("close", item.get("c"))
            volume = item.get("volume", item.get("vol", item.get("v")))
            quote_volume = item.get("quote_volume", item.get("volCcy", item.get("volCcyQuote")))
            confirmed = item.get("confirm", item.get("confirmed"))
        elif isinstance(item, (list, tuple)) and len(item) >= 9:
            timestamp, opened, high, low, close, volume, quote_volume = item[:7]
            confirmed = item[8]
        else:
            continue

        try:
            stamp = int(float(timestamp))
        except (TypeError, ValueError, OverflowError):
            continue
        is_confirmed = str(confirmed).strip().lower() in {"1", "true", "yes"}
        if not is_confirmed or stamp <= 0 or stamp + interval_ms > now_value:
            continue

        values = [_number(value) for value in (opened, high, low, close, volume)]
        if any(value is None for value in values):
            continue
        o, h, lo, c, vol = values
        if min(o, h, lo, c) <= 0 or vol < 0:
            continue
        if h < max(o, c, lo) or lo > min(o, c, h) or h <= lo:
            continue
        quote = _number(quote_volume)
        rows.append({
            "timestamp": stamp,
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": vol,
            "quote_volume": quote if quote is not None and quote >= 0 else np.nan,
        })

    if not rows:
        raise ValueError(f"No exchange-confirmed {bar} candles were returned.")
    frame = pd.DataFrame(rows).drop_duplicates(subset=["timestamp"], keep="last")
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    frame = frame.loc[:, ["timestamp", "open", "high", "low", "close", "volume", "quote_volume"]]
    # Closed flags do not prove freshness or continuity. Retain Staging's
    # existing stale-feed and recent-gap guards, applied to the 5m interval.
    timestamps_ms = frame["timestamp"].to_numpy(dtype="int64")
    latest_close_ms = int(timestamps_ms[-1]) + interval_ms
    latest_age_ms = now_value - latest_close_ms
    if latest_age_ms > interval_ms + 60_000:
        raise ValueError(
            f"Latest confirmed {bar} candle is stale ({latest_age_ms} ms old)."
        )
    recent_timestamps_ms = timestamps_ms[-40:]
    if len(recent_timestamps_ms) > 1 and np.any(
        np.diff(recent_timestamps_ms) != interval_ms
    ):
        raise ValueError(
            f"Recent {bar} candles contain a missing interval; "
            "refusing to analyze discontinuous data."
        )
    frame.index = pd.to_datetime(frame.pop("timestamp"), unit="ms", utc=True)
    if len(frame) < int(minimum_rows):
        raise ValueError(
            f"Only {len(frame)} completed {bar} candles are available; "
            f"{minimum_rows} are required."
        )
    return frame.astype({
        "open": "float64", "high": "float64", "low": "float64",
        "close": "float64", "volume": "float64",
    })


def _confirmed_pattern_candidates(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Detect directional candle patterns using only the latest completed bars."""
    required = {"open", "high", "low", "close", "volume"}
    if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns) or len(frame) < 12:
        return []
    values = frame.loc[:, ["open", "high", "low", "close", "volume"]].tail(40).apply(
        pd.to_numeric, errors="coerce"
    )
    if values.isna().any().any() or not np.isfinite(values.to_numpy(dtype=float)).all():
        return []

    candidates: list[dict[str, Any]] = []

    def add(name: str, direction: str, score: int, detail: str) -> None:
        candidates.append({
            "name_en": name,
            "direction": direction,
            "score": int(score),
            "category": "CANDLESTICK",
            "detail": detail,
            "bar_open_utc": frame.index[-1].isoformat(),
        })

    prev, last = values.iloc[-2], values.iloc[-1]
    po, pc = float(prev["open"]), float(prev["close"])
    lo, lc = float(last["open"]), float(last["close"])
    prev_bull, prev_bear = pc > po, pc < po
    last_bull, last_bear = lc > lo, lc < lo
    prev_body, last_body = abs(pc - po), abs(lc - lo)

    if prev_bear and last_bull and lo <= pc and lc >= po and last_body >= prev_body * 0.8:
        add("Bullish Engulfing", "BULLISH", 13, "The completed bullish body engulfs the prior bearish body.")
    if prev_bull and last_bear and lo >= pc and lc <= po and last_body >= prev_body * 0.8:
        add("Bearish Engulfing", "BEARISH", -13, "The completed bearish body engulfs the prior bullish body.")
    if prev_bear and last_bull and lo < pc and lc > (po + pc) / 2 and lc < po:
        add("Piercing Line", "BULLISH", 11, "The bullish close recovered more than half of the prior real body.")
    if prev_bull and last_bear and lo > pc and lc < (po + pc) / 2 and lc > po:
        add("Dark Cloud Cover", "BEARISH", -11, "The bearish close crossed below half of the prior real body.")
    if prev_bear and last_bull and min(po, pc) < lo and max(lo, lc) < max(po, pc) and lc < po:
        add("Bullish Harami", "BULLISH", 9, "The latest bullish real body is contained inside the prior bearish body.")
    if prev_bull and last_bear and min(po, pc) < lo and max(lo, lc) < max(po, pc) and lc > po:
        add("Bearish Harami", "BEARISH", -9, "The latest bearish real body is contained inside the prior bullish body.")

    first, middle = values.iloc[-3], values.iloc[-2]
    fo, fc = float(first["open"]), float(first["close"])
    mo, mc = float(middle["open"]), float(middle["close"])
    first_body, middle_body = abs(fc - fo), abs(mc - mo)
    if (
        fo > fc and first_body > 0 and middle_body <= first_body * 0.45
        and last_bull and lc >= (fo + fc) / 2 and mc <= fc + first_body * 0.15
    ):
        add("Morning Star", "BULLISH", 14, "A small middle candle is followed by a bullish recovery through the first-body midpoint.")
    if (
        fc > fo and first_body > 0 and middle_body <= first_body * 0.45
        and last_bear and lc <= (fo + fc) / 2 and mc >= fc - first_body * 0.15
    ):
        add("Evening Star", "BEARISH", -14, "A small middle candle is followed by a bearish recovery through the first-body midpoint.")

    trio = values.iloc[-3:]
    trio_open = trio["open"].to_numpy(dtype=float)
    trio_close = trio["close"].to_numpy(dtype=float)
    trio_range = trio["high"].to_numpy(dtype=float) - trio["low"].to_numpy(dtype=float)
    trio_body = np.abs(trio_close - trio_open)
    opens_inside_prior = (
        np.all(trio_open[1:] >= np.minimum(trio_open[:-1], trio_close[:-1]))
        and np.all(trio_open[1:] <= np.maximum(trio_open[:-1], trio_close[:-1]))
    )
    if (
        np.all(trio_close > trio_open) and np.all(np.diff(trio_close) > 0)
        and np.all(trio_body >= trio_range * 0.45) and opens_inside_prior
    ):
        add("Three White Soldiers", "BULLISH", 14, "Three rising completed bullish candles have substantial bodies.")
    if (
        np.all(trio_close < trio_open) and np.all(np.diff(trio_close) < 0)
        and np.all(trio_body >= trio_range * 0.45) and opens_inside_prior
    ):
        add("Three Black Crows", "BEARISH", -14, "Three falling completed bearish candles have substantial bodies.")

    candle_range = float(last["high"] - last["low"])
    body_floor = max(last_body, candle_range * 0.01)
    lower_shadow = float(min(lo, lc) - last["low"])
    upper_shadow = float(last["high"] - max(lo, lc))
    prior_closes = values["close"].iloc[-8:-1]
    downtrend = len(prior_closes) >= 5 and float(prior_closes.iloc[-1]) < float(prior_closes.iloc[0]) * 0.995
    uptrend = len(prior_closes) >= 5 and float(prior_closes.iloc[-1]) > float(prior_closes.iloc[0]) * 1.005
    small_body = last_body <= candle_range * 0.35
    if downtrend and small_body and lower_shadow >= body_floor * 2 and upper_shadow <= max(body_floor * 0.5, candle_range * 0.08):
        add("Hammer", "BULLISH", 10, "A long lower rejection wick formed after a preceding down move.")
    if uptrend and small_body and upper_shadow >= body_floor * 2 and lower_shadow <= max(body_floor * 0.5, candle_range * 0.08):
        add("Shooting Star", "BEARISH", -10, "A long upper rejection wick formed after a preceding up move.")

    prior = values.iloc[-21:-1]
    if len(prior) == 20:
        prior_high, prior_low = float(prior["high"].max()), float(prior["low"].min())
        if float(last["close"]) > prior_high and last_bull:
            add("20-Bar Range Breakout", "BULLISH", 12, "A completed bullish candle closed above the prior 20-bar high.")
        if float(last["close"]) < prior_low and last_bear:
            add("20-Bar Range Breakdown", "BEARISH", -12, "A completed bearish candle closed below the prior 20-bar low.")

    return sorted(candidates, key=lambda item: abs(int(item.get("score") or 0)), reverse=True)


def _volume_confirmation(frame: pd.DataFrame, direction: str, *, minimum_ratio: float = 1.2) -> dict[str, Any]:
    """Require relative volume and a close near the directional end of the candle."""
    if not isinstance(frame, pd.DataFrame) or len(frame) < 21:
        return {"confirmed": False, "volume_ratio": None, "close_location": None,
                "reason": "Twenty prior completed volume bars are required."}
    recent = frame.loc[:, ["high", "low", "close", "volume"]].tail(21).apply(
        pd.to_numeric, errors="coerce"
    )
    if recent.isna().any().any() or not np.isfinite(recent.to_numpy(dtype=float)).all():
        return {"confirmed": False, "volume_ratio": None, "close_location": None,
                "reason": "Recent OHLCV data is incomplete."}
    baseline = float(recent["volume"].iloc[:-1].median())
    candle_range = float(recent["high"].iloc[-1] - recent["low"].iloc[-1])
    if baseline <= 0 or candle_range <= 0:
        return {"confirmed": False, "volume_ratio": None, "close_location": None,
                "reason": "A positive volume baseline and candle range are required."}
    ratio = float(recent["volume"].iloc[-1] / baseline)
    location = float((recent["close"].iloc[-1] - recent["low"].iloc[-1]) / candle_range)
    direction_ok = location >= 0.65 if direction == "BULLISH" else location <= 0.35
    confirmed = ratio >= float(minimum_ratio) and direction_ok
    return {
        "confirmed": bool(confirmed),
        "volume_ratio": round(ratio, 4),
        "close_location": round(location, 4),
        "minimum_volume_ratio": float(minimum_ratio),
        "reason": (
            "Relative volume and directional candle-close location are confirmed."
            if confirmed else
            "Relative volume or directional candle-close location did not meet the threshold."
        ),
    }


def _atr(frame: pd.DataFrame, period: int = 14) -> float | None:
    if not isinstance(frame, pd.DataFrame) or len(frame) < period + 1:
        return None
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    previous = np.r_[close[0], close[:-1]]
    true_range = np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))
    value = float(np.mean(true_range[-period:]))
    return value if math.isfinite(value) and value > 0 else None


def _time_label(frame: pd.DataFrame) -> str:
    return frame.index[-1].isoformat()


def _candles_for_report(frame: pd.DataFrame, *, limit: int) -> list[dict[str, Any]]:
    """Serialize only validated, exchange-confirmed candles for reproducible reports."""
    rows: list[dict[str, Any]] = []
    for stamp, row in frame.tail(max(1, min(int(limit), 200))).iterrows():
        quote_volume = _number(row.get("quote_volume"))
        rows.append({
            "time_utc": stamp.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "quote_volume": quote_volume,
        })
    return rows




def challenge1_entry_evidence_ready(record: Mapping[str, Any]) -> tuple[bool, str]:
    """Validate one immutable, completed 5m-only pattern decision before Demo sizing."""
    if not isinstance(record, Mapping):
        return False, "گزارش اختصاصی کندل و الگو موجود نیست."
    evidence = record.get("challenge1_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    execution = record.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    signal = str(record.get("signal") or "").upper()
    expected_direction = "BULLISH" if signal == "LONG" else "BEARISH" if signal == "SHORT" else ""
    if not expected_direction or execution.get("status") != "PASS":
        return False, "سیگنال جهت‌دار و تأیید الگوی اختصاصی ساخته نشده است."
    if str(execution.get("direction") or "").upper() != signal:
        return False, "جهت الگوی انتخاب‌شده با سیگنال ربات یکسان نیست."
    if evidence.get("decision_engine_version") != _ENGINE_VERSION:
        return False, "نسخه موتور ورود Challenge 1 معتبر نیست."
    if (
        evidence.get("setup_timeframe") != "5m"
        or evidence.get("completed_candles_only") is not True
        or evidence.get("higher_timeframes_used") is not False
        or evidence.get("legacy_12_gates_used_for_entry") is not False
        or evidence.get("external_ai_required") is not True
        or evidence.get("tinyfish_required") is not False
    ):
        return False, "ورودی Challenge 1 باید فقط بر پایه کندل ۵ دقیقه‌ای باشد."
    if evidence.get("decision") != signal:
        return False, "تصمیم ثبت‌شده با جهت سیگنال یکسان نیست."
    if any(str(key).startswith("candles_") and key != "candles_5m" for key in evidence):
        return False, "شواهد Challenge 1 نباید کندل تایم‌فریم دیگری داشته باشد."

    wave = evidence.get("elliott_wave")
    wave = wave if isinstance(wave, Mapping) else {}
    wave_status = str(wave.get("status") or "").upper()
    wave_direction = str(wave.get("pattern") or "").upper()
    wave_phase = str(wave.get("phase") or "").upper()
    wave_pivots = _number(wave.get("confirmed_pivot_count"))
    if (
        wave_status not in {"CANDIDATE", "CONFIRMED"}
        or wave_direction != expected_direction
        or wave_phase not in {
            "WAVES_1_AND_2_CONFIRMED", "WAVE_3_CONFIRMED",
            "WAVE_4_CONFIRMED", "FIVE_WAVE_IMPULSE_CONFIRMED",
        }
        or wave_pivots is None or wave_pivots < 3
        or _number(wave.get("invalidation_price")) is None
        or evidence.get("wave_alignment") != "ALIGNED"
    ):
        return False, "ساختار موج الیوت ۵ دقیقه‌ای تأیید یا با جهت الگوی ورود هم‌سو نیست."

    chosen = evidence.get("chosen_pattern")
    if not isinstance(chosen, Mapping):
        return False, "الگوی کندلی یا ساختار نموداری منتخب وجود ندارد."
    pattern_name = str(chosen.get("name_en") or "").strip()
    category = str(chosen.get("category") or "").upper()
    if not pattern_name or category not in {"CANDLESTICK", "CHART_STRUCTURE"}:
        return False, "نوع الگوی منتخب معتبر نیست."
    if (
        str(chosen.get("direction") or "").upper() != expected_direction
        or str(chosen.get("timeframe") or "").lower() != "5m"
    ):
        return False, "جهت یا تایم‌فریم الگوی منتخب معتبر نیست."

    detected = evidence.get("detected_patterns")
    detected = detected if isinstance(detected, list) else []
    matched = any(
        isinstance(item, Mapping)
        and str(item.get("name_en") or "").strip() == pattern_name
        and str(item.get("direction") or "").upper() == expected_direction
        and str(item.get("timeframe") or "").lower() == "5m"
        and str(item.get("category") or "").upper() == category
        for item in detected
    )
    if not matched:
        return False, "الگوی منتخب در شواهد کندل‌های بسته‌شده ۵ دقیقه‌ای وجود ندارد."

    rows = evidence.get("candles_5m")
    if not isinstance(rows, list) or len(rows) < 3:
        return False, "نمونه کندل‌های بسته‌شده ۵ دقیقه‌ای ناقص است."
    normalised: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            return False, "ردیف کندل ۵ دقیقه‌ای قابل تفسیر نیست."
        timestamp = str(item.get("open_time_utc") or item.get("time_utc") or "").strip()
        values = {key: _number(item.get(key)) for key in ("open", "high", "low", "close", "volume")}
        if not timestamp or any(value is None for value in values.values()):
            return False, "زمان یا OHLCV یکی از کندل‌های ۵ دقیقه‌ای ناقص است."
        if min(values["open"], values["high"], values["low"], values["close"]) <= 0 or values["volume"] < 0:
            return False, "مقادیر OHLCV کندل ۵ دقیقه‌ای معتبر نیست."
        if values["high"] < max(values["open"], values["close"], values["low"]):
            return False, "سقف کندل ۵ دقیقه‌ای با OHLC سازگار نیست."
        if values["low"] > min(values["open"], values["close"], values["high"]):
            return False, "کف کندل ۵ دقیقه‌ای با OHLC سازگار نیست."
        normalised.append({"time": timestamp, **values})

    timestamps = [row["time"] for row in normalised]
    if len(set(timestamps)) != len(timestamps) or timestamps != sorted(timestamps):
        return False, "ترتیب زمانی کندل‌های ۵ دقیقه‌ای معتبر یا یکتا نیست."

    confirmation = evidence.get("confirmation_candle")
    if not isinstance(confirmation, Mapping):
        return False, "کندل تأیید نهایی ۵ دقیقه‌ای ثبت نشده است."
    last_candle = normalised[-1]
    confirmation_time = str(
        confirmation.get("open_time_utc") or confirmation.get("time_utc") or ""
    ).strip()
    if confirmation_time != last_candle["time"]:
        return False, "زمان کندل تأیید با آخرین کندل بسته‌شده ۵ دقیقه‌ای تطبیق ندارد."
    signal_bar = str(record.get("signal_bar_open_utc") or "").strip()
    if signal_bar and signal_bar != confirmation_time:
        return False, "شناسه کندل سیگنال با کندل تأیید ۵ دقیقه‌ای یکسان نیست."
    for field in ("open", "high", "low", "close", "volume"):
        actual = _number(confirmation.get(field))
        if actual is None or not math.isclose(
            actual, last_candle[field], rel_tol=1e-9, abs_tol=1e-12
        ):
            return False, "OHLCV کندل تأیید با نمونه بازار ۵ دقیقه‌ای تطبیق ندارد."

    instrument = str(record.get("inst_id") or record.get("instrument_id") or "").strip().upper()
    expected_signal_id = f"challenge1-v4:{instrument}:5m:{confirmation_time}"
    if (
        not instrument
        or str(record.get("signal_id") or "") != expected_signal_id
        or str(execution.get("signal_id") or "") != expected_signal_id
    ):
        return False, "کلید یکتای سیگنال باید فقط به نماد و کندل بسته‌شده ۵ دقیقه‌ای وابسته باشد."

    if category == "CHART_STRUCTURE":
        structure = evidence.get("chart_structure")
        structure = structure if isinstance(structure, Mapping) else {}
        expected_breakout = "UP" if signal == "LONG" else "DOWN"
        if (
            structure.get("breakout_confirmation") != "CONFIRMED"
            or str(structure.get("breakout") or "").upper() != expected_breakout
        ):
            return False, "شکست ساختار نموداری برای الگوی منتخب تأیید نشده است."

    return True, "شواهد OHLCV و الگوی کندل بسته‌شده ۵ دقیقه‌ای معتبر است؛ هیچ تایم‌فریم بالاتری در تصمیم ورود استفاده نمی‌شود."


def analyze_blofin_challenge1(
    client: Any,
    inst_id: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Build a 5m-only candle-pattern entry decision for Challenge 1."""
    instrument = str(inst_id or "").strip().upper()
    if not instrument.endswith("-USDT"):
        raise ValueError("Challenge 1 accepts only USDT linear Demo instrument IDs.")

    # This Challenge 1 engine fetches, evaluates and deduplicates 5m candles only.
    # No 15m or 1H data is requested, required, or used as an entry condition.
    frame_5m = _completed_frame(
        client, inst_id=instrument, bar="5m", now_ms=now_ms, minimum_rows=30,
    )
    patterns_5m = [
        dict(item, timeframe="5m")
        for item in _confirmed_pattern_candidates(frame_5m)
    ]
    # 5m swings require an intraday reversal floor. The generic 3% floor
    # suppresses ordinary 5m pivots and is not used in Challenge 1.
    elliott_wave = monitor_elliott_waves(
        frame_5m, min_reversal_pct=0.0015, max_reversal_pct=0.03,
    )
    chart_structure = monitor_chart_patterns(
        frame_5m, elliott_wave=elliott_wave,
    )
    if (
        chart_structure.get("status") in {"BREAKOUT_UP", "BREAKOUT_DOWN"}
        and chart_structure.get("breakout_confirmation") == "CONFIRMED"
    ):
        direction = "BULLISH" if chart_structure.get("breakout") == "UP" else "BEARISH"
        patterns_5m.append({
            "name_en": str(chart_structure.get("pattern") or "Confirmed Chart Structure Breakout"),
            "direction": direction,
            "score": 12 if direction == "BULLISH" else -12,
            "category": "CHART_STRUCTURE",
            "detail": str(chart_structure.get("reason") or ""),
            "timeframe": "5m",
        })

    bullish = [item for item in patterns_5m if item.get("direction") == "BULLISH"]
    bearish = [item for item in patterns_5m if item.get("direction") == "BEARISH"]
    signal = "NO_TRADE"
    chosen: dict[str, Any] | None = None
    volume = {"confirmed": False, "volume_ratio": None, "close_location": None}
    reason_fa = "الگوی معتبر و تأییدشده‌ای در کندل‌های بسته‌شده ۵ دقیقه‌ای پیدا نشد."
    execution: dict[str, Any] = {"status": "NO_TRADE", "direction": None, "reason_fa": reason_fa}
    entry = stop = target = None
    signal_id = None
    decision_code = "CONFLICTING_5M_PATTERNS" if bullish and bearish else "NO_TRADE"
    wave_status = str(elliott_wave.get("status") or "").upper()
    wave_direction = str(elliott_wave.get("pattern") or "").upper()
    pivot_count = _number(elliott_wave.get("confirmed_pivot_count"))
    wave_directional = (
        wave_status in {"CANDIDATE", "CONFIRMED"}
        and wave_direction in {"BULLISH", "BEARISH"}
        and pivot_count is not None and pivot_count >= 3
        and _number(elliott_wave.get("invalidation_price")) is not None
    )
    wave_alignment = "UNCONFIRMED"

    if bullish and bearish:
        reason_fa = "الگوهای صعودی و نزولی هم‌زمان در کندل‌های بسته‌شده ۵ دقیقه‌ای دیده شدند؛ معامله انجام نمی‌شود."
    elif not patterns_5m:
        reason_fa = "در کندل‌های بسته‌شده ۵ دقیقه‌ای الگوی ورود معتبر پیدا نشد؛ عدم معامله تصمیم معتبر است."
    else:
        chosen = max(patterns_5m, key=lambda item: abs(int(item.get("score") or 0)))
        direction = str(chosen["direction"])
        expected_wave_direction = "BULLISH" if direction == "BULLISH" else "BEARISH"
        if not wave_directional:
            chosen = None
            decision_code = "ELLIOTT_WAVE_UNCONFIRMED"
            reason_fa = (
                "الگوی کندلی/ساختاری شناسایی شد، اما موج الیوت ۵ دقیقه‌ای "
                "شمارش معتبر و نقطه ابطال تأییدشده ندارد؛ ورود مسدود شد."
            )
        elif wave_direction != expected_wave_direction:
            chosen = None
            decision_code = "ELLIOTT_WAVE_DIRECTION_CONFLICT"
            reason_fa = (
                "جهت موج الیوت با جهت الگوی کندلی/ساختاری هم‌سو نیست؛ "
                "برای جلوگیری از ورود متناقض معامله انجام نمی‌شود."
            )
        else:
            wave_alignment = "ALIGNED"

        if chosen is not None:
            volume = _volume_confirmation(frame_5m, direction)
            atr = _atr(frame_5m)
            entry_value = float(frame_5m["close"].iloc[-1])
            if atr is None or not math.isfinite(entry_value) or entry_value <= 0:
                reason_fa = "دادهٔ کندل‌های ۵ دقیقه‌ای برای محاسبهٔ حد ضرر ساختاری کافی نیست."
            else:
                buffer = atr * 0.10
                if direction == "BULLISH":
                    structural_low = float(frame_5m["low"].tail(5).min())
                    stop_value = min(structural_low - buffer, entry_value - 1.5 * atr)
                    risk_distance = entry_value - stop_value
                    target_value = entry_value + 2.0 * risk_distance
                    signal = "LONG"
                else:
                    structural_high = float(frame_5m["high"].tail(5).max())
                    stop_value = max(structural_high + buffer, entry_value + 1.5 * atr)
                    risk_distance = stop_value - entry_value
                    target_value = entry_value - 2.0 * risk_distance

                if risk_distance <= 0 or target_value <= 0:
                    signal = "NO_TRADE"
                    reason_fa = "سطوح ساختاری ورود، حد ضرر یا هدف معتبر نیستند."
                else:
                    signal = "LONG" if direction == "BULLISH" else "SHORT"
                    entry, stop, target = entry_value, stop_value, target_value
                    signal_bar = _time_label(frame_5m)
                    # Preserve v4 ids to honor reservations from the previous engine.
                    signal_id = f"challenge1-v4:{instrument}:5m:{signal_bar}"
                    execution = {
                        "status": "PASS",
                        "direction": signal,
                        "signal_id": signal_id,
                        "signal_bar_open_utc": signal_bar,
                        "entry_price": entry,
                        "stop_loss": stop,
                        "take_profit": target,
                        "reward_risk": 2.0,
                        "elliott_wave_phase": str(elliott_wave.get("phase") or ""),
                        "elliott_wave_direction": wave_direction,
                        "elliott_wave_invalidation_price": _number(elliott_wave.get("invalidation_price")),
                    }
                    advisory = []
                    if not volume.get("confirmed"):
                        advisory.append("حجم نسبی فقط به‌عنوان دادهٔ توصیفی ثبت شد و شرط ورود نیست")
                    reason_fa = (
                        f"الگوی {chosen.get('name_en')} و موج {elliott_wave.get('phase')} "
                        "در داده‌های بسته‌شدهٔ ۵ دقیقه‌ای هم‌جهت تأیید شدند؛ "
                        "پیش از سفارش، دو ناظر مستقل باید هر دو همین شواهد را بررسی و تأیید کنند."
                        + (" " + "؛ ".join(advisory) + "." if advisory else "")
                    )

    latest_time = _time_label(frame_5m)
    last_5m = frame_5m.iloc[-1]
    confirmation_candle = {
        "open_time_utc": latest_time,
        "open": float(last_5m["open"]),
        "high": float(last_5m["high"]),
        "low": float(last_5m["low"]),
        "close": float(last_5m["close"]),
        "volume": float(last_5m["volume"]),
    }
    evidence = {
        "decision_engine_version": _ENGINE_VERSION,
        "setup_timeframe": "5m",
        "completed_candles_only": True,
        "legacy_12_gates_used_for_entry": False,
        "external_ai_required": True,
        "tinyfish_required": False,
        "volume_is_advisory": True,
        "higher_timeframes_used": False,
        "decision": decision_code if decision_code != "NO_TRADE" else signal,
        "detected_patterns": patterns_5m,
        "chosen_pattern": chosen,
        "confirmation_candle": confirmation_candle,
        "chart_structure": chart_structure,
        "elliott_wave": elliott_wave,
        "wave_alignment": wave_alignment,
        "volume_confirmation": volume,
        "decision_reason_fa": reason_fa,
        "candles_5m": _candles_for_report(frame_5m, limit=160),
    }
    return {
        "signal": signal,
        "signal_id": signal_id,
        "instrument_id": instrument,
        "inst_id": instrument,
        "execution_profile": "CHALLENGE_1",
        "signal_bar_open_utc": latest_time,
        "entry_price_usd": entry,
        "stop_loss_usd": stop,
        "take_profit_usd": target,
        "execution": execution,
        "challenge1_evidence": evidence,
        "reason_fa": reason_fa,
        "orders_submitted": False,
        "trade_executed": False,
    }
