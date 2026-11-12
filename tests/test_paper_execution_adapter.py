from __future__ import annotations

import sqlite3
import unittest

from crypto_super_radar.paper_execution import PaperExecutionEventLedger
from crypto_super_radar.paper_execution_adapter import PaperExecutionAdapter


class PaperExecutionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.ledger = PaperExecutionEventLedger(self.db)
        self.adapter = PaperExecutionAdapter(self.ledger)
        self.adapter.submit_order(
            client_order_id="paper-depth-1", asset="cex:BTCUSDT", side="BUY",
            quantity=1.0, occurred_at=100,
        )
        repeated = self.adapter.submit_order(
            client_order_id="paper-depth-1", asset="cex:BTCUSDT", side="BUY",
            quantity=1.0, occurred_at=100,
        )
        self.assertEqual(repeated["status"], "ACCEPTED")
        self.assertEqual(self.ledger.event_count(), 4)

    def tearDown(self):
        self.db.close()

    def test_partial_then_full_fill_respects_participation_limit(self):
        partial = self.adapter.simulate_fill(
            client_order_id="paper-depth-1", snapshot_id="book-1",
            best_bid=100.0, best_ask=101.0, available_liquidity_qty=0.4,
            max_participation_pct=0.5, fee_rate=0.001, slippage_bps=10,
            occurred_at=101,
        )
        self.assertEqual(partial["status"], "PARTIALLY_FILLED")
        self.assertAlmostEqual(partial["new_fill_quantity"], 0.2)
        self.assertAlmostEqual(partial["fill_price"], 101.101)
        self.assertAlmostEqual(partial["fee_paid_usd"], 0.2 * 101.101 * 0.001)

        full = self.adapter.simulate_fill(
            client_order_id="paper-depth-1", snapshot_id="book-2",
            best_bid=100.5, best_ask=101.5, available_liquidity_qty=2.0,
            max_participation_pct=0.5, fee_rate=0.001, slippage_bps=10,
            occurred_at=102,
        )
        self.assertEqual(full["status"], "FILLED")
        self.assertAlmostEqual(full["filled_quantity"], 1.0)
        self.assertAlmostEqual(full["remaining_quantity"], 0.0)
        events = self.ledger.recent_events(20, client_order_id="paper-depth-1")
        self.assertEqual([e["status"] for e in reversed(events)], [
            "CREATED", "VALIDATED", "SUBMITTED", "ACCEPTED",
            "PARTIALLY_FILLED", "FILLED",
        ])
        self.assertEqual(self.ledger.replay_order_states()["status"], "PASS")

    def test_same_snapshot_is_idempotent_and_does_not_double_fill(self):
        args = dict(
            client_order_id="paper-depth-1", snapshot_id="same-book",
            best_bid=100, best_ask=101, available_liquidity_qty=10,
            max_participation_pct=0.5,
        )
        first = self.adapter.simulate_fill(**args)
        repeated = self.adapter.simulate_fill(**args)
        self.assertEqual(first["status"], "FILLED")
        self.assertEqual(repeated["status"], "DUPLICATE_SNAPSHOT")
        self.assertEqual(self.ledger.event_count(), 5)
        self.assertEqual(self.ledger.order_state("paper-depth-1")["filled_quantity"], 1.0)
        with self.assertRaises(Exception):
            self.adapter.simulate_fill(
                client_order_id="paper-depth-1", snapshot_id="same-book",
                best_bid=100, best_ask=102, available_liquidity_qty=10,
                max_participation_pct=0.5,
            )

    def test_stale_quote_and_empty_liquidity_do_not_fill(self):
        stale = self.adapter.simulate_fill(
            client_order_id="paper-depth-1", snapshot_id="stale",
            best_bid=100, best_ask=101, available_liquidity_qty=10,
            quote_age_ms=6000,
        )
        empty = self.adapter.simulate_fill(
            client_order_id="paper-depth-1", snapshot_id="empty",
            best_bid=100, best_ask=101, available_liquidity_qty=0,
        )
        self.assertEqual(stale["status"], "UNKNOWN")
        self.assertEqual(empty["status"], "NO_FILL")
        self.assertEqual(self.ledger.order_state("paper-depth-1")["filled_quantity"], 0.0)

    def test_rejects_invalid_book_and_participation(self):
        with self.assertRaisesRegex(ValueError, "best_ask"):
            self.adapter.simulate_fill(
                client_order_id="paper-depth-1", snapshot_id="crossed",
                best_bid=102, best_ask=101, available_liquidity_qty=10,
            )
        with self.assertRaisesRegex(ValueError, "participation"):
            self.adapter.simulate_fill(
                client_order_id="paper-depth-1", snapshot_id="overbooked",
                best_bid=100, best_ask=101, available_liquidity_qty=10,
                max_participation_pct=1.1,
            )

    def test_sell_simulation_uses_bid_and_adverse_slippage(self):
        self.adapter.submit_order(
            client_order_id="paper-sell", asset="cex:ETHUSDT", side="SELL",
            quantity=0.5, occurred_at=200,
        )
        result = self.adapter.simulate_fill(
            client_order_id="paper-sell", snapshot_id="sell-book",
            best_bid=2000, best_ask=2002, available_liquidity_qty=1.0,
            max_participation_pct=1.0, fee_rate=0.001, slippage_bps=10,
        )
        self.assertEqual(result["status"], "FILLED")
        self.assertAlmostEqual(result["fill_price"], 1998.0)


if __name__ == "__main__":
    unittest.main()
