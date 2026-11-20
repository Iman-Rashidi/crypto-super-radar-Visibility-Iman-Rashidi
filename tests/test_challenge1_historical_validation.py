import unittest

from scripts.challenge1_historical_validation import (
    _bootstrap_mean_ci,
    _floor_to_step,
    _round_protective_price,
    _wilson_interval,
)


class Challenge1HistoricalValidationTests(unittest.TestCase):
    def test_contract_quantity_rounds_down_to_exchange_lot(self):
        self.assertAlmostEqual(_floor_to_step(1.29, 0.1), 1.2)
        self.assertAlmostEqual(_floor_to_step(0.09, 0.1), 0.0)

    def test_protective_prices_round_away_from_entry(self):
        self.assertAlmostEqual(_round_protective_price(99.97, 0.1, "LONG", "STOP"), 99.9)
        self.assertAlmostEqual(_round_protective_price(101.01, 0.1, "LONG", "TARGET"), 101.1)
        self.assertAlmostEqual(_round_protective_price(101.01, 0.1, "SHORT", "STOP"), 101.1)
        self.assertAlmostEqual(_round_protective_price(98.09, 0.1, "SHORT", "TARGET"), 98.0)

    def test_wilson_interval_is_bounded_and_ordered(self):
        low, high = _wilson_interval(60, 100)
        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)
        self.assertLess(low, high)

    def test_block_bootstrap_requires_multiple_days(self):
        trades = [
            {"net_r": (0.2 if day % 2 else -0.1), "exit_time_ms": day * 86_400_000}
            for day in range(12)
        ]
        low, high = _bootstrap_mean_ci(trades)
        self.assertIsNotNone(low)
        self.assertIsNotNone(high)
        self.assertLessEqual(low, high)


if __name__ == "__main__":
    unittest.main()
