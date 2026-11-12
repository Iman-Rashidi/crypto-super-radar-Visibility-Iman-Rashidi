from __future__ import annotations

import unittest

from crypto_super_radar.order_flow_service import build_parser


class OrderFlowServiceCliTests(unittest.TestCase):
    def test_defaults_cover_both_public_spot_venues_and_core_symbols(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.providers, ["binance", "bybit"])
        self.assertEqual(args.symbols, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self.assertEqual(args.max_attempts, 0)

    def test_runtime_limits_and_selected_provider_are_configurable(self):
        args = build_parser().parse_args([
            "--providers", "binance",
            "--symbols", "BTCUSDT", "ADAUSDT",
            "--window-seconds", "120",
            "--minimum-coverage-pct", "90",
            "--stale-after-seconds", "10",
            "--max-attempts", "3",
        ])
        self.assertEqual(args.providers, ["binance"])
        self.assertEqual(args.symbols, ["BTCUSDT", "ADAUSDT"])
        self.assertEqual(args.window_seconds, 120)
        self.assertEqual(args.minimum_coverage_pct, 90)
        self.assertEqual(args.stale_after_seconds, 10)
        self.assertEqual(args.max_attempts, 3)


if __name__ == "__main__":
    unittest.main()
