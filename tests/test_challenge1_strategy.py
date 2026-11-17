from __future__ import annotations

import unittest
from unittest.mock import patch

from crypto_super_radar.challenge1_strategy import (
    _completed_frame,
    analyze_blofin_challenge1,
    challenge1_entry_evidence_ready,
)


class FakeBloFin:
    def __init__(self, rows_by_bar):
        self.rows_by_bar = rows_by_bar
        self.calls = []

    def get_candles(self, inst_id, *, bar, limit):
        self.calls.append(bar)
        return self.rows_by_bar[bar]


def candle_rows(now_ms, interval_ms, count=65, unclosed_last=False):
    rows = []
    start = now_ms - count * interval_ms - interval_ms
    for index in range(count):
        stamp = start + index * interval_ms
        close = 100.0 + index * 0.05
        rows.append([
            stamp, close - 0.01, close + 0.03, close - 0.03, close,
            1000.0, 100000.0, 0.0, "1",
        ])
    if unclosed_last:
        rows[-1][8] = "0"
    return rows


def market_data(now):
    return {
        "5m": candle_rows(now, 5 * 60 * 1000),
        "15m": candle_rows(now, 15 * 60 * 1000),
        "1H": candle_rows(now, 60 * 60 * 1000),
    }


def bullish_pattern():
    return {
        "name_en": "Bullish Engulfing", "direction": "BULLISH",
        "score": 13, "category": "CANDLESTICK",
    }


def bearish_pattern():
    return {
        "name_en": "Bearish Engulfing", "direction": "BEARISH",
        "score": -13, "category": "CANDLESTICK",
    }


class Challenge1CandleTests(unittest.TestCase):
    def test_completed_frame_discards_unclosed_5m_bars_and_preserves_volume(self):
        now = 2_000_000_000_000
        rows = candle_rows(now, 5 * 60 * 1000, count=65, unclosed_last=True)
        frame = _completed_frame(
            FakeBloFin({"5m": rows}), inst_id="BTC-USDT",
            bar="5m", now_ms=now, minimum_rows=60,
        )
        self.assertEqual(len(frame), 64)
        self.assertEqual(float(frame["volume"].iloc[-1]), 1000.0)
        self.assertTrue(frame.index.is_monotonic_increasing)

    def test_no_trade_is_valid_when_no_confirmed_5m_pattern_exists(self):
        now = 2_000_000_000_000
        client = FakeBloFin(market_data(now))
        with patch("crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates", return_value=[]):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertFalse(result["orders_submitted"])
        self.assertEqual(client.calls, ["5m", "15m", "1H"])
        evidence = result["challenge1_evidence"]
        self.assertEqual(evidence["setup_timeframe"], "5m")
        self.assertEqual(evidence["context_timeframe"], "15m")
        self.assertGreaterEqual(len(evidence["candles_5m"]), 30)
        self.assertGreaterEqual(len(evidence["candles_15m"]), 30)
        self.assertFalse(evidence["legacy_12_gates_used_for_entry"])

    def test_conflicting_confirmed_5m_patterns_block_entry(self):
        now = 2_000_000_000_000
        client = FakeBloFin(market_data(now))
        with patch(
            "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
            side_effect=[[bullish_pattern(), bearish_pattern()], [], []],
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertEqual(result["challenge1_evidence"]["decision"], "CONFLICTING_5M_PATTERNS")

    def test_5m_trigger_is_blocked_by_opposing_15m_context(self):
        now = 2_000_000_000_000
        client = FakeBloFin(market_data(now))
        with patch(
            "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
            side_effect=[[bullish_pattern()], [bearish_pattern()], []],
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertEqual(
            result["challenge1_evidence"]["context_alignment_15m"], "CONFLICT"
        )
        self.assertEqual(
            result["challenge1_evidence"]["decision"], "CONFLICTING_5M_15M_CONTEXT"
        )

    def test_5m_trigger_and_aligned_15m_context_are_eligible(self):
        now = 2_000_000_000_000
        client = FakeBloFin(market_data(now))
        with (
            patch(
                "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
                side_effect=[[bullish_pattern()], [bullish_pattern()], [bearish_pattern()]],
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
        evidence = result["challenge1_evidence"]
        self.assertEqual(evidence["context_alignment_15m"], "ALIGNED")
        self.assertTrue(evidence["volume_is_advisory"])
        self.assertTrue(evidence["context_conflict_1h"])
        self.assertIn(":5m:", result["signal_id"])
        self.assertGreaterEqual(len(evidence["candles_5m"]), 30)
        self.assertGreaterEqual(len(evidence["candles_15m"]), 30)
        eligible, reason = challenge1_entry_evidence_ready(result)
        self.assertTrue(eligible, reason)

    def test_neutral_15m_and_unavailable_1h_do_not_block_valid_5m_entry(self):
        now = 2_000_000_000_000

        class NoHourlyFeed(FakeBloFin):
            def get_candles(self, inst_id, *, bar, limit):
                self.calls.append(bar)
                if bar == "1H":
                    raise RuntimeError("1H feed unavailable")
                return self.rows_by_bar[bar]

        client = NoHourlyFeed(market_data(now))
        with (
            patch(
                "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
                side_effect=[[bullish_pattern()], [], []],
            ),
            patch(
                "crypto_super_radar.challenge1_strategy._volume_confirmation",
                return_value={"confirmed": False, "volume_ratio": 0.8, "reason": "low volume"},
            ),
            patch("crypto_super_radar.challenge1_strategy._atr", return_value=1.0),
            patch("crypto_super_radar.challenge1_strategy.monitor_chart_patterns", return_value={}),
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        evidence = result["challenge1_evidence"]
        self.assertEqual(result["signal"], "LONG")
        self.assertEqual(evidence["context_alignment_15m"], "NEUTRAL")
        self.assertEqual(evidence["context_1h_data_status"], "UNAVAILABLE")
        self.assertEqual(evidence["candles_1h"], [])
        eligible, reason = challenge1_entry_evidence_ready(result)
        self.assertTrue(eligible, reason)


if __name__ == "__main__":
    unittest.main()
