from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from crypto_super_radar import challenge1_v2


def candles(index, *, start=100.0, step=0.05):
    close = [start + i * step for i in range(len(index))]
    return pd.DataFrame({
        "open": [x - 0.02 for x in close],
        "high": [x + 0.2 for x in close],
        "low": [x - 0.2 for x in close],
        "close": close,
        "volume": [1000.0] * len(index),
    }, index=index)


class Challenge1V2FastTriggerTests(unittest.TestCase):
    def test_confirmed_5m_wave_plus_aligned_15m_builds_fast_v2_plan(self):
        asof = pd.Timestamp("2026-11-23T12:00:00Z")
        h1_index = pd.date_range(end=asof - pd.Timedelta(hours=1), periods=210, freq="1h")
        h4_index = pd.date_range(end=asof - pd.Timedelta(hours=4), periods=210, freq="4h")
        m15_index = pd.date_range(end=asof - pd.Timedelta(minutes=15), periods=70, freq="15min")
        btc_index = pd.date_range(end=asof - pd.Timedelta(minutes=15), periods=40, freq="15min")
        fast_open = (asof - pd.Timedelta(minutes=5)).isoformat()
        wave = {
            "status": "CANDIDATE", "pattern": "BULLISH",
            "phase": "WAVES_1_AND_2_CONFIRMED", "confirmed_pivot_count": 3,
            "invalidation_price": 98.0, "pivots": [],
        }
        fast = {
            "signal": "LONG", "instrument_id": "BTC-USDT",
            "signal_bar_open_utc": fast_open,
            "signal_id": f"challenge1-v4:BTC-USDT:5m:{fast_open}",
            "entry_price_usd": 100.0, "stop_loss_usd": 98.5,
            "challenge1_evidence": {"elliott_wave": wave},
        }
        with patch.object(challenge1_v2, "challenge1_entry_evidence_ready", return_value=(True, "ok")):
            result = challenge1_v2.build_fast_trigger_signal(
                instrument_id="BTC-USDT",
                frame_1h=candles(h1_index),
                frame_4h=candles(h4_index),
                frame_15m=candles(m15_index),
                btc_15m=candles(btc_index),
                asof=asof,
                funding_rate=0.0001,
                funding_kind="0",
                funding_event_time_ms=int((asof - pd.Timedelta(hours=1)).timestamp() * 1000),
                funding_max_age_ms=9 * 60 * 60_000,
                spread_fraction=0.0002,
                fast_trigger_record=fast,
            )
        self.assertEqual(result["signal"], "LONG", result.get("reason"))
        self.assertTrue(result["fast_trigger_active"])
        self.assertEqual(result["challenge1_v2_evidence"]["setup_timeframe"], "5m")
        self.assertEqual(result["challenge1_v2_evidence"]["entry_execution_timeframe"], "15m")
        self.assertEqual(result["gross_rr"], 1.5)
        self.assertAlmostEqual(result["take_profit"], 102.25)
        self.assertFalse(result["orders_submitted"])

    def test_fast_trigger_is_blocked_when_completed_15m_trend_is_opposed(self):
        asof = pd.Timestamp("2026-11-23T12:00:00Z")
        h1_index = pd.date_range(end=asof - pd.Timedelta(hours=1), periods=210, freq="1h")
        h4_index = pd.date_range(end=asof - pd.Timedelta(hours=4), periods=210, freq="4h")
        m15_index = pd.date_range(end=asof - pd.Timedelta(minutes=15), periods=70, freq="15min")
        btc_index = pd.date_range(end=asof - pd.Timedelta(minutes=15), periods=40, freq="15min")
        fast_open = (asof - pd.Timedelta(minutes=5)).isoformat()
        fast = {
            "signal": "LONG", "instrument_id": "BTC-USDT",
            "signal_bar_open_utc": fast_open,
            "signal_id": f"challenge1-v4:BTC-USDT:5m:{fast_open}",
            "entry_price_usd": 100.0, "stop_loss_usd": 98.5,
            "challenge1_evidence": {"elliott_wave": {"status": "CANDIDATE", "pattern": "BULLISH"}},
        }
        # A falling 15m series cannot confirm the long trigger.
        falling = candles(m15_index, start=110.0, step=-0.05)
        with patch.object(challenge1_v2, "challenge1_entry_evidence_ready", return_value=(True, "ok")):
            result = challenge1_v2.build_fast_trigger_signal(
                instrument_id="BTC-USDT",
                frame_1h=candles(h1_index),
                frame_4h=candles(h4_index),
                frame_15m=falling,
                btc_15m=candles(btc_index),
                asof=asof,
                funding_rate=0.0001,
                funding_kind="0",
                funding_event_time_ms=int((asof - pd.Timedelta(hours=1)).timestamp() * 1000),
                funding_max_age_ms=9 * 60 * 60_000,
                spread_fraction=0.0002,
                fast_trigger_record=fast,
            )
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertIn("15m EMA21 trend", result["reason"])


if __name__ == "__main__":
    unittest.main()
