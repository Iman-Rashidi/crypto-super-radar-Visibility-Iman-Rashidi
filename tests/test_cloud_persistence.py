"""Offline tests for optional PostgreSQL/Notion event persistence."""
import os
import unittest
from unittest.mock import patch

from crypto_super_radar.cloud_persistence import CloudStore, NotionSink, _json_safe


class CloudPersistenceTests(unittest.TestCase):
    def test_missing_database_url_disables_cloud_store_without_breaking_app(self):
        with patch.dict(os.environ, {
            "DATABASE_URL": "",
            "CSR_DATABASE_URL": "",
            "CSR_REQUIRE_POSTGRES": "",
            "NOTION_API_KEY": "",
            "NOTION_LOGS_DB_ID": "",
            "NOTION_TRADES_DB_ID": "",
            "NOTION_TOKENS_DB_ID": "",
        }, clear=False):
            store = CloudStore()
        self.assertFalse(store.enabled)
        self.assertEqual(store.status()["postgres"], "not_configured")
        self.assertFalse(store.record_log("test event"))

    def test_require_postgres_rejects_missing_connection_string(self):
        with patch.dict(os.environ, {
            "DATABASE_URL": "",
            "CSR_DATABASE_URL": "",
            "CSR_REQUIRE_POSTGRES": "true",
        }, clear=False):
            with self.assertRaises(RuntimeError):
                CloudStore()

    def test_sensitive_fields_are_redacted_before_persistence(self):
        safe = _json_safe({
            "api_key": "secret-value",
            "message": "request failed with token=private-value",
            "nested": {"password": "do-not-store"},
        })
        self.assertEqual(safe["api_key"], "[REDACTED]")
        self.assertIn("[REDACTED]", safe["message"])
        self.assertEqual(safe["nested"]["password"], "[REDACTED]")

    def test_notion_properties_map_common_log_database_columns(self):
        sink = NotionSink()
        schema = {
            "Name": {"type": "title"},
            "Event ID": {"type": "rich_text"},
            "Timestamp": {"type": "date"},
            "Level": {"type": "select"},
            "Details JSON": {"type": "rich_text"},
        }
        payload = {
            "event_id": "event-1",
            "timestamp": "2026-11-11T00:00:00+00:00",
            "level": "ERROR",
            "message": "provider failed",
            "details": {"http_status": 451},
        }
        properties = sink._properties(schema, payload)
        self.assertEqual(properties["Name"]["title"][0]["text"]["content"], "provider failed")
        self.assertEqual(properties["Event ID"]["rich_text"][0]["text"]["content"], "event-1")
        self.assertEqual(properties["Level"]["select"]["name"], "ERROR")
        self.assertIn('"http_status":451', properties["Details JSON"]["rich_text"][0]["text"]["content"])


if __name__ == "__main__":
    unittest.main()
