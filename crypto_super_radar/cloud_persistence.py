"""Durable PostgreSQL event store and retryable Notion outbox.

The legacy paper ledger remains SQLite/Turso-compatible. This module adds a
separate PostgreSQL system of record for operational events and a durable,
at-least-once Notion delivery queue. Notion outages never block the web app.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

try:
    from sqlalchemy import (
        JSON, Column, DateTime, Float, Integer, MetaData, String, Table, Text,
        and_, create_engine, or_, select, update,
    )
    from sqlalchemy.dialects.postgresql import insert as pg_insert
except ImportError:  # Optional until DATABASE_URL is configured.
    JSON = Column = DateTime = Float = Integer = MetaData = String = Table = Text = None
    and_ = create_engine = or_ = select = update = pg_insert = None


NOTION_VERSION = os.getenv("NOTION_VERSION", "2026-03-11")
NOTION_BASE_URL = "https://api.notion.com/v1"
_SECRET_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|token|password|secret|authorization|bearer)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_REDACTED = "[REDACTED]"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_text(value: Any, limit: int = 1800) -> str:
    text = str(value if value is not None else "")
    text = _SECRET_RE.sub(lambda match: match.group(1) + match.group(2) + _REDACTED, text)
    return text[:limit]


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return _clean_text(value, 500)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _clean_text(value, 1800)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        safe = {}
        for key, item in list(value.items())[:100]:
            name = _clean_text(key, 80)
            if re.search(r"(?i)(^token$|api.?key|password|secret|authorization|access.?token)", name):
                safe[name] = _REDACTED
            else:
                safe[name] = _json_safe(item, depth + 1)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth + 1) for item in list(value)[:100]]
    return _clean_text(value, 500)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class CloudStore:
    """PostgreSQL tables are created automatically when DATABASE_URL is set."""

    def __init__(self) -> None:
        self.engine = None
        self.enabled = False
        self.error = None
        self._worker_started = False
        self._worker_lock = threading.Lock()
        self._schemas: dict[str, tuple[str, dict[str, Any]]] = {}
        self.database_url_present = bool(
            os.getenv("DATABASE_URL", "").strip()
            or os.getenv("CSR_DATABASE_URL", "").strip()
        )
        if not self.database_url_present:
            if os.getenv("CSR_REQUIRE_POSTGRES", "").lower() in {"1", "true", "yes"}:
                raise RuntimeError("CSR_REQUIRE_POSTGRES is enabled but DATABASE_URL is missing")
            return
        if create_engine is None:
            self.error = "SQLAlchemy is not installed"
            if os.getenv("CSR_REQUIRE_POSTGRES", "").lower() in {"1", "true", "yes"}:
                raise RuntimeError(self.error)
            return
        url = (os.getenv("DATABASE_URL") or os.getenv("CSR_DATABASE_URL") or "").strip()
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://") and "+" not in url.split(":", 1)[0]:
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        try:
            self.engine = create_engine(
                url, pool_pre_ping=True, pool_recycle=300, pool_size=3,
                max_overflow=2, connect_args={"connect_timeout": 8},
            )
            self._define_schema()
            self.metadata.create_all(self.engine)
            with self.engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            self.enabled = True
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + _clean_text(exc, 300)
            if self.engine is not None:
                self.engine.dispose()
                self.engine = None
            if os.getenv("CSR_REQUIRE_POSTGRES", "").lower() in {"1", "true", "yes"}:
                raise RuntimeError("PostgreSQL initialization failed: " + self.error) from exc

    def _define_schema(self) -> None:
        self.metadata = MetaData()
        self.logs = Table(
            "csr_event_logs", self.metadata,
            Column("event_id", String(64), primary_key=True),
            Column("occurred_at", DateTime(timezone=True), nullable=False),
            Column("level", String(16), nullable=False),
            Column("component", String(100), nullable=False),
            Column("event_type", String(100), nullable=False),
            Column("message", Text, nullable=False),
            Column("details", JSON, nullable=False),
        )
        self.trades = Table(
            "csr_trade_records", self.metadata,
            Column("trade_id", String(100), primary_key=True),
            Column("occurred_at", DateTime(timezone=True), nullable=False),
            Column("symbol", String(80)),
            Column("chain", String(40)),
            Column("side", String(20)),
            Column("entry_price", Float),
            Column("exit_price", Float),
            Column("quantity", Float),
            Column("pnl_usd", Float),
            Column("pnl_pct", Float),
            Column("status", String(32)),
            Column("payload", JSON, nullable=False),
        )
        self.tokens = Table(
            "csr_token_snapshots", self.metadata,
            Column("snapshot_id", String(100), primary_key=True),
            Column("observed_at", DateTime(timezone=True), nullable=False),
            Column("chain", String(40), nullable=False),
            Column("token_address", String(160), nullable=False),
            Column("symbol", String(80)),
            Column("research_score", Float),
            Column("screening_status", String(48)),
            Column("payload", JSON, nullable=False),
        )
        self.outbox = Table(
            "csr_notion_outbox", self.metadata,
            Column("event_id", String(100), primary_key=True),
            Column("target", String(20), nullable=False),
            Column("payload", JSON, nullable=False),
            Column("status", String(20), nullable=False, default="pending"),
            Column("attempts", Integer, nullable=False, default=0),
            Column("next_attempt_at", DateTime(timezone=True), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            Column("synced_at", DateTime(timezone=True)),
            Column("last_error", Text),
        )

    def status(self) -> dict[str, Any]:
        return {
            "postgres": "connected" if self.enabled else (
                "not_configured" if not self.database_url_present else "unavailable"
            ),
            "notion": "configured" if self._notion_configured() else "not_configured",
            "outbox": "enabled" if self.enabled else "disabled",
            "error_type": self.error.split(":", 1)[0] if self.error else None,
        }

    @staticmethod
    def _notion_configured() -> bool:
        return bool(
            os.getenv("NOTION_API_KEY", "").strip()
            and os.getenv("NOTION_LOGS_DB_ID", "").strip()
            and os.getenv("NOTION_TRADES_DB_ID", "").strip()
            and os.getenv("NOTION_TOKENS_DB_ID", "").strip()
        )

    def _enqueue(
        self, target: str, record_table: Table, record: dict[str, Any],
        record_key: str, outbox_payload: dict[str, Any],
    ) -> bool:
        if not self.enabled or self.engine is None:
            return False
        now = _utcnow()
        event_id = str(record[record_key])
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    pg_insert(record_table).values(**record).on_conflict_do_nothing()
                )
                connection.execute(
                    pg_insert(self.outbox).values(
                        event_id=event_id, target=target, payload=_json_safe(outbox_payload),
                        status="pending", attempts=0, next_attempt_at=now,
                        created_at=now, updated_at=now,
                    ).on_conflict_do_nothing()
                )
            return True
        except Exception as exc:
            self.error = type(exc).__name__
            return False

    def record_log(
        self, message: str, *, level: str = "INFO", component: str = "application",
        event_type: str = "application_log", details: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> bool:
        event_id = event_id or str(uuid.uuid4())
        now = _utcnow()
        payload = {
            "event_id": event_id, "timestamp": now.isoformat(), "level": level.upper()[:16],
            "component": component[:100], "event_type": event_type[:100],
            "message": _clean_text(message), "details": _json_safe(details or {}),
        }
        record = {
            "event_id": event_id, "occurred_at": now, "level": payload["level"],
            "component": payload["component"], "event_type": payload["event_type"],
            "message": payload["message"], "details": payload["details"],
        }
        return self._enqueue("logs", self.logs, record, "event_id", payload)

    def record_trade(self, trade: dict[str, Any]) -> bool:
        payload = _json_safe(dict(trade))
        trade_id = str(payload.get("trade_id") or uuid.uuid4())
        payload["trade_id"] = trade_id
        now = _utcnow()
        occurred_at = now
        for key in ("closed_at_utc", "timestamp_utc", "timestamp"):
            value = payload.get(key)
            if isinstance(value, str):
                try:
                    occurred_at = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
                    break
                except ValueError:
                    pass
        payload["timestamp"] = occurred_at.isoformat()
        payload["pnl_usd"] = payload.get("net_pnl_usd", payload.get("pnl_usd"))
        payload["pnl_pct"] = payload.get("pnl_pct", payload.get("pnl_percent"))
        record = {
            "trade_id": trade_id, "occurred_at": occurred_at,
            "symbol": _clean_text(payload.get("symbol") or payload.get("asset") or "", 80) or None,
            "chain": _clean_text(payload.get("chain") or "", 40) or None,
            "side": _clean_text(payload.get("side") or "", 20) or None,
            "entry_price": _to_float(payload.get("entry_price")),
            "exit_price": _to_float(payload.get("exit_price")),
            "quantity": _to_float(payload.get("quantity")),
            "pnl_usd": _to_float(payload.get("net_pnl_usd") or payload.get("pnl_usd")),
            "pnl_pct": _to_float(payload.get("pnl_pct")),
            "status": _clean_text(payload.get("status") or "CLOSED", 32),
            "payload": payload,
        }
        return self._enqueue("trades", self.trades, record, "trade_id", payload)

    def record_token_snapshot(self, candidate: dict[str, Any]) -> bool:
        payload = _json_safe(dict(candidate))
        chain = _clean_text(payload.get("chain") or "", 40)
        address = _clean_text(payload.get("token_address") or "", 160)
        if not chain or not address:
            return False
        now = _utcnow()
        security = payload.get("security_evidence")
        security = security if isinstance(security, dict) else {}
        rugcheck = security.get("rugcheck")
        rugcheck = rugcheck if isinstance(rugcheck, dict) else {}
        flags = payload.get("risk_flags")
        flags = flags if isinstance(flags, list) else []
        payload["holder_count"] = (
            security.get("holder_count") if security.get("holder_count") is not None
            else rugcheck.get("holder_count")
        )
        payload["lp_locked"] = (
            security.get("lp_locked") if security.get("lp_locked") is not None
            else rugcheck.get("lp_locked")
        )
        payload["honeypot_status"] = (
            "HONEYPOT_SUSPECT" if "HONEYPOT_SUSPECT" in flags else
            "HONEYPOT" if rugcheck.get("honeypot") is True else
            "NOT_FLAGGED" if rugcheck.get("honeypot") is False else "UNKNOWN"
        )
        payload["screening_status"] = (
            payload.get("candidate_status") or payload.get("trade_authorization") or "UNKNOWN"
        )
        payload["security_json"] = security
        payload["discovered_at"] = now.isoformat()
        minute = now.replace(second=0, microsecond=0).isoformat()
        snapshot_id = "tok-" + hashlib.sha256(
            f"{chain}:{address}:{minute}".encode("utf-8")
        ).hexdigest()[:48]
        record = {
            "snapshot_id": snapshot_id, "observed_at": now, "chain": chain,
            "token_address": address,
            "symbol": _clean_text(payload.get("token_symbol") or "", 80) or None,
            "research_score": _to_float(payload.get("discovery_priority_score")),
            "screening_status": _clean_text(
                payload.get("candidate_status") or payload.get("trade_authorization") or "UNKNOWN", 48
            ),
            "payload": payload,
        }
        payload["snapshot_id"] = snapshot_id
        payload["observed_at"] = now.isoformat()
        return self._enqueue("tokens", self.tokens, record, "snapshot_id", payload)

    def _claim_one(self) -> dict[str, Any] | None:
        now = _utcnow()
        stale = now - timedelta(minutes=2)
        eligible = or_(
            and_(self.outbox.c.status.in_(["pending", "retry"]), self.outbox.c.next_attempt_at <= now),
            and_(self.outbox.c.status == "processing", self.outbox.c.updated_at <= stale),
        )
        with self.engine.begin() as connection:
            row = connection.execute(
                select(self.outbox).where(eligible)
                .order_by(self.outbox.c.created_at.asc())
                .with_for_update(skip_locked=True).limit(1)
            ).mappings().first()
            if row is None:
                return None
            attempts = int(row["attempts"] or 0) + 1
            connection.execute(
                update(self.outbox).where(self.outbox.c.event_id == row["event_id"]).values(
                    status="processing", attempts=attempts, updated_at=now,
                )
            )
            result = dict(row)
            result["attempts"] = attempts
            return result

    def _finish(self, event_id: str, *, error: str | None = None) -> None:
        now = _utcnow()
        if error is None:
            values = {"status": "delivered", "synced_at": now, "updated_at": now, "last_error": None}
        else:
            row = None
            with self.engine.connect() as connection:
                row = connection.execute(
                    select(self.outbox.c.attempts).where(self.outbox.c.event_id == event_id)
                ).scalar_one_or_none()
            attempts = int(row or 1)
            delay = min(3600, 2 ** min(attempts, 11))
            values = {
                "status": "dead" if attempts >= 12 else "retry",
                "next_attempt_at": now + timedelta(seconds=delay),
                "updated_at": now,
                "last_error": _clean_text(error, 1000),
            }
        with self.engine.begin() as connection:
            connection.execute(
                update(self.outbox).where(self.outbox.c.event_id == event_id).values(**values)
            )

    def _sync_one(self, row: dict[str, Any], sink: "NotionSink") -> None:
        try:
            sink.create_page(row["target"], row["payload"])
        except Exception as exc:
            self._finish(row["event_id"], error=f"{type(exc).__name__}: {exc}")
        else:
            self._finish(row["event_id"])

    def sync_pending(self, batch_size: int = 8) -> int:
        if not self.enabled or not self._notion_configured():
            return 0
        sink = NotionSink()
        sent = 0
        for _ in range(max(1, min(int(batch_size), 20))):
            row = self._claim_one()
            if row is None:
                break
            self._sync_one(row, sink)
            sent += 1
            # Notion documents an average request rate of roughly three requests/sec.
            time.sleep(0.45)
        return sent

    def start_worker(self) -> None:
        if not self.enabled or not self._notion_configured():
            return
        with self._worker_lock:
            if self._worker_started:
                return
            self._worker_started = True

        def run() -> None:
            while True:
                try:
                    self.sync_pending()
                except Exception as exc:
                    self.error = type(exc).__name__
                time.sleep(5)

        threading.Thread(target=run, daemon=True, name="csr-notion-outbox").start()


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


class NotionSink:
    """Maps stable event payloads to the user-provided Notion database schemas."""

    def __init__(self) -> None:
        self.token = os.getenv("NOTION_API_KEY", "").strip()
        self.database_ids = {
            "logs": os.getenv("NOTION_LOGS_DB_ID", "").strip(),
            "trades": os.getenv("NOTION_TRADES_DB_ID", "").strip(),
            "tokens": os.getenv("NOTION_TOKENS_DB_ID", "").strip(),
        }
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": "Bearer " + self.token,
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._rate_lock = threading.Lock()
        self._last_request_at = 0.0
        self._schema_cache: dict[str, tuple[str, dict[str, Any]]] = {}

    @staticmethod
    def _normal(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        with self._rate_lock:
            delay = 0.4 - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            self._last_request_at = time.monotonic()
        response = self.session.request(
            method, NOTION_BASE_URL + path, json=payload, timeout=(5, 20),
        )
        if response.status_code >= 400:
            body = _clean_text(response.text, 400)
            raise RuntimeError(f"Notion HTTP {response.status_code}: {body}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Notion returned a non-object response")
        return data

    def _schema(self, database_id: str) -> tuple[str, dict[str, Any]]:
        cached = getattr(self, "_schema_cache", {}).get(database_id)
        if cached:
            return cached
        database = self._request("GET", "/databases/" + database_id)
        sources = database.get("data_sources")
        if isinstance(sources, list) and sources:
            source_id = str(sources[0].get("id") or "")
            source = self._request("GET", "/data_sources/" + source_id)
            properties = source.get("properties") or {}
            if not isinstance(properties, dict):
                properties = {}
            result = (source_id, properties)
        else:
            # Compatibility with a legacy database response.
            properties = database.get("properties") or {}
            result = ("", properties if isinstance(properties, dict) else {})
        if not hasattr(self, "_schema_cache"):
            self._schema_cache = {}
        self._schema_cache[database_id] = result
        return result

    @staticmethod
    def _property_value(prop_type: str, value: Any) -> dict | None:
        if value is None:
            return None
        if prop_type == "title":
            return {"title": [{"type": "text", "text": {"content": _clean_text(value, 1800)}}]}
        if prop_type == "rich_text":
            if isinstance(value, (dict, list)):
                value = json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))
            return {"rich_text": [{"type": "text", "text": {"content": _clean_text(value, 1800)}}]}
        if prop_type == "number":
            number = _to_float(value)
            return {"number": number} if number is not None else None
        if prop_type == "checkbox":
            return {"checkbox": bool(value)}
        if prop_type == "date":
            date_value = _iso(value)
            return {"date": {"start": date_value}} if date_value else None
        if prop_type == "select":
            return {"select": {"name": _clean_text(value, 100)}} if str(value).strip() else None
        if prop_type == "status":
            return {"status": {"name": _clean_text(value, 100)}} if str(value).strip() else None
        if prop_type == "multi_select":
            values = value if isinstance(value, list) else [value]
            return {"multi_select": [{"name": _clean_text(item, 100)} for item in values if str(item).strip()]}
        if prop_type == "url":
            return {"url": _clean_text(value, 1800)}
        if prop_type == "email":
            return {"email": _clean_text(value, 320)}
        if prop_type == "phone_number":
            return {"phone_number": _clean_text(value, 64)}
        return None

    def _properties(self, schema: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        properties = {}
        normalized_payload = {self._normal(str(key)): value for key, value in payload.items()}
        aliases = {
            "name": ("message", "symbol", "token_symbol", "trade_id", "event_id", "token_address"),
            "title": ("message", "symbol", "token_symbol", "trade_id", "event_id", "token_address"),
            "timestamp": ("timestamp", "occurred_at", "discovered_at", "observed_at", "closed_at_utc"),
            "created_at": ("timestamp", "occurred_at", "discovered_at", "observed_at"),
            "discovered_at": ("timestamp", "observed_at", "pool_created_at_utc"),
            "pnl": ("pnl_usd", "net_pnl_usd", "gross_pnl_usd"),
            "pnl_percent": ("pnl_pct", "pnl_percent"),
            "volume_24h": ("volume_24h_usd",),
            "liquidity": ("liquidity_usd",),
            "volume_liquidity": ("volume_to_liquidity_24h",),
            "research_score": ("discovery_priority_score",),
            "screening_status": ("candidate_status", "trade_authorization"),
            "status": ("status", "screening_status", "candidate_status"),
            "honeypot_status": ("honeypot_status", "candidate_status"),
            "holders": ("holder_count", "holders_count", "top10_holder_pct"),
            "details_json": ("details", "payload"),
            "security_json": ("security_evidence", "security_json"),
            "execution_json": ("execution", "payload"),
        }
        for name, definition in schema.items():
            if not isinstance(definition, dict):
                continue
            prop_type = definition.get("type")
            if not prop_type:
                continue
            normalized = self._normal(str(name))
            value = normalized_payload.get(normalized)
            if value is None:
                for alias in aliases.get(normalized, ()):
                    if alias in normalized_payload:
                        value = normalized_payload[alias]
                        break
            if prop_type == "title" and value is None:
                value = payload.get("message") or payload.get("symbol") or payload.get("trade_id") or payload.get("event_id") or payload.get("token_address") or "Crypto Super Radar event"
            rendered = self._property_value(prop_type, value)
            if rendered is not None:
                properties[name] = rendered
        if not any(
            isinstance(definition, dict) and definition.get("type") == "title"
            for definition in schema.values()
        ):
            raise RuntimeError("Notion target database has no title property")
        return properties

    def create_page(self, target: str, payload: dict[str, Any]) -> None:
        database_id = self.database_ids.get(target)
        if not database_id:
            raise RuntimeError("Notion database ID is not configured for " + target)
        source_id, schema = self._schema(database_id)
        parent = {"type": "data_source_id", "data_source_id": source_id} if source_id else {"database_id": database_id}
        self._request("POST", "/pages", {
            "parent": parent,
            "properties": self._properties(schema, payload),
        })


_STORE: CloudStore | None = None
_STORE_LOCK = threading.Lock()


def initialize_cloud_store() -> CloudStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = CloudStore()
        store = _STORE
    store.start_worker()
    return store


def get_cloud_store() -> CloudStore:
    return _STORE if _STORE is not None else initialize_cloud_store()


class CloudLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            get_cloud_store().record_log(
                self.format(record), level=record.levelname,
                component=record.name[:100],
            )
        except Exception:
            # Logging must never recurse or take down request handling.
            return


def configure_cloud_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(handler, CloudLogHandler) for handler in root.handlers):
        handler = CloudLogHandler(level=logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
