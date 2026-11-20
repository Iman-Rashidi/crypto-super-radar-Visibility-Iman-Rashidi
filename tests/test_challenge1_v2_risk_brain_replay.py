from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_super_radar.adaptive_brain import OnlineMetaBrain
from crypto_super_radar.challenge1_v2 import analyze_signal
from crypto_super_radar.ai_sandbox import (
    SandboxDataError,
    _calibration_report,
    _required_replay_end,
    _within_purge_window,
)
from crypto_super_radar.market_replay import (
    ReplayDownloadError,
    _parse_funding_event,
    _record_unique_timestamp,
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


class StrategyAsOfIntegrationTests(unittest.TestCase):
    @staticmethod
    def _market_frame(index: pd.DatetimeIndex) -> pd.DataFrame:
        x = np.arange(len(index), dtype=float)
        close = 100.0 + 0.04 * x + 0.8 * np.sin(x / 5.0)
        open_ = close - 0.1 * np.cos(x / 3.0)
        high = np.maximum(open_, close) + 0.4 + 0.05 * np.sin(x / 2.0)
        low = np.minimum(open_, close) - 0.4 - 0.05 * np.cos(x / 2.0)
        volume = 100.0 + 20.0 * np.sin(x / 4.0)
        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=index,
        )

    def test_full_strategy_output_is_unchanged_by_mutating_future_candles(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        frame_1h = self._market_frame(pd.date_range(start, periods=300, freq="1h"))
        frame_4h = self._market_frame(pd.date_range(start, periods=300, freq="4h"))
        frame_15m = self._market_frame(pd.date_range(start, periods=1000, freq="15min"))
        btc_15m = self._market_frame(pd.date_range(start, periods=1000, freq="15min"))
        asof = start + pd.Timedelta(hours=221)

        original = analyze_signal(
            instrument_id="BTC-USDT",
            frame_1h=frame_1h,
            frame_4h=frame_4h,
            frame_15m=frame_15m,
            btc_15m=btc_15m,
            asof=asof,
            funding_rate=0.0,
            spread_fraction=0.0002,
        )

        def mutate_future(frame: pd.DataFrame, interval: pd.Timedelta) -> pd.DataFrame:
            changed = frame.copy()
            future = changed.index + interval > asof
            changed.loc[future, ["open", "high", "low", "close", "volume"]] = [
                [501.0, 999.0, 1.0, 800.0, 1_000_000.0]
            ] * int(future.sum())
            return changed

        changed = analyze_signal(
            instrument_id="BTC-USDT",
            frame_1h=mutate_future(frame_1h, pd.Timedelta(hours=1)),
            frame_4h=mutate_future(frame_4h, pd.Timedelta(hours=4)),
            frame_15m=mutate_future(frame_15m, pd.Timedelta(minutes=15)),
            btc_15m=mutate_future(btc_15m, pd.Timedelta(minutes=15)),
            asof=asof,
            funding_rate=0.0,
            spread_fraction=0.0002,
        )
        self.assertEqual(original, changed)


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
        required = _required_replay_end(start, pd.Timestamp("2026-07-31T00:00:00Z"))
        self.assertEqual(required, pd.Timestamp("2026-07-31T00:00:00Z"))
        with self.assertRaises(SandboxDataError):
            _required_replay_end(start, pd.Timestamp("2026-07-30T23:45:00Z"))

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


class ReplayDuplicateTimestampTests(unittest.TestCase):
    def test_first_timestamp_is_recorded(self):
        seen = {}
        row = ("BTC-USDT", "15m", 1000)
        _record_unique_timestamp(seen, 1000, row, series="BTC-USDT 15m")
        self.assertEqual(seen, {1000: row})

    def test_duplicate_candle_timestamp_fails_without_overwrite(self):
        seen = {}
        first = ("BTC-USDT", "15m", 1000, "first")
        second = ("BTC-USDT", "15m", 1000, "second")
        _record_unique_timestamp(seen, 1000, first, series="BTC-USDT 15m")
        with self.assertRaisesRegex(ReplayDownloadError, "duplicate timestamp"):
            _record_unique_timestamp(seen, 1000, second, series="BTC-USDT 15m")
        self.assertEqual(seen[1000], first)

    def test_duplicate_funding_timestamp_fails(self):
        seen = {}
        row = ("BTC-USDT", 2000, 0.0001, "0")
        _record_unique_timestamp(seen, 2000, row, series="BTC-USDT funding")
        with self.assertRaisesRegex(ReplayDownloadError, "BTC-USDT funding"):
            _record_unique_timestamp(seen, 2000, row, series="BTC-USDT funding")


class ReplayFundingSchemaTests(unittest.TestCase):
    def test_missing_funding_kind_is_not_fabricated_as_regular(self):
        with self.assertRaisesRegex(ReplayDownloadError, "missing or unsupported fundingKind"):
            _parse_funding_event(
                {"fundingTime": "1000", "fundingRate": "0.0001"},
                symbol="BTC-USDT",
            )

    def test_unknown_funding_kind_is_rejected(self):
        with self.assertRaisesRegex(ReplayDownloadError, "missing or unsupported fundingKind"):
            _parse_funding_event(
                {"fundingTime": "1000", "fundingRate": "0.0001", "fundingKind": "9"},
                symbol="BTC-USDT",
            )

    def test_regular_and_dividend_funding_kinds_are_preserved(self):
        regular = _parse_funding_event(
            {"fundingTime": "1000", "fundingRate": "0.0001", "fundingKind": "0"},
            symbol="BTC-USDT",
        )
        dividend = _parse_funding_event(
            {"fundingTime": "2000", "fundingRate": "0.0002", "fundingKind": "1"},
            symbol="BTC-USDT",
        )
        self.assertEqual(regular, ("BTC-USDT", 1000, 0.0001, "0"))
        self.assertEqual(dividend, ("BTC-USDT", 2000, 0.0002, "1"))


if __name__ == "__main__":
    unittest.main()
