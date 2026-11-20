"""Isolated, order-incapable Challenge 1 AI Sandbox worker.

Run as a separate process from the BloFin Demo web/worker service. It reads a
validated local replay database, creates a report, and publishes only an
evidence summary to the durable Turso queue. It has no exchange imports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .ai_sandbox import run_sandbox
from .challenge1_event_queue import Challenge1EventQueue
from .database import storage_backend


def _report_digest(report: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_sandbox_event(report: Mapping[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Create a compact, deterministic evidence event with no execution command."""
    audit = report.get("walk_forward_audit")
    audit = dict(audit) if isinstance(audit, Mapping) else {}
    phase_status = str(audit.get("status") or "UNKNOWN").upper()
    digest = _report_digest(report)
    event_type = "REPLAY_REPORT" if phase_status == "PASS" else "REPLAY_BLOCKED"
    metrics = report.get("trade_metrics")
    metrics = dict(metrics) if isinstance(metrics, Mapping) else {}
    equity = report.get("equity")
    equity = dict(equity) if isinstance(equity, Mapping) else {}
    periods = report.get("periods")
    periods = dict(periods) if isinstance(periods, Mapping) else {}
    calibration = report.get("model_calibration")
    calibration = dict(calibration) if isinstance(calibration, Mapping) else {}
    ai_audit = report.get("ai_audit")
    ai_audit = dict(ai_audit) if isinstance(ai_audit, Mapping) else {}

    return {
        "event_id": f"csr-replay-{digest}",
        "producer": "AI_SANDBOX",
        "event_type": event_type,
        "occurred_at_ms": int(now_ms if now_ms is not None else time.time() * 1000),
        "payload": {
            "report_version": str(report.get("report_version") or "UNKNOWN"),
            "report_sha256": digest,
            "replay_status": phase_status,
            "data_source": str(report.get("data_source") or "UNKNOWN"),
            "network_order_writes": report.get("network_order_writes") is True,
            "periods": {
                "start_utc": periods.get("start_utc"),
                "end_utc": periods.get("end_utc"),
                "training_end_utc": periods.get("training_end_utc"),
                "validation_end_utc": periods.get("validation_end_utc"),
            },
            "equity": {
                "initial_usdt": equity.get("initial_usdt"),
                "final_realized_usdt": equity.get("final_realized_usdt"),
                "net_pnl_usdt": equity.get("net_pnl_usdt"),
            },
            "trade_metrics": {
                key: metrics.get(key)
                for key in (
                    "closed_trades", "wins", "losses", "net_win_rate_pct",
                    "fees_usdt", "funding_usdt", "spread_cost_usdt",
                    "slippage_cost_usdt", "unresolved_at_dataset_end",
                    "pending_entries_at_dataset_end",
                )
            },
            "walk_forward_audit": {
                key: audit.get(key)
                for key in (
                    "status", "purge_window_minutes", "purged_boundary_signal_blocks",
                    "cross_boundary_positions", "model_frozen_after_training",
                    "model_train_count_at_freeze",
                )
            },
            "model_calibration": {
                "VALIDATION": calibration.get("VALIDATION"),
                "OUT_OF_SAMPLE": calibration.get("OUT_OF_SAMPLE"),
            },
            "ai_audit": {
                "trained": ai_audit.get("trained"),
                "model_sha256": ai_audit.get("model_sha256"),
                "training_closed_trade_count": ai_audit.get("training_closed_trade_count"),
                "training_examples": ai_audit.get("training_examples"),
            },
        },
    }


def build_blocked_event(error_type: str, *, now_ms: int | None = None) -> dict[str, Any]:
    """Publish a safe BLOCKED marker without copying raw exception text or secrets."""
    timestamp = int(now_ms if now_ms is not None else time.time() * 1000)
    raw_type = str(error_type or "UNKNOWN").split(":", 1)[0].split()[0]
    safe_type = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in raw_type)[:80]
    identity = hashlib.sha256(f"{timestamp}:{safe_type}".encode("utf-8")).hexdigest()
    return {
        "event_id": f"csr-replay-blocked-{identity}",
        "producer": "AI_SANDBOX",
        "event_type": "REPLAY_BLOCKED",
        "occurred_at_ms": timestamp,
        "payload": {
            "replay_status": "BLOCKED",
            "error_type": safe_type or "UNKNOWN",
            "network_order_writes": False,
        },
    }


def run_worker(
    *,
    db_path: str,
    initial_equity: float = 1000.0,
    spread_scenario: float = 0.0002,
    output_path: str = "reports/ai_sandbox_report.json",
    brain_path: str = "reports/trained_brain.json",
) -> dict[str, Any]:
    """Run one isolated Sandbox cycle and publish its evidence through Turso."""
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("shared Turso storage is required for the isolated Sandbox worker")

    queue = Challenge1EventQueue(require_shared_backend=True)
    try:
        try:
            report = run_sandbox(
                db_path=db_path,
                initial_equity=initial_equity,
                spread_scenario=spread_scenario,
                output_path=output_path,
                brain_path=brain_path,
            )
            event = build_sandbox_event(report)
            published = queue.enqueue(event, now_ms=event["occurred_at_ms"])
            result = {
                "status": "REPORT_PUBLISHED" if published else "REPORT_ALREADY_PUBLISHED",
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "report_sha256": event["payload"]["report_sha256"],
                "queue_backend": queue.backend,
                "report_path": output_path,
                "network_order_writes": False,
            }
            return result
        except Exception as exc:
            event = build_blocked_event(type(exc).__name__)
            published = queue.enqueue(event, now_ms=event["occurred_at_ms"])
            return {
                "status": "REPLAY_BLOCKED",
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "error_type": type(exc).__name__,
                "blocked_event_published": published,
                "queue_backend": queue.backend,
                "network_order_writes": False,
            }
    finally:
        queue.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the order-incapable Challenge 1 Sandbox in its own process."
    )
    parser.add_argument("--db", default="data/blofin_replay.sqlite3")
    parser.add_argument("--equity", type=float, default=1000.0)
    parser.add_argument("--spread", type=float, default=0.0002)
    parser.add_argument("--output", default="reports/ai_sandbox_report.json")
    parser.add_argument("--brain", default="reports/trained_brain.json")
    args = parser.parse_args()
    try:
        result = run_worker(
            db_path=args.db,
            initial_equity=args.equity,
            spread_scenario=args.spread,
            output_path=args.output,
            brain_path=args.brain,
        )
    except Exception as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "error_type": type(exc).__name__,
            "network_order_writes": False,
        }, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"REPORT_PUBLISHED", "REPORT_ALREADY_PUBLISHED"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
