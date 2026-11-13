from __future__ import annotations

import unittest

from crypto_super_radar.execution_costs import calculate_fill_costs, summarize_fill_costs


class ExecutionCostTests(unittest.TestCase):
    def test_buy_fill_separates_half_spread_from_adverse_slippage(self):
        row = calculate_fill_costs(
            side="BUY", quantity=2, bid=99, ask=101, fill_price=102,
            fee_paid_usd=0.4, funding_applicable=False,
            quote_quality="OBSERVED",
        )
        self.assertEqual(row["spread_cost_usd"], 2.0)
        self.assertEqual(row["slippage_cost_usd"], 2.0)
        self.assertEqual(row["fee_cost_usd"], 0.4)
        self.assertEqual(row["funding_status"], "NOT_APPLICABLE")
        self.assertEqual(row["total_execution_cost_usd"], 4.4)

    def test_sell_fill_has_adverse_slippage_only_below_bid(self):
        row = calculate_fill_costs(
            side="SELL", quantity=1, bid=99, ask=101, fill_price=98,
            fee_paid_usd=0.1, funding_applicable=True, funding_paid_usd=-0.05,
            quote_quality="MODELLED",
        )
        self.assertEqual(row["spread_cost_usd"], 1.0)
        self.assertEqual(row["slippage_cost_usd"], 1.0)
        self.assertEqual(row["funding_cost_usd"], -0.05)

    def test_quote_provenance_is_mandatory(self):
        with self.assertRaisesRegex(ValueError, "quote_quality"):
            calculate_fill_costs(
                side="BUY", quantity=1, bid=1, ask=2, fill_price=2,
                fee_paid_usd=0, funding_applicable=False, quote_quality="",
            )

    def test_invalid_crossed_quote_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ask"):
            calculate_fill_costs(
                side="BUY", quantity=1, bid=2, ask=1, fill_price=2,
                fee_paid_usd=0, funding_applicable=False, quote_quality="OBSERVED",
            )

    def test_perpetual_funding_cannot_be_assumed_zero(self):
        with self.assertRaisesRegex(ValueError, "funding_paid_usd"):
            calculate_fill_costs(
                side="BUY", quantity=1, bid=99, ask=101, fill_price=101,
                fee_paid_usd=0, funding_applicable=True, quote_quality="OBSERVED",
            )

    def test_aggregate_costs_require_full_component_coverage(self):
        first = calculate_fill_costs(
            side="BUY", quantity=1, bid=99, ask=101, fill_price=101,
            fee_paid_usd=0.1, funding_applicable=False, quote_quality="OBSERVED",
        )
        incomplete = dict(first)
        incomplete["slippage_cost_usd"] = None
        report = summarize_fill_costs([first, incomplete])
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIsNone(report["total_execution_cost_usd"])
        self.assertEqual(report["component_coverage_pct"]["slippage_cost_usd"], 50.0)
        self.assertFalse(report["performance_claim_allowed"])


if __name__ == "__main__":
    unittest.main()
