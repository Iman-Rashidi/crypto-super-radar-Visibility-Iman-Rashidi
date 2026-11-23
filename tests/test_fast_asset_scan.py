"""Tests for the bounded DEX manual-scan fast path."""
import time
import unittest
from unittest.mock import Mock, patch

from crypto_super_radar.fast_asset_scan import build_fast_asset_scan_report, run_with_hard_timeout
from crypto_super_radar.models import AssetSpec
from crypto_super_radar import web_app


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
            "txns": {"h24": {"buys": 240, "sells": 130}, "h1": {"buys": 18, "sells": 7}},
            "pairCreatedAt": 1795400000000,
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
        submitted = processor.call_args.args[0][0]
        self.assertEqual(submitted["buys_1h"], 18)
        self.assertEqual(submitted["sells_1h"], 7)
        self.assertTrue(submitted["pool_created_at_utc"])
        self.assertFalse(processor.call_args.kwargs["notify"])
        self.assertFalse(processor.call_args.kwargs["persist"])

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

    def test_manual_scan_routes_dex_contract_to_fast_engine_not_twelve_gate_engine(self):
        asset = AssetSpec.parse("solana:So11111111111111111111111111111111111111112")
        job_id = "fast-scan-test"
        web_app.JOBS[job_id] = {"status": "running"}
        report = {"status": "COMPLETE", "symbol": "EX", "token_name": "Example",
                  "opportunity_score": 70, "trade_authorization": "NO_TRADE"}
        with patch.object(web_app, "build_fast_asset_scan_report", return_value=report), \
             patch.object(web_app, "run_with_hard_timeout", return_value=(True, report)), \
             patch.object(web_app.ENGINE, "analyze") as analyze:
            web_app._run_scan(job_id, asset, "", "")
        self.assertEqual(web_app.JOBS[job_id]["status"], "completed")
        self.assertTrue(web_app.JOBS[job_id]["result"]["fast_asset_scan"])
        self.assertEqual(web_app.JOBS[job_id]["result"]["fast_scan"], report)
        analyze.assert_not_called()

    def test_manual_scan_timeout_is_terminal_and_does_not_wait_for_engine(self):
        asset = AssetSpec.parse("base:0x1111111111111111111111111111111111111111")
        job_id = "fast-scan-timeout-test"
        web_app.JOBS[job_id] = {"status": "running"}
        with patch.object(web_app, "run_with_hard_timeout", return_value=(False, None)), \
             patch.object(web_app.ENGINE, "analyze") as analyze:
            web_app._run_scan(job_id, asset, "", "")
        self.assertEqual(web_app.JOBS[job_id]["status"], "failed")
        self.assertEqual(web_app.JOBS[job_id]["error"], "FAST_SCAN_TIMEOUT")
        analyze.assert_not_called()

    def test_standalone_contract_addresses_are_detected(self):
        evm = web_app._parse_scan_asset("0x1111111111111111111111111111111111111111")
        sol = web_app._parse_scan_asset("So11111111111111111111111111111111111111112")
        self.assertEqual(evm.market_type, "dex")
        self.assertEqual(evm.chain, "eth")
        self.assertEqual(sol.market_type, "dex")
        self.assertEqual(sol.chain, "solana")


if __name__ == "__main__":
    unittest.main()
