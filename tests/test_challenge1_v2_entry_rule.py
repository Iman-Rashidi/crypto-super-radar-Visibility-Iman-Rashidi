from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from crypto_super_radar import challenge1_v2


def candles(index, *, close=100.0, high=101.0, low=99.0, opened=100.0, volume=100.0):
    return pd.DataFrame({
        "open": opened, "high": high, "low": low,
        "close": close, "volume": volume,
    }, index=index)


class Challenge1V2OneHourEntryRuleTests(unittest.TestCase):
    def test_stop_distance_cap_is_3_5_percent(self):
        self.assertEqual(challenge1_v2.MAX_STOP_DISTANCE_PCT, 0.035)

    def _regime_frames(self, side):
        h1_index = pd.date_range("2026-01-01", periods=210, freq="1h", tz="UTC")
        h4_index = pd.date_range("2025-11-20", periods=210, freq="4h", tz="UTC")
        if side == "LONG":
            h1 = candles(h1_index, close=105.0, high=106.0, low=99.0)
            values = {21: 100.0, 55: 110.0, 200: 90.0}
        else:
            h1 = candles(h1_index, close=95.0, high=101.0, low=94.0)
            values = {21: 100.0, 55: 90.0, 200: 110.0}
        h4 = candles(h4_index)
        asof = h1_index[-1] + pd.Timedelta(hours=1)

        def fake_ema(series, period):
            value = values[period] if series.index.equals(h1_index) else 100.0
            return pd.Series(value, index=series.index, dtype=float)

        with (
            patch.object(challenge1_v2, "ema", side_effect=fake_ema),
            patch.object(challenge1_v2, "adx", side_effect=lambda frame, period: pd.Series(25.0, index=frame.index)),
        ):
            result = challenge1_v2._regime(h1, h4, asof)
        return result

    def test_long_uses_adx_and_ema21_even_when_close_is_below_ema55(self):
        result = self._regime_frames("LONG")
        self.assertEqual(result["direction"], "LONG")
        self.assertGreaterEqual(result["adx_1h"], 22)
        self.assertGreater(result["close_1h"], result["ema21_1h"])
        self.assertLess(result["close_1h"], 110.0)

    def test_short_uses_adx_and_ema21_even_when_close_is_above_ema55(self):
        result = self._regime_frames("SHORT")
        self.assertEqual(result["direction"], "SHORT")
        self.assertGreaterEqual(result["adx_1h"], 22)
        self.assertLess(result["close_1h"], result["ema21_1h"])
        self.assertGreater(result["close_1h"], 90.0)

    def test_15m_candle_pattern_rsi_and_volume_are_diagnostic_not_entry_gates(self):
        asof = pd.Timestamp("2026-11-23T12:00:00Z")
        h1_index = pd.date_range(end=asof - pd.Timedelta(hours=1), periods=210, freq="1h")
        h4_index = pd.date_range(end=asof - pd.Timedelta(hours=4), periods=210, freq="4h")
        m15_index = pd.date_range(end=asof - pd.Timedelta(minutes=15), periods=70, freq="15min")
        btc_index = pd.date_range(end=asof - pd.Timedelta(minutes=15), periods=40, freq="15min")
        h1 = candles(h1_index)
        h4 = candles(h4_index)
        # Deliberately bearish/weak 15m close: no breakout, no bullish candle,
        # no EMA value-zone overlap, and only 0.1x average volume.
        m15 = candles(m15_index, close=100.0, high=100.2, low=99.8, opened=100.1, volume=10.0)
        m15.iloc[-1, m15.columns.get_loc("volume")] = 1.0
        btc = candles(btc_index)
        regime = {
            "status": "TREND", "direction": "LONG", "adx_1h": 25.0, "ema21_slope_1h": 0.1,
            "close_1h": 105.0, "ema21_1h": 100.0, "ema55_1h": 110.0,
            "ema200_1h": 90.0, "aligned_4h": False, "opposed_4h": True,
            "neutral_4h": True, "structure_aligned_1h": False,
            "candle_open_utc": h1_index[-1].isoformat(),
            "candle_open_utc_4h": h4_index[-1].isoformat(),
        }
        atr_values = pd.Series(0.5, index=m15.index)
        wave = {
            "status": "AMBIGUOUS", "pattern": "AMBIGUOUS",
            "phase": "INSUFFICIENT_DATA", "timeframe": "1H",
            "evidence_source": "CONFIRMED_CLOSED_1H_CANDLES",
            "expected_signal_direction": "LONG",
            "latest_closed_candle_open_utc": h1_index[-1].isoformat(),
            "data_asof_utc": asof.isoformat(),
            "wave_alignment": "UNCONFIRMED",
        }
        with (
            patch.object(challenge1_v2, "_regime", return_value=regime),
            patch.object(challenge1_v2, "atr", return_value=atr_values),
            patch.object(challenge1_v2, "ema", side_effect=lambda series, period: pd.Series([99.0 + i * 0.001 for i in range(len(series))] if period == 21 else 103.0, index=series.index)),
            patch.object(challenge1_v2, "rsi", return_value=pd.Series(50.0, index=m15.index)),
            patch.object(challenge1_v2, "_elliott_wave_1h_evidence", return_value=wave),
        ):
            result = challenge1_v2.analyze_signal(
                instrument_id="BTC-USDT", frame_1h=h1, frame_4h=h4,
                frame_15m=m15, btc_15m=btc, asof=asof,
                funding_rate=0.0001, funding_kind="0",
                funding_event_time_ms=int((asof - pd.Timedelta(hours=1)).timestamp() * 1000),
                funding_max_age_ms=9 * 60 * 60_000, spread_fraction=0.0002,
            )
        self.assertEqual(result["signal"], "LONG", result.get("reason"))
        self.assertFalse(result["orders_submitted"])
        self.assertLess(result["volume_ratio"], 1.0)
        self.assertFalse(result["score_parts"].get("value_zone_rejection", 0))


if __name__ == "__main__":
    unittest.main()



class Challenge1V2EasyDemoEntryModeTests(unittest.TestCase):
    def test_btc_and_elliott_context_gates_are_advisory_only_when_demo_mode_is_explicit(self):
        import os
        from crypto_super_radar import challenge1_v2 as strategy
        from crypto_super_radar import blofin_demo_executor as executor
        with patch.dict(os.environ, {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
        }, clear=False):
            self.assertTrue(strategy._easy_demo_entry_enabled())
            self.assertTrue(strategy._btc_master_guard_allows_entry(False))
            wave = {"phase": "NO_RULE_VALID_IMPULSE", "pattern": "AMBIGUOUS",
                    "wave_alignment": "UNCONFIRMED"}
            self.assertTrue(executor._v2_elliott_entry_gate_allows("LONG", wave))
            self.assertTrue(executor._v2_elliott_entry_gate_allows("SHORT", {
                "phase": "WAVE_3_CONFIRMED", "pattern": "BULLISH",
                "wave_alignment": "OPPOSED",
            }))

    def test_easy_demo_mode_never_activates_without_both_demo_flags(self):
        import os
        from crypto_super_radar import challenge1_v2 as strategy
        from crypto_super_radar import blofin_demo_executor as executor
        with patch.dict(os.environ, {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "false",
        }, clear=False):
            self.assertFalse(strategy._easy_demo_entry_enabled())
            self.assertFalse(executor._v2_elliott_entry_gate_allows(
                "LONG", {"phase": "NO_RULE_VALID_IMPULSE", "pattern": "AMBIGUOUS"}
            ))

    def test_non_demo_v2_still_requires_aligned_confirmed_elliott(self):
        from crypto_super_radar import blofin_demo_executor as executor
        wave = {"phase": "NO_RULE_VALID_IMPULSE", "pattern": "AMBIGUOUS"}
        self.assertFalse(executor._v2_elliott_entry_gate_allows(
            "LONG", wave, easy_demo_entry=False
        ))
        self.assertTrue(executor._v2_elliott_entry_gate_allows(
            "LONG", {"phase": "WAVE_3_CONFIRMED", "pattern": "BULLISH"},
            easy_demo_entry=False
        ))
