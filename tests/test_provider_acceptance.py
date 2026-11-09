from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from crypto_super_radar.models import AssetSpec
from crypto_super_radar.provider_acceptance import check_asset


def _service(prices=(100.0, 101.0), candle_age_hours=1):
    service = MagicMock()
    service.config.thresholds.provider_discrepancy_pct = 5.0
    service.binance.ticker.return_value = {"price": prices[0]}
    service.bybit.ticker.return_value = {"price": prices[1]}
    stamp = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=candle_age_hours)
    service.binance.candles.return_value = pd.DataFrame(
        {"close": [prices[0]]}, index=pd.DatetimeIndex([stamp])
    )
    return service


class LiveProviderAcceptanceTests(unittest.TestCase):
    def test_records_both_sources_and_fresh_candle(self):
        service = _service()
        result = check_asset(AssetSpec.parse("cex:BTCUSDT"), service)

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["read_only"])
        self.assertEqual([row["status"] for row in result["sources"]], ["PASS", "PASS"])
        for row in result["sources"]:
            self.assertIn("received_at_utc", row)
            self.assertGreaterEqual(row["latency_ms"], 0)
        self.assertEqual(result["freshness"]["status"], "PASS")
        self.assertEqual(service.binance.ticker.call_count, 1)
        self.assertEqual(service.bybit.ticker.call_count, 1)

    def test_price_difference_over_limit_fails(self):
        service = _service(prices=(100.0, 120.0))
        result = check_asset(AssetSpec.parse("cex:BTCUSDT"), service)

        self.assertEqual(result["price_comparison"]["status"], "FAIL")
        self.assertEqual(result["status"], "FAIL")

    def test_source_error_is_reported_and_other_source_is_still_checked(self):
        service = _service()
        service.binance.ticker.side_effect = RuntimeError("provider unavailable")
        result = check_asset(AssetSpec.parse("cex:BTCUSDT"), service)

        self.assertEqual(result["sources"][0]["status"], "FAIL")
        self.assertIn("provider unavailable", result["sources"][0]["error"])
        self.assertEqual(service.bybit.ticker.call_count, 1)
        self.assertEqual(result["price_comparison"]["status"], "UNKNOWN")
        self.assertEqual(result["status"], "FAIL")

    def test_stale_candles_fail_freshness(self):
        service = _service(candle_age_hours=72)
        result = check_asset(AssetSpec.parse("cex:BTCUSDT"), service, max_candle_age_hours=48)

        self.assertEqual(result["freshness"]["status"], "FAIL")
        self.assertEqual(result["status"], "FAIL")

    def test_solana_rpc_security_result_is_recorded(self):
        service = _service()
        service.solana_rpc.token_security.return_value = {
            "mint_authority_active": False,
            "freeze_authority_active": False,
            "top10_token_account_concentration_pct": 18.5,
        }
        asset = AssetSpec.parse("solana:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        result = check_asset(asset, service)

        self.assertEqual(result["on_chain_security"]["status"], "PASS")
        self.assertEqual(result["on_chain_security"]["provider"], "Solana RPC")
        self.assertIn("received_at_utc", result["on_chain_security"])
        self.assertEqual(service.solana_rpc.token_security.call_count, 1)

    def test_missing_candles_remain_unknown(self):
        service = _service()
        service.binance.candles.return_value = pd.DataFrame(
            columns=["close"],
            index=pd.DatetimeIndex([], tz="UTC"),
        )
        result = check_asset(AssetSpec.parse("cex:BTCUSDT"), service)

        self.assertEqual(result["freshness"]["status"], "UNKNOWN")
        self.assertEqual(result["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
