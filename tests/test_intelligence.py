import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

from crypto_super_radar.whale_intelligence import analyze_whale_activity, WhaleEvent, extract_goplus_whale_watchlist, build_wallet_label_map
from crypto_super_radar.dashboard_data import build_dashboard_frames
from crypto_super_radar.sentiment import analyze_market_sentiment, detect_sentiment_divergence, score_text, fear_greed_to_sentiment, score_headlines
from crypto_super_radar.walk_forward import WalkForwardConfig, walk_forward_validate
from crypto_super_radar.evm_security import screen_evm_token, _tri, _pct
from crypto_super_radar.sentiment_feeds import fetch_fear_greed_index, fetch_rss_headlines
from crypto_super_radar.solana_whales import SolanaWhaleScanner
from crypto_super_radar.market_sentiment import fetch_market_sentiment
from crypto_super_radar.integrated_engine import WhaleAwareConfirmationEngine
from crypto_super_radar.cex_whales import CexWhaleScanner
from crypto_super_radar.providers import DataProviderError
from crypto_super_radar.whale_store import WhaleStore
from crypto_super_radar.models import AssetSpec, MarketSnapshot
from crypto_super_radar.config import AppConfig, IntelligenceScoringConfig


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

    def test_whale_labels_preserve_explicit_exchange_labels(self):
        address = "0x" + "a" * 40
        labels = build_wallet_label_map(
            [{"address": address}, {"address": "not-valid"}],
            known_labels={address: "exchange"},
        )
        self.assertEqual(labels, {address: "exchange"})

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

    def test_legacy_headline_api_remains_compatible(self):
        self.assertEqual(score_headlines([])["status"], "UNKNOWN")
        result = score_headlines(["Protocol suffers exploit and withdrawals paused"])
        self.assertEqual(result["status"], "KNOWN")
        self.assertLess(result["score"], 35)
        self.assertIn("hype_score", result)

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
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


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
                "is_blacklisted": "0", "cannot_buy": "0", "cannot_sell_all": "0",
                "slippage_modifiable": "0", "personal_slippage_modifiable": "0",
                "anti_whale_modifiable": "0", "trading_cooldown": "0", "external_call": "0",
                "buy_tax": "0", "sell_tax": "0",
                "holders": [{"percent": "0.10"}, {"percent": "0.05"}],
            }},
        }
        session = FakeSession([FakeResponse(goplus), FakeResponse({"match": "exact_match"})])
        result = screen_evm_token("base", address, session=session, cache={}, limiter=NoWaitLimiter())
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

    def test_partial_news_feed_coverage_blocks_full_sentiment_coverage(self):
        rss_news = b"<rss><channel><item><title>Bitcoin adoption grows</title></item></channel></rss>"
        rss_social = b"<rss><channel><item><title>Community bullish breakout</title></item></channel></rss>"
        fng = {"data": [{"value": "70", "value_classification": "Greed", "timestamp": "1800000000"}]}
        result = fetch_market_sentiment(
            ["https://news-a.example/rss", "https://news-b.example/rss"],
            social_feed_urls=["https://social.example/rss"],
            session=FakeSession([
                FakeResponse({}, content=rss_news), FakeResponse({}, status_code=503),
                FakeResponse({}, content=rss_social), FakeResponse(fng),
            ]),
            cache={}, limiter=NoWaitLimiter(),
        )
        self.assertEqual(result["news"]["status"], "PARTIAL")
        self.assertEqual(result["news_coverage"], "PARTIAL")
        self.assertEqual(result["status"], "PARTIAL")

    def test_whitespace_social_text_does_not_count_as_coverage(self):
        rss_news = b"<rss><channel><item><title>Bitcoin adoption grows</title></item></channel></rss>"
        fng = {"data": [{"value": "70", "value_classification": "Greed", "timestamp": "1800000000"}]}
        result = fetch_market_sentiment(
            ["https://news.example/rss"], social_text=[" ", "   "],
            session=FakeSession([FakeResponse({}, content=rss_news), FakeResponse(fng)]),
            cache={}, limiter=NoWaitLimiter(),
        )
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["social_coverage"], "UNKNOWN")
        self.assertEqual(result["social_text_count"], 0)


class SentimentFeedTests(unittest.TestCase):
    def test_rss_partial_when_one_configured_feed_fails(self):
        rss = b"<rss><channel><item><title>Bitcoin adoption grows</title></item></channel></rss>"
        result = fetch_rss_headlines(
            ["https://news-a.example/rss", "https://news-b.example/rss"],
            session=FakeSession([
                FakeResponse({}, content=rss), FakeResponse({}, status_code=503),
            ]),
            limiter=NoWaitLimiter(),
        )
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["coverage"], "PARTIAL")
        self.assertEqual(result["successful_feed_count"], 1)
        self.assertEqual(len(result["failed_feeds"]), 1)

    def test_empty_configured_feed_prevents_full_coverage(self):
        empty = b"<rss><channel></channel></rss>"
        populated = b"<rss><channel><item><title>Bitcoin adoption grows</title></item></channel></rss>"
        result = fetch_rss_headlines(
            ["https://news-a.example/rss", "https://news-b.example/rss"],
            session=FakeSession([
                FakeResponse({}, content=empty), FakeResponse({}, content=populated),
            ]),
            limiter=NoWaitLimiter(),
        )
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(result["empty_feeds"]), 1)

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


def sample_candles(stronger=True):
    t = np.arange(400, dtype=float)
    slope = 0.19 if stronger else 0.09
    if stronger:
        close = 100.0 + slope * t + 8.0 * np.sin(2.0 * np.pi * t / 55.0 + 1.0) + 0.5 * np.sin(2.0 * np.pi * t / 13.0 + 0.3)
    else:
        close = 100.0 + slope * t + 2.3 * np.sin(t / 5.0 + 0.3) + np.sin(t / 13.0 + 0.3)
    index = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("D"), periods=400, freq="D")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.012,
        "low": close * 0.988, "close": close,
        "volume": np.full(len(t), 1_000_000.0),
    }, index=index)


class FakeIntegratedDataService:
    def __init__(self, whale_status="KNOWN", security_status="PASS"):
        self.asset_candles = sample_candles(True)
        self.benchmark = sample_candles(False)
        self.candle_frames = {
            "cex:BTCUSDT": self.asset_candles.copy(),
            "perp:BTCUSDT": self.asset_candles.copy(),
        }
        self.funding_frames = {"perp:BTCUSDT": pd.Series(dtype=float)}
        self.security_status = security_status
        self.whale_evidence = {
            "cex:BTCUSDT": {
                "status": whale_status,
                "net_whale_trade_flow_usd": 250000 if whale_status == "KNOWN" else None,
            },
            "perp:BTCUSDT": {
                "status": whale_status,
                "net_whale_trade_flow_usd": 250000 if whale_status == "KNOWN" else None,
            },
        }
        self.http = SimpleNamespace(limiter=NoWaitLimiter())

    def snapshot(self, asset):
        return MarketSnapshot(
            asset=asset, price_usd=float(self.asset_candles["close"].iloc[-1]),
            volume_24h_usd=25_000_000.0, liquidity_usd=5_000_000.0,
            price_discrepancy_pct=0.2, volume_discrepancy_pct=None,
            provider_a="Mock-A", provider_b="Mock-B",
        )

    def candles(self, asset, snapshot):
        return self.asset_candles.copy()

    def benchmark_candles(self):
        return self.benchmark.copy()

    def security(self, asset):
        return {"status": self.security_status, "reason": f"Mock security {self.security_status}."}

    def funding_history(self, asset):
        return self.funding_frames.get(asset.raw, pd.Series(dtype=float)).copy()


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, *args, **kwargs):
        return self.responses.pop(0)


class WhaleStoreTests(unittest.TestCase):
    def test_records_local_whale_history(self):
        store = WhaleStore(":memory:")
        store.record("base:token", {
            "status": "KNOWN", "net_whale_transfer_usd": 250000,
            "coverage": "BLOCK_RANGE",
        }, "ACCUMULATION_PRESSURE")
        summary = store.summary("base:token")
        self.assertEqual(summary["observation_count"], 1)
        self.assertEqual(summary["recent_bias"], "ACCUMULATION_PRESSURE")
        store.close()


class CexWhaleTests(unittest.TestCase):
    def test_cross_venue_large_trade_flow(self):
        scanner = CexWhaleScanner(threshold_usd=100000)
        scanner.http = FakeHttp([
            [{"p": "1000", "q": "200", "m": False}],
            {"result": {"list": [{"price": "1000", "size": "200", "side": "Buy"}]}},
        ])
        result = scanner.scan("BTCUSDT", "cex")
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["net_whale_trade_flow_usd"], 200000)

    def test_cross_venue_disagreement_is_unknown(self):
        scanner = CexWhaleScanner(threshold_usd=100000)
        scanner.http = FakeHttp([
            [{"p": "1000", "q": "200", "m": False}],
            {"result": {"list": [{"price": "1000", "size": "200", "side": "Sell"}]}},
        ])
        result = scanner.scan("BTCUSDT", "cex")
        self.assertEqual(result["status"], "UNKNOWN")


    def test_regional_denials_use_free_gate_okx_whale_feeds(self):
        scanner = CexWhaleScanner(threshold_usd=100000)
        scanner.http = FakeHttp([
            DataProviderError("HTTP 451 from fapi.binance.com"),
            [{"price": "1000", "size": "200", "create_time": 123}],
            {"quanto_multiplier": "1"},
            DataProviderError("HTTP 403 from api.bybit.com"),
            {"code": "0", "data": [{"px": "1000", "sz": "200", "side": "buy"}]},
            {"code": "0", "data": [{"ctVal": "1"}]},
        ])
        result = scanner.scan("BTCUSDT", "perp")
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["sources"], ["Gate.io", "OKX"])
        self.assertEqual(result["net_whale_trade_flow_usd"], 200000)


class IntegratedEngineTests(unittest.TestCase):
    @staticmethod
    def sentiment_payload(status="KNOWN"):
        return {
            "status": status, "score": 35.0 if status == "KNOWN" else None,
            "label": "POSITIVE" if status == "KNOWN" else None,
            "news": {"status": "KNOWN"}, "social": {"status": "KNOWN"},
            "fear_greed": {"status": "KNOWN", "value": 68},
            "social_coverage": "KNOWN" if status == "KNOWN" else "UNKNOWN",
        }

    def test_scenario_a_all_twelve_mandatory_gates_pass(self):
        engine = WhaleAwareConfirmationEngine(
            data_service=FakeIntegratedDataService(),
            sentiment_fetcher=lambda *args, **kwargs: self.sentiment_payload("KNOWN"),
            whale_store=WhaleStore(":memory:"),
        )
        result = engine.analyze("cex:BTCUSDT", headlines=["Bitcoin adoption institutional growth"])
        self.assertEqual(len(result.gates), 12)
        self.assertEqual(
            [gate.status.value for gate in result.gates],
            ["PASS"] * 12,
            [(gate.gate_id, gate.name_en, gate.status.value, gate.reason_en, gate.evidence) for gate in result.gates],
        )
        self.assertIn("evidence_confidence_pct", result.metrics)
        self.assertEqual(result.metrics["evidence_confidence_pct"], 100.0)
        self.assertEqual(result.metrics["confidence_level_pct"], 100.0)
        self.assertEqual(result.metrics["risk_score"], result.estimated_risk_score)
        self.assertEqual(result.metrics["opportunity_rank"], result.opportunity_rank)

    def test_dashboard_dataframes_show_twelve_gates_and_quant_metrics(self):
        engine = WhaleAwareConfirmationEngine(
            data_service=FakeIntegratedDataService(),
            sentiment_fetcher=lambda *args, **kwargs: self.sentiment_payload("KNOWN"),
            whale_store=WhaleStore(":memory:"),
        )
        result = engine.analyze("cex:BTCUSDT", headlines=["Bitcoin adoption institutional growth"])
        frames = build_dashboard_frames(result)
        self.assertEqual(len(frames["gates"]), 12)
        self.assertTrue(frames["errors"].empty)
        self.assertIn("RSI-14", frames["quantitative"]["شاخص"].tolist())
        self.assertIn("تغییر RSI-14", frames["quantitative"]["شاخص"].tolist())
        self.assertIn("Risk Score", frames["whale_sentiment"]["شاخص"].tolist())
        self.assertIn("Confidence Level", frames["whale_sentiment"]["شاخص"].tolist())

    def test_dashboard_error_table_lists_failed_security_gate(self):
        engine = WhaleAwareConfirmationEngine(
            data_service=FakeIntegratedDataService(security_status="FAIL"),
            sentiment_fetcher=lambda *args, **kwargs: self.sentiment_payload("KNOWN"),
            whale_store=WhaleStore(":memory:"),
        )
        result = engine.analyze("cex:BTCUSDT", headlines=["Bitcoin adoption institutional growth"])
        errors = build_dashboard_frames(result)["errors"]
        self.assertIn(6, errors["شماره فیلتر"].tolist())
        self.assertIn("FAIL", errors["وضعیت"].tolist())

    def test_whale_divergence_reduces_confidence_without_failing_gate(self):
        data = FakeIntegratedDataService()
        data.whale_evidence["cex:BTCUSDT"]["net_whale_trade_flow_usd"] = -250000
        engine = WhaleAwareConfirmationEngine(
            data_service=data,
            sentiment_fetcher=lambda *args, **kwargs: self.sentiment_payload("KNOWN"),
            whale_store=WhaleStore(":memory:"),
        )
        result = engine.analyze("cex:BTCUSDT", headlines=["Bitcoin adoption institutional growth"])
        self.assertEqual(result.gates[9].status.value, "PASS")
        self.assertEqual(result.metrics["whale_alignment"], "OPPOSED")
        self.assertEqual(result.metrics["opportunity_whale_adjustment"], -6.0)
        self.assertEqual(result.metrics["opportunity_sentiment_adjustment"], 2.0)
        self.assertEqual(result.metrics["opportunity_total_adjustment"], -4.0)
        self.assertLess(
            result.metrics["confidence_level_pct"],
            result.metrics["evidence_confidence_pct"],
        )

    def test_opportunity_rank_uses_central_scoring_config_not_confirmation_score(self):
        scoring = IntelligenceScoringConfig(
            opportunity_whale_aligned=7.0,
            opportunity_sentiment_aligned=4.0,
        )
        engine = WhaleAwareConfirmationEngine(
            config=AppConfig(scoring=scoring),
            data_service=FakeIntegratedDataService(),
            sentiment_fetcher=lambda *args, **kwargs: self.sentiment_payload("KNOWN"),
            whale_store=WhaleStore(":memory:"),
        )
        original_analyze = engine.base.analyze

        def analyze_with_independent_rank(*args, **kwargs):
            result = original_analyze(*args, **kwargs)
            result.opportunity_rank = 12.34
            return result

        engine.base.analyze = analyze_with_independent_rank
        result = engine.analyze("cex:BTCUSDT", headlines=["Bitcoin adoption institutional growth"])
        self.assertEqual(result.opportunity_rank, 23.34)
        self.assertEqual(result.metrics["opportunity_total_adjustment"], 11.0)
        self.assertNotEqual(result.opportunity_rank, result.confirmation_score)

    def test_no_trade_from_low_confirmation_keeps_known_low_risk_status(self):
        engine = WhaleAwareConfirmationEngine(
            data_service=FakeIntegratedDataService(),
            sentiment_fetcher=lambda *args, **kwargs: self.sentiment_payload("KNOWN"),
            whale_store=WhaleStore(":memory:"),
        )
        original_analyze = engine.base.analyze

        def analyze_below_confirmation(*args, **kwargs):
            result = original_analyze(*args, **kwargs)
            result.signal = type(result.signal).LONG
            result.confirmation_score = 1.0
            return result

        engine.base.analyze = analyze_below_confirmation
        result = engine.analyze("cex:BTCUSDT", headlines=["Bitcoin adoption institutional growth"])
        self.assertEqual([gate.status.value for gate in result.gates], ["PASS"] * 12)
        self.assertEqual(result.signal.value, "NO TRADE")
        self.assertEqual(result.safety_status.value, "LOW_RISK")
        self.assertTrue(result.metrics["decision_blockers"]["confirmation_below_threshold"])
        self.assertIn("۱۲ فیلتر", result.summary_fa)

    def test_scenario_b_security_failure_blocks_signal(self):
        engine = WhaleAwareConfirmationEngine(
            data_service=FakeIntegratedDataService(security_status="FAIL"),
            sentiment_fetcher=lambda *args, **kwargs: self.sentiment_payload("KNOWN"),
            whale_store=WhaleStore(":memory:"),
        )
        result = engine.analyze("cex:BTCUSDT", headlines=["Bitcoin adoption institutional growth"])
        self.assertEqual(result.gates[5].status.value, "FAIL")
        self.assertEqual(result.signal.value, "NO TRADE")

    def test_scenario_c_missing_perpetual_funding_is_unknown(self):
        engine = WhaleAwareConfirmationEngine(
            data_service=FakeIntegratedDataService(),
            sentiment_fetcher=lambda *args, **kwargs: self.sentiment_payload("KNOWN"),
            whale_store=WhaleStore(":memory:"),
        )
        result = engine.analyze("perp:BTCUSDT", headlines=["Bitcoin adoption institutional growth"])
        self.assertEqual(result.gates[11].status.value, "UNKNOWN")
        self.assertEqual(result.signal.value, "NO TRADE")

    def test_partial_sentiment_coverage_blocks_any_trade(self):
        engine = WhaleAwareConfirmationEngine(
            data_service=FakeIntegratedDataService(),
            sentiment_fetcher=lambda *args, **kwargs: self.sentiment_payload("PARTIAL"),
            whale_store=WhaleStore(":memory:"),
        )
        result = engine.analyze("cex:BTCUSDT", headlines=["Bitcoin adoption institutional growth"])
        self.assertEqual(result.gates[10].status.value, "UNKNOWN")
        self.assertEqual(result.signal.value, "NO TRADE")


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
