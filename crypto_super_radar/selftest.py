"""Offline deterministic tests; these never call public APIs or sleep on rate limits."""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from .backtesting import walk_forward_backtest
from .config import AppConfig
from .engine import ConfirmationEngine
from .indicators import rsi, technical_metrics
from .models import AssetSpec, GateStatus, MarketSnapshot, SafetyStatus, Signal
from .providers import _ohlcv_frame, relative_difference_pct
from .reporting import results_dataframe
from .sentiment import score_headlines


def _sample_candles(n: int = 240, stronger: bool = True) -> pd.DataFrame:
    t = np.arange(n, dtype=float)
    base_slope = 0.19 if stronger else 0.09
    wave = 2.3 * np.sin(t / 5.0) + 1.0 * np.sin(t / 13.0)
    close = 100.0 + base_slope * t + wave
    close = np.maximum(close, 1.0)
    index = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("D"), periods=n, freq="D")
    return pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.012,
        "low": close * 0.988,
        "close": close,
        "volume": np.full(n, 1_000_000.0),
    }, index=index)


class _FakeDataService:
    def __init__(self):
        self.asset_candles = _sample_candles(240, stronger=True)
        self.benchmark = _sample_candles(240, stronger=False)

    def snapshot(self, asset):
        return MarketSnapshot(
            asset=asset,
            price_usd=float(self.asset_candles["close"].iloc[-1]),
            volume_24h_usd=25_000_000.0,
            liquidity_usd=None,
            price_discrepancy_pct=0.2,
            volume_discrepancy_pct=1.1,
            provider_a="Mock-A",
            provider_b="Mock-B",
        )

    def candles(self, asset, snapshot):
        return self.asset_candles.copy()

    def benchmark_candles(self):
        return self.benchmark.copy()

    def security(self, asset):
        return {"status": "PASS", "reason": "Mock native asset security gate."}

    def funding_history(self, asset):
        return pd.Series(dtype=float)


class RadarTests(unittest.TestCase):
    def test_valid_asset_parsing(self):
        self.assertEqual(AssetSpec.parse("solana:So11111111111111111111111111111111111111112").chain, "solana")
        self.assertEqual(AssetSpec.parse("cex:BTCUSDT").symbol, "BTCUSDT")
        self.assertEqual(AssetSpec.parse("perp:ETHUSDT").market_type, "perp")

    def test_invalid_asset_parsing(self):
        with self.assertRaises(ValueError):
            AssetSpec.parse("solana:")
        with self.assertRaises(ValueError):
            AssetSpec.parse("unknown:123456789012")

    def test_cross_provider_discrepancy(self):
        self.assertAlmostEqual(relative_difference_pct(100.0, 102.0), 1.980198, places=4)
        self.assertGreater(relative_difference_pct(100.0, 110.0), 5.0)

    def test_missing_timestamp_is_not_fresh(self):
        frame = _ohlcv_frame([["bad-time", 1, 2, 0.5, 1.5, 100]])
        self.assertTrue(frame.empty)

    def test_rsi_is_computed_or_unknown(self):
        self.assertIsNone(rsi(pd.Series([1.0, 2.0, 3.0]), period=14))
        rising = pd.Series(np.arange(1.0, 30.0))
        self.assertEqual(rsi(rising, period=14), 100.0)

    def test_sentiment_missing_data_is_unknown(self):
        result = score_headlines([])
        self.assertIsNone(result["score"])
        self.assertEqual(result["status"], "UNKNOWN")

    def test_sentiment_penalizes_adverse_headline(self):
        result = score_headlines(["Protocol suffers exploit and withdrawals paused"])
        self.assertLess(result["score"], 35)

    def test_walk_forward_uses_costs(self):
        config = AppConfig()
        result = walk_forward_backtest(_sample_candles(), config, allow_short=False)
        self.assertIn(result["status"], {"PASS", "FAIL", "UNKNOWN"})
        if result["status"] != "UNKNOWN":
            self.assertEqual(result["cost_assumption_bps_per_side"], 18.0)
            self.assertGreaterEqual(result["oos_bars"], config.thresholds.min_oos_bars)

    def test_three_state_safety_layout(self):
        allowed = {SafetyStatus.LOW_RISK, SafetyStatus.HIGH_RISK, SafetyStatus.UNKNOWN}
        self.assertEqual(len(allowed), 3)
        self.assertEqual({item.value for item in allowed}, {"LOW_RISK", "HIGH_RISK", "UNKNOWN"})

    def test_engine_returns_nine_gates_and_never_overrides_unknown(self):
        engine = ConfirmationEngine(data_service=_FakeDataService())
        result = engine.analyze(
            "cex:ETHUSDT",
            headlines=["Ethereum adoption and institutional integration upgrade"],
        )
        self.assertEqual(len(result.gates), 9)
        self.assertIn(result.signal, {Signal.LONG, Signal.NO_TRADE})
        if any(g.status != GateStatus.PASS for g in result.gates):
            self.assertEqual(result.signal, Signal.NO_TRADE)

    def test_master_dataframe_preserves_all_records(self):
        engine = ConfirmationEngine(data_service=_FakeDataService())
        a = engine.analyze("cex:ETHUSDT", headlines=["Ethereum adoption upgrade"])
        b = engine.analyze("cex:BTCUSDT", headlines=["Bitcoin adoption upgrade"])
        frame = results_dataframe([a, b])
        self.assertEqual(len(frame), 2)
        self.assertEqual(set(frame["asset"]), {"cex:ETHUSDT", "cex:BTCUSDT"})


def run_automated_tests() -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RadarTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(0 if run_automated_tests() else 1)
