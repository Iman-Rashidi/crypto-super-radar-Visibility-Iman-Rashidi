from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from crypto_super_radar.challenge1_strategy import (
    _completed_frame,
    analyze_blofin_challenge1,
)


class FakeBloFin:
    def __init__(self, rows_by_bar):
        self.rows_by_bar = rows_by_bar

    def get_candles(self, inst_id, *, bar, limit):
        return self.rows_by_bar[bar]


def candle_rows(now_ms, interval_ms, count=65, unclosed_last=False):
    rows = []
    start = now_ms - count * interval_ms
    for i in range(count):
        stamp = start + i * interval_ms
        close = 100.0 + i * 0.05
        rows.append([
            stamp, close - 0.01, close + 0.03, close - 0.03, close,
            1000.0, 100000.0, 0.0, "1",
        ])
    if unclosed_last:
        rows[-1][8] = "0"
    return rows


class Challenge1CandleTests(unittest.TestCase):
    def test_completed_frame_discards_unclosed_bars_and_preserves_volume(self):
        now = 2_000_000_000_000
        rows = candle_rows(now, 15 * 60 * 1000, count=65, unclosed_last=True)
        frame = _completed_frame(
            FakeBloFin({"15m": rows}), inst_id="BTC-USDT",
            bar="15m", now_ms=now, minimum_rows=60,
        )
        self.assertEqual(len(frame), 64)
        self.assertEqual(float(frame["volume"].iloc[-1]), 1000.0)
        self.assertTrue(frame.index.is_monotonic_increasing)

    def test_completed_frame_rejects_stale_latest_candle(self):
        now = 2_000_000_000_000
        interval = 15 * 60 * 1000
        rows = candle_rows(now, interval, count=65)
        for row in rows:
            row[0] -= 2 * interval
        with self.assertRaisesRegex(ValueError, "stale"):
            _completed_frame(
                FakeBloFin({"15m": rows}), inst_id="BTC-USDT",
                bar="15m", now_ms=now, minimum_rows=60,
            )

    def test_completed_frame_rejects_a_gap_in_recent_candles(self):
        now = 2_000_000_000_000
        interval = 15 * 60 * 1000
        rows = candle_rows(now, interval, count=65)
        rows.pop(-10)
        with self.assertRaisesRegex(ValueError, "missing interval"):
            _completed_frame(
                FakeBloFin({"15m": rows}), inst_id="BTC-USDT",
                bar="15m", now_ms=now, minimum_rows=60,
            )

    def test_analysis_rejects_stale_one_hour_context(self):
        now = 2_000_000_000_000
        fifteen = candle_rows(now, 15 * 60 * 1000, count=65)
        hourly = candle_rows(now, 60 * 60 * 1000, count=65)
        for row in hourly:
            row[0] -= 2 * 60 * 60 * 1000
        client = FakeBloFin({"15m": fifteen, "1H": hourly})
        with self.assertRaisesRegex(ValueError, "stale"):
            analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)

    def test_no_trade_is_a_valid_result_when_no_confirmed_pattern_exists(self):
        now = 2_000_000_000_000
        client = FakeBloFin({
            "15m": candle_rows(now, 15 * 60 * 1000),
            "1H": candle_rows(now, 60 * 60 * 1000),
        })
        with patch("crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates", return_value=[]):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertFalse(result["orders_submitted"])
        self.assertFalse(result["challenge1_evidence"]["legacy_12_gates_used_for_entry"])
        self.assertEqual(result["elliott_wave"]["timeframe"], "1H")
        self.assertEqual(result["elliott_wave"]["source"], "BLOFIN_DEMO_COMPLETED_1H_CANDLES")
        self.assertEqual(result["elliott_wave"]["completed_candle_count"], 65)
        self.assertIn("elliott_wave", result["challenge1_evidence"])

    def test_conflicting_confirmed_patterns_block_entry(self):
        now = 2_000_000_000_000
        client = FakeBloFin({
            "15m": candle_rows(now, 15 * 60 * 1000),
            "1H": candle_rows(now, 60 * 60 * 1000),
        })
        bullish = {"name_en": "Bullish Engulfing", "direction": "BULLISH", "score": 13}
        bearish = {"name_en": "Bearish Engulfing", "direction": "BEARISH", "score": -13}
        with patch(
            "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
            side_effect=[[bullish, bearish], []],
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertEqual(result["challenge1_evidence"]["decision"], "CONFLICTING_PATTERNS")


if __name__ == "__main__":
    unittest.main()
