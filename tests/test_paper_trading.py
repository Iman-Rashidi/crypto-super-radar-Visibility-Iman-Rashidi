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

    def test_bot_limits_portfolio_to_one_open_position(self):
        self.bot.run_once("cex:BTCUSDT")
        result = self.bot.run_once("cex:ETHUSDT")
        self.assertEqual(result["status"], "BLOCKED")

    def test_non_spot_asset_is_rejected(self):
        result = self.bot.run_once("perp:BTCUSDT")
        self.assertEqual(result["status"], "SKIPPED")


if __name__ == "__main__":
    unittest.main()
