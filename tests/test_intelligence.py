import unittest

import pandas as pd

from crypto_super_radar.whale_intelligence import analyze_whale_activity, WhaleEvent, extract_goplus_whale_watchlist
from crypto_super_radar.sentiment import analyze_market_sentiment, detect_sentiment_divergence, score_text, fear_greed_to_sentiment
from crypto_super_radar.walk_forward import WalkForwardConfig, walk_forward_validate
from crypto_super_radar.evm_security import screen_evm_token, _tri, _pct
from crypto_super_radar.sentiment_feeds import fetch_fear_greed_index, fetch_rss_headlines
from crypto_super_radar.solana_whales import SolanaWhaleScanner
from crypto_super_radar.market_sentiment import fetch_market_sentiment


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

    def test_labelled_whale_wallet_distribution_is_separate_from_exchange_flow(self):
        result = analyze_whale_activity([{
            "tx_hash": "whale-out", "timestamp": 2, "token_address": "token",
            "from_address": "0x" + "a" * 40, "to_address": "0x" + "b" * 40,
            "amount": 1, "amount_usd": 250000, "from_label": "whale",
        }])
        self.assertIsNone(result["net_exchange_flow_usd"])
        self.assertEqual(result["net_whale_transfer_usd"], -250000)

    def test_goplus_top_holder_whale_watchlist(self):
        result = extract_goplus_whale_watchlist({
            "total_supply": "1000000", "decimals": "0",
            "holders": [
                {"address": "0x" + "a" * 40, "percent": "0.4", "tag": "unknown"},
                {"address": "0x" + "b" * 40, "percent": "0.05"},
            ],
        }, token_price_usd=1.0, min_balance_usd=100000)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(result["watchlist"]), 1)
        self.assertEqual(result["watchlist"][0]["label"], "whale")

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

    def test_persian_sentiment_and_fear_greed(self):
        result = score_text("رشد و پذیرش نهادی")
        self.assertEqual(result["status"], "KNOWN")
        self.assertGreater(result["score"], 0)
        self.assertEqual(fear_greed_to_sentiment(80), 60.0)
        combined = analyze_market_sentiment(
            {"news": []}, numeric_scores={"fear_greed": fear_greed_to_sentiment(80)}
        )
        self.assertEqual(combined["status"], "KNOWN")


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


class FakeResponse:
    def __init__(self, payload, status_code=200, content=b""):
        self.payload = payload
        self.status_code = status_code
        self.content = content

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, *args, **kwargs):
        return self.responses.pop(0)


class NoWaitLimiter:
    def wait(self, url):
        return 0


class EvmTests(unittest.TestCase):
    def test_unknown_semantics(self):
        self.assertIsNone(_tri(None))
        self.assertIsNone(_pct(None))
        self.assertEqual(_tri("0"), False)
        self.assertEqual(_pct("0.01"), 1.0)

    def test_invalid_address_fails_closed(self):
        result = screen_evm_token("base", "not-an-address")
        self.assertEqual(result.status, "FAIL")

    def test_exact_sourcify_match_and_complete_goplus_data_pass(self):
        address = "0x" + "1" * 40
        goplus = {
            "code": 1,
            "result": {address: {
                "is_honeypot": "0", "is_open_source": "1", "is_proxy": "0",
                "is_mintable": "0", "owner_change_balance": "0",
                "can_take_back_ownership": "0", "hidden_owner": "0",
                "selfdestruct": "0", "transfer_pausable": "0",
                "is_blacklisted": "0", "cannot_buy": "0", "cannot_sell_all": "0",\n                "slippage_modifiable": "0", "personal_slippage_modifiable": "0",\n                "anti_whale_modifiable": "0", "trading_cooldown": "0", "external_call": "0",\n                "buy_tax": "0", "sell_tax": "0",
                "holders": [{"percent": "0.10"}, {"percent": "0.05"}],
            }},
        }
        session = FakeSession([FakeResponse(goplus), FakeResponse({"match": "exact_match"})])
        result = screen_evm_token("base", address, session=session, cache={})
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.top10_holder_pct, 15.0)

    def test_partial_sourcify_match_is_not_accepted(self):
        address = "0x" + "2" * 40
        goplus = {
            "code": 1,
            "result": {address: {
                "is_honeypot": "0", "is_open_source": "1", "is_proxy": "0",
                "is_mintable": "0", "owner_change_balance": "0",
                "can_take_back_ownership": "0", "hidden_owner": "0",
                "selfdestruct": "0", "transfer_pausable": "0",
                "is_blacklisted": "0", "buy_tax": "0", "sell_tax": "0",
                "holders": [{"percent": "0.10"}],
            }},
        }
        session = FakeSession([FakeResponse(goplus), FakeResponse({"match": "match"})])
        result = screen_evm_token("base", address, session=session, cache={})
        self.assertEqual(result.status, "FAIL")


from crypto_super_radar.intelligence_engine import combine_intelligence


class FakeRPCSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def post(self, *args, **kwargs):
        return FakeResponse({"result": self.payloads.pop(0)})


class SolanaWhaleTests(unittest.TestCase):
    def test_top_holder_owner_and_recent_transfer_sample(self):
        tx = {
            "meta": {
                "preTokenBalances": [{
                    "mint": "S" * 32, "owner": "WalletOwner",
                    "uiTokenAmount": {"uiAmount": 300000},
                }],
                "postTokenBalances": [{
                    "mint": "S" * 32, "owner": "Receiver",
                    "uiTokenAmount": {"uiAmount": 300000},
                }],
            }
        }
        rpc = FakeRPCSession([
            {"value": [{"address": "TokenAccount", "uiAmount": 200000}]},
            {"value": {"data": {"parsed": {"info": {"owner": "WalletOwner"}}}}},
            [{"signature": "signature-1", "blockTime": 1700000000, "err": None}],
            tx,
        ])
        scanner = SolanaWhaleScanner(session=rpc, limiter=NoWaitLimiter())
        result = scanner.scan_mint("S" * 32, price_usd=1.0)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["whale_watchlist"][0]["address"], "walletowner")
        self.assertIn("LARGE_ENTITY_TRANSFER", result["classification_counts"])


class MarketSentimentPipelineTests(unittest.TestCase):
    def test_missing_social_coverage_stays_partial(self):
        rss = b"<rss><channel><item><title>Bitcoin adoption grows</title></item></channel></rss>"
        fng = {"data": [{"value": "70", "value_classification": "Greed", "timestamp": "1800000000"}]}
        result = fetch_market_sentiment(
            ["https://news.example/rss"],
            session=FakeSession([FakeResponse({}, content=rss), FakeResponse(fng)]),
            cache={}, limiter=NoWaitLimiter(),
        )
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["social_coverage"], "UNKNOWN")

    def test_full_news_social_and_fear_greed_coverage(self):
        rss_news = b"<rss><channel><item><title>Bitcoin adoption grows</title></item></channel></rss>"
        rss_social = b"<rss><channel><item><title>Community bullish breakout</title></item></channel></rss>"
        fng = {"data": [{"value": "70", "value_classification": "Greed", "timestamp": "1800000000"}]}
        result = fetch_market_sentiment(
            ["https://news.example/rss"], social_feed_urls=["https://social.example/rss"],
            session=FakeSession([
                FakeResponse({}, content=rss_news), FakeResponse({}, content=rss_social), FakeResponse(fng)
            ]),
            cache={}, limiter=NoWaitLimiter(),
        )
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["social_coverage"], "KNOWN")


class SentimentFeedTests(unittest.TestCase):
    def test_fear_greed_provider(self):
        payload = {"data": [
            {"value": "72", "value_classification": "Greed", "timestamp": "1800000000"},
            {"value": "64", "value_classification": "Greed", "timestamp": "1799913600"},
        ]}
        result = fetch_fear_greed_index(
            session=FakeSession([FakeResponse(payload)]), cache={}, limiter=NoWaitLimiter()
        )
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["value"], 72.0)

    def test_rss_provider_extracts_headlines(self):
        xml = b"<rss><channel><item><title>Bitcoin adoption grows</title><description>Positive</description></item></channel></rss>"
        result = fetch_rss_headlines(
            ["https://example.test/rss"],
            session=FakeSession([FakeResponse({}, content=xml)]),
            limiter=NoWaitLimiter(),
        )
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["headlines"][0]["title"], "Bitcoin adoption grows")


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

    def test_wallet_whale_flow_can_confirm_without_exchange_labels(self):
        result = combine_intelligence(
            whale={"status": "KNOWN", "net_exchange_flow_usd": None,
                   "net_whale_transfer_usd": -200000},
            sentiment={"status": "KNOWN", "score": -40},
            market={"status": "PASS", "direction": "SHORT"},
            derivatives={"status": "PASS", "direction": "SHORT"},
            security_status="PASS", backtest_status="PASS",
        )
        self.assertEqual(result["signal"], "SHORT")
        self.assertEqual(result["whale_bias"], "DISTRIBUTION_PRESSURE")

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
