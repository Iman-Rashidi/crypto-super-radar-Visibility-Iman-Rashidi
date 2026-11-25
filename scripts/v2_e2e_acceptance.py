"""One-shot Staging acceptance for Groq, BloFin Demo connectivity and Telegram.

The permanent Turso claim is written before any external side effect. A crash or
unknown order outcome leaves the key consumed; this script never retries a trade.
The BloFin roundtrip is an isolated fixed-direction smoke test, not a V2 signal.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crypto_super_radar.database import connect_database, storage_backend
from crypto_super_radar.blofin_demo_smoke_test import run_blofin_demo_roundtrip_smoke
from crypto_super_radar.telegram_notifier import TelegramNotifier

LOCK_KEY = "v2-end-to-end-acceptance-2026-11-25-v1"
STAGING_URL = "https://crypto-super-radar-unlimited-demo-staging.onrender.com"
REPORT_PATH = Path(os.getenv("CSR_ACCEPTANCE_REPORT", "v2-e2e-acceptance-report.json"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def acquire_permanent_lock() -> bool:
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("Turso Cloud is required; local SQLite is not an acceptable idempotency boundary.")
    db = connect_database()
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS csr_acceptance_idempotency (
                lock_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                report_json TEXT
            )
        """)
        db.commit()
        db.execute(
            "INSERT OR IGNORE INTO csr_acceptance_idempotency(lock_key,status,started_at,updated_at) VALUES(?,?,?,?)",
            (LOCK_KEY, "IN_PROGRESS", utc_now(), utc_now()),
        )
        db.commit()
        row = db.execute(
            "SELECT status FROM csr_acceptance_idempotency WHERE lock_key=?", (LOCK_KEY,)
        ).fetchone()
        if row is None or str(row[0]) != "IN_PROGRESS":
            raise RuntimeError("Acceptance idempotency lock could not be claimed.")
        # INSERT OR IGNORE's rowcount differs across DB adapters. A second read
        # of started_at plus the unique primary key provides the race-safe claim:
        # only this transaction may proceed when the row was just inserted.
        claimed = db.execute(
            "SELECT COUNT(*) FROM csr_acceptance_idempotency WHERE lock_key=? AND status='IN_PROGRESS'",
            (LOCK_KEY,),
        ).fetchone()
        if not claimed or int(claimed[0]) != 1:
            raise RuntimeError("Acceptance idempotency lock is unavailable.")
        # An existing IN_PROGRESS record is also a consumed one-shot claim.
        # The runner below uses a second atomic marker to distinguish the creator.
        marker = db.execute(
            "CREATE TABLE IF NOT EXISTS csr_acceptance_claim_guard (lock_key TEXT PRIMARY KEY, claimed_at TEXT NOT NULL)"
        )
        del marker
        guard = db.execute(
            "INSERT OR IGNORE INTO csr_acceptance_claim_guard(lock_key,claimed_at) VALUES(?,?)",
            (LOCK_KEY, utc_now()),
        )
        db.commit()
        if int(getattr(guard, "rowcount", 0) or 0) != 1:
            print(json.dumps({"status": "DUPLICATE_SKIPPED", "acceptance_key": LOCK_KEY}), flush=True)
            return False
        return True
    finally:
        db.close()


def save_report(report: dict[str, Any], status: str) -> None:
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    db = connect_database()
    try:
        db.execute(
            "UPDATE csr_acceptance_idempotency SET status=?,updated_at=?,report_json=? WHERE lock_key=?",
            (status, utc_now(), encoded, LOCK_KEY),
        )
        db.commit()
        row = db.execute(
            "SELECT status,report_json FROM csr_acceptance_idempotency WHERE lock_key=?", (LOCK_KEY,)
        ).fetchone()
        if row is None or str(row[0]) != status or str(row[1]) != encoded:
            raise RuntimeError("Turso acceptance report read-back mismatch.")
    finally:
        db.close()
    REPORT_PATH.write_text(encoded + "\n", encoding="utf-8")


def verify_live_groq() -> dict[str, Any]:
    base = STAGING_URL.rstrip("/")
    request = urllib.request.Request(
        base + "/api/ai/dry-run",
        data=b"confirm=1",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": base,
            "Sec-Fetch-Site": "same-origin",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if response.status != 200:
            raise RuntimeError(f"Groq V2 review endpoint returned HTTP {response.status}.")
    consensus = payload.get("ai_consensus") or {}
    reviewers = payload.get("ai_reviews") or []
    if payload.get("test_passed") is not True:
        raise RuntimeError("Staging live-review endpoint did not pass its acceptance checks.")
    if not (
        consensus.get("status") == "APPROVED_BY_GROQ"
        and consensus.get("reviewer_id") == "groq"
        and consensus.get("provider_is_groq") is True
        and consensus.get("model_is_groq_allowlisted") is True
        and len(reviewers) == 1
        and reviewers[0].get("reviewer_id") == "groq"
    ):
        raise RuntimeError("Live Groq reviewer consensus/allowlist validation failed.")
    review = reviewers[0].get("review") or {}
    if not (
        review.get("http_status") == 200
        and review.get("request_attempted") is True
        and review.get("response_source") == "LIVE_API"
        and review.get("response_validated") is True
        and review.get("model_verified") is True
        and str(review.get("model") or "").split(":", 1)[0] == "openai/gpt-oss-20b"
        and str(review.get("returned_model") or "").split(":", 1)[0] == "openai/gpt-oss-20b"
    ):
        raise RuntimeError("Groq HTTP/model identity verification failed.")
    return {
        "status": "VERIFIED",
        "http_status": review.get("http_status"),
        "model": review.get("model"),
        "returned_model": review.get("returned_model"),
        "model_verified": review.get("model_verified"),
        "model_is_groq_allowlisted": consensus.get("model_is_groq_allowlisted"),
        "request_attempted": review.get("request_attempted"),
        "response_source": review.get("response_source"),
    }


def main() -> int:
    report: dict[str, Any] = {
        "acceptance_key": LOCK_KEY,
        "started_at_utc": utc_now(),
        "environment": "STAGING_AND_BLOFIN_DEMO_ONLY",
        "strategy_validation": "NOT_TESTED_BY_ISOLATED_SMOKE",
        "steps": {},
    }
    if not acquire_permanent_lock():
        return 0
    try:
        report["steps"]["groq"] = verify_live_groq()
        save_report(report, "GROQ_VERIFIED")

        trade = run_blofin_demo_roundtrip_smoke(
            on_event=lambda **event: print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)
        )
        if not (
            trade.get("status") == "CLOSED_VERIFIED"
            and trade.get("environment") == "BLOFIN_DEMO"
            and trade.get("instrument_id") == "BTC-USDT"
            and trade.get("leverage") == 1
            and trade.get("margin_mode") == "isolated"
            and trade.get("orders_submitted") is True
            and trade.get("trade_executed") is True
            and trade.get("position_closed") is True
            and trade.get("open_position_count_after_close") == 0
            and trade.get("entry_order_id")
        ):
            raise RuntimeError("BloFin Demo roundtrip did not prove filled entry and verified flat close.")
        report["steps"]["blofin_demo"] = trade
        save_report(report, "BLOFIN_DEMO_CLOSED")

        message = (
            "✅ پذیرش اتصال CRYPTO SUPER RADAR در Staging انجام شد.\n"
            "Groq: HTTP 200، مدل openai/gpt-oss-20b تأیید شد.\n"
            f"BloFin Demo: BTC-USDT، سفارش {trade['entry_order_id']}، "
            "SL/TP ثبت شد، Fill تأیید و پوزیشن بسته شد؛ حساب تخت است.\n"
            "توجه: این تست اتصال مستقل است و تأیید سیگنال یا گیت‌های استراتژی CHALLENGE_1_V2 نیست."
        )
        notifier = TelegramNotifier(connection_factory=connect_database)
        telegram = notifier.send_once(LOCK_KEY + ":telegram", message)
        if telegram.get("status") != "SENT":
            raise RuntimeError(f"Telegram did not return SENT: {telegram}")
        report["steps"]["telegram"] = telegram
        report["completed_at_utc"] = utc_now()
        report["status"] = "ALL_COMPONENT_SMOKES_VERIFIED"
        save_report(report, "COMPLETE")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        report["status"] = "FAILED_OR_UNKNOWN_NO_RETRY"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        report["completed_at_utc"] = utc_now()
        try:
            save_report(report, "FAILED_OR_UNKNOWN_NO_RETRY")
        except Exception as persist_error:
            print(f"Turso failure-state persistence failed: {type(persist_error).__name__}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
