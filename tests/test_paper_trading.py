from __future__ import annotations

import unittest
import time
import os
import tempfile
from types import SimpleNamespace

import pandas as pd

from crypto_super_radar.paper_trading import PaperLedger, PaperTradingBot
from crypto_super_radar.paper_execution import make_paper_client_order_id


class FakeEngine:
    def __init__(self, signal="LONG", gate_status="PASS", price=100.0):
        self.result = SimpleNamespace(
            signal=signal,
            gates=[SimpleNamespace(status=gate_status) for _ in range(12)],
            confirmation_score=82.0,
            estimated_risk_score=20.0,
            stop_loss=90.0,
            take_profit=120.0,
            expected_holding_days=2,
        )
        self.data = SimpleNamespace(
            snapshots={"cex:BTCUSDT": SimpleNamespace(price_usd=price)},
            candle_frames={
                "cex:BTCUSDT": pd.DataFrame(
                    {"close": [98.0, 99.0, 100.0]},
                    index=pd.date_range("2026-11-01", periods=3, freq="D", tz="UTC"),
                )
            },
        )
        self.data.order_book_source = "Fake Binance Spot bookTicker"
        self.data.order_book_bid = 99.9
        self.data.order_book_ask = 100.0
        self.data.order_book_buy_qty = 100.0
        self.data.order_book_sell_qty = 100.0
        self.data.order_book_age_ms = 0
        self.data.order_book_override = False
        def order_book_snapshot(asset):
            market_price = self.data.snapshots[asset.raw].price_usd
            bid = self.data.order_book_bid if self.data.order_book_override else max(0.01, market_price - 0.1)
            ask = self.data.order_book_ask if self.data.order_book_override else market_price
            return {
                "symbol": asset.symbol,
                "best_bid": bid,
                "best_ask": ask,
                "available_liquidity_buy_qty": self.data.order_book_buy_qty,
                "available_liquidity_sell_qty": self.data.order_book_sell_qty,
                "observed_at_ms": int(time.time() * 1000) - self.data.order_book_age_ms,
                "timestamp_kind": "LOCAL_RECEIVED_AT",
                "source": self.data.order_book_source,
                "orders_submitted": False,
            }
        self.data.order_book_snapshot = order_book_snapshot

    def analyze(self, asset):
        return self.result

    def snapshot(self, asset):
        return self.data.snapshots[asset.raw]


class PaperCircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = PaperLedger(":memory:")

    def tearDown(self):
        self.ledger.close()

    def record_loss(self, number):
        entry = {
            "asset": "cex:BTCUSDT", "quantity": 1.0, "entry_price": 100.0,
            "stop_loss": 90.0, "initial_stop_loss": 90.0, "take_profit": 120.0,
            "entry_fee": 0.0, "opened_at": number, "signal_bar": f"bar-{number}",
            "confirmation_score": 80.0, "risk_budget": 50.0,
            "max_holding_days": 14,
        }
        self.assertTrue(self.ledger.open_position(entry, 100.0))
        self.ledger.close_position(99.0, 0.0, "TEST_LOSS")

    def test_consecutive_loss_circuit_breaker(self):
        for number in range(1, 4):
            self.record_loss(number)
        result = self.ledger.risk_controls(
            asset="cex:BTCUSDT", daily_loss_limit_pct=0.50,
            max_drawdown_pct=0.50, max_consecutive_losses=3,
            cooldown_hours_after_loss=0,
        )
        self.assertFalse(result["entry_allowed"])
        self.assertIn("CONSECUTIVE_LOSSES", result["halt_reasons"])

    def test_initial_stop_and_cost_aware_performance_are_persisted(self):
        self.record_loss(1)
        state = self.ledger.state()
        self.assertEqual(state["recent_trades"][0]["initial_stop_loss"], 90.0)
        self.assertEqual(state["performance"]["sample_size"], 1)
        self.assertEqual(state["performance"]["status"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(state["performance"]["r_sample_size"], 1)
        self.assertFalse(state["performance"]["performance_claim_allowed"])

    def test_peak_to_trough_drawdown_circuit_breaker(self):
        self.record_loss(1)
        result = self.ledger.risk_controls(
            asset="cex:BTCUSDT", daily_loss_limit_pct=0.50,
            max_drawdown_pct=0.0001, max_consecutive_losses=5,
            cooldown_hours_after_loss=0,
        )
        self.assertIn("MAX_EQUITY_DRAWDOWN", result["halt_reasons"])

    def test_post_loss_cooldown_is_asset_scoped(self):
        self.record_loss(1)
        result = self.ledger.risk_controls(
            asset="cex:BTCUSDT", daily_loss_limit_pct=0.50,
            max_drawdown_pct=0.50, max_consecutive_losses=5,
            cooldown_hours_after_loss=24,
        )
        self.assertIn("POST_LOSS_COOLDOWN", result["halt_reasons"])


class PaperTradingBotTests(unittest.TestCase):
    def setUp(self):
        self.ledger = PaperLedger(":memory:")
        self.engine = FakeEngine()
        self.bot = PaperTradingBot(self.engine, ledger=self.ledger)

    def tearDown(self):
        self.ledger.close()

    def test_opens_a_small_risk_limited_paper_position(self):
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "OPENED")
        self.assertEqual(result["mode"], "PAPER_ONLY")
        self.assertFalse(result["orders_submitted"])
        self.assertLessEqual(result["quantity"] * result["entry_price"], 1_000.0)
        self.assertGreater(result["quantity"], 0)
        self.assertIsNotNone(self.ledger.position())
        position = self.ledger.position()
        self.assertEqual(position["entry_reference_price"], 100.0)
        self.assertGreater(position["entry_slippage_usd"], 0.0)

    def test_fresh_observed_bid_ask_records_spread_and_slippage_per_fill(self):
        snapshot = self.engine.data.snapshots["cex:BTCUSDT"]
        self.engine.data.order_book_bid = 99.9
        self.engine.data.order_book_ask = 100.1
        self.engine.data.order_book_source = "TestExchange"
        self.engine.data.order_book_override = True
        snapshot.raw = {
            "provider_a": {
                "bid_price": 99.9,
                "ask_price": 100.1,
                "quote_timestamp_ms": int(time.time() * 1000),
                "provider": "TestExchange",
            }
        }
        opened = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(opened["status"], "OPENED")
        position = self.ledger.position()
        self.assertEqual(position["entry_quote_source"], "TestExchange")
        self.assertGreater(position["entry_spread_cost_usd"], 0.0)
        self.assertGreater(position["entry_slippage_usd"], 0.0)

        snapshot.price_usd = 125.0
        snapshot.raw["provider_a"].update({
            "bid_price": 119.9,
            "ask_price": 120.1,
            "quote_timestamp_ms": int(time.time() * 1000),
        })
        closed = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(closed["status"], "CLOSED")
        trade = self.ledger.state()["recent_trades"][0]
        self.assertGreater(trade["entry_spread_cost_usd"], 0.0)
        self.assertGreater(trade["exit_spread_cost_usd"], 0.0)
        self.assertGreater(trade["spread_cost_usd"], 0.0)
        self.assertEqual(trade["entry_quote_source"], "TestExchange")
        self.assertEqual(trade["exit_quote_source"], "TestExchange")
        self.assertEqual(self.ledger.state()["performance"]["spread_data_status"], "COMPLETE")
        self.assertEqual(
            self.ledger.state()["performance"]["execution_cost_attribution_status"],
            "COMPLETE",
        )

    def test_stale_order_book_blocks_entry_without_creating_an_order(self):
        self.engine.data.order_book_age_ms = 6_000
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(self.ledger.position())
        self.assertEqual(self.ledger.execution_ledger.event_count(), 0)
        self.assertFalse(self.ledger.state()["orders_submitted"])

    def test_partial_entry_projects_only_actual_fills_and_cancels_remainder(self):
        self.engine.data.order_book_buy_qty = 0.25
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "PARTIALLY_OPENED")
        position = self.ledger.position()
        self.assertIsNotNone(position)
        self.assertLess(position["quantity"], result["requested_quantity"])
        self.assertAlmostEqual(position["quantity"], result["quantity"])
        self.assertAlmostEqual(position["entry_price"], result["entry_price"])
        self.assertAlmostEqual(position["entry_fee"], result["fee_usd"])

        order = self.ledger.execution_ledger.order_state(result["client_order_id"])
        self.assertEqual(order["status"], "CANCELED")
        self.assertAlmostEqual(order["filled_quantity"], position["quantity"])
        fills = [
            event for event in self.ledger.execution_ledger.recent_events(
                20, client_order_id=result["client_order_id"]
            )
            if event["status"] in {"PARTIALLY_FILLED", "FILLED"}
        ]
        self.assertTrue(fills)
        self.assertAlmostEqual(fills[0]["filled_quantity"], position["quantity"])
        self.assertAlmostEqual(fills[0]["fee_paid_usd"], position["entry_fee"])
        reconciliation = self.ledger.reconciliation()
        self.assertEqual(reconciliation["status"], "PASS")
        self.assertEqual(reconciliation["matched_fills"], 1)
        self.assertFalse(result["orders_submitted"])

    def test_zero_liquidity_cancels_order_without_opening_position_or_retrying_bar(self):
        self.engine.data.order_book_buy_qty = 0.0
        first = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(first["status"], "NO_TRADE")
        self.assertEqual(first["execution_status"], "CANCELED")
        self.assertIsNone(self.ledger.position())
        order = self.ledger.execution_ledger.order_state(first["client_order_id"])
        self.assertEqual(order["status"], "CANCELED")
        self.assertEqual(order["filled_quantity"], 0.0)
        event_count = self.ledger.execution_ledger.event_count()

        repeated = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(repeated["status"], "NO_TRADE")
        self.assertEqual(self.ledger.execution_ledger.event_count(), event_count)
        self.assertIsNone(self.ledger.position())

    def test_entry_events_and_partial_position_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper-account.sqlite3")
            first_ledger = PaperLedger(path)
            first_bot = PaperTradingBot(self.engine, ledger=first_ledger)
            self.engine.data.order_book_buy_qty = 0.25
            opened = first_bot.run_once("cex:BTCUSDT")
            self.assertEqual(opened["status"], "PARTIALLY_OPENED")
            expected_quantity = opened["quantity"]
            expected_fee = opened["fee_usd"]
            order_id = opened["client_order_id"]
            first_ledger.close()

            recovered = PaperLedger(path)
            try:
                position = recovered.position()
                self.assertAlmostEqual(position["quantity"], expected_quantity)
                self.assertAlmostEqual(position["entry_fee"], expected_fee)
                order = recovered.execution_ledger.order_state(order_id)
                self.assertEqual(order["status"], "CANCELED")
                self.assertAlmostEqual(order["filled_quantity"], expected_quantity)
                self.assertEqual(recovered.reconciliation()["status"], "PASS")
                self.assertEqual(recovered.execution_ledger.replay_order_states()["status"], "PASS")
            finally:
                recovered.close()

    def test_entry_execution_and_projection_roll_back_in_one_transaction(self):
        before_cash = self.ledger.state()["account"]["cash_balance_usd"]
        original = self.ledger._insert_open_position_projection
        self.ledger._insert_open_position_projection = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected projection failure")
        )
        try:
            result = self.bot.run_once("cex:BTCUSDT")
        finally:
            self.ledger._insert_open_position_projection = original
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(self.ledger.position())
        self.assertEqual(self.ledger.execution_ledger.event_count(), 0)
        self.assertEqual(self.ledger.state()["account"]["cash_balance_usd"], before_cash)

    def test_unknown_gate_blocks_entry(self):
        self.engine.result.gates[-1].status = "UNKNOWN"
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "NO_TRADE")
        self.assertIsNone(self.ledger.position())

    def test_spot_short_is_never_opened(self):
        self.engine.result.signal = "SHORT"
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "NO_TRADE")
        self.assertIsNone(self.ledger.position())

    def test_take_profit_closes_and_same_candle_cannot_reopen(self):
        opened = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(opened["status"], "OPENED")
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 125.0
        closed = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(closed["status"], "CLOSED")
        self.assertEqual(closed["trade"]["exit_reason"], "TAKE_PROFIT")
        self.assertGreater(closed["trade"]["entry_slippage_usd"], 0.0)
        self.assertGreater(closed["trade"]["exit_slippage_usd"], 0.0)
        self.assertIsNone(closed["trade"]["spread_cost_usd"])
        persisted = self.ledger.state()["recent_trades"][0]
        self.assertEqual(persisted["funding_status"], "NOT_APPLICABLE")
        self.assertEqual(persisted["exit_reference_price"], 120.0)
        self.assertEqual(self.ledger.state()["performance"]["spread_data_status"], "INCOMPLETE")
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 100.0
        duplicate = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(duplicate["status"], "NO_TRADE")
        self.assertIsNone(self.ledger.position())

    def test_stop_loss_closes_position_and_records_net_loss(self):
        self.bot.run_once("cex:BTCUSDT")
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 85.0
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["trade"]["exit_reason"], "STOP_LOSS")
        self.assertLess(result["trade"]["net_pnl_usd"], result["trade"]["gross_pnl_usd"])
        self.assertLess(result["trade"]["net_pnl_usd"], 0.0)

    def test_cost_adjusted_reward_risk_blocks_weak_plan(self):
        self.engine.result.take_profit = 110.0
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "NO_TRADE")
        self.assertIn("net reward/risk", result["reason"].lower())

    def test_break_even_and_trailing_stop_only_move_up(self):
        opened = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(opened["status"], "OPENED")
        entry = opened["entry_price"]
        initial_risk = entry - opened["stop_loss"]
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = entry + initial_risk * 1.1
        holding = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(holding["status"], "HOLDING")
        position = self.ledger.position()
        self.assertGreater(position["stop_loss"], entry)
        self.assertGreaterEqual(position["peak_price"], entry + initial_risk)
        breakeven_stop = position["stop_loss"]
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = entry + initial_risk * 1.6
        self.assertEqual(self.bot.run_once("cex:BTCUSDT")["status"], "HOLDING")
        trailing_stop = self.ledger.position()["stop_loss"]
        self.assertGreater(trailing_stop, breakeven_stop)
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = entry + initial_risk * 0.5
        stopped = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(stopped["status"], "CLOSED")
        self.assertEqual(stopped["trade"]["exit_reason"], "TRAILING_STOP")

    def test_time_exit_closes_old_paper_position(self):
        self.bot.run_once("cex:BTCUSDT")
        with self.ledger._lock, self.ledger._db:
            self.ledger._db.execute(
                "UPDATE paper_positions SET opened_at=? WHERE id=1",
                (int(__import__("time").time()) - 3 * 86_400,),
            )
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 105.0
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["trade"]["exit_reason"], "TIME_EXIT")

    def test_daily_loss_circuit_halts_new_entries(self):
        bot = PaperTradingBot(
            self.engine, ledger=self.ledger, daily_loss_limit_pct=0.0001
        )
        bot.run_once("cex:BTCUSDT")
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 85.0
        closed = bot.run_once("cex:BTCUSDT")
        self.assertEqual(closed["status"], "CLOSED")
        frame = self.engine.data.candle_frames["cex:BTCUSDT"]
        frame.loc[frame.index[-1] + pd.Timedelta(days=1)] = {"close": 100.0}
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 100.0
        blocked = bot.run_once("cex:BTCUSDT")
        self.assertEqual(blocked["status"], "RISK_HALT")
        self.assertIn("DAILY_LOSS_LIMIT", blocked["risk_controls"]["halt_reasons"])

    def test_bot_limits_portfolio_to_one_open_position(self):
        self.bot.run_once("cex:BTCUSDT")
        result = self.bot.run_once("cex:ETHUSDT")
        self.assertEqual(result["status"], "BLOCKED")

    def test_non_spot_asset_is_rejected(self):
        result = self.bot.run_once("perp:BTCUSDT")
        self.assertEqual(result["status"], "SKIPPED")


    def test_entry_and_exit_are_recorded_in_append_only_execution_ledger(self):
        opened = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(opened["status"], "OPENED")
        position = self.ledger.position()
        entry_id = make_paper_client_order_id(
            position["asset"], position["signal_bar"], "ENTRY"
        )
        entry_events = self.ledger.execution_ledger.recent_events(
            10, client_order_id=entry_id
        )
        self.assertEqual(
            [row["status"] for row in reversed(entry_events)],
            [
                "CREATED", "VALIDATED", "SUBMITTED", "ACCEPTED",
                "PARTIALLY_FILLED", "PARTIALLY_FILLED", "FILLED",
            ],
        )
        self.assertEqual(entry_events[0]["side"], "BUY")

        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 125.0
        closed = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(closed["status"], "CLOSED")
        exit_id = make_paper_client_order_id(
            position["asset"], position["signal_bar"], "EXIT"
        )
        exit_events = self.ledger.execution_ledger.recent_events(
            10, client_order_id=exit_id
        )
        self.assertEqual(
            [row["status"] for row in reversed(exit_events)],
            [
                "CREATED", "VALIDATED", "SUBMITTED", "ACCEPTED",
                "PARTIALLY_FILLED", "PARTIALLY_FILLED", "FILLED",
            ],
        )
        self.assertEqual(exit_events[0]["side"], "SELL")
        state = self.ledger.state()
        self.assertEqual(state["execution_ledger"]["event_count"], 14)
        self.assertTrue(state["execution_ledger"]["append_only"])
        self.assertFalse(state["orders_submitted"])

    def test_partial_exit_keeps_remaining_position_and_retries_with_new_order_id(self):
        opened = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(opened["status"], "OPENED")
        initial_position = self.ledger.position()
        initial_quantity = initial_position["quantity"]
        cash_before_exit = self.ledger.state()["account"]["cash_balance_usd"]

        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 125.0
        self.engine.data.order_book_sell_qty = 0.25
        partial = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(partial["status"], "PARTIALLY_CLOSED")
        remaining = self.ledger.position()
        self.assertIsNotNone(remaining)
        self.assertLess(remaining["quantity"], initial_quantity)
        self.assertAlmostEqual(
            remaining["quantity"] + remaining["exit_filled_quantity"],
            initial_quantity,
        )
        self.assertGreater(
            self.ledger.state()["account"]["cash_balance_usd"], cash_before_exit
        )
        first_exit_id = partial["execution"]["client_order_id"]
        first_exit = self.ledger.execution_ledger.order_state(first_exit_id)
        self.assertEqual(first_exit["status"], "CANCELED")
        self.assertGreater(first_exit["filled_quantity"], 0.0)
        self.assertEqual(partial["execution"]["execution_status"], "CANCELED")
        self.assertEqual(partial["execution"]["fill_status"], "PARTIALLY_FILLED")
        self.assertEqual(self.ledger.reconciliation()["status"], "PASS")

        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 130.0
        self.engine.data.order_book_sell_qty = 100.0
        completed = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(completed["status"], "CLOSED")
        self.assertIsNone(self.ledger.position())
        second_exit_id = completed["execution"]["client_order_id"]
        self.assertNotEqual(first_exit_id, second_exit_id)
        self.assertTrue(second_exit_id.endswith("-2"))
        trade = self.ledger._db.execute(
            "SELECT * FROM paper_trades WHERE asset='cex:BTCUSDT'"
        ).fetchone()
        self.assertAlmostEqual(trade["quantity"], initial_quantity)
        self.assertGreater(trade["exit_fee"], 0.0)
        self.assertEqual(self.ledger.reconciliation()["status"], "PASS")

    def test_zero_liquidity_exit_leaves_position_unchanged(self):
        self.bot.run_once("cex:BTCUSDT")
        before = self.ledger.position()
        cash_before = self.ledger.state()["account"]["cash_balance_usd"]
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 125.0
        self.engine.data.order_book_sell_qty = 0.0
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "EXIT_UNFILLED")
        after = self.ledger.position()
        self.assertIsNotNone(after)
        self.assertAlmostEqual(after["quantity"], before["quantity"])
        self.assertEqual(
            self.ledger.state()["account"]["cash_balance_usd"], cash_before
        )
        self.assertEqual(self.ledger.reconciliation()["status"], "PASS")

    def test_stale_order_book_blocks_exit_and_preserves_position(self):
        self.bot.run_once("cex:BTCUSDT")
        before = self.ledger.position()
        event_count = self.ledger.execution_ledger.event_count()
        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 125.0
        self.engine.data.order_book_age_ms = 6_000
        result = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(result["status"], "UNKNOWN")
        after = self.ledger.position()
        self.assertIsNotNone(after)
        self.assertEqual(after["quantity"], before["quantity"])
        self.assertEqual(self.ledger.execution_ledger.event_count(), event_count)
        self.assertEqual(self.ledger.reconciliation()["status"], "PASS")

    def test_partial_exit_projection_survives_sqlite_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "paper-exit.sqlite3")
            ledger = PaperLedger(db_path)
            bot = PaperTradingBot(self.engine, ledger=ledger)
            self.assertEqual(bot.run_once("cex:BTCUSDT")["status"], "OPENED")
            original_quantity = ledger.position()["quantity"]
            self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 125.0
            self.engine.data.order_book_sell_qty = 0.25
            partial = bot.run_once("cex:BTCUSDT")
            self.assertEqual(partial["status"], "PARTIALLY_CLOSED")
            expected_remaining = ledger.position()["quantity"]
            expected_realized = ledger.state()["account"]["realized_pnl_usd"]
            ledger.close()

            recovered = PaperLedger(db_path)
            try:
                position = recovered.position()
                self.assertIsNotNone(position)
                self.assertAlmostEqual(position["quantity"], expected_remaining)
                self.assertAlmostEqual(
                    position["quantity"] + position["exit_filled_quantity"],
                    original_quantity,
                )
                self.assertAlmostEqual(
                    recovered.state()["account"]["realized_pnl_usd"],
                    expected_realized,
                )
                self.assertEqual(recovered.reconciliation()["status"], "PASS")
                self.assertEqual(
                    recovered.execution_ledger.replay_order_states()["status"], "PASS"
                )
                self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 130.0
                self.engine.data.order_book_sell_qty = 100.0
                recovered_bot = PaperTradingBot(self.engine, ledger=recovered)
                completed = recovered_bot.run_once("cex:BTCUSDT")
                self.assertEqual(completed["status"], "CLOSED")
                self.assertTrue(completed["execution"]["client_order_id"].endswith("-2"))
                self.assertEqual(recovered.reconciliation()["status"], "PASS")
            finally:
                recovered.close()

    def test_entry_projection_and_execution_events_roll_back_together(self):
        position = {
            "asset": "cex:BTCUSDT", "quantity": 1.0, "entry_price": 100.0,
            "stop_loss": 90.0, "initial_stop_loss": 90.0, "take_profit": 120.0,
            "entry_fee": 0.1, "opened_at": 123, "signal_bar": "atomic-test-bar",
            "confirmation_score": 80.0, "risk_budget": 50.0, "max_holding_days": 14,
        }
        before = self.ledger.state()["account"]["cash_balance_usd"]
        original = self.ledger.execution_ledger.record_simulated_fill
        self.ledger.execution_ledger.record_simulated_fill = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected journal failure")
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "injected journal failure"):
                self.ledger.open_position(position, 100.1)
        finally:
            self.ledger.execution_ledger.record_simulated_fill = original
        after = self.ledger.state()["account"]["cash_balance_usd"]
        self.assertIsNone(self.ledger.position())
        self.assertEqual(after, before)
        self.assertEqual(self.ledger.execution_ledger.event_count(), 0)


    def test_projection_reconciliation_passes_for_open_and_closed_position(self):
        opened = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(opened["status"], "OPENED")
        open_check = self.ledger.reconciliation()
        self.assertEqual(open_check["status"], "PASS")
        self.assertEqual(open_check["matched_fills"], 1)

        self.engine.data.snapshots["cex:BTCUSDT"].price_usd = 125.0
        closed = self.bot.run_once("cex:BTCUSDT")
        self.assertEqual(closed["status"], "CLOSED")
        closed_check = self.ledger.reconciliation()
        self.assertEqual(closed_check["status"], "PASS")
        self.assertEqual(closed_check["matched_fills"], 2)
        self.assertAlmostEqual(closed_check["cash_delta_usd"], 0.0, places=6)

    def test_legacy_position_without_execution_events_is_incomplete(self):
        original = self.ledger.execution_ledger.record_simulated_fill
        self.ledger.execution_ledger.record_simulated_fill = lambda **kwargs: None
        position = {
            "asset": "cex:BTCUSDT", "quantity": 1.0, "entry_price": 100.0,
            "stop_loss": 90.0, "initial_stop_loss": 90.0, "take_profit": 120.0,
            "entry_fee": 0.1, "opened_at": 123, "signal_bar": "legacy-bar",
            "confirmation_score": 80.0, "risk_budget": 50.0, "max_holding_days": 14,
        }
        try:
            self.assertTrue(self.ledger.open_position(position, 100.1))
        finally:
            self.ledger.execution_ledger.record_simulated_fill = original
        result = self.ledger.reconciliation()
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(len(result["missing_events"]), 1)
        self.assertIn("open-position-entry", result["missing_events"][0])

    def test_cash_projection_drift_is_a_reconciliation_failure(self):
        with self.ledger._lock, self.ledger._db:
            self.ledger._db.execute(
                "UPDATE paper_account SET cash_balance=cash_balance-5.0 WHERE id=1"
            )
        result = self.ledger.reconciliation()
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("cash projection mismatch" in error for error in result["errors"]))

    def test_empty_paper_account_reconciliation_returns_no_data(self):
        result = self.ledger.reconciliation()
        self.assertEqual(result["status"], "NO_DATA")
        self.assertEqual(result["event_count"], 0)



    def test_close_projection_and_exit_event_roll_back_together(self):
        position = {
            "asset": "cex:BTCUSDT", "quantity": 1.0, "entry_price": 100.0,
            "stop_loss": 90.0, "initial_stop_loss": 90.0, "take_profit": 120.0,
            "entry_fee": 0.08, "opened_at": 123, "signal_bar": "close-atomic-bar",
            "confirmation_score": 80.0, "risk_budget": 50.0, "max_holding_days": 14,
        }
        self.assertTrue(self.ledger.open_position(position, 100.08))
        before_cash = self.ledger.state()["account"]["cash_balance_usd"]
        before_events = self.ledger.execution_ledger.event_count()
        original = self.ledger.execution_ledger.record_simulated_fill
        self.ledger.execution_ledger.record_simulated_fill = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected exit-journal failure")
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "injected exit-journal failure"):
                self.ledger.close_position(110.0, 0.0008, "TEST_EXIT")
        finally:
            self.ledger.execution_ledger.record_simulated_fill = original
        self.assertIsNotNone(self.ledger.position())
        after_cash = self.ledger.state()["account"]["cash_balance_usd"]
        self.assertEqual(after_cash, before_cash)
        self.assertEqual(self.ledger.execution_ledger.event_count(), before_events)
        trade_count = self.ledger._db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        self.assertEqual(trade_count, 0)
        self.assertEqual(self.ledger.reconciliation()["status"], "PASS")



    def test_orphan_fill_simulator_event_without_projection_is_incomplete(self):
        with self.ledger._lock, self.ledger._db:
            self.ledger.execution_ledger.record_simulated_fill(
                client_order_id="paper-fill-unprojected",
                asset="cex:BTCUSDT", side="BUY", quantity=1.0,
                fill_price=100.0, fee_paid_usd=0.08,
                source="PAPER_SIMULATOR:TEST_BOOK",
                reason="ORPHAN_SIMULATOR_TEST", occurred_at=123,
            )
        report = self.ledger.reconciliation()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("paper-fill-unprojected", report["missing_projections"])

    def test_orphan_paper_entry_event_is_incomplete(self):
        order_id = make_paper_client_order_id(
            "cex:BTCUSDT", "orphan-bar", "ENTRY"
        )
        with self.ledger._lock, self.ledger._db:
            self.ledger.execution_ledger.record_simulated_fill(
                client_order_id=order_id, asset="cex:BTCUSDT", side="BUY",
                quantity=1.0, fill_price=100.0, fee_paid_usd=0.08,
                source="PAPER_SIMULATOR", reason="ORPHAN_TEST", occurred_at=123,
            )
        report = self.ledger.reconciliation()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn(order_id, report["missing_projections"])


    def test_orphan_execution_adapter_order_is_incomplete(self):
        with self.ledger._lock, self.ledger._db:
            for status in ("CREATED", "VALIDATED", "SUBMITTED", "ACCEPTED"):
                self.ledger.execution_ledger.append_event(
                    client_order_id="adapter-order-unprojected",
                    idempotency_key=f"adapter-order-unprojected:{status.lower()}",
                    asset="cex:BTCUSDT", side="BUY", status=status,
                    quantity=1.0, source="PAPER_EXECUTION_ADAPTER",
                    reason="ORPHAN_ADAPTER_TEST", occurred_at=123,
                )
        report = self.ledger.reconciliation()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("adapter-order-unprojected", report["missing_projections"])



if __name__ == "__main__":
    unittest.main()
