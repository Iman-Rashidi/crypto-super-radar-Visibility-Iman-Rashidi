from __future__ import annotations

import unittest
from decimal import Decimal

import pandas as pd

from crypto_super_radar.ai_sandbox import _known_funding_rate, _visible_bars, _visible_frame
from crypto_super_radar.virtual_blofin import CostModel, InstrumentRules, VirtualBloFin


class VirtualTimeExitLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = InstrumentRules(
            inst_id="BTC-USDT",
            contract_value=Decimal("1"),
            min_size=Decimal("1"),
            lot_size=Decimal("1"),
            tick_size=Decimal("0.01"),
        )
        self.engine = VirtualBloFin(
            costs=CostModel(spread_fraction=0.0002),
            max_holding_bars=12,
        )
        self.start_ms = 1_700_000_000_000

    def _bars(self, side: str, count: int) -> list[dict[str, float | int]]:
        rows: list[dict[str, float | int]] = []
        for index in range(count):
            ts = self.start_ms + index * 15 * 60_000
            row: dict[str, float | int] = {
                "ts": ts,
                "close_ts": ts + 15 * 60_000,
                "open": 100.0,
                "high": 100.4,
                "low": 99.6,
                "close": 100.0,
                "mark_high": 100.3,
                "mark_low": 99.7,
                "atr_15m": 0.5,
            }
            if index == 1:
                if side == "LONG":
                    row["low"] = 99.98  # one-tick trade-through for the entry buy
                    row["mark_low"] = 99.5
                else:
                    row["high"] = 100.02  # one-tick trade-through for the entry sell
                    row["mark_high"] = 100.5
            rows.append(row)
        return rows

    def _simulate(self, side: str, bars: list[dict[str, float | int]]):
        return self.engine.simulate_post_only_trade(
            bars=bars,
            start_index=0,
            direction=side,
            limit_price=100.0,
            stop_price=99.0 if side == "LONG" else 101.0,
            target_price=104.5 if side == "LONG" else 95.5,
            equity_usdt=1000.0,
            risk_fraction=0.0025,
            rules=self.rules,
            # Explicit zero-rate event before entry: this test does not infer
            # missing funding to be zero.
            funding_events=[{"fundingTime": self.start_ms - 1, "fundingRate": 0.0}],
            spread_fraction=0.0002,
        )

    def test_time_exit_submits_limit_after_exactly_twelve_completed_bars(self) -> None:
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                result = self._simulate(side, self._bars(side, 14))
                self.assertEqual(result.status, "OPEN")
                self.assertEqual(result.exit_reason, "TIME_EXIT_LIMIT_PENDING")
                self.assertEqual(result.bars_held, 12)
                self.assertEqual(result.diagnostics["time_exit_order_type"], "LIMIT")
                self.assertIsNotNone(result.diagnostics["time_exit_limit_price"])
                self.assertIsNotNone(result.diagnostics["time_exit_submitted_ts_ms"])
                self.assertNotIn("exit_ts_ms", result.diagnostics)

    def test_limit_cannot_fill_on_its_submission_candle(self) -> None:
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                bars = self._bars(side, 14)
                # The twelfth holding candle already spans the calculated
                # break-even level, but the limit is submitted only at its close.
                result = self._simulate(side, bars)
                self.assertEqual(result.status, "OPEN")
                self.assertEqual(result.exit_reason, "TIME_EXIT_LIMIT_PENDING")

    def test_time_exit_limit_fills_only_on_a_later_trade_through(self) -> None:
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                result = self._simulate(side, self._bars(side, 15))
                self.assertEqual(result.status, "CLOSED")
                self.assertEqual(result.exit_reason, "TIME_EXIT")
                self.assertEqual(result.diagnostics["time_exit_order_type"], "LIMIT")
                self.assertEqual(result.diagnostics["time_exit_fee_class"], "TAKER_CONSERVATIVE")
                self.assertGreaterEqual(result.net_pnl_usdt, -1e-9)


class SandboxAsOfLeakageTests(unittest.TestCase):
    def test_future_candle_mutations_do_not_change_visible_snapshot(self) -> None:
        index = pd.date_range(
            "2026-01-01T00:00:00Z",
            periods=8,
            freq="15min",
        )
        frame = pd.DataFrame(
            {
                "open": [100.0] * 8,
                "high": [101.0] * 8,
                "low": [99.0] * 8,
                "close": [100.0] * 8,
                "volume": [10.0] * 8,
            },
            index=index,
        )
        asof = index[3] + pd.Timedelta(minutes=15)
        original_snapshot = _visible_frame(frame, asof, pd.Timedelta(minutes=15))

        changed_future = frame.copy()
        changed_future.loc[index[4]:, ["open", "high", "low", "close", "volume"]] = [
            [500.0, 900.0, 1.0, 800.0, 1_000_000.0]
        ] * 4
        changed_snapshot = _visible_frame(changed_future, asof, pd.Timedelta(minutes=15))

        pd.testing.assert_frame_equal(original_snapshot, changed_snapshot)
        self.assertEqual(len(original_snapshot), 4)
        self.assertLessEqual(
            original_snapshot.index[-1] + pd.Timedelta(minutes=15),
            asof,
        )

    def test_virtual_position_receives_only_closed_bars_asof(self) -> None:
        start = 1_700_000_000_000
        bars = [
            {"ts": start + i * 900_000, "close_ts": start + (i + 1) * 900_000}
            for i in range(5)
        ]
        asof = start + 4 * 900_000
        visible = _visible_bars(bars, asof)
        self.assertEqual(len(visible), 4)
        self.assertTrue(all(int(row["close_ts"]) <= asof for row in visible))

    def test_future_funding_rates_do_not_change_rate_known_at_cutoff(self) -> None:
        cutoff = 1_700_000_000_000
        events = [
            {"fundingTime": cutoff - 1, "fundingRate": 0.0001},
            {"fundingTime": cutoff + 8 * 60 * 60_000, "fundingRate": -0.75},
        ]
        self.assertEqual(_known_funding_rate(events, cutoff), 0.0001)
        events[1]["fundingRate"] = 0.99
        self.assertEqual(_known_funding_rate(events, cutoff), 0.0001)


if __name__ == "__main__":
    unittest.main()
