"""Offline tests for Gem Hunter V3 research-only primitives."""
import sqlite3
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

from crypto_super_radar.dashboard_data import gem_hunter_v3_dataframe, airdrop_radar_projects, airdrop_radar_dataframe
from crypto_super_radar.gem_hunter_v3 import process_gem_hunter_v3_candidates
from crypto_super_radar.web_app import _rank_gem_hunter_candidates
from crypto_super_radar.providers import SolanaRpcProvider, DataProviderError
from crypto_super_radar.gem_hunter_v3 import (
    estimate_exit_slippage, score_candidate_v3, persist_candidate_lifecycle,
    notify_qualified_gem, TursoGemHunterStore, classify_gem_tier,
    analyze_pump_dump_phase, analyze_narrative_psychology,
)
from crypto_super_radar.gem_hunter_pool_trades import analyze_pool_trade_evidence


class FakeStore:
    def __init__(self):
        self.events = set()
        self.rows = []
    def upsert_candidate(self, candidate):
        self.rows.append(dict(candidate))
    def append_event(self, candidate_id, event_type, payload, event_key=None):
        if event_key and event_key in self.events:
            return False
        if event_key:
            self.events.add(event_key)
        return True
    def release_event(self, event_key):
        self.events.discard(event_key)



class GemHunterV3Tests(unittest.TestCase):

    def test_gem_hunter_web_ui_contains_filters_whales_pump_and_wave_controls(self):
        source = Path(__file__).parents[1].joinpath(
            "crypto_super_radar", "web_app.py"
        ).read_text(encoding="utf-8")
        for required in (
            "gem-hunter-filter-bar", "🐸 میم‌کوین‌ها", "💎 آلت‌کوین‌های جدید",
            "🐋 دارای ورود نهنگ", "pump_phase_fa", "pump_start_price_usd",
            "risk_invalidation_price_usd", "whale_wallets_1h",
            "narrative_psychology_fa", "📊 مشاهده کندل و ساختار موج الیوت",
            "technical_evidence", "EMA20", "RSI14",
            "isCompleteEvaluatedGemCandidate", "sortCompleteGemCandidates",
            "نامزدهای ارزیابی‌شده", "کندل زنده · DEX Screener",
            "کندل زنده · GeckoTerminal",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_pump_dump_phase_early_mid_exhaustion_and_invalidation(self):
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        base = {
            "token_name": "Pepe AI", "token_symbol": "PEPE", "dex_id": "raydium",
            "price_usd": 0.018, "price_change_1h_pct": 20,
            "buys_1h": 80, "sells_1h": 20,
            "pool_created_at_utc": "2026-11-23T10:00:00Z",
            "market_cap_usd": 8_000_000,
        }
        early = analyze_pump_dump_phase(base, now=now)
        self.assertEqual(early["category_fa"], "🐸 میم‌کوین پامپی (نوسان کوتاه‌مدت)")
        self.assertEqual(early["pump_phase_fa"], "🟢 فاز ۱: شروع پامپ (Early Ignition)")
        self.assertAlmostEqual(early["pump_start_price_usd"], 0.015)
        self.assertAlmostEqual(early["risk_invalidation_price_usd"], 0.0123)
        self.assertEqual(early["pool_age_hours"], 2.0)
        self.assertEqual(early["trade_authorization"], "NO_TRADE")

        middle = analyze_pump_dump_phase({**base, "price_change_1h_pct": 80}, now=now)
        self.assertEqual(middle["pump_phase_fa"], "🟡 فاز ۲: میانه موج صعودی")
        exhausted = analyze_pump_dump_phase({**base, "price_change_1h_pct": 5, "buys_1h": 10, "sells_1h": 30}, now=now)
        self.assertEqual(exhausted["pump_phase_fa"], "🔴 فاز ۳: سقف فرسودگی / خطر دامپ")
        stable = analyze_pump_dump_phase({"price_usd": 2, "price_change_1h_pct": 3}, now=now)
        self.assertEqual(stable["pump_phase_fa"], "⚪ فاز ۰: پیش از حرکت / تثبیت")
        self.assertIsNone(stable["risk_invalidation_price_usd"])

    def test_narrative_psychology_uses_metadata_and_hourly_buy_sell_share(self):
        ai = analyze_narrative_psychology({
            "token_name": "Open AI Agent", "token_symbol": "OAI",
            "dex_id": "raydium", "buys_1h": 90, "sells_1h": 10,
        })
        self.assertIn("AI", ai["narrative_psychology_fa"])
        self.assertIn("هجوم خریداران خرد", ai["narrative_psychology_fa"])
        self.assertEqual(ai["narrative_buyer_share_1h_pct"], 90.0)
        self.assertIn("NOT_VERIFIED_NEWS", ai["narrative_evidence_basis"])
        selling = analyze_narrative_psychology({
            "token_name": "Unknown", "token_symbol": "UNK",
            "buys_1h": 10, "sells_1h": 90,
        })
        self.assertIn("فشار فروش", selling["narrative_psychology_fa"])
        self.assertIsNone(analyze_narrative_psychology({})["narrative_buyer_share_1h_pct"])

    def test_candidate_processing_attaches_pump_and_narrative_fields(self):
        candidate = {
            "chain": "solana", "token_address": "MintAddress",
            "token_name": "Pepe", "token_symbol": "PEPE",
            "price_usd": 0.01, "price_change_1h_pct": 30,
            "buys_1h": 70, "sells_1h": 30,
            "pool_created_at_utc": "2026-11-23T10:00:00Z",
            "market_cap_usd": 5_000_000,
        }
        with patch("crypto_super_radar.gem_hunter_v3.storage_backend", return_value="LOCAL"):
            result = process_gem_hunter_v3_candidates([candidate], notify=False)[0]
        self.assertIn("pump_dump_analysis", result)
        self.assertIn("narrative_analysis", result)
        for field in (
            "category_fa", "pump_phase_fa", "pump_start_price_usd",
            "pump_window_summary_fa", "risk_invalidation_price_usd",
            "narrative_psychology_fa",
        ):
            self.assertIn(field, result)
            nested = result["narrative_analysis"] if field == "narrative_psychology_fa" else result["pump_dump_analysis"]
            self.assertEqual(result[field], nested.get(field))
        self.assertTrue(result["category_fa"])
        self.assertTrue(result["pump_phase_fa"])
        self.assertTrue(result["pump_window_summary_fa"])
        self.assertTrue(result["narrative_psychology_fa"])
        self.assertEqual(result["trade_authorization"], candidate.get("trade_authorization"))
        self.assertEqual(result["gem_hunter_v3_trade_authorization"], "NO_TRADE")


    def test_whale_tracker_exposes_wallet_flow_token_amount_and_explorer_links(self):
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        trades = [
            {"kind": "buy", "volume_in_usd": 5000, "block_timestamp": "2026-11-23T11:30:00Z",
             "tx_hash": "0xabc", "tx_from_address": "0x" + "a" * 40, "to_token_amount": "125000"},
            {"kind": "sell", "volume_in_usd": 1000, "block_timestamp": "2026-11-23T11:40:00Z",
             "tx_hash": "0xdef", "tx_from_address": "0x" + "b" * 40, "from_token_amount": "25000"},
        ]
        result = analyze_pool_trade_evidence(
            trades, chain="base", token_address="0x" + "1" * 40,
            pool_address="0x" + "2" * 40, liquidity_usd=50000, now=now,
        )
        self.assertEqual(result["whale_buy_volume_1h_usd"], 5000)
        self.assertEqual(result["whale_sell_volume_1h_usd"], 1000)
        self.assertEqual(result["whale_buy_sell_ratio_1h"], 5.0)
        self.assertIn("🟢", result["whale_net_action_fa"])
        self.assertEqual(result["whale_events"][0]["token_amount"], 125000.0)
        self.assertEqual(result["whale_events"][0]["explorer_tx_url"], "https://basescan.org/tx/0xabc")
        self.assertEqual(result["whale_events"][0]["explorer_wallet_url"], "https://basescan.org/address/" + "0x" + "a" * 40)
        self.assertEqual(result["whale_wallets_1h"][0]["wallet_address"], "0x" + "a" * 40)

    def test_exit_slippage_unknown_without_liquidity(self):
        self.assertEqual(estimate_exit_slippage(liquidity_usd=None)["status"], "UNKNOWN")

    def test_exit_slippage_blocks_thin_pool(self):
        result = estimate_exit_slippage(liquidity_usd=1000, order_usd=500)
        self.assertEqual(result["status"], "FAIL")

    def test_exit_slippage_passes_deep_pool(self):
        result = estimate_exit_slippage(liquidity_usd=500000, order_usd=500)
        self.assertEqual(result["status"], "PASS")
        self.assertLessEqual(result["estimated_slippage_bps"], 100)

    def test_three_scores_are_separate_and_research_only(self):
        result = score_candidate_v3({
            "pool_trade_evidence": {"buy_volume_usd": 8000, "sell_volume_usd": 2000, "coverage_status": "COMPLETE", "unique_buyers_count": 3, "unique_sellers_count": 1, "wallet_attribution_status": "COMPLETE_FOR_RETURNED_SAMPLE"},
            "technical_evidence": {"coverage_status": "COMPLETE", "filters": {"trend": "PASS", "momentum": "PASS"}},
            "security_evidence": {"status": "PASS"},
            "exit_slippage_evidence": {"status": "PASS", "estimated_slippage_bps": 20},
            "market_reconciliation": {"status": "PASS", "verification_level": "INDEPENDENT_CROSS_SOURCE", "verified_liquidity_usd": 100000, "verified_volume_24h_usd": 500000},
        })
        self.assertEqual(result["short_term_momentum_smart_money_score"], 83.75)
        self.assertEqual(result["short_term_momentum_score"], 83.75)
        self.assertEqual(result["smart_money_evidence_status"], "NOT_AVAILABLE")
        self.assertEqual(result["new_buyer_cohort_status"], "NOT_AVAILABLE")
        self.assertEqual(result["wallet_breadth_evidence_status"], "VERIFIED_FOR_RETURNED_SAMPLE")
        self.assertEqual(result["candidate_status"], "QUALIFIED_GEM")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_verified_gem_does_not_require_wallet_breadth_when_core_evidence_passes(self):
        result = score_candidate_v3({
            "pool_trade_evidence": {"buy_volume_usd": 9000, "sell_volume_usd": 1000, "coverage_status": "COMPLETE"},
            "technical_evidence": {"coverage_status": "COMPLETE", "filters": {"trend": "PASS", "momentum": "PASS"}},
            "security_evidence": {"status": "PASS"},
            "exit_slippage_evidence": {"status": "PASS", "estimated_slippage_bps": 10},
            "market_reconciliation": {"status": "PASS", "verification_level": "INDEPENDENT_CROSS_SOURCE", "verified_liquidity_usd": 100000, "verified_volume_24h_usd": 500000},
        })
        self.assertEqual(result["candidate_status"], "QUALIFIED_GEM")
        self.assertEqual(result["wallet_breadth_evidence_status"], "UNKNOWN")

    def test_catema_style_high_volume_early_alpha_is_visible_as_tier2(self):
        candidate = {
            "chain": "solana", "liquidity_usd": 55_000, "volume_24h_usd": 435_000,
            "volume_to_liquidity_24h": 7.91, "buy_volume_share_pct": 62,
            "buys_24h": 620, "sells_24h": 380,
            "security_evidence": {"status": "UNKNOWN", "mint_authority_active": False,
                                  "freeze_authority_active": False, "honeypot_detected": False,
                                  "sell_tax_pct": 0, "transfer_simulation_status": "UNKNOWN"},
            "market_reconciliation": {"status": "UNKNOWN"},
            "exit_slippage_evidence": {"status": "UNKNOWN"},
        }
        tier = classify_gem_tier(candidate, {
            "short_term_momentum_score": 62,
            "momentum_evidence_coverage_pct": 50,
            "momentum_score_components": {"pool_buy_volume_share": 62},
            "smart_money_evidence_status": "NOT_AVAILABLE",
        })
        self.assertEqual(tier["tier"], "TIER_2_EARLY_ALPHA")
        self.assertEqual(tier["tier_base_security_status"], "PASS")
        self.assertTrue(tier["tier_cautions_fa"])
        self.assertEqual(tier["tier_data_confidence"], "LIMITED")

    def test_honeypot_is_blocked_but_missing_secondary_evidence_is_not_scam(self):
        scam = classify_gem_tier({
            "security_evidence": {"status": "FAIL", "honeypot_detected": True},
        }, {})
        unknown = classify_gem_tier({
            "liquidity_usd": 55_000, "volume_24h_usd": 435_000,
            "security_evidence": {"status": "UNKNOWN"},
        }, {})
        self.assertEqual(scam["tier"], "BLOCKED_SCAM")
        self.assertEqual(unknown["tier"], "REVIEW_REQUIRED")

    def test_low_liquidity_is_blocked_without_calling_it_scam(self):
        tier = classify_gem_tier({
            "liquidity_usd": 1000, "volume_24h_usd": 50000,
            "security_evidence": {"status": "PASS"},
        }, {})
        self.assertEqual(tier["tier"], "BLOCKED_LOW_LIQUIDITY")

    def test_tier1_requires_full_market_security_and_exit_evidence(self):
        candidate = {
            "liquidity_usd": 100000, "volume_24h_usd": 500000,
            "volume_to_liquidity_24h": 5, "buy_volume_share_pct": 70,
            "security_evidence": {"status": "PASS"},
            "market_reconciliation": {"status": "PASS", "verification_level": "INDEPENDENT_CROSS_SOURCE", "verified_liquidity_usd": 100000,
                                      "verified_volume_24h_usd": 500000},
            "exit_slippage_evidence": {"status": "PASS"},
        }
        tier = classify_gem_tier(candidate, {
            "short_term_momentum_score": 80, "momentum_evidence_coverage_pct": 80,
            "momentum_score_components": {"pool_buy_volume_share": 70},
            "safety_exit_liquidity_score": 90,
        })
        self.assertEqual(tier["tier"], "TIER_1_VERIFIED_GEM")

    def test_dex_single_source_is_not_mislabeled_as_independently_verified_gem(self):
        result = score_candidate_v3({
            "pool_trade_evidence": {"buy_volume_usd": 9000, "sell_volume_usd": 1000, "coverage_status": "COMPLETE"},
            "technical_evidence": {"coverage_status": "COMPLETE", "filters": {"trend": "PASS", "momentum": "PASS"}},
            "security_evidence": {"status": "PASS"},
            "exit_slippage_evidence": {"status": "PASS", "estimated_slippage_bps": 10},
            "market_reconciliation": {
                "status": "PASS", "verification_level": "DEXSCREENER_PRIMARY_SINGLE_SOURCE",
                "verified_liquidity_usd": 150000, "verified_volume_24h_usd": 350000,
            },
        })
        self.assertEqual(result["candidate_status"], "RESEARCH_WATCH")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_single_source_dex_market_cannot_enter_tier1_and_tier2_explains_limit(self):
        candidate = {
            "chain": "base", "liquidity_usd": 150000, "volume_24h_usd": 350000,
            "volume_to_liquidity_24h": 4.0, "buy_volume_share_pct": 70,
            "security_evidence": {"status": "PASS"},
            "market_reconciliation": {
                "status": "PASS", "verification_level": "DEXSCREENER_PRIMARY_SINGLE_SOURCE",
                "verified_liquidity_usd": 150000, "verified_volume_24h_usd": 350000,
            },
            "exit_slippage_evidence": {"status": "PASS"},
        }
        tier = classify_gem_tier(candidate, {
            "short_term_momentum_score": 80, "momentum_evidence_coverage_pct": 80,
            "momentum_score_components": {"pool_buy_volume_share": 70},
            "safety_exit_liquidity_score": 90,
        })
        self.assertNotEqual(tier["tier"], "TIER_1_VERIFIED_GEM")
        self.assertEqual(tier["tier"], "TIER_2_EARLY_ALPHA")
        self.assertTrue(any("منبع مستقل دوم" in item for item in tier["tier_cautions_fa"]))

    def test_mobile_six_metrics_are_enriched_and_kept_research_only(self):
        candidate = {
            "chain": "solana", "token_address": "Mint111", "price_usd": 0.01,
            "liquidity_usd": 100000, "volume_24h_usd": 600000,
            "buys_24h": 70, "sells_24h": 30,
            "security_evidence": {"status": "UNKNOWN", "largest_accounts_count": 20},
            "pool_trade_evidence": {
                "buy_volume_usd": 7000, "sell_volume_usd": 3000,
                "coverage_status": "COMPLETE", "unique_trader_count": 41,
            },
            "exit_slippage_evidence": {"status": "PASS", "estimated_slippage_bps": 39.62},
        }
        with patch("crypto_super_radar.gem_hunter_v3.storage_backend", return_value="DISABLED"):
            result = process_gem_hunter_v3_candidates([candidate], notify=False)[0]
        self.assertEqual(result["buy_volume_share_pct"], 70.0)
        self.assertEqual(result["unique_traders_24h"], 41)
        self.assertEqual(result["unique_traders_24h_basis"], "POOL_PROVIDER_UNIQUE_TRADERS")
        self.assertEqual(result["holder_count"], 20)
        self.assertEqual(result["estimated_slippage_bps"], 39.62)
        self.assertEqual(result["opportunity_score"], result["short_term_momentum_score"])
        self.assertGreaterEqual(result["risk_score"], 0)
        self.assertLessEqual(result["risk_score"], 100)
        self.assertEqual(result["gem_hunter_v3_trade_authorization"], "NO_TRADE")

    def test_mobile_metrics_fallback_labels_transaction_proxy(self):
        candidate = {
            "chain": "base", "token_address": "0x" + "2" * 40,
            "buys_24h": 8, "sells_24h": 2,
            "security_evidence": {"status": "UNKNOWN"},
            "exit_slippage_evidence": {"status": "UNKNOWN"},
        }
        with patch("crypto_super_radar.gem_hunter_v3.storage_backend", return_value="DISABLED"):
            result = process_gem_hunter_v3_candidates([candidate], notify=False)[0]
        self.assertEqual(result["buy_volume_share_pct"], 80.0)
        self.assertEqual(result["unique_traders_24h"], 10)
        self.assertEqual(result["unique_traders_24h_basis"], "TRANSACTION_COUNT_PROXY_NOT_UNIQUE_WALLETS")
        self.assertIsNone(result["holder_count"])
        self.assertEqual(result["opportunity_score"], 80.0)
        self.assertEqual(result["gem_hunter_v3_trade_authorization"], "NO_TRADE")

    def test_mobile_metrics_fall_back_to_existing_qualification_market_metrics(self):
        candidate = {
            "chain": "base", "token_address": "0x" + "3" * 40,
            "qualification": {
                "schema_version": "gem-hunter-v2",
                "market_metrics": {
                    "holder_count": 1420, "unique_traders_24h": 480,
                    "buy_volume_share_pct": 64, "estimated_slippage_bps": 42,
                },
            },
            "security_evidence": {"status": "PASS"},
        }
        with patch("crypto_super_radar.gem_hunter_v3.storage_backend", return_value="DISABLED"):
            result = process_gem_hunter_v3_candidates([candidate], notify=False)[0]
        self.assertEqual(result["holder_count"], 1420)
        self.assertEqual(result["unique_traders_24h"], 480)
        self.assertEqual(result["unique_traders_24h_basis"], "QUALIFICATION_MARKET_METRICS_REPORTED")
        self.assertEqual(result["buy_volume_share_pct"], 64)
        self.assertEqual(result["estimated_slippage_bps"], 42)
        self.assertEqual(result["qualification"]["schema_version"], "gem-hunter-v2")
        self.assertEqual(result["qualification"]["market_metrics"]["holder_count"], 1420)

    def test_gem_hunter_dataframe_maps_six_metrics(self):
        frame = gem_hunter_v3_dataframe([{
            "token_name": "Test", "holder_count": 20, "unique_traders_24h": 41,
            "unique_traders_24h_basis": "POOL_PROVIDER_UNIQUE_TRADERS",
            "buy_volume_share_pct": 70, "estimated_slippage_bps": 39.62,
            "opportunity_score": 79.5, "risk_score": 32.0,
            "exit_slippage_evidence": {"estimated_slippage_bps": 39.62},
        }])
        row = frame.iloc[0]
        self.assertEqual(row["تعداد هولدرها / حساب‌های بزرگ مشاهده‌شده"], 20)
        self.assertEqual(row["معامله‌گران یکتا / برآورد تراکنش"], 41)
        self.assertEqual(row["سهم حجمی خرید (%)"], 70)
        self.assertEqual(row["لغزش خروج (bps)"], 39.62)
        self.assertEqual(row["امتیاز فرصت (از ۱۰۰)"], 79.5)
        self.assertEqual(row["امتیاز ریسک (از ۱۰۰)"], 32.0)

    def test_dashboard_auto_loads_latest_turso_report_on_page_entry(self):
        source = open("crypto_super_radar/web_app.py", encoding="utf-8").read()
        self.assertIn("loadLatestStoredGemHunterReport();", source)
        self.assertIn("fetchGemHunterReport()", source)
        self.assertIn("/api/gem-hunter/v3/latest", source)

    def test_three_scores_do_not_invent_missing_evidence(self):
        result = score_candidate_v3({})
        self.assertIsNone(result["short_term_momentum_smart_money_score"])
        self.assertIsNone(result["safety_exit_liquidity_score"])
        self.assertIsNone(result["data_confidence_score"])
        self.assertEqual(result["candidate_status"], "RESEARCH_WATCH")

    def test_lifecycle_persistence_records_candidate_and_events(self):
        store = FakeStore()
        candidate = {"chain": "ethereum", "token_address": "0x" + "1" * 40, "price_usd": 0.01,
                     "liquidity_usd": 120000, "candidate_status": "DISCOVERED"}
        candidate_id = persist_candidate_lifecycle(store, candidate)
        self.assertEqual(candidate_id, "ethereum:0x" + "1" * 40)
        self.assertEqual(len(store.rows), 1)

    def test_telegram_deduplicates_qualified_gems(self):
        store = FakeStore()
        candidate = {
            "chain": "base", "token_address": "0x" + "1" * 40,
            "candidate_status": "UNSCREENED", "trade_authorization": "NO_TRADE",
            "gem_hunter_v3_status": "TIER_1_VERIFIED_GEM", "gem_hunter_v3_trade_authorization": "NO_TRADE",
            "security_evidence": {"status": "PASS"},
            "exit_slippage_evidence": {"status": "PASS"},
            "market_reconciliation": {"status": "PASS"},
        }
        client = Mock()
        client.post.return_value.json.return_value = {"ok": True, "result": {"message_id": 7}}
        client.post.return_value.raise_for_status.return_value = None
        first = notify_qualified_gem(candidate, store, session=client, bot_token="token", chat_id="123")
        second = notify_qualified_gem(candidate, store, session=client, bot_token="token", chat_id="123")
        self.assertEqual(first["status"], "SENT")
        self.assertEqual(second["status"], "DUPLICATE_SUPPRESSED")
        self.assertEqual(client.post.call_count, 1)

    def test_tier2_telegram_alert_can_send_with_exit_and_market_pending(self):
        store = FakeStore()
        candidate = {
            "chain": "base", "token_address": "0x" + "2" * 40,
            "candidate_status": "UNSCREENED", "trade_authorization": "NO_TRADE",
            "gem_hunter_v3_status": "TIER_2_EARLY_ALPHA",
            "gem_hunter_v3_trade_authorization": "NO_TRADE",
            "security_evidence": {"status": "UNKNOWN"},
            "tier_base_security_status": "PASS",
            "exit_slippage_evidence": {"status": "UNKNOWN"},
            "market_reconciliation": {"status": "UNKNOWN"},
            "tier_strengths_fa": ["حجم و فشار خرید اولیه"],
            "tier_cautions_fa": ["تطبیق بازار و لغزش خروج در انتظار است."],
        }
        client = Mock()
        client.post.return_value.json.return_value = {"ok": True, "result": {"message_id": 9}}
        client.post.return_value.raise_for_status.return_value = None
        result = notify_qualified_gem(candidate, store, session=client, bot_token="t", chat_id="c")
        self.assertEqual(result["status"], "SENT")
        self.assertIn("TIER_2_EARLY_ALPHA", next(iter(store.events)))

    def test_telegram_failed_delivery_releases_deduplication_reservation(self):
        store = FakeStore()
        candidate = {
            "chain": "base", "token_address": "0x" + "1" * 40,
            "candidate_status": "UNSCREENED", "trade_authorization": "NO_TRADE",
            "gem_hunter_v3_status": "TIER_1_VERIFIED_GEM", "gem_hunter_v3_trade_authorization": "NO_TRADE",
            "security_evidence": {"status": "PASS"},
            "exit_slippage_evidence": {"status": "PASS"},
            "market_reconciliation": {"status": "PASS"},
        }
        failed_client = Mock()
        failed_client.post.side_effect = RuntimeError("temporary Telegram failure")
        with self.assertRaisesRegex(RuntimeError, "temporary Telegram failure"):
            notify_qualified_gem(candidate, store, session=failed_client, bot_token="t", chat_id="c")
        self.assertEqual(len(store.events), 0)
        successful_client = Mock()
        successful_client.post.return_value.json.return_value = {"ok": True, "result": {"message_id": 8}}
        successful_client.post.return_value.raise_for_status.return_value = None
        result = notify_qualified_gem(candidate, store, session=successful_client, bot_token="t", chat_id="c")
        self.assertEqual(result["status"], "SENT")

    def test_v3_permission_is_separate_from_legacy_candidate_authorization(self):
        from unittest.mock import patch
        from crypto_super_radar.gem_hunter_v3 import process_gem_hunter_v3_candidates
        candidate = {
            "chain": "base", "token_address": "0x" + "1" * 40,
            "candidate_status": "BUY", "trade_authorization": "BUY",
            "market_reconciliation": {"status": "PASS", "verified_liquidity_usd": 100000},
            "security_evidence": {"status": "UNKNOWN"},
        }
        with patch.dict("os.environ", {}, clear=True):
            result = process_gem_hunter_v3_candidates([candidate], notify=False)[0]
        self.assertEqual(result["trade_authorization"], "BUY")
        self.assertEqual(result["gem_hunter_v3_trade_authorization"], "NO_TRADE")

    def test_telegram_requires_all_critical_gates(self):
        store = FakeStore()
        candidate = {"candidate_status": "UNSCREENED", "trade_authorization": "NO_TRADE",
                     "gem_hunter_v3_status": "TIER_1_VERIFIED_GEM",
                     "gem_hunter_v3_trade_authorization": "NO_TRADE",
                     "security_evidence": {"status": "PASS"},
                     "exit_slippage_evidence": {"status": "UNKNOWN"},
                     "market_reconciliation": {"status": "PASS"}}
        result = notify_qualified_gem(candidate, store, session=Mock(), bot_token="t", chat_id="c")
        self.assertEqual(result["status"], "BLOCKED")

    def test_turso_requires_cloud_backend(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                TursoGemHunterStore()

    def test_turso_schema_and_candidate_upsert_create_persistent_tables(self):
        connection = sqlite3.connect(":memory:")
        store = TursoGemHunterStore(connection=connection)
        store.initialize()
        store.upsert_candidate({
            "chain": "base", "token_address": "0x" + "1" * 40,
            "price_usd": 0.01, "liquidity_usd": 90000, "candidate_status": "DISCOVERED",
        })
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("gem_hunter_candidates", tables)
        self.assertIn("gem_hunter_lifecycle_events", tables)
        stored = connection.execute(
            "SELECT first_price_usd, latest_price_usd, latest_liquidity_usd FROM gem_hunter_candidates"
        ).fetchone()
        self.assertEqual(stored, (0.01, 0.01, 90000.0))
        store.close()

    def test_turso_event_deduplication_uses_unique_event_key(self):
        connection = sqlite3.connect(":memory:")
        store = TursoGemHunterStore(connection=connection)
        store.initialize()
        event = {"candidate_id": "base:0x1"}
        self.assertTrue(store.append_event("base:0x1", "ALERT", event, event_key="once"))
        self.assertFalse(store.append_event("base:0x1", "ALERT", event, event_key="once"))
        count = connection.execute(
            "SELECT COUNT(*) FROM gem_hunter_lifecycle_events WHERE event_key='once'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        store.close()

    def test_dashboard_exposes_momentum_evidence_gaps_and_telegram_status(self):
        frame = gem_hunter_v3_dataframe([{
            "chain": "ethereum", "token_address": "0x1",
            "short_term_momentum_score": 72, "momentum_evidence_coverage_pct": 75,
            "gem_hunter_v3_status": "TIER_2_EARLY_ALPHA",
            "tier_label_fa": "فرصت اولیه: واچ‌لیست داغ",
            "tier_strengths_fa": ["حجم بالا"], "tier_cautions_fa": ["تطبیق بازار در انتظار است."],
            "tier_data_confidence": "LIMITED", "tier_base_security_status": "PASS",
            "wallet_breadth_evidence_status": "VERIFIED_FOR_RETURNED_SAMPLE",
            "smart_money_evidence_status": "NOT_AVAILABLE",
            "new_buyer_cohort_status": "NOT_AVAILABLE",
            "telegram_alert_status": {"status": "NOT_SENT"},
            "trade_authorization": "BUY",
        }])
        self.assertEqual(frame.iloc[0]["امتیاز مومنتوم"], 72)
        self.assertEqual(frame.iloc[0]["طبقهٔ کشف"], "فرصت اولیه: واچ‌لیست داغ")
        self.assertEqual(frame.iloc[0]["نقاط قوت"], "حجم بالا")
        self.assertEqual(frame.iloc[0]["نکات احتیاط"], "تطبیق بازار در انتظار است.")
        self.assertEqual(frame.iloc[0]["امنیت پایه"], "PASS")
        self.assertEqual(frame.iloc[0]["پوشش شواهد مومنتوم (%)"], 75)
        self.assertEqual(frame.iloc[0]["انتساب پول هوشمند"], "NOT_AVAILABLE")
        self.assertEqual(frame.iloc[0]["وضعیت هشدار تلگرام"], "NOT_SENT")
        self.assertEqual(frame.iloc[0]["مجوز معامله"], "NO_TRADE")


    def test_low_price_high_liquidity_token_enters_tier2_without_candles_or_second_source(self):
        candidate = {
            "chain": "solana", "price_usd": 0.01, "liquidity_usd": 30_000,
            "volume_24h_usd": 120_000,
            "security_evidence": {
                "status": "UNKNOWN", "mint_authority_active": False,
                "freeze_authority_active": False, "honeypot_detected": False,
                "sell_tax_pct": 0,
            },
            "market_reconciliation": {"status": "UNKNOWN"},
            "exit_slippage_evidence": {"status": "UNKNOWN"},
        }
        tier = classify_gem_tier(candidate, {})
        self.assertEqual(tier["tier"], "TIER_2_EARLY_ALPHA")
        self.assertEqual(tier["estimated_tokens_for_100_usd"], 10_000)
        self.assertEqual(tier["gem_hunter_v3_trade_authorization"], "NO_TRADE")

    def test_low_price_candidate_with_unknown_security_is_review_not_tier2(self):
        tier = classify_gem_tier({
            "chain": "base", "price_usd": 0.02, "liquidity_usd": 40_000,
            "volume_24h_usd": 150_000, "security_evidence": {"status": "UNKNOWN"},
        }, {})
        self.assertEqual(tier["tier"], "EARLY_ALPHA_REVIEW")
        self.assertEqual(tier["tier_base_security_status"], "UNKNOWN")
        self.assertEqual(tier["gem_hunter_v3_trade_authorization"], "NO_TRADE")

    def test_cheap_token_honeypot_still_blocked(self):
        tier = classify_gem_tier({
            "chain": "solana", "price_usd": 0.001, "liquidity_usd": 50_000,
            "volume_24h_usd": 500_000,
            "security_evidence": {"honeypot_detected": True, "sell_tax_pct": 0},
        }, {})
        self.assertEqual(tier["tier"], "BLOCKED_SCAM")

    def test_evaluation_quota_is_four_solana_three_base_three_ethereum(self):
        candidates = []
        for chain, count in (("solana", 8), ("base", 6), ("ethereum", 6)):
            for i in range(count):
                candidates.append({"chain": chain, "token_address": f"{chain}-{i}",
                                   "volume_24h_usd": 1000 - i, "liquidity_usd": 500})
        selected, deferred = _rank_gem_hunter_candidates(candidates)
        counts = {chain: sum(row["chain"] == chain for row in selected)
                  for chain in ("solana", "base", "ethereum")}
        self.assertEqual(counts, {"solana": 4, "base": 3, "ethereum": 3})
        self.assertEqual(len(selected), 10)
        self.assertEqual(len(deferred), 10)

    def test_airdrop_radar_is_official_linked_and_research_only(self):
        projects = airdrop_radar_projects()
        frame = airdrop_radar_dataframe()
        self.assertGreaterEqual(len(projects), 4)
        self.assertTrue(all(item["official_url"].startswith("https://") for item in projects))
        self.assertTrue(all(item["trade_authorization"] == "NO_TRADE" for item in projects))
        self.assertIn("Seed Phrase", "Seed Phrase")  # dashboard warning is explicit in the rendered card
        self.assertEqual(len(frame), len(projects))

    def test_dashboard_status_never_authorizes_trading(self):
        result = score_candidate_v3({"security_evidence": {"status": "PASS"},
                                     "exit_slippage_evidence": {"status": "PASS"},
                                     "market_reconciliation": {"status": "PASS"},
                                     "pool_trade_evidence": {"buy_volume_usd": 90, "sell_volume_usd": 10},
                                     "technical_evidence": {"filters": {"trend": "PASS", "momentum": "PASS"}}})
        self.assertEqual(result["trade_authorization"], "NO_TRADE")


    def test_solana_rpc_fallback_retries_full_snapshot_after_each_primary_method_failure(self):
        primary = SolanaRpcProvider.URL
        fallback = "https://solana-rpc.publicnode.com"

        class FakeHttp:
            def __init__(self, fail_method):
                self.fail_method = fail_method
                self.calls = []

            def post(self, url, payload, timeout=None, max_retries=None):
                method = payload["method"]
                self.calls.append((url, method))
                if url == primary and method == self.fail_method:
                    raise DataProviderError("simulated primary RPC timeout")
                if method == "getAccountInfo":
                    value = {"data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": None}}}}
                elif method == "getTokenSupply":
                    value = {"uiAmountString": "1000"}
                elif method == "getTokenLargestAccounts":
                    value = [{"uiAmountString": "100"}]
                else:
                    raise AssertionError(method)
                return {"jsonrpc": "2.0", "id": 1, "result": {"value": value}}

        for failed_method in ("getAccountInfo", "getTokenSupply", "getTokenLargestAccounts"):
            with self.subTest(failed_method=failed_method):
                http = FakeHttp(failed_method)
                result = SolanaRpcProvider(http).token_security("mint-address")
                self.assertEqual(result["rpc_endpoint"], fallback)
                self.assertFalse(result["mint_authority_active"])
                self.assertFalse(result["freeze_authority_active"])
                fallback_methods = [method for url, method in http.calls if url == fallback]
                self.assertEqual(fallback_methods, [
                    "getAccountInfo", "getTokenSupply", "getTokenLargestAccounts"
                ])

    def test_early_alpha_review_telegram_alert_has_full_persian_fields_and_six_hour_dedupe(self):
        store = FakeStore()
        candidate = {
            "chain": "solana", "token_address": "MintAddress123",
            "token_name": "Minelius", "token_symbol": "MNL",
            "price_usd": 0.01, "liquidity_usd": 75000, "volume_24h_usd": 650000,
            "candidate_status": "UNSCREENED", "trade_authorization": "NO_TRADE",
            "gem_hunter_v3_status": "EARLY_ALPHA_REVIEW",
            "gem_hunter_v3_trade_authorization": "NO_TRADE",
            "tier_base_security_status": "UNKNOWN",
            "pump_phase_fa": "🟢 فاز ۱: شروع پامپ (Early Ignition)",
            "buy_volume_share_pct": 60,
            "security_evidence": {"status": "UNKNOWN", "provider": "SOLANA_RPC"},
            "short_term_momentum_score": 75,
            "tier_data_confidence": "INCOMPLETE",
            "pump_phase_fa": "🟢 فاز ۱: شروع پامپ (Early Ignition)",
            "buy_volume_share_pct": 60,
        }
        client = Mock()
        client.post.return_value.json.return_value = {"ok": True, "result": {"message_id": 21}}
        client.post.return_value.raise_for_status.return_value = None
        fixed_now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)

        class FrozenDateTime(datetime):
            current = fixed_now
            @classmethod
            def now(cls, tz=None):
                return cls.current if tz else cls.current.replace(tzinfo=None)

        with patch("crypto_super_radar.gem_hunter_v3.datetime", FrozenDateTime):
            first = notify_qualified_gem(candidate, store, session=client, bot_token="t", chat_id="private-chat")
            second = notify_qualified_gem(candidate, store, session=client, bot_token="t", chat_id="private-chat")
            FrozenDateTime.current = fixed_now + timedelta(hours=6)
            third = notify_qualified_gem(candidate, store, session=client, bot_token="t", chat_id="private-chat")

        self.assertEqual(first["status"], "SENT")
        self.assertEqual(second["status"], "DUPLICATE_SUPPRESSED")
        self.assertEqual(third["status"], "SENT")
        self.assertEqual(client.post.call_count, 2)
        sent_text = client.post.call_args_list[0].kwargs["json"]["text"]
        for expected in (
            "🐋 <b>CRYPTO SUPER RADAR · رادار شکار فرصت‌ها</b>",
            "🟠 فرصت زودهنگام نیازمند بررسی",
            "Minelius (MNL)", "Solana (شبکه سولانا)", "$0.01",
            "حدود 10,000 توکن", "$75,000.00", "$650,000.00",
            "<code>MintAddress123</code>", "[مرجع: GeckoTerminal]",
            "[مرجع: Solana RPC]", "۳ تا ۱۴ روزه",
            "https://jup.ag/swap/SOL-MintAddress123",
            "https://dexscreener.com/solana/MintAddress123",
            "https://crypto-super-radar-unlimited-demo-staging.onrender.com",
            "🔍 وضعیت: سیگنال پژوهشی تاییدشده (خرید دستی در کیف پول با کاربر)",
        ):
            self.assertIn(expected, sent_text)
        self.assertIn("\n", sent_text)
        self.assertNotIn("\\n", sent_text)
        self.assertIn("🛡️ <b>بخش ۳", sent_text)
        self.assertIn("Seed Phrase", sent_text)
        self.assertIn("📡 <b>بخش ۴", sent_text)
        self.assertEqual(client.post.call_args_list[0].kwargs["json"]["parse_mode"], "HTML")

    def test_early_alpha_review_telegram_skips_non_bullish_or_low_buy_share(self):
        base = {
            "chain": "solana", "token_address": "MintAddress123",
            "gem_hunter_v3_status": "EARLY_ALPHA_REVIEW",
            "gem_hunter_v3_trade_authorization": "NO_TRADE",
            "price_usd": 0.01, "liquidity_usd": 75000, "volume_24h_usd": 650000,
            "pump_phase_fa": "🔴 فاز ۳: سقف فرسودگی / خطر دامپ",
            "buy_volume_share_pct": 80,
        }
        result = notify_qualified_gem(base, FakeStore(), session=Mock(), bot_token="t", chat_id="c")
        self.assertEqual(result["status"], "SKIPPED")
        base["pump_phase_fa"] = "🟡 فاز ۲: میانه موج صعودی"
        base["buy_volume_share_pct"] = 51.9
        result = notify_qualified_gem(base, FakeStore(), session=Mock(), bot_token="t", chat_id="c")
        self.assertEqual(result["status"], "SKIPPED")

    def test_early_alpha_review_telegram_rejects_values_below_requested_thresholds(self):
        candidate = {
            "chain": "base", "token_address": "0xabc", "price_usd": 0.01,
            "liquidity_usd": 50000, "volume_24h_usd": 499999,
            "gem_hunter_v3_status": "EARLY_ALPHA_REVIEW",
            "gem_hunter_v3_trade_authorization": "NO_TRADE",
            "security_evidence": {"status": "UNKNOWN"},
            "tier_base_security_status": "UNKNOWN",
            "pump_phase_fa": "🟢 فاز ۱: شروع پامپ (Early Ignition)",
            "buy_volume_share_pct": 60,
        }
        result = notify_qualified_gem(candidate, FakeStore(), session=Mock(), bot_token="t", chat_id="c")
        self.assertEqual(result["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
