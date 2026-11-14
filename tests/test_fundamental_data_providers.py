import unittest
from datetime import datetime, timezone

from crypto_super_radar.fundamental_data_providers import (
    DefiLlamaFundamentalProvider,
    FundamentalProviderError,
    evidence_record,
)
from crypto_super_radar.fundamental_engine import build_fundamental_report


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError("Unexpected URL: " + url)
        return self.responses[url]


class DefiLlamaFundamentalProviderTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 14, 19, 0, tzinfo=timezone.utc)
        self.protocol_url = "https://api.llama.fi/protocol/aave"
        self.fees_url = "https://api.llama.fi/summary/fees/aave"

    def provider(self, protocol=None, fees=None):
        http = FakeHttp({
            self.protocol_url: protocol if protocol is not None else {
                "name": "Aave",
                "category": "Lending",
                "symbol": "AAVE",
                "chains": ["Ethereum"],
                "url": "https://aave.com",
                "github": ["aave"],
                "tvl": [{"date": self.now.timestamp() - 3600, "totalLiquidityUSD": 1_000_000}],
            },
            self.fees_url: fees if fees is not None else {
                "total24h": 1000,
                "totalRevenue24h": 250,
                "totalDataChart": [[self.now.timestamp() - 1800, 1000]],
            },
        })
        return DefiLlamaFundamentalProvider(http, clock=lambda: self.now), http

    def test_valid_response_has_source_timestamp_and_freshness(self):
        provider, http = self.provider()
        result = provider.fetch("aave")
        tvl = result["project"]["evidence"]["data_points"]["tvl_usd"]
        fees = result["project"]["evidence"]["data_points"]["fees_24h_usd"]
        self.assertEqual(tvl["status"], "KNOWN")
        self.assertEqual(tvl["freshness"], "FRESH")
        self.assertEqual(tvl["source"], "DefiLlama protocol TVL")
        self.assertEqual(fees["value"], 1000)
        self.assertEqual(result["provider_metadata"]["data_quality"], "VALID")
        self.assertEqual(len(http.calls), 2)

    def test_old_observation_is_stale_and_not_known(self):
        old = self.now.timestamp() - 10 * 86400
        provider, _ = self.provider(
            protocol={"tvl": [{"date": old, "totalLiquidityUSD": 1000}]},
            fees={"total24h": 10, "totalDataChart": [[old, 10]]},
        )
        result = provider.fetch("aave")
        tvl = result["project"]["evidence"]["data_points"]["tvl_usd"]
        self.assertEqual(tvl["freshness"], "EXPIRED")
        self.assertEqual(tvl["status"], "UNKNOWN")

    def test_missing_or_invalid_metrics_remain_unknown(self):
        provider, _ = self.provider(
            protocol={"tvl": [{"date": self.now.timestamp(), "totalLiquidityUSD": None}]},
            fees={"total24h": "not-a-number", "totalDataChart": []},
        )
        result = provider.fetch("aave")
        points = result["project"]["evidence"]["data_points"]
        self.assertEqual(points["tvl_usd"]["status"], "UNKNOWN")
        self.assertEqual(points["fees_24h_usd"]["status"], "UNKNOWN")
        self.assertIsNone(points["fees_24h_usd"]["value"])

    def test_invalid_slug_is_rejected_before_network_access(self):
        provider, http = self.provider()
        with self.assertRaises(FundamentalProviderError):
            provider.fetch("../aave")
        self.assertEqual(http.calls, [])

    def test_invalid_provider_schema_is_rejected(self):
        provider, _ = self.provider(protocol=[], fees={})
        with self.assertRaises(FundamentalProviderError):
            provider.fetch("aave")

    def test_future_dated_observation_is_conflicting_and_unknown(self):
        future = self.now.timestamp() + 3600
        record = evidence_record(
            value=42, source="fixture", source_url="https://example.invalid",
            fetched_at=self.now, observed_at=future, unit="USD",
        )
        self.assertEqual(record["freshness"], "CONFLICT")
        self.assertEqual(record["quality"], "CONFLICT")
        self.assertEqual(record["status"], "UNKNOWN")

    def test_conflicting_quality_is_never_marked_known(self):
        record = evidence_record(
            value=42, source="fixture", source_url="https://example.invalid",
            fetched_at=self.now, observed_at=self.now.timestamp(),
            unit="USD", quality="CONFLICT",
        )
        self.assertEqual(record["freshness"], "CONFLICT")
        self.assertEqual(record["status"], "UNKNOWN")

    def test_raw_provenance_survives_fundamental_report(self):
        provider, _ = self.provider()
        evidence = provider.fetch("aave")
        report = build_fundamental_report(evidence)
        project = report["modules"]["project"]
        self.assertEqual(project["status"], "UNKNOWN")
        self.assertIsNone(project["score"])
        self.assertEqual(project["evidence"]["provider"], "DefiLlama")
        self.assertFalse(report["trade_authorized"])


if __name__ == "__main__":
    unittest.main()
