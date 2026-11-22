from __future__ import annotations

import sqlite3
import unittest

from crypto_super_radar.telegram_notifier import TelegramNotifier, TelegramNotificationError, SMOKE_MESSAGE


class FakeResponse:
    def __init__(self, body):
        self.body = body
    def raise_for_status(self):
        return None
    def json(self):
        return self.body


class TelegramNotifierTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.calls = []
        def post(url, *, json, timeout):
            self.calls.append((url, json, timeout))
            if url.endswith("/getUpdates"):
                return FakeResponse({"ok": True, "result": [
                    {"update_id": 4, "message": {"from": {"id": 22, "username": "other"},
                     "chat": {"id": 22}}},
                    {"update_id": 9, "message": {"from": {"id": 1042, "username": "gErMaNy004"},
                     "chat": {"id": 1042}, "text": "/start"}},
                ]})
            return FakeResponse({"ok": True, "result": {"message_id": 88}})
        self.notifier = TelegramNotifier(token="test-token", connection=self.db, http_post=post)

    def tearDown(self):
        self.db.close()

    def test_resolves_case_insensitive_username_and_locks_numeric_identity(self):
        recipient = self.notifier.resolve_recipient()
        self.assertEqual(recipient, {"username": "gErMaNy004", "chat_id": 1042, "user_id": 1042})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_recipient_lock").fetchone()[0], 1)
        self.notifier.http_post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("getUpdates must not run after lock"))
        self.assertEqual(self.notifier.resolve_recipient()["chat_id"], 1042)

    def test_send_once_is_idempotent(self):
        first = self.notifier.send_once("trade:entry:pos-1", "entry")
        second = self.notifier.send_once("trade:entry:pos-1", "entry")
        self.assertEqual(first["status"], "SENT")
        self.assertEqual(second["status"], "DUPLICATE_SKIPPED")
        sent = [call for call in self.calls if call[0].endswith("/sendMessage")]
        self.assertEqual(len(sent), 1)

    def test_smoke_message_exact_text(self):
        self.assertEqual(SMOKE_MESSAGE, "✅ اتصال نوتیفیکیشن CRYPTO SUPER RADAR به حساب @Germany004 برقرار شد.")

    def test_missing_username_fails_closed(self):
        def post(url, **kwargs):
            return FakeResponse({"ok": True, "result": []})
        notifier = TelegramNotifier(token="test-token", connection=self.db, http_post=post)
        with self.assertRaises(TelegramNotificationError):
            notifier.resolve_recipient()

    def test_send_failure_is_recorded_and_not_retried(self):
        def post(url, *, json, timeout):
            if url.endswith("/getUpdates"):
                return FakeResponse({"ok": True, "result": [
                    {"update_id": 9, "message": {"from": {"id": 1042, "username": "Germany004"},
                     "chat": {"id": 1042}}}]})
            raise OSError("network down")
        notifier = TelegramNotifier(token="test-token", connection=self.db, http_post=post)
        with self.assertRaises(TelegramNotificationError):
            notifier.send_once("trade:closed:pos-2", "closed")
        row = self.db.execute("SELECT status FROM telegram_notification_outbox WHERE event_key='trade:closed:pos-2'").fetchone()
        self.assertEqual(row[0], "FAILED")
        with self.assertRaises(TelegramNotificationError):
            notifier.send_once("trade:closed:pos-2", "closed")


if __name__ == "__main__":
    unittest.main()
