"""Deterministic, closed-candle Challenge 1 strategy for BloFin Demo.

Only completed 15m candles create setups. The 1H frame is a conflict veto,
not a legacy indicator-score gate. All decisions are local and reproducible.
"""
from __future__ import annotations

import math
import time
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .chart_patterns import monitor_chart_patterns

DEFAULT_CHALLENGE1_INSTRUMENTS = (
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "LINK-USDT",
)

_INTERVAL_MS = {"5m": 5 * 60 * 1000, "15m": 15 * 60 * 1000, "1H": 60 * 60 * 1000, "1h": 60 * 60 * 1000}
_ENGINE_VERSION = "CHALLENGE_1_LOCAL_RULES_V3"


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


def analyze_blofin_challenge1(
    client: Any,
    inst_id: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Return a fully evidenced Challenge 1 decision; it never submits an order."""
    instrument = str(inst_id or "").strip().upper()
    if not instrument.endswith("-USDT"):
        raise ValueError("Challenge 1 accepts only USDT linear Demo instrument IDs.")

    # Thirty completed 5m bars cover the 20-bar breakout and ATR warm-up.
    # The 1H frame is optional context only; an unavailable context feed must not
    # suppress a valid, fully completed 5m candle-pattern setup.
    frame_5m = _completed_frame(client, inst_id=instrument, bar="5m", now_ms=now_ms, minimum_rows=30)
    context_data_status = "AVAILABLE"
    try:
        frame_1h = _completed_frame(client, inst_id=instrument, bar="1H", now_ms=now_ms, minimum_rows=30)
    except Exception:
        frame_1h = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "quote_volume"])
        context_data_status = "UNAVAILABLE"

    patterns_5m = [dict(item, timeframe="5m") for item in _confirmed_pattern_candidates(frame_5m)]
    patterns_1h = [dict(item, timeframe="1H") for item in _confirmed_pattern_candidates(frame_1h)]
    chart_structure = monitor_chart_patterns(frame_5m)
    if (
        chart_structure.get("status") in {"BREAKOUT_UP", "BREAKOUT_DOWN"}
        and chart_structure.get("breakout_confirmation") == "CONFIRMED"
    ):
        direction = "BULLISH" if chart_structure.get("breakout") == "UP" else "BEARISH"
        patterns_5m.append({
            "name_en": str(chart_structure.get("pattern") or "Confirmed Triangle Breakout"),
            "direction": direction,
            "score": 12 if direction == "BULLISH" else -12,
            "category": "CHART_STRUCTURE",
            "detail": str(chart_structure.get("reason") or ""),
            "timeframe": "5m",
        })

    all_detected = patterns_5m + patterns_1h
    bullish = [item for item in patterns_5m if item.get("direction") == "BULLISH"]
    bearish = [item for item in patterns_5m if item.get("direction") == "BEARISH"]
    signal = "NO_TRADE"
    chosen: dict[str, Any] | None = None
    volume: dict[str, Any] = {"confirmed": False, "volume_ratio": None, "close_location": None}
    reason_fa = "الگوی معتبر و تأییدشده‌ای در کندل‌های بسته‌شده پیدا نشد."
    execution: dict[str, Any] = {"status": "NO_TRADE", "direction": None, "reason_fa": reason_fa}
    entry = stop = target = None
    signal_id = None

    decision_code = "CONFLICTING_PATTERNS" if bullish and bearish else "NO_TRADE"
    context_conflict = False
    if bullish and bearish:
        reason_fa = "الگوهای صعودی و نزولی هم‌زمان در ۵ دقیقه دیده شدند؛ به‌دلیل تعارض، معامله انجام نمی‌شود."
    elif not patterns_5m:
        reason_fa = "در تایم‌فریم ۵ دقیقه الگوی ورود معتبر پیدا نشد؛ عدم معامله تصمیم معتبر است."
    else:
        chosen = max(patterns_5m, key=lambda item: abs(int(item.get("score") or 0)))
        direction = str(chosen["direction"])
        opposite = "BEARISH" if direction == "BULLISH" else "BULLISH"
        context_conflict = any(item.get("direction") == opposite for item in patterns_1h)
        # Volume and 1H context are reported as advisory evidence, not entry vetoes.
        volume = _volume_confirmation(frame_5m, direction)
        atr = _atr(frame_5m)
        entry_value = float(frame_5m["close"].iloc[-1])
        if atr is None or not math.isfinite(entry_value) or entry_value <= 0:
            reason_fa = "داده کافی برای محاسبه حد ضرر ساختاری و ATR وجود ندارد."
        else:
            buffer = atr * 0.10
            if direction == "BULLISH":
                structural_low = float(frame_5m["low"].tail(5).min())
                stop_value = min(structural_low - buffer, entry_value - 1.5 * atr)
                risk_distance = entry_value - stop_value
                target_value = entry_value + 1.0 * risk_distance
                signal = "LONG"
            else:
                structural_high = float(frame_5m["high"].tail(5).max())
                stop_value = max(structural_high + buffer, entry_value + 1.5 * atr)
                risk_distance = stop_value - entry_value
                target_value = entry_value - 1.0 * risk_distance
                signal = "SHORT"
            if risk_distance <= 0 or target_value <= 0:
                signal = "NO_TRADE"
                reason_fa = "سطوح ساختاری ورود، حد ضرر یا هدف معتبر نیستند."
            else:
                entry, stop, target = entry_value, stop_value, target_value
                signal_bar = _time_label(frame_5m)
                signal_id = f"challenge1-v3:{instrument}:5m:{signal_bar}:{signal}"
                execution = {
                    "status": "PASS",
                    "direction": signal,
                    "signal_id": signal_id,
                    "signal_bar_open_utc": signal_bar,
                    "entry_price": entry,
                    "stop_loss": stop,
                    "take_profit": target,
                    "reward_risk": 1.0,
                }
                advisory = []
                if not volume.get("confirmed"):
                    advisory.append("تأیید حجم/محل بسته‌شدن کامل نیست")
                if context_conflict:
                    advisory.append("الگوی یک‌ساعته خلاف جهت است")
                if context_data_status != "AVAILABLE":
                    advisory.append("داده زمینه یک‌ساعته در دسترس نبود")
                suffix = "؛ موارد نظارتی: " + "، ".join(advisory) if advisory else ""
                reason_fa = f"الگوی {chosen.get('name_en')} با کندل بسته‌شده ۱۵دقیقه‌ای تأیید شد؛ ورود بر اساس همان الگو انجام‌پذیر است{suffix}."

    latest_time = _time_label(frame_5m)
    confirmation_candle = {
        "open_time_utc": latest_time,
        "open": float(frame_5m["open"].iloc[-1]),
        "high": float(frame_5m["high"].iloc[-1]),
        "low": float(frame_5m["low"].iloc[-1]),
        "close": float(frame_5m["close"].iloc[-1]),
        "volume_confirmation": volume,
    }
    evidence = {
        "decision_engine_version": _ENGINE_VERSION,
        "setup_timeframe": "5m",
        "context_timeframe": "1H",
        "completed_candles_only": True,
        "legacy_12_gates_used_for_entry": False,
        "external_ai_required": False,
        "tinyfish_required": False,
        "volume_is_advisory": True,
        "context_is_advisory": True,
        "context_data_status": context_data_status,
        "decision": decision_code if decision_code == "CONFLICTING_PATTERNS" else signal,
        "detected_patterns": all_detected,
        "chosen_pattern": chosen,
        "confirmation_candle": confirmation_candle,
        "chart_structure": chart_structure,
        "context_patterns": patterns_1h,
        "volume_confirmation": volume,
        "volume_is_advisory": True,
        "context_conflict": context_conflict,
        "context_data_status": context_data_status,
        "context_is_advisory": True,
        "decision_reason_fa": reason_fa,
        # These are the exact validated completed candles used by this decision.
        # Future candles are never added to the decision-time snapshot.
        "candles_5m": _candles_for_report(frame_5m, limit=120),
        "candles_1h": _candles_for_report(frame_1h, limit=60),
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
