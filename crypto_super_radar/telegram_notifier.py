"""Best-effort Telegram alerts with Turso-locked recipient and event idempotency.

Trading code dispatches notifications on daemon threads. Telegram/network failures
are isolated from the Challenge 1 V2 decision and execution path.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from typing import Any, Callable, Mapping

import requests

API_BASE = "https://api.telegram.org"
TARGET_USERNAME = "Germany004"
SMOKE_MESSAGE = "✅ اتصال نوتیفیکیشن CRYPTO SUPER RADAR به حساب @Germany004 برقرار شد."


class TelegramNotificationError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(
        self,
        *,
        token: str | None = None,
        connection_factory: Callable[[], Any] | None = None,
        connection: Any | None = None,
        http_post: Callable[..., Any] | None = None,
        timeout: tuple[float, float] = (3.0, 5.0),
    ) -> None:
        self.token = str(token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.connection_factory = connection_factory
        self.connection = connection
        self.http_post = http_post or requests.post
        self.timeout = timeout
        self._schema_lock = threading.Lock()
        if not self.token:
            raise TelegramNotificationError("TELEGRAM_BOT_TOKEN is not configured.")
        if self.connection is None and self.connection_factory is None:
            raise TelegramNotificationError("A Turso database connection is required.")

    def _connection(self):
        return self.connection if self.connection is not None else self.connection_factory()

    def _ensure_schema(self, db: Any) -> None:
        with self._schema_lock:
            db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_recipient_lock (
                    lock_key TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    locked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_notification_outbox (
                    event_key TEXT PRIMARY KEY,
                    payload_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    error_type TEXT
                )
            """)
            db.commit()

    def _api(self, method: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.http_post(
                f"{API_BASE}/bot{self.token}/{method}",
                json=dict(payload or {}),
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise TelegramNotificationError(f"Telegram {method} request failed: {type(exc).__name__}") from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramNotificationError(f"Telegram {method} returned an unsuccessful response.")
        result = body.get("result")
        if not isinstance(result, (dict, list)):
            raise TelegramNotificationError(f"Telegram {method} returned an invalid result.")
        return body

    @staticmethod
    def _rows(updates: Any) -> list[Mapping[str, Any]]:
        if not isinstance(updates, list):
            return []
        return [row for row in updates if isinstance(row, Mapping)]

    def resolve_recipient(self) -> dict[str, int | str]:
        db = self._connection()
        close_db = db is not self.connection
        try:
            self._ensure_schema(db)
            locked = db.execute(
                "SELECT username, chat_id, user_id FROM telegram_recipient_lock WHERE lock_key='primary'"
            ).fetchone()
            if locked is not None:
                return {"username": str(locked[0]), "chat_id": int(locked[1]), "user_id": int(locked[2])}

            updates = self._rows(self._api("getUpdates", {"limit": 100, "timeout": 0})["result"])
            matches = []
            for update in updates:
                message = update.get("message") or update.get("edited_message")
                if not isinstance(message, Mapping):
                    continue
                sender = message.get("from")
                chat = message.get("chat")
                if not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
                    continue
                username = str(sender.get("username") or "")
                if username.casefold() != TARGET_USERNAME.casefold():
                    continue
                try:
                    user_id, chat_id = int(sender["id"]), int(chat["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                matches.append((int(update.get("update_id") or 0), user_id, chat_id, username))
            if not matches:
                raise TelegramNotificationError(
                    f"No Telegram update from @{TARGET_USERNAME} was found; user must start the bot first."
                )
            _, user_id, chat_id, username = max(matches, key=lambda item: item[0])
            db.execute(
                "INSERT OR IGNORE INTO telegram_recipient_lock(lock_key,username,chat_id,user_id) VALUES('primary',?,?,?)",
                (username, chat_id, user_id),
            )
            db.commit()
            locked = db.execute(
                "SELECT username, chat_id, user_id FROM telegram_recipient_lock WHERE lock_key='primary'"
            ).fetchone()
            if locked is None or int(locked[1]) != chat_id or int(locked[2]) != user_id:
                raise TelegramNotificationError("Telegram recipient is already locked to a different account.")
            return {"username": str(locked[0]), "chat_id": int(locked[1]), "user_id": int(locked[2])}
        finally:
            if close_db:
                db.close()

    def send_once(self, event_key: str, message: str) -> dict[str, Any]:
        key, text = str(event_key or "").strip(), str(message or "").strip()
        if not key or not text:
            raise ValueError("event_key and message are required.")
        recipient = self.resolve_recipient()
        db = self._connection()
        close_db = db is not self.connection
        try:
            self._ensure_schema(db)
            cursor = db.execute(
                "INSERT OR IGNORE INTO telegram_notification_outbox(event_key,payload_text,status) VALUES(?,?,'SENDING')",
                (key, text),
            )
            inserted = int(getattr(cursor, "rowcount", 0) or 0) == 1
            db.commit()
            row = db.execute(
                "SELECT payload_text,status FROM telegram_notification_outbox WHERE event_key=?", (key,)
            ).fetchone()
            if row is None:
                raise TelegramNotificationError("Notification idempotency record could not be read back.")
            if str(row[0]) != text:
                raise TelegramNotificationError("An idempotency key was reused with a different message.")
            if not inserted:
                return {"status": "DUPLICATE_SKIPPED", "event_key": key}
            # The unique-key insert is the atomic claim. Exactly one concurrent
            # worker can own the external send for this event key.
        finally:
            if close_db:
                db.close()

        try:
            self._api("sendMessage", {"chat_id": recipient["chat_id"], "text": text, "disable_web_page_preview": True})
        except Exception as exc:
            self._finish_event(key, "FAILED", type(exc).__name__)
            raise
        self._finish_event(key, "SENT", None)
        return {"status": "SENT", "event_key": key, "chat_id": recipient["chat_id"]}

    def _finish_event(self, event_key: str, status: str, error_type: str | None) -> None:
        db = self._connection()
        close_db = db is not self.connection
        try:
            self._ensure_schema(db)
            db.execute(
                "UPDATE telegram_notification_outbox SET status=?, completed_at=CURRENT_TIMESTAMP, error_type=? WHERE event_key=? AND status='SENDING'",
                (status, error_type, event_key),
            )
            db.commit()
        finally:
            if close_db:
                db.close()

    def notify_async(self, event_key: str, message: str) -> None:
        """Queue one bounded network attempt on a daemon thread; never block trading."""
        def deliver() -> None:
            try:
                self.send_once(event_key, message)
            except Exception:
                # Deliberately swallowed: notification failure is not a trade failure.
                return
        try:
            threading.Thread(target=deliver, name="csr-telegram-alert", daemon=True).start()
        except Exception:
            return


def send_smoke_test() -> dict[str, Any]:
    from .database import connect_database, storage_backend
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise TelegramNotificationError("Telegram recipient lock requires configured Turso storage.")
    notifier = TelegramNotifier(connection_factory=connect_database)
    return notifier.send_once("telegram-smoke:account-link:v1", SMOKE_MESSAGE)


def main() -> int:
    parser = argparse.ArgumentParser(description="CRYPTO SUPER RADAR Telegram notifier")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    if not args.smoke_test:
        parser.error("Use --smoke-test for the isolated Telegram connectivity check.")
    result = send_smoke_test()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
