"""Completed-candle safeguards for daily research signals."""
from __future__ import annotations

from typing import Optional

import pandas as pd


def completed_daily_candles(
    candles: pd.DataFrame, *, now: Optional[pd.Timestamp] = None
) -> pd.DataFrame:
    """Drop the latest still-open daily bar; preserve malformed data for fail-closed checks."""
    if not isinstance(candles, pd.DataFrame) or candles.empty:
        return candles
    if not isinstance(candles.index, pd.DatetimeIndex) or len(candles) < 2:
        return candles.copy()
    if candles.index.has_duplicates or not candles.index.is_monotonic_increasing:
        return candles.copy()
    index = candles.index
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    diffs = index.to_series().diff().dt.total_seconds().dropna()
    daily_diffs = diffs[(diffs >= 20 * 3600) & (diffs <= 28 * 3600)]
    if daily_diffs.empty:
        return candles.copy()
    cadence = float(daily_diffs.median())
    current = now or pd.Timestamp.now(tz="UTC")
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    else:
        current = current.tz_convert("UTC")
    latest_open = index[-1]
    if latest_open > current:
        return candles.copy()
    if current < latest_open + pd.Timedelta(seconds=cadence):
        return candles.iloc[:-1].copy()
    return candles.copy()
