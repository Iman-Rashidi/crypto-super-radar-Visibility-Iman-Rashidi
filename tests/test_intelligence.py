import unittest

import pandas as pd

from crypto_super_radar.whale_intelligence import analyze_whale_activity, WhaleEvent
from crypto_super_radar.sentiment import analyze_market_sentiment, detect_sentiment_divergence
from crypto_super_radar.walk_forward import WalkForwardConfig, walk_forward_validate
from crypto_super_radar.evm_security import screen_evm_token, _tri, _pct


class WhaleTests(unittest.TestCase):
    def test_exchange_flow_not_called_buy_or_sell(self):
        rows = [
            {"tx_hash": "a", "timestamp": 1, "token_address": "0xt", "from_address": "0x1",
             "to_address": "0x2", "amount": 1, "amount_usd": 200000,
             "from_label": "wallet", "to_label": "exchange"},
            {"tx_hash": "b", "timestamp": 2, "token_address": "0xt", "from_address": "0x3",
             "to_address": "0x4", "amount": 1, "amount_usd": 150000,
             "from_label": "exchange", "to_label": "wallet"},
        ]
        result = analyze_whale_activity(rows)
        self.assertEqual(result["net_exchange_flow_usd"], 50000)
        self.assertIn("not proof of selling", result["interpretation"])

    def test_missing_usd_is_not_zero(self):
        result = analyze_whale_activity([{
            "tx_hash": "a", "timestamp": 1, "token_address": "t", "from_address": "a",
            "to_address": "b", "amount": 1, "amount_usd": None,
        }])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIsNone(result["net_exchange_flow_usd"])


class SentimentTests(unittest.TestCase):
    def test_sentiment_and_divergence(self):
        result = analyze_market_sentiment({"news": ["Institutional adoption approved"]})
        self.assertGreater(result["score"], 0)
        divergence = detect_sentiment_divergence(result["score"], 250000)
        self.assertEqual(divergence["divergence"], "BULLISH_SENTIMENT_WITH_EXCHANGE_INFLOW")

    def test_missing_source_unknown(self):
        self.assertEqual(analyze_market_sentiment({"social": []})["status"], "UNKNOWN")


class WalkForwardTests(unittest.TestCase):
    def test_multiple_chronological_folds(self):
        index = pd.date_range("2024-01-01", periods=400, freq="D", tz="UTC")
        returns = pd.Series([0.002 if i % 3 else -0.001 for i in range(400)], index=index)
        cfg = WalkForwardConfig(train_bars=100, test_bars=40, step_bars=40, min_folds=3)
        result = walk_forward_validate(returns, cfg)
        self.assertEqual(result["status"], "KNOWN")
        self.assertGreaterEqual(result["fold_count"], 3)
        for fold in result["folds"]:
            self.assertLess(fold["train_end"], fold["test_start"])

    def test_overlapping_test_folds_rejected(self):
        index = pd.date_range("2024-01-01", periods=300, freq="D", tz="UTC")
        returns = pd.Series(0.001, index=index)
        result = walk_forward_validate(
            returns, WalkForwardConfig(train_bars=100, test_bars=50, step_bars=25, min_folds=2)
        )
        self.assertEqual(result["status"], "FAIL")

    def test_missing_return_not_filled(self):
        index = pd.date_range("2024-01-01", periods=300, freq="D", tz="UTC")
        returns = pd.Series(0.001, index=index)
        returns.iloc[120] = float("nan")
        result = walk_forward_validate(
            returns, WalkForwardConfig(train_bars=100, test_bars=40, min_folds=2)
        )
        self.assertEqual(result["status"], "UNKNOWN")

    def test_perpetual_missing_funding_unknown(self):
        index = pd.date_range("2024-01-01", periods=300, freq="D", tz="UTC")
        returns = pd.Series(0.001, index=index)
        result = walk_forward_validate(
            returns, WalkForwardConfig(train_bars=100, test_bars=40, min_folds=3),
            perpetual=True, funding_returns=None,
        )
        self.assertEqual(result["status"], "UNKNOWN")


class EvmTests(unittest.TestCase):
    def test_unknown_semantics(self):
        self.assertIsNone(_tri(None))
        self.assertIsNone(_pct(None))
        self.assertEqual(_tri("0"), False)
        self.assertEqual(_pct("0.01"), 1.0)

    def test_invalid_address_fails_closed(self):
        result = screen_evm_token("base", "not-an-address")
        self.assertEqual(result.status, "FAIL")

from crypto_super_radar.intelligence_engine import combine_intelligence


class UnifiedEngineTests(unittest.TestCase):
    def test_unknown_whale_gate_blocks(self):
        result = combine_intelligence(
            whale={"status": "PARTIAL", "net_exchange_flow_usd": None},
            sentiment={"status": "KNOWN", "score": 30},
            market={"status": "PASS", "direction": "LONG"},
            derivatives={"status": "PASS", "direction": "LONG"},
            security_status="PASS", backtest_status="PASS",
        )
        self.assertEqual(result["signal"], "NO TRADE")
        self.assertIn("whale", result["blocked_by"])

    def test_all_gates_and_alignment_can_emit_paper_signal(self):
        result = combine_intelligence(
            whale={"status": "KNOWN", "net_exchange_flow_usd": -200000},
            sentiment={"status": "KNOWN", "score": 40},
            market={"status": "PASS", "direction": "LONG"},
            derivatives={"status": "PASS", "direction": "LONG"},
            security_status="PASS", backtest_status="PASS",
        )
        self.assertEqual(result["signal"], "LONG")
        self.assertEqual(result["execution"], "PAPER_RESEARCH_ONLY")


if __name__ == "__main__":
    unittest.main()
