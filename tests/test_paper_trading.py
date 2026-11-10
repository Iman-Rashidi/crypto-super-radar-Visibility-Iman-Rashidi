from __future__ import annotations

import unittest
from types import SimpleNamespace

import pandas as pd

from crypto_super_radar.paper_trading import PaperLedger, PaperTradingBot


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
        self.assertIn("net reward/risk", result["reason"])

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
        self.assertEqual(stopped["trade"]["exit_reason"], "TRAILING_OR_INITIAL_STOP")

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


if __name__ == "__main__":
    unittest.main()
