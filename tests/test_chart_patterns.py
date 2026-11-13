from __future__ import annotations

import unittest

import pandas as pd

from crypto_super_radar.chart_patterns import (
    classify_confirmed_pivots,
    confirm_breakout_with_volume,
    contradicts_confirmed_breakout,
)


class TrianglePatternTests(unittest.TestCase):
    def setUp(self):
        self.pivots = [
            {"index": 0, "kind": "LOW", "price": 100.0},
            {"index": 4, "kind": "HIGH", "price": 110.0},
            {"index": 8, "kind": "LOW", "price": 102.0},
            {"index": 12, "kind": "HIGH", "price": 109.0},
            {"index": 16, "kind": "LOW", "price": 104.0},
            {"index": 20, "kind": "HIGH", "price": 108.0},
        ]

    def test_recognizes_symmetric_triangle_without_trade_signal(self):
        result = classify_confirmed_pivots(self.pivots, 106.0, 21)
        self.assertEqual(result["status"], "FORMING")
        self.assertEqual(result["pattern"], "SYMMETRICAL_TRIANGLE")
        self.assertEqual(result["upper_touches"], 3)
        self.assertEqual(result["lower_touches"], 3)
        self.assertEqual(result["breakout"], "NONE")
        self.assertIn("upper_line", result)

    def test_requires_three_confirmed_contacts_each_side(self):
        result = classify_confirmed_pivots(self.pivots[:4], 106.0, 21)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["pattern"], "UNKNOWN")

    def test_reports_breakout_only_after_close_outside_buffer(self):
        result = classify_confirmed_pivots(self.pivots, 110.0, 22)
        self.assertEqual(result["status"], "BREAKOUT_UP")
        self.assertEqual(result["breakout"], "UP")

    def test_opposing_volume_confirmed_breakout_vetoes_direction(self):
        self.assertTrue(contradicts_confirmed_breakout(
            "LONG", {"breakout": "DOWN", "breakout_confirmation": "CONFIRMED"}
        ))
        self.assertTrue(contradicts_confirmed_breakout(
            "SHORT", {"breakout": "UP", "breakout_confirmation": "CONFIRMED"}
        ))
        self.assertFalse(contradicts_confirmed_breakout(
            "LONG", {"breakout": "DOWN", "breakout_confirmation": "UNCONFIRMED"}
        ))

    def test_aligned_or_unconfirmed_pattern_does_not_veto(self):
        self.assertFalse(contradicts_confirmed_breakout("LONG", {"breakout": "UP"}))
        self.assertFalse(contradicts_confirmed_breakout("SHORT", {"breakout": "NONE"}))
        self.assertFalse(contradicts_confirmed_breakout("LONG", {"status": "UNKNOWN"}))

    def test_volume_and_close_location_confirm_up_breakout(self):
        index = pd.date_range("2026-01-01", periods=21, freq="D", tz="UTC")
        frame = pd.DataFrame({
            "high": [101.0] * 20 + [110.0],
            "low": [99.0] * 20 + [100.0],
            "close": [100.0] * 20 + [109.0],
            "volume": [100.0] * 20 + [150.0],
        }, index=index)
        result = confirm_breakout_with_volume("UP", frame)
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["volume_ratio"], 1.5)

    def test_volume_confirmation_rejects_weak_close_or_volume(self):
        index = pd.date_range("2026-01-01", periods=21, freq="D", tz="UTC")
        frame = pd.DataFrame({
            "high": [101.0] * 20 + [110.0],
            "low": [99.0] * 20 + [100.0],
            "close": [100.0] * 20 + [103.0],
            "volume": [100.0] * 21,
        }, index=index)
        result = confirm_breakout_with_volume("UP", frame)
        self.assertEqual(result["status"], "UNCONFIRMED")

    def test_rejects_non_finite_price(self):
        result = classify_confirmed_pivots(self.pivots, float("nan"), 21)
        self.assertEqual(result["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
