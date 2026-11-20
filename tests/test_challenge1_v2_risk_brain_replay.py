from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

from crypto_super_radar.adaptive_brain import OnlineMetaBrain
from crypto_super_radar.ai_sandbox import (
    SandboxDataError,
    _calibration_report,
    _required_replay_end,
    _within_purge_window,
)
from crypto_super_radar.market_replay import (
    ReplayDownloadError,
    _subtract_calendar_months,
    download_candles,
)
from crypto_super_radar.virtual_blofin import (
    CostModel,
    InsufficientEquityForRisk,
    InstrumentRules,
    SimulationError,
    VirtualBloFin,
    _net_breakeven_stop_price,
    _snap_trade_prices,
    size_position,
)


class AllInPositionRiskTests(unittest.TestCase):
    def test_costs_are_inside_the_025_percent_risk_ceiling(self) -> None:
        rules = InstrumentRules(
            inst_id="BTC-USDT",
            contract_value=Decimal("1"),
            min_size=Decimal("0.1"),
            lot_size=Decimal("0.1"),
            tick_size=Decimal("0.01"),
        )
        sized = size_position(
            equity_usdt=1000,
            risk_fraction=0.0025,
            entry=100,
            stop=99,
            rules=rules,
            costs=CostModel(),
        )
        self.assertLess(sized.quantity_contracts, Decimal("2.5"))
        self.assertLessEqual(sized.stop_risk_usdt, sized.risk_budget_usdt)
        self.assertGreater(sized.stop_risk_usdt, Decimal("2.0"))

    def test_minimum_quantity_is_rejected_instead_of_rounded_up(self) -> None:
        rules = InstrumentRules(
            inst_id="BTC-USDT",
            contract_value=Decimal("1"),
            min_size=Decimal("1.2"),
            lot_size=Decimal("0.1"),
            tick_size=Decimal("0.01"),
        )
        with self.assertRaises(InsufficientEquityForRisk):
            size_position(
                equity_usdt=1000,
                risk_fraction=0.0025,
                entry=100,
                stop=98,
                rules=rules,
                costs=CostModel(),
            )

    def test_entry_stop_and_target_prices_follow_tick_rounding_policy(self) -> None:
        tick = Decimal("0.05")
        self.assertEqual(
            _snap_trade_prices(
                direction="LONG", entry=100.03, stop=99.03, target=102.03, tick_size=tick
            ),
            (100.0, 99.0, 102.05),
        )
        self.assertEqual(
            _snap_trade_prices(
                direction="SHORT", entry=100.03, stop=101.02, target=98.02, tick_size=tick
            ),
            (100.05, 101.05, 98.0),
        )


class NetBreakEvenStopTests(unittest.TestCase):
    def test_tp1_stop_uses_realized_profit_and_accrued_costs(self) -> None:
        stop = _net_breakeven_stop_price(
            entry=100.0,
            quantity=1.0,
            contract_value=1.0,
            side="LONG",
            accumulated_fees=0.05,
            funding_paid=0.01,
            spread_cost_usdt=0.02,
            realized_gross_pnl_usdt=0.5,
            stop_slippage=0.001,
            stop_fee_rate=0.0006,
            tick_size=Decimal("0.01"),
        )
        self.assertGreaterEqual(stop, 99.73)
        self.assertAlmostEqual(stop * 100 % 1, 0.0, places=8)

    def test_short_tp1_stop_rounds_down_to_tick(self) -> None:
        stop = _net_breakeven_stop_price(
            entry=100.0,
            quantity=1.0,
            contract_value=1.0,
            side="SHORT",
            accumulated_fees=0.05,
            funding_paid=-0.01,
            spread_cost_usdt=0.02,
            realized_gross_pnl_usdt=0.5,
            stop_slippage=0.001,
            stop_fee_rate=0.0006,
            tick_size=Decimal("0.05"),
        )
        self.assertGreater(stop, 0)
        self.assertAlmostEqual((stop / 0.05) % 1, 0.0, places=8)

    def test_negative_spread_assumption_is_rejected(self) -> None:
        engine = VirtualBloFin()
        rules = InstrumentRules(
            inst_id="BTC-USDT",
            contract_value=Decimal("1"),
            min_size=Decimal("1"),
            lot_size=Decimal("1"),
            tick_size=Decimal("0.01"),
        )
        bars = [
            {"ts": 1_700_000_000_000, "low": 99.0, "high": 101.0},
            {"ts": 1_700_000_900_000, "low": 99.0, "high": 101.0},
        ]
        result = engine.simulate_post_only_trade(
            bars=bars,
            start_index=0,
            direction="LONG",
            limit_price=100,
            stop_price=99,
            target_price=104,
            equity_usdt=1000,
            risk_fraction=0.0025,
            rules=rules,
            funding_events=[{"fundingTime": 1_699_999_999_999, "fundingRate": 0.0}],
            spread_fraction=-0.0001,
        )
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(result.exit_reason, "WIDE_SPREAD")


class WalkForwardAndCalibrationTests(unittest.TestCase):
    def test_six_calendar_month_coverage_is_mandatory(self) -> None:
        start = pd.Timestamp("2026-01-31T00:00:00Z")
        required = _required_replay_end(start, pd.Timestamp("2026-07-30T23:45:00Z"))
        self.assertEqual(required, pd.Timestamp("2026-07-31T00:00:00Z"))
        with self.assertRaises(SandboxDataError):
            _required_replay_end(start, pd.Timestamp("2026-07-30T23:00:00Z"))

    def test_purge_window_blocks_entries_that_can_overlap_fold_boundary(self) -> None:
        boundary = 2_000_000_000
        self.assertTrue(_within_purge_window(boundary - 194 * 60_000, boundary))
        self.assertFalse(_within_purge_window(boundary - 196 * 60_000, boundary))

    def test_brier_score_and_calibration_curve_are_calculated(self) -> None:
        report = _calibration_report([
            {"probability": 0.1, "label": 0},
            {"probability": 0.9, "label": 1},
        ])
        self.assertEqual(report["status"], "CALCULATED")
        self.assertEqual(report["sample_count"], 2)
        self.assertAlmostEqual(report["brier_score"], 0.01, places=8)
        self.assertEqual(len(report["calibration_curve"]), 2)

    def test_brain_memory_is_bounded_persisted_and_hash_stable(self) -> None:
        features = {
            "adx_1h": 30.0,
            "rsi_15m": 50.0,
            "ema200_distance_pct": 1.0,
            "funding_rate_pct": 0.01,
            "spread_pct": 0.02,
            "volume_ratio": 1.5,
            "utc_hour_sin": 0.0,
            "utc_hour_cos": 1.0,
            "last_trade_1_win": 1,
            "last_trade_2_win": 0,
            "last_trade_3_win": -1,
        }
        brain = OnlineMetaBrain(max_examples=100)
        for index in range(101):
            stamp = pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=index)
            brain.add_finalized_outcome(
                signal_id=f"signal-{index}",
                instrument_id="BTC-USDT",
                candle_time_utc=stamp.isoformat(),
                features=features,
                net_pnl_usdt=1.0 if index % 2 else -1.0,
                fees_complete=True,
                funding_complete=True,
            )
        self.assertEqual(len(brain.examples), 100)
        self.assertEqual(brain.train()["status"], "TRAINED")
        expected_hash = brain.model_sha256()
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "brain.json"
            brain.save_json(model_path)
            restored = OnlineMetaBrain.load_json(model_path)
        self.assertEqual(len(restored.examples), 100)
        self.assertEqual(restored.max_examples, 100)
        self.assertEqual(restored.model_sha256(), expected_hash)


class ReplayDownloaderCompletenessTests(unittest.TestCase):
    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, rows):
            self.rows = rows

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": "0", "msg": "success", "data": self.rows}

    class Session:
        def __init__(self, rows):
            self.rows = rows

        def get(self, *args, **kwargs):
            return ReplayDownloaderCompletenessTests.Response(self.rows)

    def test_six_calendar_months_uses_calendar_arithmetic(self) -> None:
        value = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _subtract_calendar_months(value, 6),
            datetime(2026, 2, 28, 12, 0, tzinfo=timezone.utc),
        )

    def test_short_recent_candle_response_is_not_mistaken_for_six_month_coverage(self) -> None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        interval = 15 * 60_000
        latest_open = (now_ms // interval - 2) * interval
        row = lambda ts: [
            str(ts), "100", "101", "99", "100", "1", "1", "100", "1"
        ]
        session = self.Session([row(latest_open), row(latest_open - interval)])
        with self.assertRaises(ReplayDownloadError):
            download_candles(
                "BTC-USDT",
                "15m",
                months=6,
                session=session,
                base_url="https://example.invalid",
                page_size=10,
            )


if __name__ == "__main__":
    unittest.main()
