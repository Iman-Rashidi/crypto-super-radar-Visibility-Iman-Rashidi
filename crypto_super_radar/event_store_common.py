"""Shared hashing, canonicalization, timestamp and error helpers."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any

class IdempotencyViolationError(ValueError):
    """An intent or event identifier was reused with different immutable data."""


class EventChainIntegrityError(ValueError):
    """The persisted event history cannot be trusted or replayed safely."""


class ConcurrentStateChangeError(RuntimeError):
    """The intent changed while an event was being committed."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be valid finite JSON data") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_timestamp(value: str | None = None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("timestamp_utc must be a non-empty ISO-8601 string")
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("timestamp_utc must be a valid ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp_utc must include a timezone")
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()
