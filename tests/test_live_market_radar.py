from __future__ import annotations

import unittest
from datetime import datetime, timezone

import pandas as pd

from crypto_super_radar.live_market_radar import build_live_market_radar_payload


def synthetic_frame():
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(61):
        base = 100.0
        opened = base
        high = base + 1.0
        low = base - 1.0
        close = base + 0.2
        if i == 50:
            opened, high, low, close = 106.0, 110.0, 105.0, 108.0
        if i == 55:
            opened, high, low, close = 92.0, 94.0, 90.0, 91.0
        if i in (56, 57, 58):
            opened, high, low, close = 92.0, 94.0 + (i - 56) * 0.2, 91.5, 93.0
        if i == 59:
            opened, high, low, close = 93.0, 96.0, 92.5, 95.5
        if i == 60:
            # Still-forming candle: rendered, never used to produce a signal.
            opened, high, low, close = 95.5, 97.0, 95.0, 96.5
        rows.append({
            "open": opened, "high": high, "low": low,
            "close": close, "volume": 1000.0,
        })
    index = pd.date_range(start=start, periods=61, freq="5min")
    return pd.DataFrame(rows, index=index)


class LiveMarketRadarTests(unittest.TestCase):
    def setUp(self):
        self.frame = synthetic_frame()
        self.now = datetime(2025, 1, 1, 5, 3, tzinfo=timezone.utc)

    def test_completed_candles_only_drive_buy_alert_and_live_bar_is_display_only(self):
        payload = build_live_market_radar_payload(
            "BTCUSDT", "5m", self.frame, self.frame.copy(), now=self.now,
        )
        self.assertEqual(payload["cross_validation"]["status"], "PASS")
        self.assertEqual(payload["freshness_status"], "FRESH")
        self.assertEqual(payload["signal"]["signal"], "BUY")
        self.assertEqual(payload["signal"]["status"], "CONFIRMED")
        self.assertGreaterEqual(payload["signal"]["reward_risk"], 1.5)
        self.assertFalse(payload["candles"][-1]["is_closed"])
        self.assertTrue(payload["candles"][-2]["is_closed"])
        self.assertEqual(
            payload["signal"]["signal_candle_at"],
            payload["last_closed_candle_at"],
        )
        self.assertTrue(payload["signal_only"])
        self.assertFalse(payload["paper_bot_connected"])
        self.assertFalse(payload["orders_submitted"])
        self.assertIn("market_analysis", payload)
        self.assertIn("direction", payload["market_analysis"])
        self.assertIn("candlestick_patterns", payload["market_analysis"])
        self.assertIn("geometric_patterns", payload["market_analysis"])
        self.assertIn("wave_structure", payload["market_analysis"])
        self.assertEqual(payload["market_analysis"]["data_quality"]["cross_validation_status"], "PASS")
        self.assertTrue(payload["market_analysis"]["data_quality"]["signal_eligible"])

    def test_single_source_never_emits_buy_or_sell(self):
        payload = build_live_market_radar_payload(
            "BTCUSDT", "5m", self.frame, None, now=self.now,
            bybit_error="Bybit public endpoint unavailable",
        )
        self.assertEqual(payload["cross_validation"]["status"], "UNKNOWN")
        self.assertNotIn(payload["signal"]["signal"], {"BUY", "SELL"})
        self.assertEqual(payload["signal"]["status"], "BLOCKED")

    def test_provider_disagreement_blocks_signal(self):
        other = self.frame.copy()
        recent = other.index[-10:]
        other.loc[recent, "close"] *= 1.02
        other.loc[recent, "high"] = other.loc[recent, ["open", "high", "close"]].max(axis=1)
        payload = build_live_market_radar_payload(
            "BTCUSDT", "5m", self.frame, other, now=self.now,
        )
        self.assertEqual(payload["cross_validation"]["status"], "FAIL")
        self.assertNotIn(payload["signal"]["signal"], {"BUY", "SELL"})

    def test_stale_last_closed_candle_blocks_signal(self):
        stale_now = datetime(2025, 1, 1, 5, 30, tzinfo=timezone.utc)
        payload = build_live_market_radar_payload(
            "BTCUSDT", "5m", self.frame, self.frame.copy(), now=stale_now,
        )
        self.assertEqual(payload["freshness_status"], "STALE")
        self.assertNotIn(payload["signal"]["signal"], {"BUY", "SELL"})

    def test_invalid_interval_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported candle interval"):
            build_live_market_radar_payload(
                "BTCUSDT", "2m", self.frame, self.frame.copy(), now=self.now,
            )

    def test_one_second_interval_is_supported_but_signal_requires_independent_validation(self):
        frame = self.frame.iloc[:].copy()
        frame.index = pd.date_range("2025-01-01T00:00:00Z", periods=len(frame), freq="1s")
        now = datetime(2025, 1, 1, 0, 1, 0, 500000, tzinfo=timezone.utc)
        payload = build_live_market_radar_payload(
            "BTCUSDT", "1s", frame, None, now=now,
            bybit_error="Bybit does not support one-second candles",
        )
        self.assertEqual(payload["interval"], "1s")
        self.assertEqual(payload["cross_validation"]["status"], "UNKNOWN")
        self.assertEqual(payload["signal"]["status"], "BLOCKED")
        self.assertIn("market_flow", payload)
        self.assertFalse(payload["orders_submitted"])

    def test_monthly_interval_uses_calendar_month_boundaries(self):
        frame = self.frame.iloc[:4].copy()
        frame.index = pd.to_datetime([
            "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z",
            "2025-03-01T00:00:00Z", "2025-04-01T00:00:00Z",
        ])
        now = datetime(2025, 4, 15, tzinfo=timezone.utc)
        payload = build_live_market_radar_payload(
            "BTCUSDT", "1M", frame, None, now=now,
            bybit_error="Not independently validated",
        )
        self.assertEqual(payload["interval"], "1M")
        self.assertEqual(payload["last_closed_candle_at"], "2025-03-01T00:00:00+00:00")
        self.assertEqual(payload["candles"][-1]["is_closed"], False)

    def test_duplicate_or_malformed_candles_are_rejected(self):
        duplicate = self.frame.copy()
        duplicate.index = list(duplicate.index[:-1]) + [duplicate.index[-2]]
        with self.assertRaisesRegex(ValueError, "unique"):
            build_live_market_radar_payload(
                "BTCUSDT", "5m", duplicate, self.frame, now=self.now,
            )
        malformed = self.frame.copy()
        malformed.iloc[-2, malformed.columns.get_loc("high")] = 1.0
        with self.assertRaisesRegex(ValueError, "malformed"):
            build_live_market_radar_payload(
                "BTCUSDT", "5m", malformed, self.frame, now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
