from __future__ import annotations

import unittest
from decimal import Decimal

from crypto_super_radar.challenge1_v2_trade_controls import (
    V2PositionControlBlocked,
    plan_v2_position_exit,
    v2_entry_capacity,
)


class Challenge1V2TradeControlTests(unittest.TestCase):
    def test_four_positions_allowed_and_fifth_rejected(self):
        # Three active positions/orders leave exactly one of four slots available.
        positions = [{"instId": f"{symbol}-USDT"} for symbol in ("BTC", "ETH")]
        pending = [{"instId": "XRP-USDT"}]
        allowed, reason = v2_entry_capacity(
            positions, pending, candidate_symbol="LINK-USDT", max_concurrent_positions=4
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "CAPACITY_AVAILABLE")
        allowed, reason = v2_entry_capacity(
            positions + [{"instId": "LINK-USDT"}], pending,
            candidate_symbol="DOGE-USDT", max_concurrent_positions=4
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "MAX_CONCURRENT_POSITIONS_REACHED")

    def test_duplicate_symbol_is_rejected_even_below_capacity(self):
        allowed, reason = v2_entry_capacity(
            [{"instId": "BTC-USDT"}], [], candidate_symbol="btc-usdt"
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "SYMBOL_ALREADY_OPEN_OR_PENDING")

    def test_tp1_closes_half_and_calculates_fee_adjusted_be(self):
        result = plan_v2_position_exit(
            {"side": "LONG", "entry_price": "100", "initial_stop_loss": "90",
             "remaining_size": "2", "tp1_taken": False},
            mark_price="112", bars_since_entry=3, lot_size="0.1",
            min_size="0.1", fee_rate_per_side="0.0006",
        )
        self.assertEqual(result["action"], "TP1_PARTIAL_CLOSE_AND_MOVE_STOP")
        self.assertEqual(result["close_size"], "1")
        self.assertEqual(result["remaining_size"], "1")
        self.assertGreater(float(result["net_break_even_stop"]), 100)

    def test_twelve_bar_time_exit_uses_net_be_limit(self):
        result = plan_v2_position_exit(
            {"side": "SHORT", "entry_price": "100", "initial_stop_loss": "110",
             "remaining_size": "1", "tp1_taken": False},
            mark_price="102", bars_since_entry=12, lot_size="0.1",
            min_size="0.1", fee_rate_per_side="0.0006", tick_size="0.1",
        )
        self.assertEqual(result["action"], "TIME_EXIT_LIMIT_AT_NET_BREAK_EVEN")
        self.assertLess(float(result["limit_price"]), 100)
        self.assertEqual(Decimal(result["limit_price"]) % Decimal("0.1"), Decimal("0"))

    def test_tp1_half_below_minimum_fails_closed(self):
        with self.assertRaises(V2PositionControlBlocked):
            plan_v2_position_exit(
                {"side": "LONG", "entry_price": "100", "initial_stop_loss": "90",
                 "remaining_size": "0.1", "tp1_taken": False},
                mark_price="112", bars_since_entry=1, lot_size="0.1",
                min_size="0.1", fee_rate_per_side="0.0006",
            )


if __name__ == "__main__":
    unittest.main()
