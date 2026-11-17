"""Durable, idempotent Notion event outbox for BloFin Demo operations.

The local Turso database is the source of truth. Notion is a retryable projection:
missing or invalid Notion credentials must never block analysis or order safety.
"""
from __future__ import annotations

import json
import os
import time
import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

import requests

from .database import connect_database, storage_backend

NOTION_DATA_SOURCE_ID = "41a6c893-0218-81ce-9359-000bf3e47b44"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2025-09-03"

EVENT_TYPES = {
    "STARTUP", "WAKE", "SLEEP", "RESTART", "HEARTBEAT",
    "SCAN_STARTED", "SCAN_COMPLETED", "ANALYSIS_COMPLETED", "NO_TRADE",
    "DATA_ERROR", "ORDER_SUBMITTED", "ORDER_FILLED", "POSITION_CLOSED",
    "AI_REVIEW_REQUESTED", "AI_RECOMMENDATION", "AI_VETO", "AI_APPROVAL",
    "NOTION_SYNC_PENDING", "NOTION_SYNCED", "NOTION_SYNC_FAILED", "SYSTEM_ERROR",
    "AI_DISAGREEMENT", "MANUAL_ORDER_CANCELED",
    "MANUAL_POSITION_CLOSE_REQUESTED", "MANUAL_ACTION_FAILED",
}
ACTORS = {
    "Robot", "AI Supervisor", "Render Runtime", "BloFin Demo",
    "Turso Event Store", "Notion Sync", "Scheduler",
    "Nemotron Supervisor", "Second AI Supervisor", "Operator",
}
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
        self._db.commit()

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
            "ai_model": _text(event.get("ai_model"), 160),
            "reviewer_id": _text(event.get("reviewer_id"), 80),
            "ai_provider": _text(event.get("ai_provider"), 100),
            "ai_prompt_version": _text(event.get("ai_prompt_version"), 120),
            "ai_requested_at_utc": _utc(event.get("ai_requested_at_utc")) if event.get("ai_requested_at_utc") else "",
            "ai_completed_at_utc": _utc(event.get("ai_completed_at_utc")) if event.get("ai_completed_at_utc") else "",
            "ai_call_status": _text(event.get("ai_call_status"), 30).upper(),
            "ai_validation": _text(event.get("ai_validation"), 30).upper(),
            "reviewer_consensus": _text(event.get("reviewer_consensus"), 30).upper(),
            "input_data_type": _text(event.get("input_data_type"), 30).upper(),
            "ai_analysis": _text(event.get("ai_analysis"), 1800),
            "ai_suggestion": _text(event.get("ai_suggestion"), 1800),
            "reason": _text(event.get("reason"), 1800),
            "details": _text(event.get("details"), 1800),
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
            "مدل ناظر AI": self._rich_text(event["ai_model"]),
            "تحلیل ناظر AI": self._rich_text(event["ai_analysis"]),
            "پیشنهاد ناظر AI": self._rich_text(event["ai_suggestion"]),
            "دلایل رد معامله": self._rich_text(event["reason"]),
            "جزئیات و Payload خلاصه": self._rich_text(event["details"]),
            "آخرین خطای Sync": self._rich_text(""),
        }
        if event["source_url"]:
            props["منبع شواهد"] = {"url": event["source_url"]}
        if event["reviewer_id"]:
            props["شناسه ناظر"] = self._rich_text(event["reviewer_id"])
        if event["ai_provider"]:
            props["Provider ناظر AI"] = self._rich_text(event["ai_provider"])
        if event["ai_prompt_version"]:
            props["نسخه Prompt ناظر"] = self._rich_text(event["ai_prompt_version"])
        if event["ai_requested_at_utc"]:
            props["زمان درخواست AI"] = {"date": {"start": event["ai_requested_at_utc"]}}
        if event["ai_completed_at_utc"]:
            props["زمان پاسخ AI"] = {"date": {"start": event["ai_completed_at_utc"]}}
        if event["ai_call_status"] in {"CALLED", "NOT_CALLED", "NOT_CONFIGURED", "UNKNOWN", "ERROR"}:
            props["وضعیت فراخوانی AI"] = {"select": {"name": event["ai_call_status"]}}
        if event["ai_validation"] in {"VALID", "INVALID", "NOT_RUN", "UNKNOWN"}:
            props["اعتبارسنجی پاسخ AI"] = {"select": {"name": event["ai_validation"]}}
        if event["reviewer_consensus"] in {"AGREEMENT", "DISAGREEMENT", "SINGLE_REVIEWER", "NOT_COMPARABLE", "NOT_CALLED"}:
            props["وضعیت توافق ناظران"] = {"select": {"name": event["reviewer_consensus"]}}
        if event["input_data_type"] in {"LIVE_MARKET", "SYNTHETIC_DRY_RUN", "HISTORICAL_REPLAY", "UNKNOWN"}:
            props["نوع داده ورودی"] = {"select": {"name": event["input_data_type"]}}
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
        row = self._db.execute(
            """SELECT
               SUM(CASE WHEN sync_status='PENDING' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN sync_status='SYNCED' THEN 1 ELSE 0 END) AS synced,
               SUM(CASE WHEN sync_status='FAILED' THEN 1 ELSE 0 END) AS failed,
               MAX(synced_at_utc) AS last_success_utc
               FROM csr_notion_event_outbox"""
        ).fetchone()
        token_configured = bool((os.getenv("NOTION_API_TOKEN") or "").strip())
        return {
            "status": "CONFIGURED" if token_configured else "NOT_CONFIGURED",
            "token_configured": token_configured,
            "storage_backend": storage_backend(),
            "pending": int(row["pending"] or 0),
            "synced": int(row["synced"] or 0),
            "failed": int(row["failed"] or 0),
            "last_success_utc": row["last_success_utc"],
        }

    def close(self) -> None:
        if self._owns_db:
            self._db.close()
