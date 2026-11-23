"""Tests for the bounded DEX manual-scan fast path."""
import time
import unittest
from unittest.mock import Mock

from crypto_super_radar.fast_asset_scan import build_fast_asset_scan_report, run_with_hard_timeout
from crypto_super_radar.models import AssetSpec


class FastAssetScanTests(unittest.TestCase):
    def test_contract_scan_uses_gem_hunter_v3_and_returns_market_card(self):
        asset = AssetSpec.parse("solana:So11111111111111111111111111111111111111112")
        pair = {
            "chainId": "solana",
            "dexId": "raydium",
            "url": "https://dexscreener.com/solana/pair",
            "pairAddress": "pair123",
            "baseToken": {"name": "Example", "symbol": "EX"},
            "quoteToken": {"symbol": "SOL"},
            "priceUsd": "0.00012",
            "liquidity": {"usd": 125000},
            "volume": {"h24": 56000},
            "txns": {"h24": {"buys": 240, "sells": 130}},
            "priceChange": {"m5": 3, "h1": 12, "h6": 24, "h24": 55},
            "marketCap": 500000,
            "fdv": 600000,
        }
        processor = Mock(return_value=[{
            **pair, "token_name": "Example", "token_symbol": "EX",
            "opportunity_score": 81, "risk_score": 62,
            "pump_phase_fa": "مرحله شتاب",
            "security_evidence": {"status": "UNKNOWN"},
            "whale_evidence": {"status": "NOT_AVAILABLE"},
        }])
        report = build_fast_asset_scan_report(
            asset, fetch_json=lambda url: [pair], v3_processor=processor
        )
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["price_usd"], 0.00012)
        self.assertEqual(report["opportunity_score"], 81)
        self.assertEqual(report["trade_authorization"], "NO_TRADE")
        processor.assert_called_once()
        self.assertFalse(processor.call_args.kwargs["notify"])

    def test_missing_pair_returns_limited_card(self):
        asset = AssetSpec.parse("base:0x1111111111111111111111111111111111111111")
        report = build_fast_asset_scan_report(asset, fetch_json=lambda url: [])
        self.assertEqual(report["status"], "LIMITED")
        self.assertEqual(report["red_flag_status"], "UNKNOWN")
        self.assertEqual(report["trade_authorization"], "NO_TRADE")

    def test_hard_timeout_returns_without_waiting_for_blocked_worker(self):
        started = time.monotonic()
        completed, result = run_with_hard_timeout(lambda: time.sleep(0.2), 0.02)
        self.assertFalse(completed)
        self.assertIsNone(result)
        self.assertLess(time.monotonic() - started, 0.15)

    def test_fast_path_timeout_does_not_execute_blocking_call_on_request_thread(self):
        completed, result = run_with_hard_timeout(lambda: "ok", 0.5)
        self.assertTrue(completed)
        self.assertEqual(result, "ok")


if __name__ == "__main__":
    unittest.main()
