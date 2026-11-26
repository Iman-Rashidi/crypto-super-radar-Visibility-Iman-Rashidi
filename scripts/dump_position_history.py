#!/usr/bin/env python3
"""Read-only BloFin Demo position-history dump for selected position IDs.

The script performs GET requests only. It never submits orders or writes to a
database. Position-to-instrument mapping can be supplied with --map; otherwise
it reads existing mappings from configured Turso storage using SELECT only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from crypto_super_radar.blofin_demo_readonly import BloFinDemoReadOnly
from crypto_super_radar.database import connect_database

POSITION_IDS = (
    "1000000995117",
    "1000000995119",
    "1000000995121",
)
HISTORY_STATES = ("closed", "partially_closed")
SENSITIVE_KEY = re.compile(
    r"(?:key|secret|token|passphrase|sign)", re.IGNORECASE
)


def normalize_id(value: Any) -> str:
    """Normalize integer-like IDs without losing precision through float."""
    if value is None or isinstance(value, bool):
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    try:
        number = Decimal(raw)
        if number.is_finite() and number == number.to_integral_value():
            return str(number.quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        pass
    return raw


def normalize_rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("data", value)
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def redact(value: Any) -> Any:
    """Recursively remove values whose field names resemble credentials."""
    if isinstance(value, Mapping):
        return {
            str(key): redact(item)
            for key, item in value.items()
            if not SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def parse_mapping(values: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"INVALID_MAP_ENTRY:{item.split('=')[0][:32]}")
        position_id, inst_id = item.split("=", 1)
        position_id = normalize_id(position_id)
        inst_id = inst_id.strip().upper()
        if not position_id or not inst_id:
            raise ValueError("INVALID_MAP_ENTRY")
        if position_id not in POSITION_IDS:
            raise ValueError("UNEXPECTED_POSITION_ID")
        if not re.fullmatch(r"[A-Z0-9_-]{3,40}", inst_id):
            raise ValueError("INVALID_INSTRUMENT_ID")
        mapping[position_id] = inst_id

    missing = [item for item in POSITION_IDS if item not in mapping]
    if missing:
        raise ValueError("MISSING_MAP_IDS:" + ",".join(missing))
    return mapping


def mapping_from_turso() -> dict[str, str]:
    """Read mappings from configured Turso only; never create a local DB."""
    url = os.getenv("TURSO_DATABASE_URL", "").strip()
    token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        raise RuntimeError(
            "TURSO_NOT_CONFIGURED: supply all three --map entries or configure "
            "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN"
        )

    db = connect_database()
    try:
        mapping: dict[str, str] = {}
        for position_id in POSITION_IDS:
            rows = db.execute(
                """
                SELECT DISTINCT instrument_id
                FROM blofin_demo_orders
                WHERE position_id = ?
                """,
                (position_id,),
            ).fetchall()
            instruments = {
                str(row["instrument_id"]).strip().upper()
                for row in rows
                if row["instrument_id"]
            }
            if len(instruments) != 1:
                raise RuntimeError(
                    "POSITION_MAPPING_UNAVAILABLE:" + position_id
                )
            mapping[position_id] = instruments.pop()
        return mapping
    finally:
        db.close()


def collect(
    client: BloFinDemoReadOnly,
    mapping: dict[str, str],
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "schema_version": 2,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": "BLOFIN_DEMO_READ_ONLY",
        "positions": [],
        "funding_rate_context": [],
        "field_names_seen": [],
        "warnings": [],
        "limitations": [
            "Position history is requested with a maximum of 100 rows per "
            "instrument and state; older rows may be outside the returned window.",
            "Funding-rate history is context only and is not proof of an "
            "actual funding payment.",
            "Actual funding is reported only when a funding field is present "
            "in a matched position-history row.",
        ],
    }

    all_field_names: set[str] = set()
    instruments = sorted(set(mapping.values()))

    for position_id in POSITION_IDS:
        inst_id = mapping[position_id]
        position_result: dict[str, Any] = {
            "position_id": position_id,
            "inst_id": inst_id,
            "history": {},
        }

        for requested_state in HISTORY_STATES:
            rows = normalize_rows(
                client.get_positions_history(
                    inst_id=inst_id,
                    state=requested_state,
                    limit=100,
                )
            )

            for row in rows:
                all_field_names.update(str(key) for key in row.keys())

            matched: list[Mapping[str, Any]] = []
            rejected: list[Mapping[str, Any]] = []

            for row in rows:
                same_position = (
                    normalize_id(row.get("positionId"))
                    == normalize_id(position_id)
                )
                same_instrument = (
                    str(row.get("instId") or "").strip().upper()
                    == inst_id
                )
                if same_position and same_instrument:
                    matched.append(row)
                else:
                    rejected.append(row)

            # Preserve the API's newest-first ordering and expose at most
            # three nonmatching rows so unexpected ID formats are inspectable.
            rejected_sample = rejected[:3]

            position_result["history"][requested_state] = {
                "requested_state": requested_state,
                "returned_count": len(rows),
                "matched_count": len(matched),
                "matched_rows": [redact(row) for row in matched],
                "rejected_sample_count": len(rejected_sample),
                "rejected_sample": [
                    redact(row) for row in rejected_sample
                ],
            }

            if len(rows) == 100:
                output["warnings"].append({
                    "code": "PAGINATION_LIMIT_POSSIBLE",
                    "inst_id": inst_id,
                    "requested_state": requested_state,
                    "returned_count": 100,
                })

        output["positions"].append(position_result)

    for inst_id in instruments:
        try:
            rows = normalize_rows(
                client.get_funding_rate_history(
                    inst_id=inst_id,
                    limit=100,
                )
            )
            for row in rows:
                all_field_names.update(str(key) for key in row.keys())

            output["funding_rate_context"].append({
                "inst_id": inst_id,
                "label": "RATE_CONTEXT_NOT_ACTUAL_SETTLEMENT",
                "returned_count": len(rows),
                "rows": [redact(row) for row in rows],
            })
            if len(rows) == 100:
                output["warnings"].append({
                    "code": "PAGINATION_LIMIT_POSSIBLE",
                    "inst_id": inst_id,
                    "requested_state": "funding_rate_context",
                    "returned_count": 100,
                })
        except Exception as exc:
            output["funding_rate_context"].append({
                "inst_id": inst_id,
                "label": "RATE_CONTEXT_NOT_ACTUAL_SETTLEMENT",
                "status": "UNAVAILABLE",
                "error_type": type(exc).__name__,
            })

    output["field_names_seen"] = sorted(all_field_names)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump BloFin Demo position history without trading or DB writes."
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="POSITION_ID=INST_ID",
        help="Repeat three times, e.g. --map 1000000995117=BTC-USDT",
    )
    args = parser.parse_args()

    try:
        mapping = parse_mapping(args.map) if args.map else mapping_from_turso()
        client = BloFinDemoReadOnly()
        result = collect(client, mapping)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        reason = str(exc)
        safe_reason = reason if re.fullmatch(
            r"(?:TURSO_NOT_CONFIGURED|POSITION_MAPPING_UNAVAILABLE:[0-9]+|"
            r"MISSING_MAP_IDS:[0-9,]+|INVALID_MAP_ENTRY|UNEXPECTED_POSITION_ID|"
            r"INVALID_INSTRUMENT_ID)", reason
        ) else "READ_ONLY_COLLECTION_FAILED"
        print(json.dumps({
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "reason": safe_reason,
        }, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
