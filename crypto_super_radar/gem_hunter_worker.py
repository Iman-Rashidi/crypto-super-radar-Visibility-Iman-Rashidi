"""GitHub Actions entrypoint for the read-only Gem Hunter V3 worker."""
from __future__ import annotations

import argparse
import json
import os
import threading
from urllib.parse import urlencode
from datetime import datetime, timezone
from typing import Any

from .database import connect_database, storage_backend


_TURSO_READ_CREDENTIALS: dict[str, str] = {}
_TURSO_READ_CREDENTIALS_LOCK = threading.Lock()
GEM_HUNTER_RENDER_URL = "https://crypto-super-radar-unlimited-demo-staging.onrender.com"
GITHUB_REPOSITORY = "Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi"
GITHUB_WORKFLOW_PATH = ".github/workflows/gem-hunter-v3-live-worker.yml"
GITHUB_ALLOWED_REFS = {
    "refs/heads/main",
    "refs/heads/staging/challenge1-integration-2026-11-16",
}


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
    """Read the latest completed worker snapshot directly from Turso, never DEX APIs."""
    with _TURSO_READ_CREDENTIALS_LOCK:
        cached_url = _TURSO_READ_CREDENTIALS.get("url")
        cached_token = _TURSO_READ_CREDENTIALS.get("token")
    if cached_url and cached_token:
        db = connect_database(url=cached_url, token=cached_token)
    else:
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            return {"status": "UNAVAILABLE", "message_fa": "اتصال امن Turso هنوز از GitHub Actions دریافت نشده است.", "candidates": [], "trade_authorization": "NO_TRADE"}
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


def cache_turso_credentials_from_github_actions(
    oidc_token: str, database_url: str, auth_token: str,
) -> dict[str, Any]:
    """Verify GitHub Actions OIDC and keep Turso read credentials in process memory only."""
    if not isinstance(database_url, str) or not database_url.startswith("libsql://"):
        raise ValueError("Only the configured libsql:// Turso endpoint is accepted.")
    if not isinstance(auth_token, str) or len(auth_token.strip()) < 20:
        raise ValueError("A valid Turso auth token is required.")
    try:
        import jwt
        import requests
        jwks = jwt.PyJWKClient("https://token.actions.githubusercontent.com/.well-known/jwks")
        signing_key = jwks.get_signing_key_from_jwt(oidc_token).key
        claims = jwt.decode(
            oidc_token, signing_key, algorithms=["RS256"],
            audience="gem-hunter-v3-render",
            issuer="https://token.actions.githubusercontent.com",
        )
    except Exception as exc:
        raise PermissionError("GitHub Actions OIDC verification failed.") from exc

    workflow_ref = str(claims.get("workflow_ref") or "")
    expected_prefix = f"{GITHUB_REPOSITORY}/{GITHUB_WORKFLOW_PATH}@"
    if (
        claims.get("repository") != GITHUB_REPOSITORY
        or claims.get("ref") not in GITHUB_ALLOWED_REFS
        or not workflow_ref.startswith(expected_prefix)
        or workflow_ref.rsplit("@", 1)[-1] not in GITHUB_ALLOWED_REFS
        or claims.get("event_name") not in {"push", "schedule", "workflow_dispatch"}
    ):
        raise PermissionError("OIDC token is not from the authorized Gem Hunter V3 workflow.")

    with _TURSO_READ_CREDENTIALS_LOCK:
        _TURSO_READ_CREDENTIALS["url"] = database_url.strip()
        _TURSO_READ_CREDENTIALS["token"] = auth_token.strip()
        _TURSO_READ_CREDENTIALS["verified_at_utc"] = _utc_now()

    report = latest_report_from_turso()
    if report.get("status") in {"UNAVAILABLE", "NOT_SCANNED"}:
        raise RuntimeError("Turso credentials were verified but no saved Gem Hunter report is readable.")
    return {
        "status": "OK",
        "report_id": report.get("report_id"),
        "report_source": report.get("report_source"),
        "discovered": report.get("discovered_candidate_count", 0),
        "tier1": report.get("gem_hunter_v3_tier1_count", 0),
        "tier2": report.get("gem_hunter_v3_tier2_count", 0),
        "trade_authorization": "NO_TRADE",
    }


def _publish_turso_credentials_to_render() -> dict[str, Any]:
    """Use GitHub OIDC to let Render read Turso without storing secrets in Render env."""
    import requests
    request_url = os.getenv("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    request_token = os.getenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    database_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if not all((request_url, request_token, database_url, auth_token)):
        raise RuntimeError("GitHub OIDC or Turso credentials are unavailable to the worker.")
    separator = "&" if "?" in request_url else "?"
    oidc_response = requests.get(
        request_url + separator + urlencode({"audience": "gem-hunter-v3-render"}),
        headers={"Authorization": f"Bearer {request_token}"},
        timeout=15,
    )
    oidc_response.raise_for_status()
    oidc_token = oidc_response.json().get("value")
    if not oidc_token:
        raise RuntimeError("GitHub did not issue a dashboard OIDC token.")
    response = requests.post(
        f"{GEM_HUNTER_RENDER_URL}/api/gem-hunter/v3/refresh",
        headers={"Authorization": f"Bearer {oidc_token}", "Content-Type": "application/json"},
        json={"database_url": database_url, "auth_token": auth_token},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("status") != "OK":
        raise RuntimeError("Render could not read the latest Turso snapshot.")
    return result


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
    evaluated_rows = [row for row in rows if isinstance(row.get("qualification"), dict)]
    failed = [row for row in evaluated_rows if row.get("gem_hunter_storage_status") != "PERSISTED"]
    if failed:
        raise RuntimeError(f"{len(failed)} evaluated V3 candidates were not confirmed persisted to Turso.")
    payload = dict(payload)
    payload["report_id"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    payload["generated_at_utc"] = payload.get("generated_at_utc") or _utc_now()
    payload["candidates"] = rows
    payload["trade_authorization"] = "NO_TRADE"
    payload["read_only"] = True
    payload["report_source"] = "GITHUB_ACTIONS_WORKER"
    from .gem_hunter_watchlist import sync_watchlist_scan
    watchlist_sync = sync_watchlist_scan(rows)
    payload["watchlist_ledger_sync"] = watchlist_sync
    _save_report(payload)
    render_dashboard_sync = _publish_turso_credentials_to_render()
    alert_counts: dict[str, int] = {}
    for row in rows:
        alert_status = str((row.get("telegram_alert_status") or {}).get("status") or "NOT_SENT")
        alert_counts[alert_status] = alert_counts.get(alert_status, 0) + 1
    return {
        "status": "OK",
        "report_id": payload["report_id"],
        "discovered": payload.get("discovered_candidate_count", len(rows)),
        "tier1": payload.get("gem_hunter_v3_tier1_count", 0),
        "tier2": payload.get("gem_hunter_v3_tier2_count", 0),
        "evaluated_persisted_candidates": len(evaluated_rows) - len(failed),
        "persisted_candidate_rows": sum(1 for row in rows if row.get("gem_hunter_storage_status") == "PERSISTED"),
        "telegram_alert_status_counts": alert_counts,
        "watchlist_ledger_sync": watchlist_sync,
        "render_dashboard_sync": render_dashboard_sync,
        "trade_authorization": "NO_TRADE",
    }



def watchlist_from_turso() -> dict[str, Any]:
    """Read the durable €100 watchlist using the verified worker-provided Turso credentials."""
    with _TURSO_READ_CREDENTIALS_LOCK:
        cached_url = _TURSO_READ_CREDENTIALS.get("url")
        cached_token = _TURSO_READ_CREDENTIALS.get("token")
    if cached_url and cached_token:
        db = connect_database(url=cached_url, token=cached_token)
    else:
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            return {"status":"UNAVAILABLE","message_fa":"اتصال امن Turso در دسترس نیست.","items":[]}
        db = connect_database()
    try:
        from .gem_hunter_watchlist import watchlist_snapshot
        return watchlist_snapshot(db)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run read-only Gem Hunter V3 from GitHub Actions.")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(run_scan(max(1, min(args.limit, 50))), ensure_ascii=False))


if __name__ == "__main__":
    main()
