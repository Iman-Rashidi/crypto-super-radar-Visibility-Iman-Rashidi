from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from crypto_super_radar.paper_execution import IdempotencyConflict, PaperExecutionEventLedger
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

    def simulate(self, **kwargs):
        test_now_ms = kwargs.pop("test_now_ms", 1_000_100)
        kwargs.setdefault("observed_at_ms", 1_000_000)
        with patch(
            "crypto_super_radar.paper_execution_adapter.time.time",
            return_value=test_now_ms / 1000.0,
        ):
            return self.adapter.simulate_fill(**kwargs)

    def test_partial_then_full_fill_respects_participation_limit(self):
        partial = self.simulate(
            client_order_id="paper-depth-1", snapshot_id="book-1",
            best_bid=100.0, best_ask=101.0, available_liquidity_qty=0.4,
            max_participation_pct=0.5, fee_rate=0.001, slippage_bps=10,
            occurred_at=101,
        )
        self.assertEqual(partial["status"], "PARTIALLY_FILLED")
        self.assertAlmostEqual(partial["new_fill_quantity"], 0.2)
        self.assertAlmostEqual(partial["fill_price"], 101.101)
        self.assertAlmostEqual(partial["fee_paid_usd"], 0.2 * 101.101 * 0.001)

        full = self.simulate(
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
        first = self.simulate(**args)
        repeated = self.simulate(**args)
        self.assertEqual(first["status"], "FILLED")
        self.assertEqual(repeated["status"], "DUPLICATE_SNAPSHOT")
        self.assertEqual(self.ledger.event_count(), 5)
        self.assertEqual(self.ledger.order_state("paper-depth-1")["filled_quantity"], 1.0)
        with self.assertRaises(IdempotencyConflict):
            self.simulate(
                client_order_id="paper-depth-1", snapshot_id="same-book",
                best_bid=100, best_ask=102, available_liquidity_qty=10,
                max_participation_pct=0.5,
            )

    def test_cumulative_fee_remains_monotonic_when_later_incremental_fee_is_smaller(self):
        first = self.simulate(
            client_order_id="paper-depth-1", snapshot_id="large-first-fill",
            best_bid=100.0, best_ask=101.0, available_liquidity_qty=0.8,
            max_participation_pct=1.0, fee_rate=0.001, slippage_bps=0,
            occurred_at=101,
        )
        self.assertEqual(first["status"], "PARTIALLY_FILLED")
        self.assertAlmostEqual(first["fee_paid_usd"], 0.0808)

        second = self.simulate(
            client_order_id="paper-depth-1", snapshot_id="small-final-fill",
            best_bid=100.0, best_ask=101.0, available_liquidity_qty=0.2,
            max_participation_pct=1.0, fee_rate=0.001, slippage_bps=0,
            occurred_at=102,
        )
        self.assertEqual(second["status"], "FILLED")
        self.assertAlmostEqual(second["fee_paid_usd"], 0.0202)
        self.assertAlmostEqual(second["cumulative_fee_paid_usd"], 0.101)
        state = self.ledger.order_state("paper-depth-1")
        self.assertAlmostEqual(state["fee_paid_usd"], 0.101)
        self.assertEqual(self.ledger.replay_order_states()["status"], "PASS")

    def test_stale_quote_and_empty_liquidity_do_not_fill(self):
        stale = self.simulate(
            client_order_id="paper-depth-1", snapshot_id="stale",
            best_bid=100, best_ask=101, available_liquidity_qty=10,
            observed_at_ms=994_100, test_now_ms=1_000_100,
        )
        empty = self.simulate(
            client_order_id="paper-depth-1", snapshot_id="empty",
            best_bid=100, best_ask=101, available_liquidity_qty=0,
        )
        self.assertEqual(stale["status"], "UNKNOWN")
        self.assertEqual(empty["status"], "NO_FILL")
        self.assertEqual(self.ledger.order_state("paper-depth-1")["filled_quantity"], 0.0)

    def test_rejects_invalid_book_and_participation(self):
        with self.assertRaisesRegex(ValueError, "best_ask"):
            self.simulate(
                client_order_id="paper-depth-1", snapshot_id="crossed",
                best_bid=102, best_ask=101, available_liquidity_qty=10,
            )
        with self.assertRaisesRegex(ValueError, "participation"):
            self.simulate(
                client_order_id="paper-depth-1", snapshot_id="overbooked",
                best_bid=100, best_ask=101, available_liquidity_qty=10,
                max_participation_pct=1.1,
            )

    def test_sell_simulation_uses_bid_and_adverse_slippage(self):
        self.adapter.submit_order(
            client_order_id="paper-sell", asset="cex:ETHUSDT", side="SELL",
            quantity=0.5, occurred_at=200,
        )
        result = self.simulate(
            client_order_id="paper-sell", snapshot_id="sell-book",
            best_bid=2000, best_ask=2002, available_liquidity_qty=1.0,
            max_participation_pct=1.0, fee_rate=0.001, slippage_bps=10,
        )
        self.assertEqual(result["status"], "FILLED")
        self.assertAlmostEqual(result["fill_price"], 1998.0)


    def test_future_quote_is_unknown_and_does_not_fill(self):
        result = self.simulate(
            client_order_id="paper-depth-1", snapshot_id="future-book",
            best_bid=100, best_ask=101, available_liquidity_qty=10,
            observed_at_ms=1_010_000, test_now_ms=1_000_100,
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.ledger.order_state("paper-depth-1")["filled_quantity"], 0.0)

    def test_missing_observed_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "observed_at_ms"):
            self.adapter.simulate_fill(
                client_order_id="paper-depth-1", snapshot_id="missing-time",
                best_bid=100, best_ask=101, available_liquidity_qty=10,
            )



    def test_caller_cannot_override_freshness_clock(self):
        with self.assertRaises(TypeError):
            self.adapter.simulate_fill(
                client_order_id="paper-depth-1", snapshot_id="caller-clock",
                best_bid=100, best_ask=101, available_liquidity_qty=10,
                observed_at_ms=1_000_000, now_ms=1_000_100,
            )



    def test_cancel_partial_order_preserves_filled_quantity_and_blocks_more_fills(self):
        partial = self.simulate(
            client_order_id="paper-depth-1", snapshot_id="cancel-after-partial",
            best_bid=100, best_ask=101, available_liquidity_qty=0.4,
            max_participation_pct=0.5, fee_rate=0.001, slippage_bps=0,
        )
        self.assertEqual(partial["status"], "PARTIALLY_FILLED")
        canceled = self.adapter.cancel_order(
            client_order_id="paper-depth-1", reason="RISK_REMAINDER_CANCEL", occurred_at=102
        )
        self.assertEqual(canceled["status"], "CANCELED")
        self.assertAlmostEqual(canceled["filled_quantity"], 0.2)
        self.assertAlmostEqual(canceled["remaining_quantity"], 0.8)
        repeated = self.adapter.cancel_order(client_order_id="paper-depth-1")
        self.assertEqual(repeated["status"], "ALREADY_CANCELED")
        blocked = self.simulate(
            client_order_id="paper-depth-1", snapshot_id="after-cancel",
            best_bid=100, best_ask=101, available_liquidity_qty=10,
        )
        self.assertEqual(blocked["status"], "BLOCKED")
        state = self.ledger.order_state("paper-depth-1")
        self.assertEqual(state["status"], "CANCELED")
        self.assertAlmostEqual(state["filled_quantity"], 0.2)
        self.assertEqual(self.ledger.replay_order_states()["status"], "PASS")

    def test_cancel_empty_order_and_filled_order_are_handled(self):
        empty = self.adapter.cancel_order(client_order_id="paper-depth-1")
        self.assertEqual(empty["status"], "CANCELED")
        self.assertEqual(empty["filled_quantity"], 0.0)
        self.assertEqual(empty["remaining_quantity"], 1.0)

        self.adapter.submit_order(
            client_order_id="paper-already-filled", asset="cex:ETHUSDT",
            side="BUY", quantity=0.5, occurred_at=200,
        )
        filled = self.simulate(
            client_order_id="paper-already-filled", snapshot_id="all-depth",
            best_bid=2000, best_ask=2001, available_liquidity_qty=1,
            max_participation_pct=1.0,
        )
        self.assertEqual(filled["status"], "FILLED")
        blocked = self.adapter.cancel_order(client_order_id="paper-already-filled")
        self.assertEqual(blocked["status"], "BLOCKED")

    def test_cancel_unknown_order_is_read_only(self):
        result = self.adapter.cancel_order(client_order_id="missing-order")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.ledger.event_count(), 4)



if __name__ == "__main__":
    unittest.main()
