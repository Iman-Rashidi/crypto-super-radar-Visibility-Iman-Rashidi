from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from crypto_super_radar.challenge1_v2 import (
    analyze_signal,
    prepare_signal_context,
)


class WalkForwardCausalPrecomputationEquivalenceTests(unittest.TestCase):
    @staticmethod
    def _frame(index: pd.DatetimeIndex, *, drift: float, amplitude: float, period: float) -> pd.DataFrame:
        x = np.arange(len(index), dtype=float)
        close = (
            100.0 + drift * x
            + amplitude * np.sin(x / period)
            + 0.10 * np.sin(x / 2.7)
        )
        open_ = close - 0.025 * np.cos(x / 3.1)
        high = np.maximum(open_, close) + 0.12 + 0.015 * np.sin(x / 4.0)
        low = np.minimum(open_, close) - 0.12 - 0.015 * np.cos(x / 4.3)
        volume = 100.0 + 8.0 * (1.0 + np.sin(x / 6.0))
        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=index,
        )

    @classmethod
    def setUpClass(cls) -> None:
        cls.start = pd.Timestamp("2026-01-01T00:00:00Z")
        cls.h1 = cls._frame(
            pd.date_range(cls.start, periods=900, freq="1h"),
            drift=0.12, amplitude=1.0, period=5.0,
        )
        cls.h4 = cls._frame(
            pd.date_range(cls.start, periods=240, freq="4h"),
            drift=0.48, amplitude=1.8, period=3.0,
        )
        cls.m15 = cls._frame(
            pd.date_range(cls.start, periods=3600, freq="15min"),
            drift=0.00015, amplitude=0.20, period=5.0,
        )
        # Build one deterministic breakout candle near an otherwise bounded
        # volatility regime so the test compares a complete executable plan too.
        asof = cls.start + pd.Timedelta(hours=864)
        current = int((asof - cls.start) / pd.Timedelta(minutes=15)) - 1
        previous_close = float(cls.m15.iloc[current - 1]["close"])
        previous_high = previous_close + 0.08
        cls.m15.iloc[current - 1, cls.m15.columns.get_loc("high")] = previous_high
        cls.m15.iloc[current, cls.m15.columns.get_loc("open")] = previous_close + 0.025
        cls.m15.iloc[current, cls.m15.columns.get_loc("close")] = previous_high + 0.025
        cls.m15.iloc[current, cls.m15.columns.get_loc("low")] = previous_close - 0.10
        cls.m15.iloc[current, cls.m15.columns.get_loc("high")] = previous_high + 0.12
        cls.m15.iloc[current, cls.m15.columns.get_loc("volume")] = 260.0
        cls.btc = cls.m15.copy(deep=True)
        cls.asof = asof
        cls.funding_time_ms = int((asof - pd.Timedelta(minutes=5)).timestamp() * 1000)

    @classmethod
    def _analyze(cls, *, optimized: bool, h1=None, h4=None, m15=None, btc=None):
        source_h1 = cls.h1 if h1 is None else h1
        source_h4 = cls.h4 if h4 is None else h4
        source_m15 = cls.m15 if m15 is None else m15
        source_btc = cls.btc if btc is None else btc
        context = (
            prepare_signal_context(source_h1, source_h4, source_m15, source_btc)
            if optimized else None
        )
        return analyze_signal(
            instrument_id="BTC-USDT",
            frame_1h=source_h1,
            frame_4h=source_h4,
            frame_15m=source_m15,
            btc_15m=source_btc,
            asof=cls.asof,
            funding_rate=0.00001,
            funding_kind="0",
            funding_event_time_ms=cls.funding_time_ms,
            funding_max_age_ms=24 * 60 * 60_000,
            spread_fraction=0.0002,
            last_three_results=(1, 0, 1),
            tp_r=2.4,
            precomputed_context=context,
        )

    def test_precomputed_signal_and_trade_plan_are_exactly_equal_to_legacy(self):
        legacy = self._analyze(optimized=False)
        optimized = self._analyze(optimized=True)
        self.assertEqual(optimized, legacy)
        for key in ("signal", "entry", "stop_loss", "take_profit", "atr_15m", "gross_rr"):
            self.assertEqual(optimized.get(key), legacy.get(key), key)
        if legacy.get("signal") in {"LONG", "SHORT"}:
            self.assertIsNotNone(legacy.get("entry"))
            self.assertIsNotNone(legacy.get("stop_loss"))
            self.assertIsNotNone(legacy.get("take_profit"))

    def test_future_candle_mutations_do_not_change_optimized_or_legacy_asof_output(self):
        def mutate_future(frame: pd.DataFrame, duration: pd.Timedelta) -> pd.DataFrame:
            changed = frame.copy(deep=True)
            future = changed.index + duration > self.asof
            changed.loc[future, "open"] *= 1.35
            changed.loc[future, "close"] *= 1.35
            changed.loc[future, "high"] = (
                changed.loc[future, ["open", "close"]].max(axis=1) + 0.5
            )
            changed.loc[future, "low"] = (
                changed.loc[future, ["open", "close"]].min(axis=1) - 0.5
            )
            changed.loc[future, "volume"] *= 7.0
            return changed

        baseline = self._analyze(optimized=False)
        mutated_frames = {
            "h1": mutate_future(self.h1, pd.Timedelta(hours=1)),
            "h4": mutate_future(self.h4, pd.Timedelta(hours=4)),
            "m15": mutate_future(self.m15, pd.Timedelta(minutes=15)),
            "btc": mutate_future(self.btc, pd.Timedelta(minutes=15)),
        }
        legacy_mutated = self._analyze(
            optimized=False, **mutated_frames
        )
        optimized_mutated = self._analyze(
            optimized=True, **mutated_frames
        )
        self.assertEqual(legacy_mutated, baseline)
        self.assertEqual(optimized_mutated, baseline)


if __name__ == "__main__":
    unittest.main()
