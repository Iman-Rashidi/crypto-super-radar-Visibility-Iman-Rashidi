from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from crypto_super_radar.telegram_notifier import (
    TelegramNotifier, TelegramNotificationError, SMOKE_MESSAGE, TARGET_CHAT_ID,
    TARGET_USERNAME,
)


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
            if url.endswith("/getChat"):
                return FakeResponse({
                    "ok": True,
                    "result": {
                        "id": TARGET_CHAT_ID,
                        "type": "private",
                        "username": TARGET_USERNAME,
                    },
                })
            return FakeResponse({"ok": True, "result": {"message_id": 88}})

        self.notifier = TelegramNotifier(token="test-token", connection=self.db, http_post=post)

    def tearDown(self):
        self.db.close()

    def test_resolves_pinned_account_and_persists_numeric_identity(self):
        recipient = self.notifier.resolve_recipient()
        self.assertEqual(recipient, {
            "username": TARGET_USERNAME,
            "chat_id": TARGET_CHAT_ID,
            "user_id": TARGET_CHAT_ID,
        })
        row = self.db.execute(
            "SELECT username, chat_id, user_id FROM telegram_recipient_lock WHERE lock_key='primary'"
        ).fetchone()
        self.assertEqual(row, (TARGET_USERNAME, TARGET_CHAT_ID, TARGET_CHAT_ID))
        self.notifier.http_post = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("getChat must not run after the verified lock is persisted")
        )
        self.assertEqual(self.notifier.resolve_recipient()["chat_id"], TARGET_CHAT_ID)

    def test_existing_wrong_lock_is_repaired_only_after_telegram_verifies_target(self):
        self.notifier._ensure_schema(self.db)
        self.db.execute(
            "INSERT INTO telegram_recipient_lock(lock_key,username,chat_id,user_id) VALUES('primary','other',22,22)"
        )
        self.db.commit()
        recipient = self.notifier.resolve_recipient()
        self.assertEqual(recipient["chat_id"], TARGET_CHAT_ID)
        row = self.db.execute(
            "SELECT username, chat_id, user_id FROM telegram_recipient_lock WHERE lock_key='primary'"
        ).fetchone()
        self.assertEqual(row, (TARGET_USERNAME, TARGET_CHAT_ID, TARGET_CHAT_ID))

    def test_chat_id_alias_is_supported_and_must_match_approved_recipient(self):
        with patch.dict("os.environ", {"CSR_TELEGRAM_CHAT_ID": str(TARGET_CHAT_ID)}, clear=True):
            notifier = TelegramNotifier(token="test-token", connection=self.db, http_post=self.notifier.http_post)
        self.assertEqual(notifier.resolve_recipient()["chat_id"], TARGET_CHAT_ID)

        with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "22"}, clear=True):
            with self.assertRaises(TelegramNotificationError):
                TelegramNotifier(token="test-token", connection=self.db, http_post=self.notifier.http_post)

    def test_send_once_is_idempotent(self):
        first = self.notifier.send_once("trade:entry:pos-1", "entry")
        second = self.notifier.send_once("trade:entry:pos-1", "entry")
        self.assertEqual(first["status"], "SENT")
        self.assertEqual(second["status"], "DUPLICATE_SKIPPED")
        sent = [call for call in self.calls if call[0].endswith("/sendMessage")]
        self.assertEqual(len(sent), 1)

    def test_smoke_message_exact_text(self):
        self.assertEqual(
            SMOKE_MESSAGE,
            "✅ اتصال نوتیفیکیشن CRYPTO SUPER RADAR به حساب @Germany004 برقرار شد.",
        )

    def test_wrong_username_or_non_private_chat_fails_closed(self):
        def post(url, *, json, timeout):
            return FakeResponse({
                "ok": True,
                "result": {
                    "id": TARGET_CHAT_ID,
                    "type": "group",
                    "username": "Germany004",
                },
            })

        notifier = TelegramNotifier(token="test-token", connection=self.db, http_post=post)
        with self.assertRaises(TelegramNotificationError):
            notifier.resolve_recipient()

    def test_send_failure_is_recorded_and_not_retried(self):
        attempts = []

        def post(url, *, json, timeout):
            if url.endswith("/getChat"):
                return FakeResponse({
                    "ok": True,
                    "result": {
                        "id": TARGET_CHAT_ID,
                        "type": "private",
                        "username": TARGET_USERNAME,
                    },
                })
            attempts.append(url)
            raise OSError("network down")

        notifier = TelegramNotifier(token="test-token", connection=self.db, http_post=post)
        with self.assertRaises(TelegramNotificationError):
            notifier.send_once("trade:closed:pos-2", "closed")
        row = self.db.execute(
            "SELECT status FROM telegram_notification_outbox WHERE event_key='trade:closed:pos-2'"
        ).fetchone()
        self.assertEqual(row[0], "FAILED")
        self.assertEqual(
            notifier.send_once("trade:closed:pos-2", "closed")["status"],
            "DUPLICATE_SKIPPED",
        )
        self.assertEqual(len(attempts), 1)

    def test_notify_async_uses_non_daemon_worker(self):
        with patch("crypto_super_radar.telegram_notifier.threading.Thread") as thread:
            self.notifier.notify_async("trade:entry:pos-3", "entry")
        self.assertIs(thread.call_args.kwargs["daemon"], False)
        thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
