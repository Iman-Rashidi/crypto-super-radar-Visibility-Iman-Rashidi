import unittest

from crypto_super_radar.fundamental_engine import (
    MODULE_WEIGHTS,
    build_fundamental_report,
    classify_industry,
    comparable_projects,
    discounted_protocol_cashflows,
    industry_analysis,
    accounting_principles_analysis,
    market_analysis,
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


class AccountingAndMarketAnalysisTests(unittest.TestCase):
    def test_inventory_and_depreciation_can_be_not_applicable_without_positive_score(self):
        result = accounting_principles_analysis({
            "inventory_valuation": {"applicability": "NOT_APPLICABLE"},
            "depreciation": {"applicability": "NOT_APPLICABLE"},
        })
        self.assertEqual(result["principles"]["inventory_valuation"]["status"], "NOT_APPLICABLE")
        self.assertEqual(result["principles"]["depreciation"]["status"], "NOT_APPLICABLE")
        self.assertIsNone(result["principles"]["inventory_valuation"]["score"])

    def test_accounting_methods_are_only_accepted_from_known_disclosures(self):
        result = accounting_principles_analysis({
            "inventory_valuation": {
                "applicability": "APPLICABLE",
                "valuation_method": "FIFO",
                "inventory_scope_disclosed": "PASS",
                "valuation_method_disclosed": "PASS",
                "impairment_or_obsolescence_disclosed": "PASS",
            },
            "depreciation": {
                "applicability": "APPLICABLE",
                "depreciation_method": "STRAIGHT_LINE",
            },
        })
        self.assertEqual(result["principles"]["inventory_valuation"]["reported_method"], "FIFO")
        self.assertIsNone(result["principles"]["depreciation"]["reported_method"])
        self.assertIn("WEIGHTED_AVERAGE", result["principles"]["inventory_valuation"]["accepted_methods"])

    def test_missing_accounting_evidence_remains_unknown(self):
        result = accounting_principles_analysis({})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["principles"]["revenue_recognition"]["status"], "UNKNOWN")

    def test_revenue_recognition_failure_is_flagged(self):
        result = accounting_principles_analysis({
            "revenue_recognition": {
                "performance_obligation_satisfied": "PASS",
                "revenue_basis_disclosed": "FAIL",
                "fees_separated_from_tokenholder_revenue": "UNKNOWN",
                "collectability_supported": "PASS",
            }
        })
        self.assertEqual(result["principles"]["revenue_recognition"]["status"], "FAIL")

    def test_market_analysis_requires_timestamped_source(self):
        result = market_analysis({
            "gdp_growth": {"status": "KNOWN", "value": 2.1, "source": "official"},
            "inflation": {"status": "KNOWN", "value": 2.5, "source": "official",
                          "observed_at_utc": "2026-11-01T00:00:00+00:00"},
        })
        self.assertEqual(result["observations"]["gdp_growth"]["status"], "UNKNOWN")
        self.assertEqual(result["observations"]["inflation"]["status"], "KNOWN")
        self.assertEqual(result["status"], "PARTIAL")

    def test_supporting_modules_do_not_change_seven_module_score_weights(self):
        result = build_fundamental_report({
            "accounting_principles": {
                "inventory_valuation": {"applicability": "NOT_APPLICABLE"},
            },
            "market_analysis": {},
        })
        self.assertEqual(set(result["weights"]), {
            "macro", "tokenomics", "onchain", "project", "industry", "dcf", "comparables"
        })
        self.assertIn("accounting_principles", result["modules"])
        self.assertIn("market_analysis", result["modules"])
        self.assertEqual(result["modules"]["market_analysis"]["status"], "UNKNOWN")
        self.assertIsNone(result["score"])


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
        expected = sum(MODULE_WEIGHTS[key] * result["modules"][key]["score"]
                       for key in MODULE_WEIGHTS) / 100
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["coverage_pct"], 100.0)
        self.assertEqual(result["score"], round(expected, 2))
        self.assertFalse(result["trade_authorized"])


if __name__ == "__main__":
    unittest.main()
