from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from crypto_super_radar.paper_execution import PaperExecutionEventLedger
from crypto_super_radar.paper_fill_simulator import PaperFillSimulationError, PaperFillSimulator
from crypto_super_radar.paper_execution_adapter import PaperExecutionAdapter


class PaperFillSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.ledger = PaperExecutionEventLedger(self.db)
        self.simulator = PaperFillSimulator(self.ledger)
        self.quote = {
            "client_order_id": "paper-fill-test", "asset": "cex:BTCUSDT",
            "side": "BUY", "requested_quantity": 1.0, "bid": 99.0, "ask": 101.0,
            "available_liquidity_quantity": 10.0, "max_participation_rate": 1.0,
            "fee_bps": 10.0, "adverse_impact_bps": 10.0,
            "quote_quality": "OBSERVED", "quote_source": "TEST_BOOK",
            "quote_timestamp_ms": 1_000_000, "now_ms": 1_000_100,
            "simulated_latency_ms": 200,
        }

    def tearDown(self):
        self.db.close()

    def test_deterministic_two_slice_full_fill_records_cumulative_events(self):
        result = self.simulator.simulate_order(**self.quote, fill_fractions=(0.4, 0.6))
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["fill_count"], 2)
        self.assertAlmostEqual(result["filled_quantity"], 1.0)
        self.assertEqual(
            [event["status"] for event in result["simulated_events"]],
            ["CREATED", "VALIDATED", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "FILLED"],
        )
        self.assertEqual(self.ledger.order_state("paper-fill-test")["status"], "FILLED")
        self.assertEqual(self.ledger.replay_order_states()["status"], "PASS")
        self.assertFalse(result["orders_submitted"])

    def test_simulator_consumes_adapter_accepted_order_lifecycle(self):
        adapter = PaperExecutionAdapter(self.ledger)
        adapter.submit_order(
            client_order_id="paper-adapter-simulator",
            asset="cex:BTCUSDT", side="BUY", quantity=1.0,
            occurred_at=100,
        )
        result = self.simulator.simulate_order(
            **{
                **self.quote,
                "client_order_id": "paper-adapter-simulator",
            },
            fill_fractions=(0.5, 0.5),
        )
        self.assertEqual(result["status"], "FILLED")
        history = self.ledger.recent_events(
            20, client_order_id="paper-adapter-simulator"
        )
        self.assertEqual(
            [event["status"] for event in reversed(history)],
            [
                "CREATED", "VALIDATED", "SUBMITTED", "ACCEPTED",
                "PARTIALLY_FILLED", "FILLED",
            ],
        )
        self.assertEqual(self.ledger.replay_order_states()["status"], "PASS")

    def test_liquidity_and_participation_cap_produce_partial_fill(self):
        values = dict(self.quote)
        values["available_liquidity_quantity"] = 0.4
        values["max_participation_rate"] = 0.5
        result = self.simulator.simulate_order(**values, fill_fractions=(0.5, 0.5))
        self.assertEqual(result["status"], "PARTIALLY_FILLED")
        self.assertAlmostEqual(result["filled_quantity"], 0.2)
        self.assertAlmostEqual(result["remaining_quantity"], 0.8)
        self.assertTrue(result["liquidity"]["participation_cap_applied"])
        self.assertEqual(self.ledger.order_state("paper-fill-test")["status"], "PARTIALLY_FILLED")

    def test_cancel_unfilled_remainder_after_partial_fill(self):
        values = dict(self.quote)
        values["available_liquidity_quantity"] = 0.25
        result = self.simulator.simulate_order(
            **values, fill_fractions=(1.0,), cancel_unfilled_remainder=True
        )
        self.assertEqual(result["status"], "CANCELED")
        self.assertAlmostEqual(result["filled_quantity"], 0.25)
        self.assertAlmostEqual(result["remaining_quantity"], 0.75)
        self.assertEqual(self.ledger.order_state("paper-fill-test")["status"], "CANCELED")

    def test_no_liquidity_keeps_order_accepted_or_cancels_explicitly(self):
        values = dict(self.quote)
        values["available_liquidity_quantity"] = 0.0
        accepted = self.simulator.simulate_order(**values)
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertEqual(accepted["fill_count"], 0)
        self.assertEqual(self.ledger.order_state("paper-fill-test")["status"], "ACCEPTED")

        db2 = sqlite3.connect(":memory:")
        db2.row_factory = sqlite3.Row
        ledger2 = PaperExecutionEventLedger(db2)
        canceled = PaperFillSimulator(ledger2).simulate_order(
            **values, cancel_unfilled_remainder=True
        )
        self.assertEqual(canceled["status"], "CANCELED")
        self.assertEqual(ledger2.order_state("paper-fill-test")["status"], "CANCELED")
        db2.close()

    def test_spread_slippage_and_fee_are_decomposed_per_fill(self):
        result = self.simulator.simulate_order(**self.quote)
        self.assertAlmostEqual(result["execution_costs"]["spread_cost_usd"], 1.0)
        self.assertAlmostEqual(result["execution_costs"]["adverse_slippage_cost_usd"], 0.101)
        self.assertAlmostEqual(result["execution_costs"]["fee_cost_usd"], 0.101101)
        self.assertEqual(result["execution_costs"]["funding_status"], "NOT_APPLICABLE")

    def test_latency_is_deterministic_metadata_not_sleep(self):
        result = self.simulator.simulate_order(**self.quote, fill_fractions=(0.5, 0.5))
        self.assertEqual(result["simulated_latency_ms"], 200)
        self.assertFalse(result["latency_is_wall_clock_delay"])
        self.assertEqual(
            [fill["occurred_at_ms"] for fill in result["fills"]],
            [1_000_200, 1_000_300],
        )

    def test_sell_fill_applies_adverse_impact_below_bid(self):
        values = dict(self.quote)
        values["side"] = "SELL"
        result = self.simulator.simulate_order(**values)
        self.assertAlmostEqual(result["fills"][0]["average_fill_price"], 98.901)

    def test_observed_stale_or_future_quote_fails_closed(self):
        values = dict(self.quote)
        values["quote_timestamp_ms"] = 1_000
        with self.assertRaisesRegex(PaperFillSimulationError, "stale or from the future"):
            self.simulator.simulate_order(**values)
        values = dict(self.quote)
        values["quote_timestamp_ms"] = 1_010_000
        with self.assertRaisesRegex(PaperFillSimulationError, "stale or from the future"):
            self.simulator.simulate_order(**values)

    def test_missing_observed_quote_timestamp_is_rejected(self):
        values = dict(self.quote)
        values["quote_timestamp_ms"] = None
        with self.assertRaisesRegex(PaperFillSimulationError, "requires quote_timestamp_ms"):
            self.simulator.simulate_order(**values)

    def test_schedule_and_input_validation_fail_closed(self):
        for fractions in ((), (0.5, 0.4), (0.0, 1.0), (float("nan"),)):
            with self.subTest(fractions=fractions):
                with self.assertRaises(PaperFillSimulationError):
                    self.simulator.simulate_order(**self.quote, fill_fractions=fractions)
        values = dict(self.quote)
        values["available_liquidity_quantity"] = None
        with self.assertRaises(PaperFillSimulationError):
            self.simulator.simulate_order(**values)
        values = dict(self.quote)
        values["max_participation_rate"] = 1.1
        with self.assertRaises(PaperFillSimulationError):
            self.simulator.simulate_order(**values)

    def test_modelled_quote_is_explicitly_labelled(self):
        values = dict(self.quote)
        values["quote_quality"] = "MODELLED"
        values["quote_timestamp_ms"] = None
        result = self.simulator.simulate_order(**values)
        self.assertEqual(result["quote"]["quality"], "MODELLED")


    def test_simulated_fill_events_survive_database_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "simulated-execution.sqlite3")
            first_db = sqlite3.connect(path)
            first_db.row_factory = sqlite3.Row
            first_ledger = PaperExecutionEventLedger(first_db)
            with first_db:
                result = PaperFillSimulator(first_ledger).simulate_order(
                    **self.quote, fill_fractions=(0.25, 0.75)
                )
                self.assertEqual(result["status"], "FILLED")
            first_db.close()

            second_db = sqlite3.connect(path)
            second_db.row_factory = sqlite3.Row
            second_ledger = PaperExecutionEventLedger(second_db)
            state = second_ledger.order_state("paper-fill-test")
            self.assertEqual(state["status"], "FILLED")
            self.assertAlmostEqual(state["filled_quantity"], 1.0)
            self.assertEqual(second_ledger.replay_order_states()["status"], "PASS")
            second_db.close()



if __name__ == "__main__":
    unittest.main()
