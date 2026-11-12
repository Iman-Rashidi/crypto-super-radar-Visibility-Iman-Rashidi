"""Offline tests for evidence-bounded Gem Hunter multi-timeframe analysis."""
from datetime import datetime, timedelta, timezone
import unittest

import pandas as pd

from crypto_super_radar.gem_hunter_technical import (
    analyze_multi_timeframe_market,
    _validated_close,
)


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def candle_frame(interval: timedelta, count: int, *, start_price: float = 100.0,
                 up: float = 0.002, down: float = -0.001) -> pd.DataFrame:
    index = pd.date_range(
        end=pd.Timestamp(NOW - interval),
        periods=count,
        freq=pd.Timedelta(interval),
    )
    closes = [start_price]
    for position in range(1, count):
        change = up if position % 2 else down
        closes.append(closes[-1] * (1.0 + change))
    return pd.DataFrame({
        "open": closes,
        "high": [value * 1.002 for value in closes],
        "low": [value * 0.998 for value in closes],
        "close": closes,
        "volume": [1000.0] * count,
    }, index=index)


def daily_frame(count: int, *, start_price: float = 100.0,
                daily_return: float = 0.01) -> pd.DataFrame:
    interval = timedelta(days=1)
    index = pd.date_range(
        end=pd.Timestamp(NOW - interval), periods=count, freq="1D",
    )
    closes = [start_price * ((1.0 + daily_return) ** i) for i in range(count)]
    return pd.DataFrame({
        "open": closes,
        "high": [value * 1.01 for value in closes],
        "low": [value * 0.99 for value in closes],
        "close": closes,
        "volume": [1000.0] * count,
    }, index=index)


class GemHunterTechnicalTests(unittest.TestCase):
    def _inputs(self):
        pool = {
            "15m": candle_frame(timedelta(minutes=15), 140),
            "4h": candle_frame(timedelta(hours=4), 120),
            "1d": daily_frame(90, daily_return=0.01),
        }
        benchmarks = {
            "BTCUSDT": daily_frame(90, start_price=50000, daily_return=0.001),
            "ETHUSDT": daily_frame(90, start_price=3000, daily_return=0.002),
        }
        return pool, benchmarks

    def test_complete_fresh_history_produces_auditable_filter_results(self):
        pool, benchmarks = self._inputs()
        result = analyze_multi_timeframe_market(
            pool, benchmarks, chain="base",
            token_address="0x" + "1" * 40,
            pool_address="0x" + "2" * 40,
            now=NOW,
        )
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["coverage_status"], "COMPLETE")
        self.assertEqual(result["filters"]["trend"], "PASS")
        self.assertEqual(result["filters"]["momentum"], "PASS")
        self.assertEqual(result["filters"]["relative_strength"], "PASS")
        self.assertEqual(result["filters"]["market_regime_validation"], "PASS")
        self.assertEqual(result["timeframes"]["15m"]["bars"], 140)
        self.assertEqual(result["coverage"]["benchmarks_present"], ["BTCUSDT", "ETHUSDT"])

    def test_bearish_pool_and_benchmarks_are_classified_without_false_pass(self):
        pool = {
            "15m": candle_frame(timedelta(minutes=15), 140, up=-0.002, down=-0.001),
            "4h": candle_frame(timedelta(hours=4), 120, up=-0.002, down=-0.001),
            "1d": daily_frame(90, daily_return=-0.01),
        }
        benchmarks = {
            "BTCUSDT": daily_frame(90, start_price=50000, daily_return=-0.001),
            "ETHUSDT": daily_frame(90, start_price=3000, daily_return=-0.002),
        }
        result = analyze_multi_timeframe_market(
            pool, benchmarks, chain="solana",
            token_address="So11111111111111111111111111111111111111112",
            pool_address="PoolAddress", now=NOW,
        )
        self.assertEqual(result["filters"]["trend"], "FAIL")
        self.assertEqual(result["filters"]["momentum"], "FAIL")
        self.assertEqual(result["filters"]["market_regime_validation"], "FAIL")

    def test_partial_or_stale_history_keeps_indicators_unknown(self):
        pool, benchmarks = self._inputs()
        pool["1d"] = pool["1d"].iloc[-20:]
        result = analyze_multi_timeframe_market(
            pool, benchmarks, chain="base",
            token_address="0x" + "1" * 40,
            pool_address="0x" + "2" * 40,
            now=NOW,
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["coverage_status"], "PARTIAL")
        self.assertEqual(result["filters"]["trend"], "UNKNOWN")
        self.assertIn("MULTI_TIMEFRAME_COVERAGE_INCOMPLETE", result["reason_codes"])

    def test_naive_timestamps_are_rejected(self):
        frame = daily_frame(90)
        frame.index = frame.index.tz_localize(None)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            _validated_close(
                frame, interval=timedelta(days=1),
                max_age=timedelta(hours=36), minimum_bars=60, now=NOW,
            )

    def test_duplicate_timestamps_are_rejected(self):
        frame = daily_frame(90)
        frame.index = list(frame.index[:-1]) + [frame.index[-2]]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _validated_close(
                frame, interval=timedelta(days=1),
                max_age=timedelta(hours=36), minimum_bars=60, now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
