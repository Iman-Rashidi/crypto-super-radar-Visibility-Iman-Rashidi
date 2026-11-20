import unittest

from scripts.diagnose_blofin_funding_history import summarize_schema_rows


class FundingHistorySchemaDiagnosticTests(unittest.TestCase):
    def test_missing_funding_kind_is_counted_as_missing_not_regular(self):
        result = summarize_schema_rows(
            [{"fundingTime": "1000", "fundingRate": "0.0001"},
             {"fundingTime": "2000", "fundingRate": "-0.0001", "fundingKind": None}],
            environment="BLOFIN_DEMO_REPLAY_SOURCE", query="latest_page",
        )
        self.assertEqual(result["funding_kind_distribution"], {"MISSING": 2})
        self.assertFalse(result["funding_kind_field_present_for_all_rows"])
        self.assertFalse(result["funding_kind_values_supported"])

    def test_supported_values_are_preserved_in_separate_control(self):
        result = summarize_schema_rows(
            [{"fundingTime": "1000", "fundingRate": "0.0001", "fundingKind": "0"},
             {"fundingTime": "2000", "fundingRate": "0.0002", "fundingKind": "1"}],
            environment="BLOFIN_PRODUCTION_SCHEMA_ONLY_NOT_FOR_REPLAY", query="historical_page",
        )
        self.assertEqual(result["funding_kind_distribution"], {"0": 1, "1": 1})
        self.assertTrue(result["funding_kind_values_supported"])
        self.assertEqual(result["environment"], "BLOFIN_PRODUCTION_SCHEMA_ONLY_NOT_FOR_REPLAY")

    def test_unknown_kind_is_not_supported(self):
        result = summarize_schema_rows(
            [{"fundingTime": "1000", "fundingRate": "0.0001", "fundingKind": "9"}],
            environment="BLOFIN_DEMO_REPLAY_SOURCE", query="latest_page",
        )
        self.assertEqual(result["funding_kind_distribution"], {"9": 1})
        self.assertFalse(result["funding_kind_values_supported"])

    def test_summary_never_exposes_funding_rate(self):
        result = summarize_schema_rows(
            [{"fundingTime": "1000", "fundingRate": "999", "fundingKind": "0"}],
            environment="BLOFIN_DEMO_REPLAY_SOURCE", query="latest_page",
        )
        self.assertNotIn("fundingRate", str(result))
        self.assertNotIn("999", str(result))


if __name__ == "__main__":
    unittest.main()
