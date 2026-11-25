import unittest
from decimal import Decimal

from scripts.v2_managed_profit_demo_position import rank_top1_momentum, _tp1_price, SYMBOLS


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

    def test_tp1_target_uses_existing_v2_1_2r_rule(self):
        self.assertEqual(_tp1_price("100", "98", "LONG"), "102.4")
        self.assertEqual(_tp1_price("100", "102", "SHORT"), "97.6")


if __name__ == "__main__":
    unittest.main()
