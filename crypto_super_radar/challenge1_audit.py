"""Append-only, hash-chained evidence for every BloFin Demo Challenge 1 decision."""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

from .database import connect_database, storage_backend


def _json_safe(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Convert evidence to JSON-safe values and redact credential-shaped fields."""
    if depth > 12:
        return "[MAX_DEPTH]"
    lowered = key.casefold()
    if any(part in lowered for part in ("secret", "api_key", "apikey", "token", "authorization", "passphrase", "password")):
        return "[REDACTED]"
    if value is None or isinstance(value, (str, bool, int)):
        return value[:12000] if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            str(k)[:120]: _json_safe(v, key=str(k), depth=depth + 1)
            for k, v in list(value.items())[:2000]
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:2000]]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)[:12000]


def _canonical(value: Any) -> str:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


class Challenge1AuditRecorder:
    """Persist a verifiable append-only evidence chain in the configured shared database."""

    def __init__(self, connection: Any | None = None) -> None:
        self._owns_db = connection is None
        self._db = connection or connect_database()
        self._lock = threading.RLock()
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS csr_challenge1_audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    cycle_id TEXT NOT NULL,
                    experience_id TEXT,
                    position_id TEXT,
                    phase TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    occurred_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_csr_ch1_audit_cycle
                ON csr_challenge1_audit_events(cycle_id, sequence)
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_csr_ch1_audit_links
                ON csr_challenge1_audit_events(experience_id, position_id)
            """)
            self._db.execute("""
                CREATE TRIGGER IF NOT EXISTS csr_ch1_audit_no_update
                BEFORE UPDATE ON csr_challenge1_audit_events
                BEGIN SELECT RAISE(ABORT, 'Challenge 1 audit events are append-only'); END
            """)
            self._db.execute("""
                CREATE TRIGGER IF NOT EXISTS csr_ch1_audit_no_delete
                BEFORE DELETE ON csr_challenge1_audit_events
                BEGIN SELECT RAISE(ABORT, 'Challenge 1 audit events are append-only'); END
            """)

    @staticmethod
    def _event_hash(previous_hash: str, event_id: str, payload_json: str) -> str:
        material = f"{previous_hash}\n{event_id}\n{payload_json}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def append_event(
        self,
        *,
        cycle_id: str,
        phase: str,
        event_key: str,
        details: Mapping[str, Any],
        experience_id: str | None = None,
        position_id: str | None = None,
        occurred_at_utc: str | None = None,
    ) -> dict[str, Any]:
        requested_cycle = str(cycle_id or "").strip()[:180]
        phase_value = str(phase or "").strip().upper()[:80]
        key_value = str(event_key or "").strip()[:240]
        if not requested_cycle or not phase_value or not key_value:
            raise ValueError("cycle_id, phase and event_key are required")
        if not isinstance(details, Mapping):
            raise TypeError("audit details must be a mapping")

        timestamp = occurred_at_utc or datetime.now(timezone.utc).isoformat()
        experience = str(experience_id or "").strip()[:180] or None
        position = str(position_id or "").strip()[:180] or None

        with self._lock, self._db:
            # The position manager may only know the stable experience/position ID.
            parent = self._db.execute(
                """SELECT cycle_id FROM csr_challenge1_audit_events
                   WHERE experience_id=? OR position_id=? OR cycle_id=?
                   ORDER BY sequence ASC LIMIT 1""",
                (requested_cycle, requested_cycle, requested_cycle),
            ).fetchone()
            canonical_cycle = str(parent["cycle_id"]) if parent else requested_cycle
            identity = hashlib.sha256(
                f"{canonical_cycle}|{phase_value}|{key_value}".encode("utf-8")
            ).hexdigest()
            event_id = "ch1audit-" + identity
            payload = {
                "environment": "BLOFIN_DEMO",
                "market_data": "LIVE_MARKET_READ_ONLY",
                "cycle_id": canonical_cycle,
                "experience_id": experience,
                "position_id": position,
                "phase": phase_value,
                "event_key": key_value,
                "occurred_at_utc": timestamp,
                "details": _json_safe(details),
            }
            payload_json = _canonical(payload)
            existing = self._db.execute(
                """SELECT sequence, event_id, cycle_id, experience_id, position_id,
                          phase, event_key, occurred_at_utc, payload_json,
                          previous_hash, event_hash
                   FROM csr_challenge1_audit_events WHERE event_id=?""",
                (event_id,),
            ).fetchone()
            inserted = existing is None
            if existing is None:
                latest = self._db.execute(
                    """SELECT event_hash FROM csr_challenge1_audit_events
                       WHERE cycle_id=? ORDER BY sequence DESC LIMIT 1""",
                    (canonical_cycle,),
                ).fetchone()
                previous_hash = str(latest["event_hash"]) if latest else "GENESIS"
                event_hash = self._event_hash(previous_hash, event_id, payload_json)
                self._db.execute(
                    """INSERT OR IGNORE INTO csr_challenge1_audit_events
                       (event_id, cycle_id, experience_id, position_id, phase, event_key,
                        occurred_at_utc, payload_json, previous_hash, event_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id, canonical_cycle, experience, position, phase_value,
                        key_value, timestamp, payload_json, previous_hash, event_hash,
                    ),
                )
                existing = self._db.execute(
                    """SELECT sequence, event_id, cycle_id, experience_id, position_id,
                              phase, event_key, occurred_at_utc, payload_json,
                              previous_hash, event_hash
                       FROM csr_challenge1_audit_events WHERE event_id=?""",
                    (event_id,),
                ).fetchone()
        if existing is None:
            raise RuntimeError("Challenge 1 audit append could not be confirmed")
        item = dict(existing)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["inserted"] = inserted
        return item

    def list_related(self, reference: str, *, limit: int = 500) -> dict[str, Any]:
        ref = str(reference or "").strip()[:240]
        bounded = max(1, min(int(limit), 2000))
        if not ref:
            raise ValueError("audit reference is required")
        seeds = self._db.execute(
            """SELECT cycle_id, experience_id, position_id
               FROM csr_challenge1_audit_events
               WHERE cycle_id=? OR experience_id=? OR position_id=?
               ORDER BY sequence ASC""",
            (ref, ref, ref),
        ).fetchall()
        cycles = {str(row["cycle_id"]) for row in seeds}
        experiences = {str(row["experience_id"]) for row in seeds if row["experience_id"]}
        positions = {str(row["position_id"]) for row in seeds if row["position_id"]}
        if not cycles:
            return {"status": "NOT_FOUND", "reference": ref, "events": [], "integrity": {"status": "NOT_FOUND"}}
        clauses = []
        args: list[Any] = []
        for column, values in (("cycle_id", cycles), ("experience_id", experiences), ("position_id", positions)):
            if values:
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                args.extend(sorted(values))
        rows = self._db.execute(
            """SELECT sequence, event_id, cycle_id, experience_id, position_id,
                      phase, event_key, occurred_at_utc, payload_json,
                      previous_hash, event_hash
               FROM csr_challenge1_audit_events
               WHERE """ + " OR ".join(clauses) + " ORDER BY sequence ASC LIMIT ?",
            (*args, bounded),
        ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            events.append(item)
        return {
            "status": "RECORDED", "reference": ref,
            "cycle_ids": sorted(cycles), "event_count": len(events),
            "events": events, "integrity": self.verify_integrity(sorted(cycles)),
        }

    def verify_integrity(self, cycle_ids: list[str] | None = None) -> dict[str, Any]:
        if cycle_ids:
            clauses = f"cycle_id IN ({','.join('?' for _ in cycle_ids)})"
            rows = self._db.execute(
                """SELECT cycle_id, event_id, payload_json, previous_hash, event_hash
                   FROM csr_challenge1_audit_events WHERE """ + clauses + " ORDER BY sequence ASC",
                tuple(cycle_ids),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT cycle_id, event_id, payload_json, previous_hash, event_hash
                   FROM csr_challenge1_audit_events ORDER BY sequence ASC"""
            ).fetchall()
        previous: dict[str, str] = {}
        checked = 0
        for row in rows:
            cycle = str(row["cycle_id"])
            expected_previous = previous.get(cycle, "GENESIS")
            if str(row["previous_hash"]) != expected_previous:
                return {"status": "FAILED", "checked_events": checked, "failed_event_id": row["event_id"]}
            expected_hash = self._event_hash(expected_previous, str(row["event_id"]), str(row["payload_json"]))
            if expected_hash != str(row["event_hash"]):
                return {"status": "FAILED", "checked_events": checked, "failed_event_id": row["event_id"]}
            previous[cycle] = expected_hash
            checked += 1
        return {"status": "PASS", "checked_events": checked, "cycles_checked": len(previous)}

    def status_snapshot(self) -> dict[str, Any]:
        row = self._db.execute(
            """SELECT COUNT(*) AS total, COUNT(DISTINCT cycle_id) AS cycles,
                      MAX(occurred_at_utc) AS latest_event_utc
               FROM csr_challenge1_audit_events"""
        ).fetchone()
        latest = self._db.execute(
            """SELECT cycle_id, phase, event_id FROM csr_challenge1_audit_events
               ORDER BY sequence DESC LIMIT 1"""
        ).fetchone()
        return {
            "status": "READY", "storage_backend": storage_backend(),
            "append_only": True, "hash_chained": True,
            "event_count": int(row["total"] or 0),
            "cycle_count": int(row["cycles"] or 0),
            "latest_event_utc": row["latest_event_utc"],
            "latest_cycle_id": latest["cycle_id"] if latest else None,
            "latest_phase": latest["phase"] if latest else None,
        }

    def close(self) -> None:
        if self._owns_db:
            self._db.close()
