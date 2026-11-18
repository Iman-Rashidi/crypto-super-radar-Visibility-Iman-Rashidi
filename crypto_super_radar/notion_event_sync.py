"""Durable, idempotent Notion event outbox for BloFin Demo operations.

The local Turso database is the source of truth. Notion is a retryable projection:
missing or invalid Notion credentials must never block analysis or order safety.
"""
from __future__ import annotations

import json
import os
import time
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping

import requests

from .database import connect_database, storage_backend

NOTION_DATA_SOURCE_ID = "41a6c893-0218-81ce-9359-000bf3e47b44"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2025-09-03"


_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:sk-or-v1-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{24,}|Bearer\s+[A-Za-z0-9._~-]{16,})"
)
_MODEL_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,50}/[A-Za-z0-9][A-Za-z0-9._:-]{0,80}$"
)


def _redact_secret_text(value: Any, limit: int = 1800) -> str:
    text = _text(value, limit)
    return _SECRET_VALUE_RE.sub("[REDACTED]", text)


def _safe_model_id(value: Any) -> str:
    """Accept a model identifier, never a credential or model+credential concatenation."""
    candidate = _text(value, 160).strip()
    if not candidate or _SECRET_VALUE_RE.search(candidate):
        return ""
    if len(candidate) > 140 or not _MODEL_ID_RE.fullmatch(candidate):
        return ""
    return candidate



EVENT_TYPES = {
    "STARTUP", "WAKE", "SLEEP", "RESTART", "HEARTBEAT",
    "SCAN_STARTED", "SCAN_COMPLETED", "ANALYSIS_COMPLETED", "NO_TRADE",
    "DATA_ERROR", "ORDER_SUBMITTED", "ORDER_FILLED", "POSITION_CLOSED",
    "AI_REVIEW_REQUESTED", "AI_RECOMMENDATION", "AI_VETO", "AI_APPROVAL",
    "NOTION_SYNC_PENDING", "NOTION_SYNCED", "NOTION_SYNC_FAILED", "SYSTEM_ERROR",
}
ACTORS = {"Robot", "AI Supervisor", "Nemotron Supervisor", "Second AI Supervisor", "Render Runtime", "BloFin Demo", "Turso Event Store", "Notion Sync", "Scheduler", "Operator"}
EVENT_STATUSES = {"SUCCESS", "WARNING", "FAILED", "UNKNOWN", "PENDING"}
SIGNALS = {"LONG", "SHORT", "NO_TRADE", "UNKNOWN"}
AI_DECISIONS = {"APPROVE", "REJECT", "MODIFY", "OBSERVE_ONLY", "NOT_CALLED"}


def _utc(value: str | None = None) -> str:
    if value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("occurred_at_utc must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: Any, limit: int = 1800) -> str:
    return str(value or "").strip()[:limit]


class NotionEventOutbox:
    """Persist operational events first, then sync them to Notion with retries."""

    def __init__(self, connection: Any | None = None) -> None:
        self._owns_db = connection is None
        self._db = connection or connect_database()
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS csr_notion_event_outbox (
                event_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                sync_status TEXT NOT NULL DEFAULT 'PENDING',
                last_error_type TEXT,
                last_http_status INTEGER,
                notion_page_id TEXT,
                synced_at_utc TEXT
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_csr_notion_outbox_pending
            ON csr_notion_event_outbox(sync_status, next_attempt_at, created_at_utc)
        """)
        self._redact_legacy_outbox_payloads()
        self._db.commit()

    def _redact_legacy_outbox_payloads(self) -> None:
        """Scrub any credential accidentally persisted in historical outbox text fields."""
        rows = self._db.execute(
            "SELECT event_id, payload_json FROM csr_notion_event_outbox"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, KeyError):
                continue
            if not isinstance(payload, dict):
                continue
            changed = False
            safe_model = _safe_model_id(payload.get("ai_model"))
            if payload.get("ai_model") != safe_model:
                payload["ai_model"] = safe_model
                changed = True
            for field in (
                "ai_analysis", "ai_suggestion", "reason", "details", "ai_provider",
            ):
                value = payload.get(field)
                if isinstance(value, str):
                    scrubbed = _redact_secret_text(value, 1800)
                    if scrubbed != value:
                        payload[field] = scrubbed
                        changed = True
            if changed:
                self._db.execute(
                    "UPDATE csr_notion_event_outbox SET payload_json=? WHERE event_id=?",
                    (_canonical(payload), str(row["event_id"])),
                )

    @staticmethod
    def normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise ValueError("event must be a mapping")
        event_id = _text(event.get("event_id"), 180)
        if not event_id:
            raise ValueError("event_id is required")
        event_type = _text(event.get("event_type"), 40).upper()
        actor = _text(event.get("actor"), 40)
        status = _text(event.get("status") or "SUCCESS", 20).upper()
        signal = _text(event.get("signal") or "UNKNOWN", 20).upper()
        decision = _text(event.get("ai_decision") or "NOT_CALLED", 30).upper()
        if event_type not in EVENT_TYPES:
            raise ValueError("unsupported event_type")
        if actor not in ACTORS:
            raise ValueError("unsupported actor")
        if status not in EVENT_STATUSES:
            raise ValueError("unsupported event status")
        if signal not in SIGNALS:
            signal = "UNKNOWN"
        if decision not in AI_DECISIONS:
            decision = "NOT_CALLED"
        payload = {
            "event_id": event_id,
            "cycle_id": _text(event.get("cycle_id"), 180),
            "occurred_at_utc": _utc(event.get("occurred_at_utc")),
            "event_type": event_type,
            "actor": actor,
            "status": status,
            "market": "Futures" if _text(event.get("market") or "Futures") == "Futures" else "Spot",
            "symbol": _text(event.get("symbol"), 60),
            "timeframe": _text(event.get("timeframe"), 40),
            "signal": signal,
            "ai_decision": decision,
            "ai_provider": _redact_secret_text(event.get("ai_provider"), 120),
            "ai_model": _safe_model_id(event.get("ai_model")),
            "ai_analysis": _redact_secret_text(event.get("ai_analysis"), 1800),
            "ai_suggestion": _redact_secret_text(event.get("ai_suggestion"), 1800),
            "reason": _redact_secret_text(event.get("reason"), 1800),
            "details": _redact_secret_text(event.get("details"), 1800),
            "experience_id": _text(event.get("experience_id"), 180),
            "order_id": _text(event.get("order_id"), 180),
            "position_id": _text(event.get("position_id"), 180),
            "source_url": _text(event.get("source_url"), 1000),
        }
        if not payload["source_url"].startswith("https://"):
            payload["source_url"] = ""
        return payload

    def enqueue(self, event: Mapping[str, Any]) -> bool:
        """Append an immutable event and an outbox item atomically; duplicates are no-ops."""
        payload = self.normalize_event(event)
        now = _utc()
        with self._db:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO csr_notion_event_outbox
                   (event_id, payload_json, created_at_utc, attempts, next_attempt_at, sync_status)
                   VALUES (?, ?, ?, 0, 0, 'PENDING')""",
                (payload["event_id"], _canonical(payload), now),
            )
        return bool(getattr(cursor, "rowcount", 0))

    def _rich_text(self, value: str) -> dict[str, Any]:
        value = _text(value, 1800)
        return {"rich_text": [{"text": {"content": value}}]} if value else {"rich_text": []}

    def _notion_properties(self, event: dict[str, Any], attempts: int) -> dict[str, Any]:
        title = f"{event['event_type']} · {event['symbol'] or 'BLOFIN DEMO'} · {event['event_id'][:24]}"
        props: dict[str, Any] = {
            "رویداد": {"title": [{"text": {"content": title[:1800]}}]},
            "شناسه رویداد": self._rich_text(event["event_id"]),
            "نوع رویداد": {"select": {"name": event["event_type"]}},
            "عامل": {"select": {"name": event["actor"]}},
            "وضعیت رویداد": {"select": {"name": event["status"]}},
            "وضعیت Sync": {"select": {"name": "SYNCED"}},
            "بازار": {"select": {"name": event["market"]}},
            "سیگنال ربات": {"select": {"name": event["signal"]}},
            "تصمیم نهایی ناظر": {"select": {"name": event["ai_decision"]}},
            "زمان رویداد UTC": {"date": {"start": event["occurred_at_utc"]}},
            "زمان آخرین تلاش Sync": {"date": {"start": _utc()}},
            "تعداد تلاش Sync": {"number": attempts},
            "نماد": self._rich_text(event["symbol"]),
            "تایم‌فریم": self._rich_text(event["timeframe"]),
            "شناسه چرخه": self._rich_text(event["cycle_id"]),
            "شناسه تجربه مرتبط": self._rich_text(event["experience_id"]),
            "شناسه سفارش BloFin": self._rich_text(event["order_id"]),
            "شناسه پوزیشن BloFin": self._rich_text(event["position_id"]),
            "Provider ناظر AI": self._rich_text(event["ai_provider"]),
            "مدل ناظر AI": self._rich_text(event["ai_model"]),
            "تحلیل ناظر AI": self._rich_text(event["ai_analysis"]),
            "پیشنهاد ناظر AI": self._rich_text(event["ai_suggestion"]),
            "دلایل رد معامله": self._rich_text(event["reason"]),
            "جزئیات و Payload خلاصه": self._rich_text(event["details"]),
            "آخرین خطای Sync": self._rich_text(""),
        }
        if event["source_url"]:
            props["منبع شواهد"] = {"url": event["source_url"]}
        return props

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        }

    def _find_existing_page(self, token: str, event_id: str) -> str | None:
        response = requests.post(
            f"{NOTION_API_BASE}/data_sources/{NOTION_DATA_SOURCE_ID}/query",
            headers=self._headers(token),
            json={
                "page_size": 1,
                "filter": {
                    "property": "شناسه رویداد",
                    "rich_text": {"equals": event_id},
                },
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        rows = data.get("results") if isinstance(data, dict) else []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return _text(rows[0].get("id"), 80) or None
        return None

    def _create_page(self, token: str, event: dict[str, Any], attempts: int) -> str:
        response = requests.post(
            f"{NOTION_API_BASE}/pages",
            headers=self._headers(token),
            json={
                "parent": {"type": "data_source_id", "data_source_id": NOTION_DATA_SOURCE_ID},
                "properties": self._notion_properties(event, attempts),
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        page_id = data.get("id") if isinstance(data, dict) else None
        if not page_id:
            raise RuntimeError("Notion create-page response omitted page ID")
        return _text(page_id, 80)

    def flush_pending(self, *, limit: int = 10, now: int | None = None) -> dict[str, Any]:
        """Retry pending events. Safe to call after restarts; query-before-create avoids duplicates."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        token = (os.getenv("NOTION_API_TOKEN") or "").strip()
        if not token:
            return {"status": "NOT_CONFIGURED", "queued": self.pending_count(), "synced": 0, "failed": 0}
        current = int(time.time()) if now is None else int(now)
        rows = self._db.execute(
            """SELECT event_id, payload_json, attempts FROM csr_notion_event_outbox
               WHERE sync_status='PENDING' AND next_attempt_at<=?
               ORDER BY created_at_utc ASC LIMIT ?""",
            (current, min(limit, 50)),
        ).fetchall()
        synced = failed = 0
        for row in rows:
            event_id = str(row["event_id"])
            attempts = int(row["attempts"] or 0) + 1
            try:
                event = json.loads(row["payload_json"])
                page_id = self._find_existing_page(token, event_id)
                if page_id is None:
                    page_id = self._create_page(token, event, attempts)
                with self._db:
                    self._db.execute(
                        """UPDATE csr_notion_event_outbox SET
                           attempts=?, sync_status='SYNCED', notion_page_id=?,
                           synced_at_utc=?, last_error_type=NULL, last_http_status=NULL
                           WHERE event_id=?""",
                        (attempts, page_id, _utc(), event_id),
                    )
                synced += 1
            except requests.HTTPError as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                delay = min(3600, 5 * (2 ** min(attempts - 1, 9)))
                with self._db:
                    self._db.execute(
                        """UPDATE csr_notion_event_outbox SET attempts=?, next_attempt_at=?,
                           last_error_type=?, last_http_status=? WHERE event_id=?""",
                        (attempts, current + delay, type(exc).__name__, status_code, event_id),
                    )
                failed += 1
            except Exception as exc:
                delay = min(3600, 5 * (2 ** min(attempts - 1, 9)))
                with self._db:
                    self._db.execute(
                        """UPDATE csr_notion_event_outbox SET attempts=?, next_attempt_at=?,
                           last_error_type=? WHERE event_id=?""",
                        (attempts, current + delay, type(exc).__name__, event_id),
                    )
                failed += 1
        snapshot = self.status_snapshot()
        snapshot.update({"synced_this_run": synced, "failed_this_run": failed})
        return snapshot

    def pending_count(self) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM csr_notion_event_outbox WHERE sync_status='PENDING'"
        ).fetchone()
        return int(row[0] or 0)

    def status_snapshot(self) -> dict[str, Any]:
        """Expose retry/backoff diagnostics without leaking event payloads or IDs."""
        now = int(time.time())
        totals = self._db.execute(
            """SELECT
               SUM(CASE WHEN sync_status='PENDING' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN sync_status='SYNCED' THEN 1 ELSE 0 END) AS synced,
               SUM(CASE WHEN sync_status='FAILED' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN sync_status='PENDING' AND
                    (attempts > 0 OR last_error_type IS NOT NULL OR last_http_status IS NOT NULL)
                    THEN 1 ELSE 0 END) AS pending_with_errors,
               SUM(CASE WHEN sync_status='PENDING' AND next_attempt_at > ?
                    THEN 1 ELSE 0 END) AS pending_in_backoff,
               MAX(synced_at_utc) AS last_success_utc
               FROM csr_notion_event_outbox""",
            (now,),
        ).fetchone()
        oldest = self._db.execute(
            """SELECT created_at_utc, attempts, next_attempt_at,
                      last_error_type, last_http_status
               FROM csr_notion_event_outbox
               WHERE sync_status='PENDING'
               ORDER BY created_at_utc ASC
               LIMIT 1"""
        ).fetchone()
        oldest_error = self._db.execute(
            """SELECT created_at_utc, attempts, next_attempt_at,
                      last_error_type, last_http_status
               FROM csr_notion_event_outbox
               WHERE sync_status='PENDING'
                 AND (last_error_type IS NOT NULL OR last_http_status IS NOT NULL)
               ORDER BY created_at_utc ASC
               LIMIT 1"""
        ).fetchone()

        def retry_at(value: Any) -> str | None:
            try:
                epoch = int(value or 0)
                if epoch <= 0:
                    return None
                return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError, OverflowError, OSError):
                return None

        def age_seconds(value: Any) -> int | None:
            try:
                created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                return max(0, int((datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                return None

        pending = int(totals["pending"] or 0)
        pending_in_backoff = int(totals["pending_in_backoff"] or 0)
        token_configured = bool((os.getenv("NOTION_API_TOKEN") or "").strip())
        return {
            "status": "CONFIGURED" if token_configured else "NOT_CONFIGURED",
            "token_configured": token_configured,
            "storage_backend": storage_backend(),
            "pending": pending,
            "synced": int(totals["synced"] or 0),
            "failed": int(totals["failed"] or 0),
            "last_success_utc": totals["last_success_utc"],
            "pending_with_errors": int(totals["pending_with_errors"] or 0),
            "pending_in_backoff": pending_in_backoff,
            "pending_due_now": max(0, pending - pending_in_backoff),
            "oldest_pending_created_at_utc": oldest["created_at_utc"] if oldest else None,
            "oldest_pending_age_seconds": age_seconds(oldest["created_at_utc"]) if oldest else None,
            "oldest_pending_attempts": int(oldest["attempts"] or 0) if oldest else None,
            "oldest_pending_next_retry_at_utc": retry_at(oldest["next_attempt_at"]) if oldest else None,
            "oldest_pending_error_type": oldest["last_error_type"] if oldest else None,
            "oldest_pending_http_status": oldest["last_http_status"] if oldest else None,
            "oldest_error_created_at_utc": oldest_error["created_at_utc"] if oldest_error else None,
            "oldest_error_attempts": int(oldest_error["attempts"] or 0) if oldest_error else None,
            "oldest_error_next_retry_at_utc": retry_at(oldest_error["next_attempt_at"]) if oldest_error else None,
            "oldest_error_type": oldest_error["last_error_type"] if oldest_error else None,
            "oldest_error_http_status": oldest_error["last_http_status"] if oldest_error else None,
            "diagnostic_scope": "SAFE_METADATA_ONLY",
        }

    def close(self) -> None:
        if self._owns_db:
            self._db.close()
