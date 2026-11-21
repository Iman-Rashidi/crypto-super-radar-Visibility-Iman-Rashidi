from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_super_radar.adaptive_brain import OnlineMetaBrain
from crypto_super_radar.challenge1_v2 import (
    _elliott_wave_1h_evidence,
    _resolve_stop_atr_multiplier,
    analyze_signal,
    challenge1_v2_execution_evidence_ready,
)
from crypto_super_radar.funding_schema_probe import (
    summarize_current_payload,
    summarize_history_payload,
)
from crypto_super_radar.ai_sandbox import (
    SandboxDataError,
    _strategy_history_ready,
    _calibration_report,
    _require_regular_funding_events,
    _record_block_reason,
    _required_replay_end,
    _within_purge_window,
    _datetime_index_to_epoch_ms,
    _price_candle_entry_block_status,
    _ambiguous_price_candle_position,
)
from crypto_super_radar.market_replay import (
    ReplayDownloadError,
    OBSERVED_PRICE_CANDLE_BLACKOUTS,
    _observed_price_candle_gap_matches,
    _is_price_candle_blackout,
    _classify_funding_schedule_events,
    _is_observed_blackout_gap,
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
class PriceCandleBlackoutTests(unittest.TestCase):
    def setUp(self):
        self.window = dict(OBSERVED_PRICE_CANDLE_BLACKOUTS[0])

    def test_registered_btc_gap_has_exact_timestamp_and_count(self):
        self.assertEqual(self.window["instrument_id"], "BTC-USDT")
        self.assertEqual(self.window["bar"], "15m")
        self.assertEqual(self.window["missing_from_utc"], "2026-03-09T07:45:00+00:00")
        self.assertEqual(self.window["missing_to_utc"], "2026-03-09T12:30:00+00:00")
        self.assertEqual(self.window["missing_intervals"], 20)

    def test_only_exact_last_price_gap_is_allowlisted(self):
        self.assertTrue(_observed_price_candle_gap_matches(
            symbol="BTC-USDT", bar="15m",
            first_missing_ms=self.window["start_ms"],
            last_missing_ms=self.window["last_missing_ms"],
            missing_count=20, mark_price=False,
        ))
        self.assertFalse(_observed_price_candle_gap_matches(
            symbol="ETH-USDT", bar="15m",
            first_missing_ms=self.window["start_ms"],
            last_missing_ms=self.window["last_missing_ms"],
            missing_count=20, mark_price=False,
        ))
        self.assertFalse(_observed_price_candle_gap_matches(
            symbol="BTC-USDT", bar="15m",
            first_missing_ms=self.window["start_ms"],
            last_missing_ms=self.window["last_missing_ms"],
            missing_count=19, mark_price=False,
        ))
        self.assertFalse(_observed_price_candle_gap_matches(
            symbol="BTC-USDT", bar="15m",
            first_missing_ms=self.window["start_ms"],
            last_missing_ms=self.window["last_missing_ms"],
            missing_count=20, mark_price=True,
        ))
        hourly = dict(OBSERVED_PRICE_CANDLE_BLACKOUTS[1])
        self.assertTrue(_observed_price_candle_gap_matches(
            symbol="BTC-USDT", bar="1H",
            first_missing_ms=hourly["start_ms"],
            last_missing_ms=hourly["last_missing_ms"],
            missing_count=5, mark_price=False,
        ))
        self.assertFalse(_observed_price_candle_gap_matches(
            symbol="BTC-USDT", bar="4H",
            first_missing_ms=hourly["start_ms"],
            last_missing_ms=hourly["last_missing_ms"],
            missing_count=5, mark_price=False,
        ))

    def test_blackout_interval_uses_inclusive_first_and_exclusive_end(self):
        hourly = dict(OBSERVED_PRICE_CANDLE_BLACKOUTS[1])
        self.assertIsNotNone(_is_price_candle_blackout(hourly["start_ms"]))
        self.assertIsNotNone(_is_price_candle_blackout(self.window["last_missing_ms"]))
        self.assertIsNone(_is_price_candle_blackout(self.window["end_exclusive_ms"]))

    def test_new_entries_block_during_gap_and_until_rewarm_completes(self):
        self.assertEqual(
            _price_candle_entry_block_status(self.window["start_ms"], self.window, indicator_rewarm_complete=False),
            "PRICE_CANDLE_BLACKOUT",
        )
        self.assertEqual(
            _price_candle_entry_block_status(self.window["end_exclusive_ms"], self.window, indicator_rewarm_complete=False),
            "PRICE_CANDLE_BLACKOUT_INDICATOR_REWARM",
        )
        self.assertIsNone(
            _price_candle_entry_block_status(self.window["end_exclusive_ms"], self.window, indicator_rewarm_complete=True)
        )

    def test_pre_gap_btc_position_becomes_ambiguous_without_fabricated_exit(self):
        order = {
            "symbol": "BTC-USDT", "signal_id": "gap-crossing",
            "direction": "LONG", "phase": "TRAIN", "entry_checked": True,
            "submitted_ts_ms": self.window["start_ms"] - 900_000,
            "entry_ts_ms": self.window["start_ms"] - 1_800_000,
            "last_observed_through_ts_ms": self.window["start_ms"],
        }
        result = _ambiguous_price_candle_position(order, self.window)
        self.assertEqual(result["status"], "AMBIGUOUS_PRICE_CANDLE_BLACKOUT")
        self.assertIsNone(result["exit_ts_ms"])
        self.assertIsNone(result["exit_price"])
        self.assertIsNone(result["net_pnl_usdt"])
        self.assertFalse(result["synthetic_candles_used"])

    def test_other_symbol_and_orders_created_after_gap_are_not_marked_ambiguous(self):
        order = {"symbol": "ETH-USDT", "submitted_ts_ms": self.window["start_ms"] - 1, "signal_id": "eth"}
        self.assertIsNone(_ambiguous_price_candle_position(order, self.window))
        order["symbol"] = "BTC-USDT"
        order["submitted_ts_ms"] = self.window["end_exclusive_ms"] + 1
        self.assertIsNone(_ambiguous_price_candle_position(order, self.window))


class StopAtrMultiplierConfigurationTests(unittest.TestCase):
    def test_symbol_defaults_are_unchanged(self):
        self.assertEqual(_resolve_stop_atr_multiplier("BTC-USDT"), 1.3)
        self.assertEqual(_resolve_stop_atr_multiplier("ETH-USDT"), 1.3)
        self.assertEqual(_resolve_stop_atr_multiplier("SOL-USDT"), 1.8)
        self.assertEqual(_resolve_stop_atr_multiplier("LINK-USDT"), 1.8)

    def test_research_override_changes_only_the_requested_symbol(self):
        self.assertEqual(_resolve_stop_atr_multiplier("ETH-USDT", 1.625), 1.625)
        self.assertEqual(_resolve_stop_atr_multiplier("SOL-USDT", 2.25), 2.25)
        self.assertEqual(_resolve_stop_atr_multiplier("BTC-USDT"), 1.3)

    def test_invalid_stop_override_fails_closed(self):
        for value in (0.0, 0.49, 4.01, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _resolve_stop_atr_multiplier("ETH-USDT", value)


class BloFinFundingSchemaProbeTests(unittest.TestCase):
    def test_history_probe_counts_explicit_and_missing_kind_separately(self):
        report = summarize_history_payload({
            "code": "0",
            "data": [
                {"fundingTime": "1782403200000", "fundingRate": "0.0001", "fundingKind": "0"},
                {"fundingTime": "1782432000000", "fundingRate": "0.0002", "fundingKind": "1"},
                {"fundingTime": "1782460800000", "fundingRate": "0.0003"},
            ],
        })
        self.assertEqual(report["sample_count"], 3)
        self.assertEqual(report["kind_present_count"], 2)
        self.assertEqual(report["kind_missing_count"], 1)
        self.assertEqual(report["kind_values"], {"0": 1, "1": 1})

    def test_current_interval_probe_accepts_eight_hour_utc_boundary(self):
        report = summarize_current_payload({
            "code": "0",
            "data": [{
                "fundingInterval": "8",
                "fundingIntervalUnit": "hour",
                "fundingTime": "1782403200000",
            }],
        })
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["funding_interval_ms"], 8 * 60 * 60_000)
        self.assertTrue(report["utc_boundary_verified"])

    def test_current_interval_probe_rejects_off_schedule_timestamp(self):
        report = summarize_current_payload({
            "code": "0",
            "data": [{
                "fundingInterval": "8",
                "fundingIntervalUnit": "hour",
                "fundingTime": "1782406800000",
            }],
        })
        self.assertEqual(report["status"], "OFF_SCHEDULE")
        self.assertFalse(report["utc_boundary_verified"])


class BlockReasonLedgerTests(unittest.TestCase):
    def test_global_and_per_symbol_block_reasons_reconcile(self):
        overall = {}
        by_symbol = {}
        _record_block_reason(overall, by_symbol, "BTC-USDT", "NO_TRADE: ADX below threshold")
        _record_block_reason(overall, by_symbol, "BTC-USDT", "NO_TRADE: ADX below threshold")
        _record_block_reason(overall, by_symbol, "ETH-USDT", "MAX_TWO_CONCURRENT_SIMULATED_POSITIONS")
        self.assertEqual(overall["NO_TRADE: ADX below threshold"], 2)
        self.assertEqual(by_symbol["BTC-USDT"]["NO_TRADE: ADX below threshold"], 2)
        self.assertEqual(by_symbol["ETH-USDT"]["MAX_TWO_CONCURRENT_SIMULATED_POSITIONS"], 1)
        self.assertEqual(sum(overall.values()), sum(sum(x.values()) for x in by_symbol.values()))




class StrategyWarmupAccountingTests(unittest.TestCase):
    def test_minimum_closed_history_requirements_are_exact(self):
        self.assertTrue(_strategy_history_ready(
            pd.DataFrame(index=range(205)),
            pd.DataFrame(index=range(205)),
            pd.DataFrame(index=range(70)),
            pd.DataFrame(index=range(40)),
        ))

    def test_short_4h_history_is_a_warmup_block_not_a_strategy_error(self):
        self.assertFalse(_strategy_history_ready(
            pd.DataFrame(index=range(205)),
            pd.DataFrame(index=range(204)),
            pd.DataFrame(index=range(70)),
            pd.DataFrame(index=range(40)),
        ))

    def test_short_15m_history_is_a_warmup_block(self):
        self.assertFalse(_strategy_history_ready(
            pd.DataFrame(index=range(205)),
            pd.DataFrame(index=range(205)),
            pd.DataFrame(index=range(69)),
            pd.DataFrame(index=range(40)),
        ))


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
            funding_kind="0",
            funding_event_time_ms=int(asof.timestamp() * 1000) - 60_000,
            funding_max_age_ms=900_000,
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
            funding_kind="0",
            funding_event_time_ms=int(asof.timestamp() * 1000) - 60_000,
            funding_max_age_ms=900_000,
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
            funding_events=[{"fundingTime": 1_699_999_999_999, "fundingRate": 0.0, "fundingKind": "0"}],
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
    def test_missing_funding_kind_is_preserved_as_missing_provenance(self):
        self.assertEqual(
            _parse_funding_event(
                {"fundingTime": "1000", "fundingRate": "0.0001"},
                symbol="BTC-USDT",
            ),
            ("BTC-USDT", 1000, 0.0001, "MISSING", "MISSING_API_FIELD"),
        )

    def test_unknown_funding_kind_is_rejected(self):
        with self.assertRaisesRegex(ReplayDownloadError, "unsupported fundingKind"):
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
        self.assertEqual(regular, ("BTC-USDT", 1000, 0.0001, "0", "EXPLICIT_API"))
        self.assertEqual(dividend, ("BTC-USDT", 2000, 0.0002, "1", "EXPLICIT_API"))


class SandboxDatetimeResolutionTests(unittest.TestCase):
    def test_epoch_milliseconds_are_stable_across_pandas_datetime_resolutions(self):
        expected = np.asarray([1779966000000, 1779966900000], dtype=np.int64)
        index = pd.to_datetime(expected, unit="ms", utc=True)
        for unit in ("ms", "us", "ns"):
            with self.subTest(unit=unit):
                converted = _datetime_index_to_epoch_ms(index.as_unit(unit))
                np.testing.assert_array_equal(converted, expected)


class SandboxFundingKindTests(unittest.TestCase):
    def test_regular_kind_zero_is_accepted(self):
        _require_regular_funding_events(
            [{"fundingTime": 1000, "fundingRate": 0.0001, "fundingKind": "0"}],
            "BTC-USDT",
        )

    def test_stock_dividend_kind_is_blocked_before_replay(self):
        with self.assertRaisesRegex(SandboxDataError, "only models regular fundingKind=0"):
            _require_regular_funding_events(
                [{"fundingTime": 1000, "fundingRate": 0.0001, "fundingKind": "1"}],
                "BTC-USDT",
            )

    def test_missing_kind_is_blocked_before_replay(self):
        with self.assertRaisesRegex(SandboxDataError, "only models regular fundingKind=0"):
            _require_regular_funding_events(
                [{"fundingTime": 1000, "fundingRate": 0.0001}],
                "BTC-USDT",
            )

    def test_verified_schedule_inferred_regular_funding_is_accepted(self):
        timestamp = int(pd.Timestamp("2026-06-25T16:00:00Z").timestamp() * 1000)
        _require_regular_funding_events(
            [{
                "fundingTime": timestamp, "fundingRate": 0.0001,
                "fundingKind": "0",
                "fundingKindSource": "SCHEDULE_INFERRED_UTC_8H_BOUNDARY",
                "fundingIntervalMs": 8 * 60 * 60_000,
                "fundingScheduleVerified": True,
            }],
            "BTC-USDT",
        )

    def test_verified_schedule_inferred_funding_rejects_non_boundary_and_outside_tolerance(self):
        boundary = int(pd.Timestamp("2026-06-25T16:00:00Z").timestamp() * 1000)
        base = {
            "fundingRate": 0.0001,
            "fundingKind": "0",
            "fundingKindSource": "SCHEDULE_INFERRED_UTC_8H_BOUNDARY",
            "fundingIntervalMs": 8 * 60 * 60_000,
            "fundingScheduleVerified": True,
        }
        for offset in (11_000, 4 * 60 * 60_000):
            with self.subTest(offset_ms=offset), self.assertRaises(SandboxDataError):
                _require_regular_funding_events(
                    [{**base, "fundingTime": boundary + offset}],
                    "BTC-USDT",
                )

    def test_schedule_inference_without_verification_is_blocked(self):
        timestamp = int(pd.Timestamp("2026-06-25T16:00:00Z").timestamp() * 1000)
        with self.assertRaisesRegex(SandboxDataError, "only models regular fundingKind=0"):
            _require_regular_funding_events(
                [{
                    "fundingTime": timestamp, "fundingRate": 0.0001,
                    "fundingKind": "0",
                    "fundingKindSource": "SCHEDULE_INFERRED_UTC_8H_BOUNDARY",
                    "fundingIntervalMs": 8 * 60 * 60_000,
                    "fundingScheduleVerified": False,
                }],
                "BTC-USDT",
            )

class VirtualBloFinFundingKindTests(unittest.TestCase):
    @staticmethod
    def _simulate(funding_event):
        engine = VirtualBloFin()
        rules = InstrumentRules(
            inst_id="BTC-USDT",
            contract_value=Decimal("1"),
            min_size=Decimal("1"),
            lot_size=Decimal("1"),
            tick_size=Decimal("0.01"),
        )
        bars = [
            {"ts": 1_700_000_000_000, "low": 99.0, "high": 101.0, "mark_high": 101.0, "mark_low": 99.0},
            {"ts": 1_700_000_900_000, "low": 99.0, "high": 101.0, "mark_high": 101.0, "mark_low": 99.0},
        ]
        return engine.simulate_post_only_trade(
            bars=bars, start_index=0, direction="LONG", limit_price=100,
            stop_price=99, target_price=104, equity_usdt=1000,
            risk_fraction=0.0025, rules=rules, funding_events=[funding_event],
            spread_fraction=0.0002,
        )

    def test_stock_dividend_event_is_not_applied_as_regular_funding(self):
        with self.assertRaisesRegex(SimulationError, "only fundingKind=0"):
            self._simulate({
                "fundingTime": "1700000000001",
                "fundingRate": "0.0001",
                "fundingKind": "1",
            })

    def test_missing_funding_kind_is_not_assumed_regular_by_simulator(self):
        with self.assertRaisesRegex(SimulationError, "only fundingKind=0"):
            self._simulate({"fundingTime": "1700000000001", "fundingRate": "0.0001"})

    def test_explicit_regular_funding_kind_is_accepted(self):
        result = self._simulate({
            "fundingTime": "1699999999999",
            "fundingRate": "0.0001",
            "fundingKind": "0",
        })
        self.assertNotEqual(result.status, "REJECTED")

    def test_verified_fixed_utc_8h_schedule_inference_is_accepted(self):
        timestamp = int(pd.Timestamp("2023-11-14T16:00:00Z").timestamp() * 1000)
        result = self._simulate({
            "fundingTime": timestamp,
            "fundingRate": "0.0001",
            "fundingKind": "0",
            "fundingKindSource": "SCHEDULE_INFERRED_UTC_8H_BOUNDARY",
            "fundingIntervalMs": 8 * 60 * 60_000,
            "fundingScheduleVerified": True,
        })
        self.assertNotEqual(result.status, "REJECTED")

    def test_schedule_inference_requires_verified_fixed_8h_utc_boundary(self):
        boundary = int(pd.Timestamp("2023-11-14T16:00:00Z").timestamp() * 1000)
        base_event = {
            "fundingTime": boundary,
            "fundingRate": "0.0001",
            "fundingKind": "0",
            "fundingKindSource": "SCHEDULE_INFERRED_UTC_8H_BOUNDARY",
            "fundingIntervalMs": 8 * 60 * 60_000,
            "fundingScheduleVerified": True,
        }
        invalid_events = (
            {**base_event, "fundingTime": boundary + 11_000},
            {**base_event, "fundingIntervalMs": 4 * 60 * 60_000},
            {**base_event, "fundingScheduleVerified": False},
        )
        for event in invalid_events:
            with self.subTest(event=event), self.assertRaises(SimulationError):
                self._simulate(event)

    def test_dynamic_interval_funding_provenance_is_rejected(self):
        with self.assertRaisesRegex(SimulationError, "funding provenance is missing or unsupported"):
            self._simulate({
                "fundingTime": "1699999999999",
                "fundingRate": "0.0001",
                "fundingKind": "0",
                "fundingKindSource": "SCHEDULE_INFERRED_DYNAMIC_INTERVAL",
                "fundingIntervalMs": 60 * 60_000,
                "fundingScheduleVerified": True,
            })

    def test_virtual_simulator_applies_funding_with_epoch_millisecond_timestamps(self):
        engine = VirtualBloFin()
        rules = InstrumentRules(
            inst_id="BTC-USDT",
            contract_value=Decimal("1"),
            min_size=Decimal("1"),
            lot_size=Decimal("1"),
            tick_size=Decimal("0.01"),
        )
        base = 1_700_000_000_000
        bars = [
            {"ts": base, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
            {"ts": base + 900_000, "low": 99.5, "high": 101.0, "mark_high": 101.0, "mark_low": 99.5},
            {"ts": base + 1_800_000, "low": 99.5, "high": 101.0, "mark_high": 101.0, "mark_low": 99.5},
        ]
        result = engine.simulate_post_only_trade(
            bars=bars, start_index=0, direction="LONG", limit_price=100,
            stop_price=99, target_price=104, equity_usdt=1000,
            risk_fraction=0.0025, rules=rules,
            funding_events=[{
                "fundingTime": base + 1_800_000,
                "fundingRate": "0.0001",
                "fundingKind": "0",
            }],
            spread_fraction=0.0002,
        )
        self.assertEqual(result.status, "OPEN")
        self.assertGreater(result.diagnostics["entry_ts_ms"], 1_000_000_000_000)
        self.assertEqual(result.diagnostics["funding_event_count"], 1)
        self.assertGreater(result.funding_usdt, 0)

    def test_virtual_simulator_rejects_epoch_seconds(self):
        with self.assertRaisesRegex(SimulationError, "Unix epoch milliseconds"):
            from crypto_super_radar.virtual_blofin import _timestamp_ms
            _timestamp_ms(1_700_000_000)



class ReplayFundingScheduleInferenceTests(unittest.TestCase):
    @staticmethod
    def _rows():
        stamps = [
            int(pd.Timestamp("2026-06-25T00:00:00Z").timestamp() * 1000),
            int(pd.Timestamp("2026-06-25T08:00:00Z").timestamp() * 1000),
            int(pd.Timestamp("2026-06-25T16:00:00Z").timestamp() * 1000),
            int(pd.Timestamp("2026-06-26T16:00:00Z").timestamp() * 1000),
            int(pd.Timestamp("2026-06-27T00:00:00Z").timestamp() * 1000),
            int(pd.Timestamp("2026-06-27T08:00:00Z").timestamp() * 1000),
        ]
        return [("BTC-USDT", stamp, 0.0001, "MISSING", "MISSING_API_FIELD") for stamp in stamps]

    def test_missing_kind_uses_only_fixed_utc_schedule_and_preserves_blackout(self):
        rows = self._rows()
        result = _classify_funding_schedule_events(rows, symbol="BTC-USDT")
        self.assertTrue(all(row[3] == "0" for row in result))
        self.assertTrue(all(row[4] == "SCHEDULE_INFERRED_UTC_8H_BOUNDARY" for row in result))
        self.assertEqual({row[5] for row in result}, {8 * 60 * 60_000})
        self.assertTrue(_is_observed_blackout_gap(rows[2][1], rows[3][1]))

    def test_missing_kind_at_utc_boundary_plus_nine_seconds_is_accepted(self):
        rows = self._rows()
        rows[0] = (rows[0][0], rows[0][1] + 9_000, rows[0][2], rows[0][3], rows[0][4])
        result = _classify_funding_schedule_events(rows, symbol="BTC-USDT")
        self.assertEqual(result[0][4], "SCHEDULE_INFERRED_UTC_8H_BOUNDARY")

    def test_missing_kind_at_utc_boundary_plus_eleven_seconds_is_rejected(self):
        rows = self._rows()
        rows[0] = (rows[0][0], rows[0][1] + 11_000, rows[0][2], rows[0][3], rows[0][4])
        with self.assertRaisesRegex(ReplayDownloadError, "00:00/08:00/16:00 UTC"):
            _classify_funding_schedule_events(rows, symbol="BTC-USDT")

    def test_missing_kind_at_four_am_is_rejected_even_if_neighboring_deltas_look_regular(self):
        rows = self._rows()
        rows[0] = (
            rows[0][0],
            int(pd.Timestamp("2026-06-25T04:00:00Z").timestamp() * 1000),
            rows[0][2], rows[0][3], rows[0][4],
        )
        with self.assertRaisesRegex(ReplayDownloadError, "outside the 00:00/08:00/16:00 UTC"):
            _classify_funding_schedule_events(rows, symbol="BTC-USDT")

    def test_explicit_nonzero_funding_kind_is_rejected(self):
        rows = self._rows()
        rows[1] = (rows[1][0], rows[1][1], rows[1][2], "1", "EXPLICIT_API")
        with self.assertRaisesRegex(ReplayDownloadError, "fundingKind"):
            _classify_funding_schedule_events(rows, symbol="BTC-USDT")

    def test_unregistered_long_gap_is_not_promoted_to_blackout(self):
        rows = self._rows()
        rows[3] = (rows[3][0], rows[3][1] + 60 * 60_000, rows[3][2], rows[3][3], rows[3][4])
        self.assertFalse(_is_observed_blackout_gap(rows[2][1], rows[3][1]))
        with self.assertRaises(ReplayDownloadError):
            _classify_funding_schedule_events(rows, symbol="BTC-USDT")


class VirtualBloFinFundingBlackoutTests(unittest.TestCase):
    @staticmethod
    def _setup():
        return VirtualBloFin(), InstrumentRules(
            inst_id="BTC-USDT", contract_value=Decimal("1"),
            min_size=Decimal("1"), lot_size=Decimal("1"), tick_size=Decimal("0.01"),
        )

    @staticmethod
    def _funding(base):
        return [{
            "fundingTime": base - 1, "fundingRate": 0.0001,
            "fundingKind": "0", "fundingKindSource": "EXPLICIT_API",
        }]

    def test_position_crossing_blackout_keeps_funding_unknown_but_remains_open(self):
        engine, rules = self._setup()
        base = 1_700_000_000_000
        bars = [
            {"ts": base, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
            {"ts": base + 900_000, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
            {"ts": base + 1_800_000, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
        ]
        result = engine.simulate_post_only_trade(
            bars=bars, start_index=0, direction="LONG", limit_price=100,
            stop_price=99, target_price=104, equity_usdt=1000,
            risk_fraction=0.0025, rules=rules, funding_events=self._funding(base),
            funding_blackouts=[{
                "start_ms": base + 1_800_000, "end_ms": base + 24 * 60 * 60_000,
                "label": "EXCHANGE_DATA_GAP_BLACKOUT",
            }],
            spread_fraction=0.0002,
        )
        self.assertEqual(result.status, "OPEN")
        self.assertIsNone(result.net_pnl_usdt)
        self.assertFalse(result.diagnostics["funding_complete"])
        self.assertEqual(len(result.diagnostics["funding_blackouts"]), 1)

    def test_price_exit_after_blackout_closes_position_but_pnl_remains_unknown(self):
        engine, rules = self._setup()
        base = 1_700_000_000_000
        blackout_start, blackout_end = base + 1_800_000, base + 24 * 60 * 60_000
        bars = [
            {"ts": base, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
            {"ts": base + 900_000, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
            {"ts": blackout_start, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
            {"ts": blackout_end, "low": 98.5, "high": 100.5, "mark_high": 100.5, "mark_low": 98.5},
        ]
        result = engine.simulate_post_only_trade(
            bars=bars, start_index=0, direction="LONG", limit_price=100,
            stop_price=99, target_price=104, equity_usdt=1000,
            risk_fraction=0.0025, rules=rules, funding_events=self._funding(base),
            funding_blackouts=[{
                "start_ms": blackout_start, "end_ms": blackout_end,
                "label": "EXCHANGE_DATA_GAP_BLACKOUT",
            }],
            spread_fraction=0.0002,
        )
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.exit_reason, "FUNDING_DATA_GAP_PNL_UNKNOWN")
        self.assertTrue(result.diagnostics["position_closed"])
        self.assertEqual(result.diagnostics["market_exit_reason"], "SL")
        self.assertIsNotNone(result.diagnostics["exit_ts_ms"])
        self.assertIsNone(result.net_pnl_usdt)
        self.assertFalse(result.diagnostics["funding_complete"])

    def test_fill_inside_blackout_is_rejected_without_position_or_fees(self):
        engine, rules = self._setup()
        base = 1_700_000_000_000
        bars = [
            {"ts": base, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
            {"ts": base + 900_000, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
            {"ts": base + 1_800_000, "low": 99.5, "high": 100.5, "mark_high": 100.5, "mark_low": 99.5},
        ]
        result = engine.simulate_post_only_trade(
            bars=bars, start_index=0, direction="LONG", limit_price=100,
            stop_price=99, target_price=104, equity_usdt=1000,
            risk_fraction=0.0025, rules=rules, funding_events=self._funding(base),
            funding_blackouts=[{
                "start_ms": base + 900_000, "end_ms": base + 24 * 60 * 60_000,
                "label": "EXCHANGE_DATA_GAP_BLACKOUT",
            }],
            spread_fraction=0.0002,
        )
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(result.exit_reason, "FUNDING_DATA_GAP_BLACKOUT")
        self.assertFalse(result.diagnostics["entry_filled"])
        self.assertFalse(result.diagnostics["orders_submitted"])
        self.assertEqual(result.fees_usdt, 0.0)


class V2HigherTimeframeWaveEvidenceTests(unittest.TestCase):
    @staticmethod
    def _valid_signal_record():
        frame = V2HigherTimeframeWaveEvidenceTests._wave_frame()
        asof = frame.index[-1] + pd.Timedelta(hours=1)
        side = "LONG"
        signal_open = asof - pd.Timedelta(minutes=15)
        h1_open = asof - pd.Timedelta(hours=1)
        h4_open = asof - pd.Timedelta(hours=4)
        wave = _elliott_wave_1h_evidence(
            frame, asof, expected_direction=side,
        )
        symbol = "BTC-USDT"
        signal_id = f"challenge1-v2:{symbol}:15m:{signal_open.isoformat()}:{side}"
        return {
            "signal": side,
            "signal_id": signal_id,
            "instrument_id": symbol,
            "entry": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.4,
            "gross_rr": 2.4,
            "confidence_score": 85.0,
            "score_parts": {
                "h1_h4_alignment": 30, "adx_gt_28": 20,
                "value_zone_rejection": 25, "volume_gt_1_5x": 0,
                "btc_alignment": 10,
            },
            "funding_rate": 0.0001,
            "funding_kind": "0",
            "funding_event_time_ms": int(asof.timestamp() * 1000) - 60_000,
            "funding_max_age_ms": 900_000,
            "spread_fraction": 0.0002,
            "signal_candle_open_utc": signal_open.isoformat(),
            "orders_submitted": False,
            "regime": {
                "status": "TREND", "direction": side,
                "candle_open_utc": h1_open.isoformat(),
                "candle_open_utc_4h": h4_open.isoformat(),
                "aligned_4h": True,
            },
            "challenge1_v2_evidence": {
                "decision_engine_version": "CHALLENGE_1_V2_1H_4H_15M",
                "signal_id": signal_id, "instrument_id": symbol,
                "setup_timeframe": "15m",
                "context_timeframes": ["1H", "4H"],
                "higher_timeframes_used": True,
                "completed_candles_only": True,
                "signal_candle_open_utc": signal_open.isoformat(),
                "context_1h_candle_open_utc": h1_open.isoformat(),
                "context_4h_candle_open_utc": h4_open.isoformat(),
                "data_asof_utc": asof.isoformat(),
                "funding_rate": 0.0001,
                "funding_kind": "0",
                "funding_event_time_ms": int(asof.timestamp() * 1000) - 60_000,
                "funding_max_age_ms": 900_000,
                "elliott_wave": wave,
                "wave_alignment": "ALIGNED",
            },
        }

    @staticmethod
    def _wave_frame() -> pd.DataFrame:
        values = [
            100, 100, 120, 120, 110, 110, 140, 140,
            125, 125, 150, 150, 140,
        ]
        values += [140] * (220 - len(values))
        index = pd.date_range(
            "2026-01-01T00:00:00Z", periods=len(values), freq="1h"
        )
        close = np.asarray(values, dtype=float)
        return pd.DataFrame(
            {
                "open": close,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": np.full(len(close), 100.0),
            },
            index=index,
        )

    def test_wave_evidence_uses_only_closed_one_hour_candles(self):
        frame = self._wave_frame()
        asof = frame.index[-1] + pd.Timedelta(hours=1)
        evidence = _elliott_wave_1h_evidence(frame, asof, expected_direction="LONG")
        self.assertEqual(evidence["timeframe"], "1H")
        self.assertLessEqual(
            pd.Timestamp(evidence["latest_closed_candle_open_utc"]) + pd.Timedelta(hours=1),
            asof,
        )
        self.assertEqual(evidence["data_asof_utc"], asof.isoformat())
        for pivot in evidence["pivots"]:
            self.assertLessEqual(pd.Timestamp(pivot["confirmed_at"]), asof)

    def test_future_one_hour_candle_mutations_do_not_change_wave_evidence(self):
        frame = self._wave_frame()
        asof = frame.index[-3] + pd.Timedelta(hours=1)
        original = _elliott_wave_1h_evidence(frame, asof, expected_direction="LONG")
        changed = frame.copy()
        future = changed.index + pd.Timedelta(hours=1) > asof
        changed.loc[future, ["open", "high", "low", "close", "volume"]] = [
            [999.0, 1000.0, 998.0, 999.5, 100.0]
        ] * int(future.sum())
        mutated = _elliott_wave_1h_evidence(changed, asof, expected_direction="LONG")
        self.assertEqual(original, mutated)

    def test_v2_execution_preflight_accepts_complete_aligned_evidence_without_authorizing_order(self):
        record = self._valid_signal_record()
        ready, reason = challenge1_v2_execution_evidence_ready(record)
        self.assertTrue(ready, reason)
        self.assertIn("independent AI consensus", reason)

    def test_v2_execution_preflight_rejects_missing_funding_kind(self):
        record = self._valid_signal_record()
        record.pop("funding_kind")
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_v2_execution_preflight_rejects_stock_dividend_funding_kind(self):
        record = self._valid_signal_record()
        record["funding_kind"] = "1"
        record["challenge1_v2_evidence"]["funding_kind"] = "1"
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_v2_execution_preflight_rejects_future_funding_event(self):
        record = self._valid_signal_record()
        record["funding_event_time_ms"] += 120_000
        record["challenge1_v2_evidence"]["funding_event_time_ms"] += 120_000
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_v2_execution_preflight_rejects_stale_funding_event(self):
        record = self._valid_signal_record()
        record["funding_max_age_ms"] = 1_000
        record["challenge1_v2_evidence"]["funding_max_age_ms"] = 1_000
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_v2_execution_preflight_rejects_signal_id_mismatch(self):
        record = self._valid_signal_record()
        record["challenge1_v2_evidence"]["signal_id"] = "other-signal"
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_v2_execution_preflight_rejects_stale_15m_candle(self):
        record = self._valid_signal_record()
        evidence = record["challenge1_v2_evidence"]
        asof = pd.Timestamp(evidence["data_asof_utc"]) + pd.Timedelta(minutes=31)
        evidence["data_asof_utc"] = asof.isoformat()
        evidence["elliott_wave"]["data_asof_utc"] = asof.isoformat()
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_v2_execution_preflight_rejects_opposed_wave(self):
        record = self._valid_signal_record()
        record["challenge1_v2_evidence"]["elliott_wave"]["pattern"] = "BEARISH"
        record["challenge1_v2_evidence"]["wave_alignment"] = "OPPOSED"
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_v2_execution_preflight_rejects_missing_4h_context(self):
        record = self._valid_signal_record()
        record["challenge1_v2_evidence"]["context_4h_candle_open_utc"] = None
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_v2_execution_preflight_rejects_order_side_effect_flag(self):
        record = self._valid_signal_record()
        record["orders_submitted"] = True
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_v2_execution_preflight_rejects_future_pivot_confirmation(self):
        record = self._valid_signal_record()
        evidence = record["challenge1_v2_evidence"]
        evidence["elliott_wave"]["pivots"][-1]["confirmed_at"] = (
            pd.Timestamp(evidence["data_asof_utc"]) + pd.Timedelta(minutes=1)
        ).isoformat()
        self.assertFalse(challenge1_v2_execution_evidence_ready(record)[0])

    def test_opposite_wave_direction_is_reported_as_opposed(self):
        frame = self._wave_frame()
        asof = frame.index[-1] + pd.Timedelta(hours=1)
        evidence = _elliott_wave_1h_evidence(frame, asof, expected_direction="SHORT")
        self.assertEqual(evidence["pattern"], "BULLISH")
        self.assertEqual(evidence["wave_alignment"], "OPPOSED")

    def test_wave_metadata_does_not_promote_ambiguous_history_to_alignment(self):
        frame = pd.DataFrame(
            {
                "open": [100.0] * 220,
                "high": [100.5] * 220,
                "low": [99.5] * 220,
                "close": [100.0] * 220,
                "volume": [100.0] * 220,
            },
            index=pd.date_range("2026-01-01T00:00:00Z", periods=220, freq="1h"),
        )
        asof = frame.index[-1] + pd.Timedelta(hours=1)
        evidence = _elliott_wave_1h_evidence(frame, asof, expected_direction="LONG")
        self.assertIn(evidence["status"], {"UNKNOWN", "AMBIGUOUS"})
        self.assertEqual(evidence["wave_alignment"], "UNCONFIRMED")


if __name__ == "__main__":
    unittest.main()
