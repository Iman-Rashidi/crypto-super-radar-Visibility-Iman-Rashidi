"""Append-only learning journal for paper/replay decisions and supervised lessons.

This is an auditable memory store, not an online self-modifying model. The
runtime may record observations; only explicitly validated lessons are exposed
as reusable knowledge. Secrets and account credentials are stripped recursively.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .database import connect_database, storage_backend

_ALLOWED_STAGES = {"PAPER_CYCLE", "BOT_ONLY", "BOT_WITH_AI_MENTOR", "BACKTEST", "REPLAY"}
_ALLOWED_ENGINES = {
    "LEGACY_SPOT_PAPER", "LEGACY_FUTURES_PAPER",
    "AGGRESSIVE_HIGH_RISK", "STRICT_12_GATE",
}
_SECRET_WORDS = ("api_key", "secret", "token", "password", "authorization", "signature", "private_key")


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (str, bool, int)):
        return value[:2000] if isinstance(value, str) else value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        clean = {}
        for key, item in list(value.items())[:500]:
            name = str(key)[:120]
            if any(word in name.lower() for word in _SECRET_WORDS):
                continue
            clean[name] = _json_safe(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in value[:500]]
    if hasattr(value, "value"):
        return _json_safe(getattr(value, "value"), depth + 1)
    return str(value)[:500]


def _encode(value: Any, *, max_bytes: int = 250_000) -> str:
    payload = json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    if len(payload.encode("utf-8")) > max_bytes:
        raise ValueError("Learning-memory record exceeds the size limit.")
    return payload


def _snapshot_hash(snapshot_json: str) -> str:
    return hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()


class LearningMemory:
    """SQLite/Turso-backed append-only episode journal with validated lessons."""

    def __init__(self, path: str | None = None):
        selected = path or os.getenv("CSR_LEARNING_DB_PATH") or os.getenv(
            "CSR_DB_PATH", "data/crypto_super_radar.sqlite3"
        )
        self._db = connect_database(selected)
        self._closed = False
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS learning_episodes (
                episode_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                engine_id TEXT NOT NULL,
                protocol_stage TEXT NOT NULL,
                asset TEXT,
                market_type TEXT,
                timeframe TEXT,
                observed_at_ms INTEGER NOT NULL,
                recorded_at_ms INTEGER NOT NULL,
                snapshot_hash TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                model_version TEXT,
                outcome_json TEXT,
                outcome_recorded_at_ms INTEGER,
                UNIQUE(case_id, engine_id, protocol_stage)
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_learning_episodes_recent
            ON learning_episodes(recorded_at_ms DESC)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_learning_episodes_engine
            ON learning_episodes(engine_id, protocol_stage, recorded_at_ms DESC)
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS learning_lessons (
                lesson_id TEXT PRIMARY KEY,
                source_case_id TEXT NOT NULL,
                title TEXT NOT NULL,
                condition_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                application_scope TEXT NOT NULL,
                confidence REAL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','VALIDATED','REJECTED')),
                created_at_ms INTEGER NOT NULL,
                validated_at_ms INTEGER
            )
        """)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LearningMemory is closed.")

    def record_episode(
        self, *, case_id: str, engine_id: str, protocol_stage: str,
        snapshot: dict[str, Any], decision: dict[str, Any],
        asset: str | None = None, market_type: str | None = None,
        timeframe: str | None = None, observed_at_ms: int | None = None,
        model_version: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_open()
        case_id = str(case_id).strip()
        engine_id = str(engine_id).strip().upper()
        stage = str(protocol_stage).strip().upper()
        if not case_id or len(case_id) > 128:
            raise ValueError("case_id must contain 1 to 128 characters.")
        if engine_id not in _ALLOWED_ENGINES:
            raise ValueError("Unknown engine_id; preserve engine attribution explicitly.")
        if stage not in _ALLOWED_STAGES:
            raise ValueError("Unsupported learning protocol stage.")
        if not isinstance(snapshot, dict) or not isinstance(decision, dict):
            raise ValueError("snapshot and decision must be JSON objects.")
        snapshot_json = _encode(snapshot)
        decision_json = _encode(decision, max_bytes=100_000)
        digest = _snapshot_hash(snapshot_json)
        now = int(time.time() * 1000)
        observed = now if observed_at_ms is None else int(observed_at_ms)
        if observed <= 0 or observed > now + 60_000:
            raise ValueError("observed_at_ms is invalid or too far in the future.")
        if stage == "BOT_WITH_AI_MENTOR":
            baseline = self._db.execute(
                """SELECT snapshot_hash, observed_at_ms FROM learning_episodes
                   WHERE case_id=? AND engine_id=? AND protocol_stage='BOT_ONLY'""",
                (case_id, engine_id),
            ).fetchone()
            if baseline is None:
                raise ValueError("BOT_ONLY baseline must be recorded before AI-mentor review.")
            if baseline["snapshot_hash"] != digest or int(baseline["observed_at_ms"]) != observed:
                raise ValueError("BOT_WITH_AI_MENTOR must use the exact frozen BOT_ONLY snapshot.")
        episode_id = uuid.uuid4().hex
        with self._db:
            self._db.execute(
                """INSERT INTO learning_episodes
                (episode_id,case_id,engine_id,protocol_stage,asset,market_type,timeframe,
                 observed_at_ms,recorded_at_ms,snapshot_hash,snapshot_json,decision_json,model_version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (episode_id, case_id, engine_id, stage, asset, market_type, timeframe,
                 observed, now, digest, snapshot_json, decision_json,
                 str(model_version)[:120] if model_version else None),
            )
        return {"episode_id": episode_id, "case_id": case_id, "engine_id": engine_id,
                "protocol_stage": stage, "snapshot_hash": digest, "recorded_at_ms": now}

    def record_paper_cycle(
        self, *, engine_id: str, asset: str, cycle: dict[str, Any],
        model_version: str | None = None,
    ) -> dict[str, Any]:
        """Record a decision summary. It deliberately does not pretend to be raw OHLCV."""
        now = int(time.time() * 1000)
        safe_cycle = _json_safe(cycle)
        snapshot = {
            "snapshot_kind": "PAPER_CYCLE_DECISION_SUMMARY",
            "observed_at_ms": now,
            "asset": str(asset)[:80],
            "cycle": safe_cycle,
        }
        outcome = safe_cycle.get("trade") if isinstance(safe_cycle, dict) else None
        decision = {"cycle": safe_cycle, "outcome_at_close": outcome}
        return self.record_episode(
            case_id=f"paper:{uuid.uuid4().hex}", engine_id=engine_id,
            protocol_stage="PAPER_CYCLE", snapshot=snapshot, decision=decision,
            asset=str(asset)[:80], market_type="SPOT" if engine_id == "LEGACY_SPOT_PAPER" else "PERPETUAL",
            observed_at_ms=now, model_version=model_version,
        )

    def complete_episode(self, episode_id: str, outcome: dict[str, Any]) -> bool:
        self._ensure_open()
        payload = _encode(outcome, max_bytes=100_000)
        now = int(time.time() * 1000)
        with self._db:
            cursor = self._db.execute(
                """UPDATE learning_episodes
                   SET outcome_json=?, outcome_recorded_at_ms=?
                   WHERE episode_id=? AND outcome_json IS NULL""",
                (payload, now, str(episode_id)),
            )
            return cursor.rowcount == 1

    def add_lesson(
        self, *, source_case_id: str, title: str, condition: dict[str, Any],
        evidence: dict[str, Any], root_cause: str, application_scope: str,
        confidence: float | None = None, status: str = "PENDING",
    ) -> str:
        self._ensure_open()
        state = str(status).upper()
        if state not in {"PENDING", "VALIDATED", "REJECTED"}:
            raise ValueError("Lesson status must be PENDING, VALIDATED or REJECTED.")
        score = None if confidence is None else float(confidence)
        if score is not None and (not 0 <= score <= 1):
            raise ValueError("Lesson confidence must be between 0 and 1.")
        required = (source_case_id, title, root_cause, application_scope)
        if any(not str(item).strip() for item in required):
            raise ValueError("Lesson source, title, root cause and scope are required.")
        lesson_id = uuid.uuid4().hex
        now = int(time.time() * 1000)
        with self._db:
            self._db.execute(
                """INSERT INTO learning_lessons
                (lesson_id,source_case_id,title,condition_json,evidence_json,root_cause,
                 application_scope,confidence,status,created_at_ms,validated_at_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (lesson_id, str(source_case_id)[:128], str(title)[:240],
                 _encode(condition, max_bytes=50_000), _encode(evidence, max_bytes=50_000),
                 str(root_cause)[:2000], str(application_scope)[:1000], score, state,
                 now, now if state == "VALIDATED" else None),
            )
        return lesson_id

    def validate_lesson(self, lesson_id: str, *, accepted: bool) -> bool:
        self._ensure_open()
        now = int(time.time() * 1000)
        state = "VALIDATED" if accepted else "REJECTED"
        with self._db:
            cursor = self._db.execute(
                """UPDATE learning_lessons SET status=?, validated_at_ms=?
                   WHERE lesson_id=? AND status='PENDING'""",
                (state, now, str(lesson_id)),
            )
            return cursor.rowcount == 1

    def list_episodes(self, limit: int = 25) -> list[dict[str, Any]]:
        self._ensure_open()
        bounded = max(1, min(int(limit), 100))
        rows = self._db.execute(
            """SELECT episode_id,case_id,engine_id,protocol_stage,asset,market_type,
                      timeframe,observed_at_ms,recorded_at_ms,snapshot_hash,
                      snapshot_json,decision_json,model_version,outcome_json
               FROM learning_episodes ORDER BY recorded_at_ms DESC LIMIT ?""",
            (bounded,),
        ).fetchall()
        return [{
            **{key: row[key] for key in (
                "episode_id","case_id","engine_id","protocol_stage","asset",
                "market_type","timeframe","observed_at_ms","recorded_at_ms",
                "snapshot_hash","model_version"
            )},
            "snapshot": json.loads(row["snapshot_json"]),
            "decision": json.loads(row["decision_json"]),
            "outcome": json.loads(row["outcome_json"]) if row["outcome_json"] else None,
        } for row in rows]

    def status(self) -> dict[str, Any]:
        self._ensure_open()
        episodes = int(self._db.execute("SELECT COUNT(*) FROM learning_episodes").fetchone()[0])
        lessons = int(self._db.execute("SELECT COUNT(*) FROM learning_lessons").fetchone()[0])
        validated = int(self._db.execute(
            "SELECT COUNT(*) FROM learning_lessons WHERE status='VALIDATED'"
        ).fetchone()[0])
        stages = self._db.execute(
            "SELECT engine_id, protocol_stage, COUNT(*) AS count FROM learning_episodes "
            "GROUP BY engine_id, protocol_stage ORDER BY engine_id, protocol_stage"
        ).fetchall()
        backend = storage_backend()
        on_render = bool(os.getenv("RENDER_GIT_COMMIT") or os.getenv("RENDER_SERVICE_ID"))
        persistence = (
            "CLOUD_PERSISTENT" if backend == "TURSO_CLOUD_SQLITE"
            else "RENDER_EPHEMERAL" if on_render
            else "LOCAL_FILE"
        )
        return {
            "status": "AVAILABLE",
            "storage_backend": backend,
            "persistence": persistence,
            "episodes_total": episodes,
            "lessons_total": lessons,
            "lessons_validated": validated,
            "lessons_pending": lessons - validated - int(self._db.execute(
                "SELECT COUNT(*) FROM learning_lessons WHERE status='REJECTED'"
            ).fetchone()[0]),
            "engine_stage_counts": [dict(row) for row in stages],
            "self_modifying_code": False,
            "model_training_active": False,
            "note": (
                "Turso cloud storage is configured."
                if persistence == "CLOUD_PERSISTENT"
                else "On Render Free, local SQLite can be lost on restart/redeploy; configure both Turso secrets for durable memory."
                if persistence == "RENDER_EPHEMERAL"
                else "Local SQLite file storage is active."
            ),
        }

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True
