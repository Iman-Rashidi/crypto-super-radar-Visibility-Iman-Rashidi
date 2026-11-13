from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

import pandas as pd

from crypto_super_radar.futures_paper_trading import (
    FuturesPaperLedger,
    FuturesPaperTradingBot,
)
from crypto_super_radar.ai_second_opinion import _prompt_messages, _validated_review


def make_position(direction: str = "LONG", *, opened_at: int | None = None) -> dict:
    entry = 100.0
    stop = 90.0 if direction == "LONG" else 110.0
    target = 120.0 if direction == "LONG" else 80.0
    now = int(time.time()) if opened_at is None else opened_at
    return {
        "asset": "perp:BTCUSDT",
        "direction": direction,
        "quantity": 1.0,
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit": target,
        "leverage": 1.0,
        "initial_margin": 100.0,
        "entry_fee": 0.10,
        "opened_at": now,
        "signal_bar": f"{direction.lower()}-{now}",
        "confirmation_score": 82.0,
        "risk_budget": 50.0,
        "max_holding_hours": 24,
        "last_funding_time_ms": now * 1000,
        "last_mark_at_ms": now * 1000,
    }


class FuturesPaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = FuturesPaperLedger(":memory:", initial_equity_usd=10_000.0)

    def tearDown(self):
        self.ledger.close()

    def test_long_and_short_pnl_use_opposite_direction_signs(self):
        long = make_position("LONG")
        self.assertTrue(self.ledger.open_position(long))
        trade = self.ledger.close_position(exit_price=110.0, exit_fee_rate=0.0, reason="TEST")
        self.assertAlmostEqual(trade["gross_pnl_usd"], 10.0)
        self.assertAlmostEqual(trade["net_pnl_usd"], 9.9)

        short = make_position("SHORT")
        short["signal_bar"] = "short-second"
        self.assertTrue(self.ledger.open_position(short))
        trade = self.ledger.close_position(exit_price=90.0, exit_fee_rate=0.0, reason="TEST")
        self.assertAlmostEqual(trade["gross_pnl_usd"], 10.0)
        self.assertAlmostEqual(trade["net_pnl_usd"], 9.9)

    def test_short_loss_is_negative_when_price_rises(self):
        self.assertTrue(self.ledger.open_position(make_position("SHORT")))
        trade = self.ledger.close_position(exit_price=110.0, exit_fee_rate=0.0, reason="TEST")
        self.assertAlmostEqual(trade["gross_pnl_usd"], -10.0)
        self.assertLess(trade["net_pnl_usd"], -10.0)

    def test_funding_is_applied_once_and_is_directional(self):
        position = make_position("LONG")
        self.assertTrue(self.ledger.open_position(position))
        before = self.ledger.state()["account"]["balance_usd"]
        funding_time = int(time.time() * 1000) + 1_000
        cost = self.ledger.apply_funding(
            asset=position["asset"], funding_time_ms=funding_time,
            funding_rate=0.001, mark_price=100.0,
        )
        self.assertAlmostEqual(cost, 0.1)
        after_funding = self.ledger.state()["account"]["balance_usd"]
        self.assertAlmostEqual(before - after_funding, 0.1)
        duplicate = self.ledger.apply_funding(
            asset=position["asset"], funding_time_ms=funding_time,
            funding_rate=0.001, mark_price=100.0,
        )
        self.assertEqual(duplicate, 0.0)
        trade = self.ledger.close_position(exit_price=100.0, exit_fee_rate=0.001, reason="TEST")
        state = self.ledger.state()
        self.assertAlmostEqual(trade["funding_cost_usd"], 0.1)
        self.assertAlmostEqual(
            state["account"]["balance_usd"],
            state["account"]["initial_equity_usd"] + trade["net_pnl_usd"],
        )

    def test_positive_funding_rate_is_income_for_short(self):
        position = make_position("SHORT")
        self.assertTrue(self.ledger.open_position(position))
        cost = self.ledger.apply_funding(
            asset=position["asset"], funding_time_ms=int(time.time() * 1000) + 1_000,
            funding_rate=0.001, mark_price=100.0,
        )
        self.assertAlmostEqual(cost, -0.1)
        self.assertAlmostEqual(
            self.ledger.state()["account"]["balance_usd"], 10_000.0 - 0.1 + 0.1
        )

    def test_reconciliation_records_both_directional_order_sides(self):
        position = make_position("SHORT")
        self.assertTrue(self.ledger.open_position(position))
        self.ledger.close_position(exit_price=90.0, exit_fee_rate=0.0, reason="TEST")
        orders = self.ledger.execution_ledger.latest_orders(10)
        sides = {row["side"] for row in orders}
        self.assertEqual(sides, {"BUY", "SELL"})
        self.assertEqual(self.ledger.execution_ledger.replay_order_states()["status"], "PASS")


class FakePerpetualProvider:
    def __init__(self):
        self.mark = 100.0

    def execution_snapshot(self, symbol: str):
        now_ms = int(time.time() * 1000)
        return {
            "status": "PASS",
            "symbol": symbol,
            "mark_price": self.mark,
            "index_price": self.mark,
            "best_bid": self.mark - 0.1,
            "best_ask": self.mark + 0.1,
            "funding_rate": 0.0001,
            "funding_interval_hours": 8.0,
            "timestamp_ms": now_ms,
            "sources": ["Binance USD-M", "Bybit Linear"],
        }


class FakeFuturesEngine:
    def __init__(self, signal: str):
        self.result = SimpleNamespace(
            signal=signal,
            gates=[SimpleNamespace(gate_id=i, status="PASS") for i in range(1, 13)],
            confirmation_score=82.0,
            estimated_risk_score=20.0,
            entry_price=100.0,
            stop_loss=90.0 if signal == "LONG" else 110.0,
            take_profit=120.0 if signal == "LONG" else 80.0,
            expected_holding_days=1,
        )
        self.data = SimpleNamespace(
            perpetual=FakePerpetualProvider(),
            candle_frames={
                "perp:BTCUSDT": pd.DataFrame(
                    {"close": [99.0, 100.0]},
                    index=pd.date_range("2026-11-01", periods=2, freq="D", tz="UTC"),
                )
            },
            funding_frames={
                "perp:BTCUSDT": pd.Series(
                    [0.0001],
                    index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")]),
                )
            },
        )

    def analyze(self, asset):
        return self.result


class FuturesPaperBotTests(unittest.TestCase):
    def setUp(self):
        self.ledger = FuturesPaperLedger(":memory:")
        self.ai = lambda result, asset: {
            "status": "APPROVED", "paper_entry_eligible": True,
            "expected_side": result.signal, "market_type": "perpetual",
        }

    def tearDown(self):
        self.ledger.close()

    def test_bot_can_open_long_and_short_paper_positions(self):
        for direction in ("LONG", "SHORT"):
            engine = FakeFuturesEngine(direction)
            ledger = FuturesPaperLedger(":memory:")
            try:
                bot = FuturesPaperTradingBot(engine, ledger=ledger, ai_reviewer=self.ai)
                result = bot.run_once("perp:BTCUSDT")
                self.assertEqual(result["status"], "OPENED")
                self.assertEqual(result["direction"], direction)
                self.assertEqual(result["mode"], "FUTURES_PAPER_ONLY")
                self.assertFalse(result["orders_submitted"])
                self.assertEqual(result["leverage"], 1.0)
                self.assertEqual(ledger.position()["direction"], direction)
            finally:
                ledger.close()

    def test_neutral_signal_is_not_traded(self):
        engine = FakeFuturesEngine("NO TRADE")
        bot = FuturesPaperTradingBot(engine, ledger=self.ledger, ai_reviewer=self.ai)
        result = bot.run_once("perp:BTCUSDT")
        self.assertEqual(result["status"], "NO_TRADE")
        self.assertIsNone(self.ledger.position())

    def test_short_ai_prompt_requires_short_and_bearish_rating(self):
        messages = _prompt_messages({
            "market_type": "perpetual", "expected_side": "SHORT",
        })
        prompt = messages[0]["content"]
        self.assertIn("propose SHORT", prompt)
        self.assertIn("final_rating Sell or Underweight", prompt)
        self.assertIn('"proposal":"LONG|SHORT|NO_TRADE"', prompt)

    def test_directional_ai_validation_vetoes_wrong_way_proposal(self):
        document = {
            "specialist_views": {
                "market_regime": {"view": "BEARISH", "reason_fa": "روند نزولی"},
                "technical": {"view": "BEARISH", "reason_fa": "ساختار نزولی"},
                "news_sentiment": {"view": "NEUTRAL", "reason_fa": "خبر خنثی"},
                "bull_researcher": {"view": "OPPOSE", "reason_fa": "شواهد ناکافی"},
                "bear_researcher": {"view": "SUPPORT", "reason_fa": "فشار فروش"},
                "trader": {"proposal": "LONG", "reason_fa": "پیشنهاد مخالف جهت"},
                "risk_manager": {"verdict": "APPROVE", "reason_fa": "ریسک کنترل‌شده"},
                "portfolio_manager": {"verdict": "APPROVE", "reason_fa": "محدودیت پرتفوی پاس"},
            },
            "final_rating": "Sell",
            "risk_flags": [],
            "rationale_fa": "پیشنهاد جهت اشتباه است.",
        }
        review = _validated_review(
            document, "test:free", "perp:BTCUSDT",
            expected_side="SHORT", market_type="perpetual",
        )
        self.assertEqual(review["status"], "VETO")
        self.assertFalse(review["paper_entry_eligible"])


if __name__ == "__main__":
    unittest.main()
