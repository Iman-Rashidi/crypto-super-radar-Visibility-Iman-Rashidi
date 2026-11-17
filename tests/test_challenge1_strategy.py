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
        if bar != "5m":
            raise AssertionError(f"Challenge 1 must not request {bar} candles")
        return self.rows_by_bar["5m"]


def candle_rows(now_ms, interval_ms, count=65, unclosed_last=False):
    rows = []
    # The final bar opens one interval before now_ms and closes at now_ms.
    # When the last row is deliberately unclosed, the preceding close is one
    # interval old and remains inside the strategy's freshness tolerance.
    start = now_ms - count * interval_ms
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


def bullish_pattern():
    return {
        "name_en": "Bullish Engulfing",
        "direction": "BULLISH",
        "score": 13,
        "category": "CANDLESTICK",
    }


def bearish_pattern():
    return {
        "name_en": "Bearish Engulfing",
        "direction": "BEARISH",
        "score": -13,
        "category": "CANDLESTICK",
    }


class Challenge1FiveMinuteOnlyTests(unittest.TestCase):
    def client(self, now):
        return FakeBloFin({"5m": candle_rows(now, 5 * 60 * 1000)})

    def test_completed_frame_discards_unclosed_5m_bars_and_preserves_volume(self):
        now = 2_000_000_000_000
        rows = candle_rows(now, 5 * 60 * 1000, count=65, unclosed_last=True)
        frame = _completed_frame(
            FakeBloFin({"5m": rows}),
            inst_id="BTC-USDT",
            bar="5m",
            now_ms=now,
            minimum_rows=60,
        )
        self.assertEqual(len(frame), 64)
        self.assertEqual(float(frame["volume"].iloc[-1]), 1000.0)
        self.assertTrue(frame.index.is_monotonic_increasing)

    def test_completed_frame_rejects_stale_latest_5m_candle(self):
        now = 2_000_000_000_000
        interval = 5 * 60 * 1000
        rows = candle_rows(now, interval)
        with self.assertRaisesRegex(ValueError, "stale"):
            _completed_frame(
                FakeBloFin({"5m": rows}),
                inst_id="BTC-USDT",
                bar="5m",
                now_ms=now + interval + 60_001,
                minimum_rows=60,
            )

    def test_completed_frame_rejects_a_gap_in_recent_5m_candles(self):
        now = 2_000_000_000_000
        interval = 5 * 60 * 1000
        rows = candle_rows(now, interval)
        rows.pop(-10)
        with self.assertRaisesRegex(ValueError, "missing interval"):
            _completed_frame(
                FakeBloFin({"5m": rows}),
                inst_id="BTC-USDT",
                bar="5m",
                now_ms=now,
                minimum_rows=60,
            )

    def test_no_trade_is_valid_when_no_confirmed_5m_pattern_exists(self):
        now = 2_000_000_000_000
        client = self.client(now)
        with patch(
            "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
            return_value=[],
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertFalse(result["orders_submitted"])
        self.assertEqual(client.calls, ["5m"])
        evidence = result["challenge1_evidence"]
        self.assertEqual(evidence["setup_timeframe"], "5m")
        self.assertFalse(evidence["higher_timeframes_used"])
        self.assertEqual(set(k for k in evidence if k.startswith("candles_")), {"candles_5m"})
        self.assertGreaterEqual(len(evidence["candles_5m"]), 30)
        self.assertFalse(evidence["legacy_12_gates_used_for_entry"])

    def test_conflicting_confirmed_5m_patterns_block_entry(self):
        now = 2_000_000_000_000
        client = self.client(now)
        with patch(
            "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
            return_value=[bullish_pattern(), bearish_pattern()],
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertEqual(
            result["challenge1_evidence"]["decision"], "CONFLICTING_5M_PATTERNS"
        )
        self.assertEqual(client.calls, ["5m"])

    def test_one_valid_5m_pattern_is_eligible_without_any_higher_timeframe_feed(self):
        now = 2_000_000_000_000
        client = self.client(now)
        with (
            patch(
                "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
                return_value=[bullish_pattern()],
            ),
            patch(
                "crypto_super_radar.challenge1_strategy._volume_confirmation",
                return_value={
                    "confirmed": False, "volume_ratio": 0.7,
                    "reason": "weak volume is advisory only",
                },
            ),
            patch("crypto_super_radar.challenge1_strategy._atr", return_value=1.0),
            patch(
                "crypto_super_radar.challenge1_strategy.monitor_chart_patterns",
                return_value={},
            ),
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)

        self.assertEqual(client.calls, ["5m"])
        self.assertEqual(result["signal"], "LONG")
        self.assertEqual(result["execution"]["status"], "PASS")
        self.assertEqual(result["signal_id"], "challenge1-v4:BTC-USDT:5m:" + result["signal_bar_open_utc"])
        evidence = result["challenge1_evidence"]
        self.assertEqual(evidence["decision_engine_version"], "CHALLENGE_1_LOCAL_RULES_V4_5M_ONLY")
        self.assertEqual(evidence["setup_timeframe"], "5m")
        self.assertTrue(evidence["volume_is_advisory"])
        self.assertFalse(evidence["higher_timeframes_used"])
        self.assertEqual(set(k for k in evidence if k.startswith("candles_")), {"candles_5m"})
        self.assertEqual(evidence["chosen_pattern"]["timeframe"], "5m")
        self.assertGreaterEqual(len(evidence["candles_5m"]), 30)
        eligible, reason = challenge1_entry_evidence_ready(result)
        self.assertTrue(eligible, reason)

    def test_same_completed_5m_candle_has_one_signal_key_even_if_pattern_side_changes(self):
        now = 2_000_000_000_000
        client = self.client(now)
        with (
            patch(
                "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
                return_value=[bullish_pattern()],
            ),
            patch("crypto_super_radar.challenge1_strategy._volume_confirmation", return_value={"confirmed": False}),
            patch("crypto_super_radar.challenge1_strategy._atr", return_value=1.0),
            patch("crypto_super_radar.challenge1_strategy.monitor_chart_patterns", return_value={}),
        ):
            long_result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        with (
            patch(
                "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
                return_value=[bearish_pattern()],
            ),
            patch("crypto_super_radar.challenge1_strategy._volume_confirmation", return_value={"confirmed": False}),
            patch("crypto_super_radar.challenge1_strategy._atr", return_value=1.0),
            patch("crypto_super_radar.challenge1_strategy.monitor_chart_patterns", return_value={}),
        ):
            short_result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)

        self.assertEqual(long_result["signal"], "LONG")
        self.assertEqual(short_result["signal"], "SHORT")
        self.assertEqual(long_result["signal_id"], short_result["signal_id"])

    def test_valid_5m_pattern_does_not_wait_for_15m_or_1h_data(self):
        now = 2_000_000_000_000
        client = self.client(now)
        with (
            patch(
                "crypto_super_radar.challenge1_strategy._confirmed_pattern_candidates",
                return_value=[bearish_pattern()],
            ),
            patch(
                "crypto_super_radar.challenge1_strategy._volume_confirmation",
                return_value={"confirmed": False, "volume_ratio": 0.5},
            ),
            patch("crypto_super_radar.challenge1_strategy._atr", return_value=1.0),
            patch(
                "crypto_super_radar.challenge1_strategy.monitor_chart_patterns",
                return_value={},
            ),
        ):
            result = analyze_blofin_challenge1(client, "BTC-USDT", now_ms=now)
        self.assertEqual(result["signal"], "SHORT")
        self.assertEqual(client.calls, ["5m"])
        self.assertNotIn("candles_15m", result["challenge1_evidence"])
        self.assertNotIn("candles_1h", result["challenge1_evidence"])
        eligible, reason = challenge1_entry_evidence_ready(result)
        self.assertTrue(eligible, reason)


if __name__ == "__main__":
    unittest.main()
