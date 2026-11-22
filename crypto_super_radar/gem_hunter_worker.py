"""GitHub Actions entrypoint for the read-only Gem Hunter V3 worker."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any

from .database import connect_database, storage_backend


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _save_report(payload: dict[str, Any]) -> None:
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("Turso credentials are required; refusing local-only scan persistence.")
    db = connect_database()
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS gem_hunter_scan_reports (
                report_id TEXT PRIMARY KEY,
                generated_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                candidate_count INTEGER NOT NULL,
                tier1_count INTEGER NOT NULL,
                tier2_count INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            )""")
            db.execute(
                """INSERT INTO gem_hunter_scan_reports
                (report_id, generated_at_utc, status, candidate_count, tier1_count, tier2_count, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["report_id"], payload["generated_at_utc"], payload.get("status", "UNKNOWN"),
                    int(payload.get("discovered_candidate_count", 0)),
                    int(payload.get("gem_hunter_v3_tier1_count", 0)),
                    int(payload.get("gem_hunter_v3_tier2_count", 0)),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
    finally:
        db.close()


def latest_report_from_turso() -> dict[str, Any]:
    """Read the latest completed worker snapshot; never call DEX APIs from Render."""
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        return {"status": "UNAVAILABLE", "message_fa": "اتصال پایدار Turso تنظیم نشده است.", "candidates": [], "trade_authorization": "NO_TRADE"}
    db = connect_database()
    try:
        try:
            row = db.execute(
                "SELECT payload_json FROM gem_hunter_scan_reports ORDER BY generated_at_utc DESC LIMIT 1"
            ).fetchone()
        except Exception as exc:
            if "no such table" in str(exc).lower():
                return {"status": "NOT_SCANNED", "message_fa": "هنوز اسکن GitHub Actions ثبت نشده است.", "candidates": [], "trade_authorization": "NO_TRADE"}
            raise
        if not row:
            return {"status": "NOT_SCANNED", "message_fa": "هنوز گزارشی در Turso ثبت نشده است.", "candidates": [], "trade_authorization": "NO_TRADE"}
        raw = row[0] if not isinstance(row, dict) else row.get("payload_json")
        payload = json.loads(raw)
        payload["report_source"] = "TURSO_GITHUB_ACTIONS_WORKER"
        payload["refresh_policy"] = "READ_CACHED_ONLY"
        return payload
    finally:
        db.close()


def _resolve_telegram_recipient() -> str:
    """Resolve the already-authorized @Germany004 private recipient via getUpdates."""
    explicit = (os.getenv("GEM_HUNTER_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if explicit:
        return explicit
    token = (os.getenv("GEM_HUNTER_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Telegram bot token is missing.")
    db = connect_database()
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS gem_hunter_telegram_recipients (
                username TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                verified_at_utc TEXT NOT NULL
            )""")
            saved = db.execute(
                "SELECT chat_id FROM gem_hunter_telegram_recipients WHERE username=?",
                ("Germany004",),
            ).fetchone()
            if saved:
                return str(saved[0] if not isinstance(saved, dict) else saved.get("chat_id"))
    finally:
        db.close()

    import requests
    response = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"limit": 100, "allowed_updates": json.dumps(["message", "my_chat_member"])},
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError("Telegram getUpdates did not return ok=true.")
    matches = []
    for update in body.get("result") or []:
        message = update.get("message") or update.get("my_chat_member") or {}
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        if str(sender.get("username") or "").lstrip("@").lower() == "germany004" and chat.get("type") == "private":
            matches.append((str(chat.get("id")), str(sender.get("id"))))
    if not matches:
        raise RuntimeError("No private Telegram update from the authorized @Germany004 account was found; no recipient was guessed.")
    chat_id, user_id = matches[-1]
    db = connect_database()
    try:
        with db:
            db.execute(
                """INSERT INTO gem_hunter_telegram_recipients (username, chat_id, user_id, verified_at_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET chat_id=excluded.chat_id,
                user_id=excluded.user_id, verified_at_utc=excluded.verified_at_utc""",
                ("Germany004", chat_id, user_id, _utc_now()),
            )
    finally:
        db.close()
    return chat_id


def run_scan(limit: int = 50) -> dict[str, Any]:
    required = ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN")
    missing = [key for key in required if not os.getenv(key, "").strip()]
    if missing:
        raise RuntimeError("Missing required secrets: " + ", ".join(missing))
    token = (os.getenv("GEM_HUNTER_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Telegram bot token is missing.")
    os.environ["GEM_HUNTER_TELEGRAM_BOT_TOKEN"] = token
    os.environ["GEM_HUNTER_TELEGRAM_CHAT_ID"] = _resolve_telegram_recipient()

    # Importing the established V3 pipeline keeps its exact discovery, cross-source
    # reconciliation, security, technical, trade-flow, tier and alert rules.
    from . import web_app

    payload = web_app._gem_hunter_discovery_payload(limit=limit, include_diagnostics=True)
    rows = payload.get("gem_hunter_v3_candidates") or payload.get("diagnostic_candidates") or []
    if not isinstance(rows, list):
        raise RuntimeError("Gem Hunter V3 did not return a candidate list.")
    if payload.get("status") not in {"PASS", "PARTIAL"}:
        raise RuntimeError("Discovery source did not return a usable scan: " + str(payload.get("status")))
    failed = [row for row in rows if row.get("gem_hunter_storage_status") not in {"PERSISTED"}]
    if failed:
        raise RuntimeError(f"{len(failed)} V3 candidates were not confirmed persisted to Turso.")
    payload = dict(payload)
    payload["report_id"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    payload["generated_at_utc"] = payload.get("generated_at_utc") or _utc_now()
    payload["candidates"] = rows
    payload["trade_authorization"] = "NO_TRADE"
    payload["read_only"] = True
    payload["report_source"] = "GITHUB_ACTIONS_WORKER"
    _save_report(payload)
    return {
        "status": "OK",
        "report_id": payload["report_id"],
        "discovered": payload.get("discovered_candidate_count", len(rows)),
        "tier1": payload.get("gem_hunter_v3_tier1_count", 0),
        "tier2": payload.get("gem_hunter_v3_tier2_count", 0),
        "persisted_candidates": len(rows),
        "telegram_sent": sum(1 for row in rows if (row.get("telegram_alert_status") or {}).get("status") == "SENT"),
        "trade_authorization": "NO_TRADE",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run read-only Gem Hunter V3 from GitHub Actions.")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(run_scan(max(1, min(args.limit, 50))), ensure_ascii=False))


if __name__ == "__main__":
    main()
