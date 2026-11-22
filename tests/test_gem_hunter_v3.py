"""Offline tests for Gem Hunter V3 research-only primitives."""
import sqlite3
import unittest
from unittest.mock import Mock, patch

from crypto_super_radar.gem_hunter_v3 import (
    estimate_exit_slippage, score_candidate_v3, persist_candidate_lifecycle,
    notify_qualified_gem, TursoGemHunterStore,
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
            "pool_trade_evidence": {"buy_volume_usd": 8000, "sell_volume_usd": 2000, "coverage_status": "COMPLETE"},
            "technical_evidence": {"coverage_status": "COMPLETE", "filters": {"trend": "PASS", "momentum": "PASS"}},
            "security_evidence": {"status": "PASS"},
            "exit_slippage_evidence": {"status": "PASS", "estimated_slippage_bps": 20},
            "market_reconciliation": {"status": "PASS"},
        })
        self.assertEqual(result["short_term_momentum_smart_money_score"], 90)
        self.assertEqual(result["candidate_status"], "QUALIFIED_GEM")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

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
            "candidate_status": "QUALIFIED_GEM", "trade_authorization": "NO_TRADE",
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
        candidate = {"candidate_status": "QUALIFIED_GEM", "trade_authorization": "NO_TRADE",
                     "security_evidence": {"status": "PASS"},
                     "exit_slippage_evidence": {"status": "UNKNOWN"},
                     "market_reconciliation": {"status": "PASS"}}
        result = notify_qualified_gem(candidate, store, session=Mock(), bot_token="t", chat_id="c")
        self.assertEqual(result["status"], "BLOCKED")

    def test_turso_requires_credentials(self):
        with self.assertRaises(RuntimeError):
            TursoGemHunterStore(database_url=None, auth_token=None)

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

    def test_dashboard_status_never_authorizes_trading(self):
        result = score_candidate_v3({"security_evidence": {"status": "PASS"},
                                     "exit_slippage_evidence": {"status": "PASS"},
                                     "market_reconciliation": {"status": "PASS"},
                                     "pool_trade_evidence": {"buy_volume_usd": 90, "sell_volume_usd": 10},
                                     "technical_evidence": {"filters": {"trend": "PASS", "momentum": "PASS"}}})
        self.assertEqual(result["trade_authorization"], "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
