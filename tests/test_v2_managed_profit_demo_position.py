import sqlite3
import unittest
from decimal import Decimal

from scripts.v2_managed_profit_demo_position import (
    rank_top1_momentum, _tp1_price, SYMBOLS,
    build_candle_idempotency_key, _claim_once, _one_position_risk_policy,
    _report_proves_no_entry, _SingleEntryExecutor,
)


class FakeMarket:
    def __init__(self, changes=None, stale=None):
        self.changes = changes or {}
        self.stale = stale or set()
        self.now = 1_800_000_000_000

    def get_candles(self, symbol, *, bar, limit):
        self.asserted = (bar, limit)
        change = Decimal(str(self.changes.get(symbol, "0.3")))
        base = Decimal("100")
        closes = [base, base, base, base * (Decimal("1") + change / Decimal("100"))]
        rows = []
        start = self.now - 4 * 900_000
        for i, close in enumerate(closes):
            stamp = start + i * 900_000
            rows.append([str(stamp), str(close), str(close), str(close), str(close),
                         "1", "1", "1", "1"])
        return rows

    def get_tickers(self, *, inst_id=None):
        ts = self.now - (120_000 if inst_id in self.stale else 0)
        return [{"instId": inst_id, "ts": str(ts)}]


class ManagedProfitTop1Tests(unittest.TestCase):
    def test_selects_exactly_one_strongest_absolute_directional_momentum(self):
        client = FakeMarket({
            "BTC-USDT": "0.1", "ETH-USDT": "-0.2", "SOL-USDT": "0.9",
            "XRP-USDT": "-0.4", "LINK-USDT": "0.3",
        })
        ranked = rank_top1_momentum(client, now_ms=client.now)
        self.assertEqual(len(ranked), len(SYMBOLS))
        self.assertEqual(ranked[0]["symbol"], "SOL-USDT")
        self.assertEqual(ranked[0]["direction"], "LONG")
        self.assertEqual(ranked[0]["absolute_momentum_pct"], "0.9")

    def test_negative_momentum_is_directional_short(self):
        client = FakeMarket({symbol: "-0.5" for symbol in SYMBOLS})
        ranked = rank_top1_momentum(client, now_ms=client.now)
        self.assertEqual(ranked[0]["direction"], "SHORT")

    def test_missing_or_stale_market_data_fails_closed(self):
        client = FakeMarket(stale={"LINK-USDT"})
        with self.assertRaisesRegex(RuntimeError, "ticker is stale"):
            rank_top1_momentum(client, now_ms=client.now)

    def test_idempotency_key_is_scoped_to_symbol_and_completed_candle(self):
        candle_open = 1_800_000_000_000
        first = build_candle_idempotency_key("SOL-USDT", candle_open)
        same = build_candle_idempotency_key("sol-usdt", candle_open)
        next_candle = build_candle_idempotency_key("SOL-USDT", candle_open + 900_000)
        other_symbol = build_candle_idempotency_key("BTC-USDT", candle_open)
        self.assertEqual(first, same)
        self.assertNotEqual(first, next_candle)
        self.assertNotEqual(first, other_symbol)
        self.assertIn("SOL-USDT", first)
        self.assertIn("2027-01-15T08:00:00Z", first)

    def test_claim_once_blocks_only_same_symbol_and_candle_key(self):
        db = sqlite3.connect(":memory:")
        try:
            key_a = build_candle_idempotency_key("SOL-USDT", 1_800_000_000_000)
            key_b = build_candle_idempotency_key("SOL-USDT", 1_800_000_900_000)
            self.assertTrue(_claim_once(db, key_a))
            self.assertFalse(_claim_once(db, key_a))
            self.assertTrue(_claim_once(db, key_b))
        finally:
            db.close()

    def test_one_shot_policy_limits_to_one_position_and_preserves_risk_caps(self):
        policy = _one_position_risk_policy()
        self.assertEqual(policy.max_concurrent_positions, 1)
        self.assertEqual(policy.risk_per_trade_pct, 0.005)
        self.assertEqual(policy.max_aggregate_risk_pct, 0.005)
        self.assertEqual(policy.max_leverage, 1)

    def test_single_entry_executor_allows_only_one_attempt(self):
        class FakeStore:
            pass
        class FakeExecutor:
            store = FakeStore()
            def __init__(self):
                self.calls = 0
            def execute(self, *_args, **_kwargs):
                self.calls += 1
                return {"status": "FILLED"}
        delegate = FakeExecutor()
        guarded = _SingleEntryExecutor(delegate)
        self.assertEqual(guarded.execute(), {"status": "FILLED"})
        with self.assertRaisesRegex(RuntimeError, "attempt limit"):
            guarded.execute()
        self.assertEqual(delegate.calls, 1)

    def test_single_entry_executor_consumes_attempt_even_when_executor_raises(self):
        class FakeStore:
            pass
        class FailingExecutor:
            store = FakeStore()
            def __init__(self):
                self.calls = 0
            def execute(self, *_args, **_kwargs):
                self.calls += 1
                raise TimeoutError("unknown exchange outcome")
        delegate = FailingExecutor()
        guarded = _SingleEntryExecutor(delegate)
        with self.assertRaises(TimeoutError):
            guarded.execute()
        self.assertTrue(guarded.entry_attempted)
        with self.assertRaisesRegex(RuntimeError, "attempt limit"):
            guarded.execute()
        self.assertEqual(delegate.calls, 1)

    def test_multiple_ranked_no_entry_records_are_safe_to_retry(self):
        import json
        report = {
            "v2_cycle": {
                "orders_submitted": False,
                "trade_executed": False,
                "symbols": [
                    {"status": "NO_TRADE", "orders_submitted": False, "execution_result": None},
                    {"status": "AI_REVIEW_BLOCKED", "orders_submitted": False, "execution_result": None},
                ],
            }
        }
        self.assertTrue(_report_proves_no_entry(json.dumps(report)))
        report["v2_cycle"]["symbols"][1]["orders_submitted"] = True
        self.assertFalse(_report_proves_no_entry(json.dumps(report)))

    def test_tp1_target_uses_existing_v2_1_2r_rule(self):
        self.assertEqual(_tp1_price("100", "98", "LONG"), "102.4")
        self.assertEqual(_tp1_price("100", "102", "SHORT"), "97.6")


if __name__ == "__main__":
    unittest.main()
