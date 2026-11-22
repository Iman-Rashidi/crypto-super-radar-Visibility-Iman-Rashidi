"""Offline tests for Gem Hunter V3 research-only primitives."""
import sqlite3
import unittest
from unittest.mock import Mock, patch

from crypto_super_radar.dashboard_data import gem_hunter_v3_dataframe
from crypto_super_radar.gem_hunter_v3 import (
    estimate_exit_slippage, score_candidate_v3, persist_candidate_lifecycle,
    notify_qualified_gem, TursoGemHunterStore, classify_gem_tier,
)


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
            "market_reconciliation": {"status": "PASS", "verified_liquidity_usd": 100000, "verified_volume_24h_usd": 500000},
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
            "market_reconciliation": {"status": "PASS", "verified_liquidity_usd": 100000, "verified_volume_24h_usd": 500000},
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
            "market_reconciliation": {"status": "PASS", "verified_liquidity_usd": 100000,
                                      "verified_volume_24h_usd": 500000},
            "exit_slippage_evidence": {"status": "PASS"},
        }
        tier = classify_gem_tier(candidate, {
            "short_term_momentum_score": 80, "momentum_evidence_coverage_pct": 80,
            "momentum_score_components": {"pool_buy_volume_share": 70},
            "safety_exit_liquidity_score": 90,
        })
        self.assertEqual(tier["tier"], "TIER_1_VERIFIED_GEM")

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

    def test_dashboard_status_never_authorizes_trading(self):
        result = score_candidate_v3({"security_evidence": {"status": "PASS"},
                                     "exit_slippage_evidence": {"status": "PASS"},
                                     "market_reconciliation": {"status": "PASS"},
                                     "pool_trade_evidence": {"buy_volume_usd": 90, "sell_volume_usd": 10},
                                     "technical_evidence": {"filters": {"trend": "PASS", "momentum": "PASS"}}})
        self.assertEqual(result["trade_authorization"], "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
