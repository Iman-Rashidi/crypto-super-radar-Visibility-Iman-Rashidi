"""Lightweight OHLCV price-action diagnostics for the Easy crypto engine.

All outputs are descriptive evidence only: this module never creates a trade
signal, vetoes a setup, or changes the Standard/Strict decision paths.
"""
from __future__ import annotations

from typing import Any
import math
import numpy as np
import pandas as pd

_REQUIRED = {"open", "high", "low", "close", "volume"}


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "UNKNOWN", "reason": reason}


def analyze_easy_price_action(
    candles: pd.DataFrame,
    *,
    pivot_radius: int = 2,
    zone_atr_fraction: float = 0.35,
    gap_threshold_pct: float = 0.75,
    last_candle_closed: bool = False,
) -> dict[str, Any]:
    """Return support/resistance, trendline, volume and crypto-gap diagnostics.

    Pivot points are only confirmed after pivot_radius subsequent candles.
    The last candle is treated as provisional and excluded from confirmed
    pivots/volume comparisons because its close status is not supplied here.
    """
    unknown = _unknown("Validated OHLCV history is unavailable.")
    if not isinstance(candles, pd.DataFrame) or not _REQUIRED.issubset(candles.columns):
        return {**unknown, "support_resistance": _unknown("OHLCV columns are missing."),
                "trendlines": _unknown("OHLCV columns are missing."),
                "volume": _unknown("OHLCV columns are missing."),
                "gaps": _unknown("OHLCV columns are missing.")}
    if not isinstance(candles.index, pd.DatetimeIndex) or candles.index.has_duplicates:
        reason = "Unique DatetimeIndex timestamps are required."
        return {**unknown, "reason": reason, "support_resistance": _unknown(reason),
                "trendlines": _unknown(reason), "volume": _unknown(reason), "gaps": _unknown(reason)}
    frame = candles.loc[:, ["open", "high", "low", "close", "volume"]].copy()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    if not frame.index.is_monotonic_increasing:
        return {**unknown, "reason": "Candles must be chronological; implicit reordering is rejected."}
    for col in _REQUIRED:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    values = frame[list(_REQUIRED)].to_numpy(dtype=float)
    malformed = (not np.isfinite(values).all() or (frame[["open","high","low","close"]] <= 0).any().any()
                 or (frame["volume"] < 0).any()
                 or (frame["high"] < frame[["open","low","close"]].max(axis=1)).any()
                 or (frame["low"] > frame[["open","high","close"]].min(axis=1)).any())
    if malformed:
        reason = "Malformed or non-finite OHLCV observations were detected."
        return {**unknown, "reason": reason, "support_resistance": _unknown(reason),
                "trendlines": _unknown(reason), "volume": _unknown(reason), "gaps": _unknown(reason)}
    if len(frame) < max(30, 2 * pivot_radius + 8):
        reason = "At least 30 valid candles are required for price-action diagnostics."
        return {**unknown, "reason": reason, "support_resistance": _unknown(reason),
                "trendlines": _unknown(reason), "volume": _unknown(reason), "gaps": _unknown(reason)}

    # Callers must explicitly confirm closure; the integrated engine does so after
    # completed_daily_candles() has removed an unfinished daily interval.
    confirmed = frame.copy() if last_candle_closed else frame.iloc[:-1].copy()
    high = confirmed["high"].to_numpy(float)
    low = confirmed["low"].to_numpy(float)
    close = confirmed["close"].to_numpy(float)
    volume = confirmed["volume"].to_numpy(float)
    previous = np.r_[close[0], close[:-1]]
    tr = np.maximum(high-low, np.maximum(np.abs(high-previous), np.abs(low-previous)))
    atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else None
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return {**unknown, "reason": "ATR scale is unavailable."}

    highs, lows = [], []
    r = max(1, int(pivot_radius))
    for i in range(r, len(confirmed)-r):
        if high[i] >= np.max(high[i-r:i+r+1]) and high[i] > np.max(high[i-r:i]):
            highs.append((i, float(high[i])))
        if low[i] <= np.min(low[i-r:i+r+1]) and low[i] < np.min(low[i-r:i]):
            lows.append((i, float(low[i])))
    current = float(confirmed["close"].iloc[-1])
    tolerance = atr * max(0.05, float(zone_atr_fraction))

    def zones(points, side):
        candidates = [p for _, p in points if (p <= current if side == "support" else p >= current)]
        clusters = []
        for price in candidates:
            match = next((z for z in clusters if abs(price-z["center"]) <= tolerance), None)
            if match:
                match["prices"].append(price)
                match["center"] = float(np.mean(match["prices"]))
            else:
                clusters.append({"center": price, "prices": [price]})
        clusters.sort(key=lambda z: abs(z["center"]-current))
        return [{"low": round(min(z["prices"]), 10), "high": round(max(z["prices"]), 10),
                 "center": round(z["center"], 10), "touch_count": len(z["prices"])}
                for z in clusters[:3]]

    support, resistance = zones(lows, "support"), zones(highs, "resistance")
    sr = {"status": "KNOWN" if support or resistance else "UNKNOWN",
          "support_zones": support, "resistance_zones": resistance,
          "zone_tolerance_atr": float(zone_atr_fraction), "atr": atr,
          "breakout": ("ABOVE_RESISTANCE" if resistance and current > resistance[0]["high"] else
                       "BELOW_SUPPORT" if support and current < support[0]["low"] else "NONE")}

    def line(points, label):
        if len(points) < 2:
            return {"status": "UNKNOWN", "reason": "Two confirmed pivots are required."}
        (x1,y1),(x2,y2)=points[-2:]
        slope=(y2-y1)/(x2-x1)
        return {"status":"KNOWN","kind":label,"from_index":x1,"to_index":x2,
                "slope_per_candle":float(slope),"last_pivot_price":y2,
                "direction":"UP" if slope>0 else "DOWN" if slope<0 else "FLAT"}
    up_line=line(lows,"ascending_support")
    down_line=line(highs,"descending_resistance")
    trend = "SIDEWAYS"
    if up_line["status"] == "KNOWN" and down_line["status"] == "KNOWN":
        if up_line["direction"] == "UP" and down_line["direction"] == "UP":
            trend = "UPTREND_CANDIDATE"
        elif up_line["direction"] == "DOWN" and down_line["direction"] == "DOWN":
            trend = "DOWNTREND_CANDIDATE"
        elif up_line["direction"] != down_line["direction"]:
            trend = "CONVERGING_OR_RANGE"
    trendlines={"status":"KNOWN" if up_line["status"]=="KNOWN" or down_line["status"]=="KNOWN" else "UNKNOWN",
                "classification":trend,"ascending_support":up_line,"descending_resistance":down_line,
                "pivot_radius":r,"confirmation_delay_bars":r}

    avg_volume=float(np.mean(volume[-20:])) if len(volume)>=20 else None
    latest_volume=float(volume[-1])
    ratio=latest_volume/avg_volume if avg_volume and avg_volume>0 else None
    delta=np.diff(close,prepend=close[0])
    obv=np.cumsum(np.where(delta>0,volume,np.where(delta<0,-volume,0.0)))
    obv_direction="RISING" if len(obv)>=5 and obv[-1]>obv[-5] else "FALLING" if len(obv)>=5 and obv[-1]<obv[-5] else "FLAT"
    price_up=close[-1]>close[-5] if len(close)>=5 else False
    price_down=close[-1]<close[-5] if len(close)>=5 else False
    volume_data={"status":"KNOWN","latest_closed_volume":latest_volume,
                 "average_20":avg_volume,"relative_volume_20":ratio,
                 "obv":float(obv[-1]),"obv_direction":obv_direction,
                 "price_obv_divergence":("BEARISH_WARNING" if price_up and obv_direction=="FALLING" else
                                         "BULLISH_WARNING" if price_down and obv_direction=="RISING" else "NONE"),
                 "breakout_volume_warning":bool(ratio is not None and ratio<0.8)}

    # A crypto price gap is reported only when timestamps are consecutive at
    # the inferred cadence. Missing intervals are data gaps, not price gaps.
    deltas=frame.index.to_series().diff().dropna().dt.total_seconds()
    cadence=float(deltas.median()) if not deltas.empty else 0.0
    gaps=[]
    for i in range(1,len(frame)):
        dt=(frame.index[i]-frame.index[i-1]).total_seconds()
        if cadence<=0 or abs(dt-cadence)>max(1.0,cadence*0.05):
            continue
        prev=float(frame["close"].iloc[i-1]); op=float(frame["open"].iloc[i])
        gap_pct=(op/prev-1.0)*100.0
        if abs(gap_pct)>=float(gap_threshold_pct):
            gaps.append({"timestamp":frame.index[i].isoformat(),"direction":"UP" if gap_pct>0 else "DOWN",
                         "previous_close":prev,"next_open":op,"gap_pct":gap_pct,
                         "status":"UNFILLED_OR_UNKNOWN","volume":float(frame["volume"].iloc[i])})
    gap_data={"status":"KNOWN","count":len(gaps),"events":gaps[-10:],
              "threshold_pct":float(gap_threshold_pct),
              "note":"Crypto trades 24/7; timestamp discontinuities are excluded as data gaps, not price gaps."}
    return {"status":"KNOWN","as_of":confirmed.index[-1].isoformat(),
            "support_resistance":sr,"trendlines":trendlines,"volume":volume_data,"gaps":gap_data,
            "data_policy":{"confirmed_candles_only":True,"last_candle_excluded_as_provisional":True,
                           "used_as_hard_gate":False,"used_for_order_execution":False}}
