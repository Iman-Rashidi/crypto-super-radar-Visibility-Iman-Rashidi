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
    start = now_ms - count * interval_ms - interval_ms
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
        rows = candle_rows(now, 5 * 60 * 1000, count=65, unclosed_last=True)
        frame = _completed_frame(
            FakeBloFin({"5m": rows}), inst_id="BTC-USDT",
            bar="5m", now_ms=now, minimum_rows=60,
        )
        self.assertEqual(len(frame), 64)
        self.assertEqual(float(frame["volume"].iloc[-1]), 1000.0)
        self.assertTrue(frame.index.is_monotonic_increasing)

    def test_no_trade_is_a_valid_result_when_no_confirmed_pattern_exists(self):
        now = 2_000_000_000_000
        client = FakeBloFin({
            "5m": candle_rows(now, 5 * 60 * 1000),
            "1H": candle_rows(now, 60 * 60 * 1000),
        })
        with patch("crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates", return_value=[]):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertFalse(result["orders_submitted"])
        self.assertFalse(result["challenge1_evidence"]["legacy_12_gates_used_for_entry"])

    def test_conflicting_confirmed_patterns_block_entry(self):
        now = 2_000_000_000_000
        client = FakeBloFin({
            "5m": candle_rows(now, 5 * 60 * 1000),
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

    def test_volume_and_opposing_hourly_context_are_advisory_not_entry_vetoes(self):
        now = 2_000_000_000_000
        client = FakeBloFin({
            "5m": candle_rows(now, 5 * 60 * 1000),
            "1H": candle_rows(now, 60 * 60 * 1000),
        })
        bullish = {"name_en": "Bullish Engulfing", "direction": "BULLISH", "score": 13}
        bearish = {"name_en": "Bearish Engulfing", "direction": "BEARISH", "score": -13}
        with (
            patch(
                "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
                side_effect=[[bullish], [bearish]],
            ),
            patch(
                "crypto_super_radar.challenge1_strategy._volume_confirmation",
                return_value={"confirmed": False, "volume_ratio": 0.7, "reason": "low volume"},
            ),
            patch("crypto_super_radar.challenge1_strategy._atr", return_value=1.0),
            patch("crypto_super_radar.challenge1_strategy.monitor_chart_patterns", return_value={}),
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "LONG")
        self.assertEqual(result["execution"]["status"], "PASS")
        self.assertAlmostEqual(
            abs(result["take_profit_usd"] - result["entry_price_usd"])
            / abs(result["entry_price_usd"] - result["stop_loss_usd"]),
            1.0,
            places=6,
        )
        self.assertTrue(result["challenge1_evidence"]["volume_is_advisory"])
        self.assertTrue(result["challenge1_evidence"]["context_is_advisory"])
        self.assertTrue(result["challenge1_evidence"]["context_conflict"])
        self.assertIn("challenge1-v3:", result["signal_id"])
        self.assertGreaterEqual(len(result["challenge1_evidence"]["candles_5m"]), 30)
        self.assertGreaterEqual(len(result["challenge1_evidence"]["candles_1h"]), 30)

    def test_unavailable_one_hour_context_does_not_block_a_valid_pattern(self):
        now = 2_000_000_000_000
        class ContextUnavailable(FakeBloFin):
            def get_candles(self, inst_id, *, bar, limit):
                if bar == "1H":
                    raise RuntimeError("1H feed unavailable")
                return super().get_candles(inst_id, bar=bar, limit=limit)

        client = ContextUnavailable({"5m": candle_rows(now, 5 * 60 * 1000)})
        bullish = {"name_en": "Hammer", "direction": "BULLISH", "score": 10}
        with (
            patch(
                "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
                return_value=[bullish],
            ),
            patch(
                "crypto_super_radar.challenge1_strategy._volume_confirmation",
                return_value={"confirmed": False, "volume_ratio": 0.8, "reason": "low volume"},
            ),
            patch("crypto_super_radar.challenge1_strategy._atr", return_value=1.0),
            patch("crypto_super_radar.challenge1_strategy.monitor_chart_patterns", return_value={}),
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "LONG")
        self.assertEqual(result["challenge1_evidence"]["context_data_status"], "UNAVAILABLE")
        self.assertEqual(result["challenge1_evidence"]["candles_1h"], [])


if __name__ == "__main__":
    unittest.main()
