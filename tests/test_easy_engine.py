from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from types import SimpleNamespace

from crypto_super_radar.easy_engine import EasyModeEngine
from crypto_super_radar.config import AppConfig
from crypto_super_radar.models import AssetSpec, Signal


class DummyMarketData:
    def __init__(self, frame, price):
        self.frame = frame
        self.snapshot_obj = SimpleNamespace(
            price_usd=price, price_discrepancy_pct=0.2, volume_discrepancy_pct=None,
            liquidity_usd=5_000_000, provider_a="provider_a", provider_b="provider_b",
        )
        self.candle_frames = {}
        self.snapshots = {}
        self.http = None
        self.whale_evidence = {}

    def snapshot(self, asset):
        self.snapshots[asset.raw] = self.snapshot_obj
        return self.snapshot_obj

    def candles(self, asset, snapshot):
        self.candle_frames[asset.raw] = self.frame.copy()
        return self.frame.copy()

    def security(self, asset):
        raise RuntimeError("optional whale/security feed unavailable")

    def fundamental_evidence(self, asset):
        return {}


class EasyEngineTests(unittest.TestCase):
    def candles(self, rising=True, count=260):
        index = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h"), periods=count, freq="D")
        x = np.arange(count, dtype=float)
        close = 100 + x * (0.25 if rising else -0.25) + np.sin(x / 4) * 0.2
        if not rising:
            close = np.maximum(close, 5)
        prior = np.r_[close[0], close[:-1]]
        open_ = prior * (1 - 0.0005 if rising else 1 + 0.0005)
        high = np.maximum(open_, close) * 1.005
        low = np.minimum(open_, close) * 0.995
        volume = np.full(count, 10000.0)
        volume[-1] = 12000.0
        return pd.DataFrame({"open":open_, "high":high, "low":low,
                             "close":close, "volume":volume}, index=index)

    def engine(self, rising=True, market_type="cex"):
        frame = self.candles(rising=rising)
        price = float(frame.close.iloc[-1])
        data = DummyMarketData(frame, price)
        engine = EasyModeEngine(
            AppConfig(), data_service=data,
            sentiment_fetcher=lambda *args, **kwargs: {"status":"UNKNOWN", "score":None},
        )
        return engine, AssetSpec.parse(f"{'perp' if market_type=='perp' else 'cex'}:{'BTCUSDT'}")

    @patch("crypto_super_radar.easy_engine.fetch_market_intelligence", return_value={"status":"UNKNOWN"})
    def test_easy_long_works_without_twelve_legacy_gates(self, _market_intel):
        engine, asset = self.engine(rising=True)
        result = engine.analyze(asset)
        self.assertEqual(result.metrics["engine_profile"], "EASY")
        self.assertEqual(result.signal, Signal.LONG)
        self.assertEqual(result.gates, [])
        self.assertEqual(result.metrics["decision_policy"]["mandatory_legacy_gates"], 0)
        self.assertFalse(result.metrics["decision_policy"]["twelve_gate_engine_used"])
        self.assertEqual(result.metrics["easy_position_sizing"]["status"], "KNOWN")
        self.assertEqual(result.metrics["whale_status"], "UNKNOWN")

    @patch("crypto_super_radar.easy_engine.fetch_market_intelligence", return_value={"status":"UNKNOWN"})
    def test_easy_spot_does_not_issue_short_signal(self, _market_intel):
        engine, asset = self.engine(rising=False, market_type="cex")
        result = engine.analyze(asset)
        self.assertNotEqual(result.signal, Signal.SHORT)
        self.assertEqual(result.gates, [])

    @patch("crypto_super_radar.easy_engine.fetch_market_intelligence", return_value={"status":"UNKNOWN"})
    def test_easy_perpetual_can_assess_short(self, _market_intel):
        engine, asset = self.engine(rising=False, market_type="perp")
        result = engine.analyze(asset)
        self.assertEqual(result.signal, Signal.SHORT)

    def test_missing_snapshot_price_fails_closed(self):
        frame = self.candles()
        data = DummyMarketData(frame, None)
        engine = EasyModeEngine(data_service=data, sentiment_fetcher=lambda *a, **k: {"status":"UNKNOWN"})
        result = engine.analyze("cex:BTCUSDT")
        self.assertEqual(result.signal, Signal.NO_TRADE)
        self.assertEqual(result.metrics["engine_profile"], "EASY")

    @patch("crypto_super_radar.easy_engine.fetch_market_intelligence", return_value={"status":"UNKNOWN"})
    def test_missing_optional_feeds_do_not_turn_easy_into_twelve_gate_no_trade(self, _market_intel):
        engine, asset = self.engine(rising=True)
        result = engine.analyze(asset)
        self.assertEqual(result.signal, Signal.LONG)
        self.assertFalse(result.metrics["decision_policy"]["nonessential_diagnostics_are_hard_gates"])


if __name__ == "__main__":
    unittest.main()
