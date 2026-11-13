"""Live, cross-validated candle radar and non-repainting swing alerts.

The module is signal-only. It does not place orders or integrate with Paper Bot.
Signals use completed candles only; the currently-forming candle is chart-only.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .elliott_waves import monitor_elliott_waves
from .market_patterns import analyze_market_patterns

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
BYBIT_INTERVAL = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}
PIVOT_RADIUS = 3
MAX_CANDLES = 240
MAX_CANDLE_DISCREPANCY_PCT = 0.40
SIGNAL_MAX_PIVOT_AGE_BARS = 10


def _utc_frame(frame: pd.DataFrame, *, now: pd.Timestamp) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    if not isinstance(frame, pd.DataFrame) or frame.empty or not required.issubset(frame.columns):
        raise ValueError("provider returned no complete OHLCV history")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.has_duplicates:
        raise ValueError("candle timestamps must be unique")
    data = frame.loc[:, ["open", "high", "low", "close", "volume"]].copy()
    if data.index.tz is None:
        data.index = data.index.tz_localize("UTC")
    else:
        data.index = data.index.tz_convert("UTC")
    data = data.sort_index()
    for column in ("open", "high", "low", "close", "volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    values = data[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
    invalid = (
        ~np.isfinite(values).all(axis=1)
        | (data[["open", "high", "low", "close"]] <= 0).any(axis=1).to_numpy()
        | (data["volume"] < 0).to_numpy()
        | (data["high"] < data["low"]).to_numpy()
        | (data["high"] < data["open"]).to_numpy()
        | (data["high"] < data["close"]).to_numpy()
        | (data["low"] > data["open"]).to_numpy()
        | (data["low"] > data["close"]).to_numpy()
    )
    if invalid.any():
        raise ValueError("malformed OHLCV candles were returned")
    if data.index[-1] > now + pd.Timedelta(seconds=30):
        raise ValueError("provider returned a future-dated candle")
    return data.loc[data.index <= now].tail(1000)


def _pivots(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Three-bars-left/right pivots; right-side bars must already be closed."""
    radius = PIVOT_RADIUS
    if len(frame) < radius * 2 + 5:
        return []
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    times = frame.index
    result: list[dict[str, Any]] = []
    for index in range(radius, len(frame) - radius):
        left_high = highs[index-radius:index]
        right_high = highs[index+1:index+radius+1]
        left_low = lows[index-radius:index]
        right_low = lows[index+1:index+radius+1]
        is_high = highs[index] > float(np.max(left_high)) and highs[index] >= float(np.max(right_high))
        is_low = lows[index] < float(np.min(left_low)) and lows[index] <= float(np.min(right_low))
        # Ambiguous outside bars are not labelled as both a top and a bottom.
        if is_high == is_low:
            continue
        kind = "HIGH" if is_high else "LOW"
        result.append({
            "kind": kind,
            "index": int(index),
            "price": float(highs[index] if is_high else lows[index]),
            "timestamp": times[index].isoformat(),
            "confirmed_at": times[index + radius].isoformat(),
            "confirmation_delay_bars": radius,
        })
    return result[-12:]


def _atr(frame: pd.DataFrame, period: int = 14) -> float | None:
    if len(frame) < period + 1:
        return None
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    previous = np.r_[close[0], close[:-1]]
    true_range = np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))
    value = float(np.mean(true_range[-period:]))
    return value if math.isfinite(value) and value > 0 else None


def _relative_difference_pct(left: float, right: float) -> float:
    denominator = (abs(left) + abs(right)) / 2.0
    return 0.0 if denominator == 0 else abs(left - right) / denominator * 100.0


def _signal(
    frame: pd.DataFrame,
    pivots: list[dict[str, Any]],
    *,
    quality_status: str,
    freshness_status: str,
) -> dict[str, Any]:
    unknown = {
        "signal": "UNKNOWN",
        "status": "BLOCKED",
        "reason": "Validated candle, freshness, or independent-provider evidence is unavailable.",
        "entry_reference_price": None,
        "stop_loss": None,
        "take_profit": None,
        "reward_risk": None,
        "pivot": None,
        "signal_candle_at": None,
        "signal_key": None,
    }
    if frame.empty or len(frame) < 35:
        return {**unknown, "reason": "At least 35 completed candles are required."}
    if quality_status != "PASS":
        return {**unknown, "reason": "A BUY/SELL alert requires aligned candles from two independent venues."}
    if freshness_status != "FRESH":
        return {**unknown, "reason": "The last completed candle is stale; no new alert is emitted."}

    latest = frame.iloc[-1]
    previous = frame.iloc[-2]
    entry = float(latest["close"])
    atr = _atr(frame)
    if atr is None:
        return {**unknown, "reason": "ATR volatility could not be calculated."}
    if not pivots:
        return {**unknown, "signal": "WAIT", "status": "WAITING",
                "reason": "No confirmed swing pivot is available yet."}

    pivot = pivots[-1]
    pivot_index = int(pivot["index"])
    bars_since_pivot_confirmation = (len(frame) - 1) - (pivot_index + PIVOT_RADIUS)
    if bars_since_pivot_confirmation < 0 or bars_since_pivot_confirmation > SIGNAL_MAX_PIVOT_AGE_BARS:
        return {**unknown, "signal": "WAIT", "status": "WAITING",
                "reason": "The latest confirmed swing is outside the alert window.",
                "pivot": pivot}
    bullish_reversal = (
        pivot["kind"] == "LOW"
        and entry > float(latest["open"])
        and entry > float(previous["high"])
        and entry > float(pivot["price"])
    )
    bearish_reversal = (
        pivot["kind"] == "HIGH"
        and entry < float(latest["open"])
        and entry < float(previous["low"])
        and entry < float(pivot["price"])
    )
    if not bullish_reversal and not bearish_reversal:
        return {
            **unknown,
            "signal": "WAIT",
            "status": "WAITING",
            "reason": "No confirmed reversal trigger beyond the prior candle boundary.",
            "pivot": pivot,
            "signal_candle_at": frame.index[-1].isoformat(),
        }

    if bullish_reversal:
        stop = float(pivot["price"]) - 0.5 * atr
        targets = [float(item["price"]) for item in pivots
                   if item["kind"] == "HIGH" and float(item["price"]) > entry]
        if not targets:
            targets = [float(frame["high"].tail(30).max())]
        target = min((value for value in targets if value > entry), default=None)
        side = "BUY"
        risk = entry - stop
        reward = target - entry if target is not None else None
    else:
        stop = float(pivot["price"]) + 0.5 * atr
        targets = [float(item["price"]) for item in pivots
                   if item["kind"] == "LOW" and float(item["price"]) < entry]
        if not targets:
            targets = [float(frame["low"].tail(30).min())]
        target = max((value for value in targets if value < entry), default=None)
        side = "SELL"
        risk = stop - entry
        reward = entry - target if target is not None else None

    rr = reward / risk if reward is not None and risk > 0 else None
    if rr is None or not math.isfinite(rr) or rr < 1.5:
        return {
            **unknown,
            "signal": "WAIT",
            "status": "WAITING",
            "reason": "Reversal pattern exists, but the structural reward/risk is below 1.5 or no target is available.",
            "pivot": pivot,
            "signal_candle_at": frame.index[-1].isoformat(),
            "entry_reference_price": entry,
            "stop_loss": round(stop, 12),
            "take_profit": round(target, 12) if target is not None else None,
            "reward_risk": round(rr, 3) if rr is not None else None,
        }
    timestamp = frame.index[-1].isoformat()
    return {
        "signal": side,
        "status": "CONFIRMED",
        "reason": (
            "Confirmed swing LOW, bullish close above the previous candle high, and structural reward/risk >= 1.5."
            if side == "BUY" else
            "Confirmed swing HIGH, bearish close below the previous candle low, and structural reward/risk >= 1.5."
        ),
        "entry_reference_price": round(entry, 12),
        "stop_loss": round(stop, 12),
        "take_profit": round(float(target), 12),
        "reward_risk": round(float(rr), 3),
        "pivot": pivot,
        "signal_candle_at": timestamp,
        "signal_key": f"{side}|{timestamp}|{pivot['timestamp']}",
    }


def build_live_market_radar_payload(
    symbol: str,
    interval: str,
    binance_candles: pd.DataFrame,
    bybit_candles: pd.DataFrame | None,
    *,
    now: datetime | None = None,
    bybit_error: str | None = None,
) -> dict[str, Any]:
    """Build a chart payload and fail-closed signal from two public OHLCV sources."""
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = str(interval or "").strip().lower()
    if normalized_interval not in INTERVAL_MS:
        raise ValueError("unsupported candle interval")
    if not normalized_symbol or not normalized_symbol.isascii() or not normalized_symbol.isalnum():
        raise ValueError("invalid market symbol")
    current = pd.Timestamp(now or datetime.now(timezone.utc))
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    else:
        current = current.tz_convert("UTC")
    interval_ms = INTERVAL_MS[normalized_interval]
    interval_delta = pd.Timedelta(milliseconds=interval_ms)
    binance = _utc_frame(binance_candles, now=current)
    # Current/in-progress candle can be drawn, but it is excluded from all signal math.
    display_frame = binance.tail(MAX_CANDLES)
    closed = binance.loc[(binance.index + interval_delta) <= current].copy()
    if closed.empty:
        raise ValueError("no completed candles are available")
    last_closed_end = closed.index[-1] + interval_delta
    age_seconds = max(0.0, float((current - last_closed_end).total_seconds()))
    freshness_status = "FRESH" if age_seconds <= (interval_ms / 1000.0) * 1.5 else "STALE"

    provider_discrepancy = None
    cross_validation = "UNKNOWN"
    cross_reason = bybit_error or "Bybit candle history is not available."
    common_count = 0
    bybit = None
    if isinstance(bybit_candles, pd.DataFrame):
        try:
            bybit = _utc_frame(bybit_candles, now=current)
            bybit_closed = bybit.loc[(bybit.index + interval_delta) <= current]
            common = closed[["close"]].join(
                bybit_closed[["close"]], how="inner", lsuffix="_binance", rsuffix="_bybit"
            )
            common_count = int(len(common))
            if common_count >= 5:
                diffs = [
                    _relative_difference_pct(float(a), float(b))
                    for a, b in zip(common["close_binance"].tail(5), common["close_bybit"].tail(5))
                ]
                provider_discrepancy = round(max(diffs), 5)
                if provider_discrepancy <= MAX_CANDLE_DISCREPANCY_PCT:
                    cross_validation = "PASS"
                    cross_reason = "Last five completed candle closes are aligned across Binance Spot and Bybit Spot."
                else:
                    cross_validation = "FAIL"
                    cross_reason = (
                        f"Maximum close discrepancy across the last five common candles is "
                        f"{provider_discrepancy:.4f}%, above the {MAX_CANDLE_DISCREPANCY_PCT:.2f}% limit."
                    )
            else:
                cross_reason = "Fewer than five common completed candles are available for independent validation."
        except Exception as exc:
            cross_reason = f"{type(exc).__name__}: {str(exc)[:160]}"
    quality_status = cross_validation

    pivots = _pivots(closed)
    wave = monitor_elliott_waves(closed)
    wave_pivots = []
    wave_count = wave.get("wave_count")
    raw_wave_pivots = wave.get("pivots") if isinstance(wave.get("pivots"), list) else []
    if (
        wave.get("pattern") in {"BULLISH", "BEARISH"}
        and isinstance(wave_count, int) and wave_count >= 2
        and len(raw_wave_pivots) >= wave_count + 1
    ):
        selected_wave_points = raw_wave_pivots[-(wave_count + 1):]
        for point_index, point in enumerate(selected_wave_points):
            wave_pivots.append({
                "timestamp": point["timestamp"],
                "price": float(point["price"]),
                "kind": point["kind"],
                "label": "مبدأ" if point_index == 0 else f"W{point_index}",
            })
    market_analysis = analyze_market_patterns(closed, pivots, wave)
    signal = _signal(
        closed, pivots, quality_status=quality_status, freshness_status=freshness_status
    )
    chart = []
    for timestamp, row in display_frame.iterrows():
        opened = timestamp + interval_delta
        is_closed = opened <= current
        chart.append({
            "time": int(timestamp.timestamp() * 1000),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "is_closed": bool(is_closed),
        })
    return {
        "status": "PASS" if freshness_status == "FRESH" else "STALE",
        "symbol": normalized_symbol,
        "interval": normalized_interval,
        "updated_at_utc": current.isoformat(),
        "last_closed_candle_at": closed.index[-1].isoformat(),
        "candle_age_seconds": round(age_seconds, 2),
        "freshness_status": freshness_status,
        "cross_validation": {
            "status": cross_validation,
            "providers": ["Binance Spot"] + (["Bybit Spot"] if bybit is not None else []),
            "common_completed_candles": common_count,
            "max_close_discrepancy_pct": provider_discrepancy,
            "reason": cross_reason,
            "limit_pct": MAX_CANDLE_DISCREPANCY_PCT,
        },
        "candles": chart,
        "pivots": pivots,
        "wave_pivots": wave_pivots,
        "elliott_waves": wave,
        "market_analysis": market_analysis,
        "signal": signal,
        "signal_only": True,
        "paper_bot_connected": False,
        "orders_submitted": False,
        "disclaimer": (
            "Signal alerts are based on completed candles and confirmed swings. "
            "The in-progress candle is display-only. No alert predicts the absolute market top or bottom."
        ),
    }
