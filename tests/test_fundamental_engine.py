import unittest

from crypto_super_radar.fundamental_engine import (
    MODULE_WEIGHTS,
    build_fundamental_report,
    classify_industry,
    comparable_projects,
    discounted_protocol_cashflows,
    industry_analysis,
)


class IndustryAnalysisTests(unittest.TestCase):
    def test_sector_classification_requires_sector(self):
        self.assertEqual(classify_industry({})["status"], "UNKNOWN")
        self.assertEqual(classify_industry({"sector": "DeFi"})["sector"], "defi")

    def test_industry_score_tracks_known_criteria_and_missingness(self):
        evidence = {
            "market_growth": {"status": "KNOWN", "score": 80},
            "competition": {"status": "KNOWN", "score": 60},
        }
        result = industry_analysis(evidence)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["score"], 70)
        self.assertIn("regulatory_environment", result["unknown_criteria"])

    def test_invalid_industry_score_is_not_counted(self):
        result = industry_analysis({"market_growth": {"status": "KNOWN", "score": 101}})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["score"])


class ValuationTests(unittest.TestCase):
    def test_dcf_requires_explicit_token_value_capture(self):
        result = discounted_protocol_cashflows(
            [100, 120], discount_rate=0.2, terminal_growth_rate=0.03,
            net_treasury_value=10,
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["estimated_value"])

    def test_dcf_calculates_captured_cashflows_and_terminal_value(self):
        result = discounted_protocol_cashflows(
            [100, 120], discount_rate=0.2, terminal_growth_rate=0.03,
            token_value_capture_ratio=0.5, net_treasury_value=10,
        )
        self.assertEqual(result["status"], "KNOWN")
        self.assertGreater(result["estimated_value"], 0)
        self.assertEqual(result["token_value_capture_ratio"], 0.5)

    def test_dcf_rejects_invalid_rates_and_negative_cashflows(self):
        invalid_rates = discounted_protocol_cashflows(
            [100], discount_rate=0.1, terminal_growth_rate=0.1,
            token_value_capture_ratio=0.5,
        )
        negative = discounted_protocol_cashflows(
            [-10, 100], discount_rate=0.2, terminal_growth_rate=0.02,
            token_value_capture_ratio=0.5,
        )
        self.assertEqual(invalid_rates["status"], "UNKNOWN")
        self.assertEqual(negative["status"], "UNKNOWN")

    def test_comparables_only_use_same_sector_and_positive_denominators(self):
        project = {"sector": "defi", "market_cap": 1000, "revenue": 100,
                   "fdv": 2000, "tvl": 500}
        peers = [
            {"sector": "defi", "market_cap": 500, "revenue": 100,
             "fdv": 1000, "tvl": 500},
            {"sector": "gaming", "market_cap": 100, "revenue": 10,
             "fdv": 200, "tvl": 50},
            {"sector": "defi", "market_cap": 300, "revenue": 0,
             "fdv": 600, "tvl": 200},
        ]
        result = comparable_projects(project, peers)
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["peer_count"], 2)
        self.assertEqual(result["excluded_non_comparable_peers"], 1)
        self.assertEqual(result["peer_medians"]["market_cap_to_revenue"], 5.0)
        self.assertEqual(result["relative_valuation"]["market_cap_to_revenue"]["relative_to_peers_pct"], 100.0)


class FundamentalReportTests(unittest.TestCase):
    def test_missing_modules_never_become_zero_and_block_score(self):
        result = build_fundamental_report({})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["score"])
        self.assertEqual(result["modules"]["macro"]["status"], "UNKNOWN")
        self.assertFalse(result["trade_authorized"])

    def test_coverage_threshold_is_enforced(self):
        result = build_fundamental_report({
            "macro": {"status": "KNOWN", "score": 80},
            "tokenomics": {"status": "KNOWN", "score": 70},
            "onchain": {"status": "KNOWN", "score": 60},
        })
        self.assertEqual(result["status"], "INSUFFICIENT_COVERAGE")
        self.assertIsNone(result["score"])

    def test_full_coverage_uses_declared_weights(self):
        evidence = {
            "macro": {"status": "KNOWN", "score": 80},
            "tokenomics": {"status": "KNOWN", "score": 70},
            "onchain": {"status": "KNOWN", "score": 60},
            "project": {"status": "KNOWN", "score": 90},
            "industry": {key: {"status": "KNOWN", "score": 75} for key in (
                "market_growth", "market_share", "competition", "entry_barriers",
                "technology_trend", "regulatory_environment", "adoption")},
            "dcf": {"status": "KNOWN", "score": 65},
            "comparables": {"status": "KNOWN", "score": 85},
        }
        result = build_fundamental_report(evidence)
        expected = sum(MODULE_WEIGHTS[key] * value["score"]
                       for key, value in result["modules"].items()) / 100
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["coverage_pct"], 100.0)
        self.assertEqual(result["score"], round(expected, 2))
        self.assertFalse(result["trade_authorized"])


if __name__ == "__main__":
    unittest.main()
