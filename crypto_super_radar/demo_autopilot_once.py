"""One scheduled Challenge 1 cycle for a GitHub-hosted runner.

This process reuses the existing BloFin Demo worker and safety checks. It does
not start the HTTP dashboard, and it exits after one complete multi-symbol
cycle so GitHub Actions can invoke it again on its next schedule.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

_REQUIRED_SECRETS = (
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "BLOFIN_DEMO_API_KEY",
    "BLOFIN_DEMO_API_SECRET",
    "BLOFIN_DEMO_PASSPHRASE",
    "BLOFIN_DEMO_TRADE_API_KEY",
    "BLOFIN_DEMO_TRADE_API_SECRET",
    "BLOFIN_DEMO_TRADE_PASSPHRASE",
    "BLOFIN_DASHBOARD_TOKEN",
)


def preflight_errors(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return safe setting names only; never echo credential values."""
    env = os.environ if environ is None else environ
    errors: list[str] = []

    if str(env.get("BLOFIN_DEMO_AUTOPILOT_ROLE", "")).strip().upper() != "WORKER":
        errors.append("BLOFIN_DEMO_AUTOPILOT_ROLE must be WORKER")

    for name in ("BLOFIN_DEMO_TRADING_ENABLED", "BLOFIN_DEMO_AUTOPILOT_ENABLED"):
        if str(env.get(name, "")).strip().lower() != "true":
            errors.append(f"{name} must be true")

    for name in _REQUIRED_SECRETS:
        if not str(env.get(name, "")).strip():
            errors.append(f"missing {name}")

    if str(env.get("BLOFIN_DEMO_SMOKE_TEST_ENABLED", "false")).strip().lower() == "true":
        errors.append("BLOFIN_DEMO_SMOKE_TEST_ENABLED must remain false")

    return errors


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _write_step_summary(payload: dict) -> None:
    summary_path = str(os.getenv("GITHUB_STEP_SUMMARY", "")).strip()
    if not summary_path:
        return
    safe_lines = [
        "## CRYPTO SUPER RADAR · BloFin Demo scheduled cycle",
        "",
        f"- Status: **{payload.get('status', 'UNKNOWN')}**",
        f"- Runtime role: `{payload.get('runtime_role', 'WORKER')}`",
        f"- Cycle count: **{payload.get('cycles_started', 0)}**",
        f"- Last cycle: `{payload.get('last_cycle_finished_at_utc') or 'not completed'}`",
        f"- Symbols: {', '.join(payload.get('instrument_ids') or []) or 'not available'}",
        f"- Signal: `{payload.get('signal') or 'NO_TRADE / UNKNOWN'}`",
        f"- Orders submitted: **{bool(payload.get('orders_submitted'))}**",
        f"- Trade executed: **{bool(payload.get('trade_executed'))}**",
        f"- Durable storage: **{payload.get('persistence_status') == 'DURABLE'}**",
        "",
        "This run used the BloFin Demo environment only. No credentials are included in this summary.",
    ]
    with Path(summary_path).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(safe_lines) + "\n")


def main() -> int:
    errors = preflight_errors()
    if errors:
        _emit({"status": "BLOCKED", "reasons": errors, "orders_submitted": False})
        return 2

    try:
        from .database import storage_backend

        backend = storage_backend()
        if backend != "TURSO_CLOUD_SQLITE":
            _emit({
                "status": "BLOCKED",
                "reason": "durable Turso storage is required",
                "storage_backend": backend,
                "orders_submitted": False,
            })
            return 2

        from . import web_app

        if not web_app._restore_blofin_autopilot_state():
            _emit({
                "status": "BLOCKED",
                "reason": "durable worker state could not be restored",
                "orders_submitted": False,
            })
            return 2

        readiness = web_app._blofin_demo_execution_readiness()
        required_ready = (
            readiness.get("status") == "READY"
            and readiness.get("environment") == "BLOFIN_DEMO"
            and readiness.get("credential_source") == "DEDICATED_TRADE_KEY"
            and readiness.get("trade_permission_status") == "READ_WRITE_CONFIRMED"
            and readiness.get("trade_permission_verified") is True
            and readiness.get("durable_storage") is True
            and readiness.get("event_store_readable") is True
            and readiness.get("dashboard_auth_configured") is True
            and readiness.get("unresolved_orders") == 0
        )
        if not required_ready:
            _emit({
                "status": "BLOCKED",
                "reason": "BloFin Demo execution readiness is not fully verified",
                "readiness": {
                    "status": readiness.get("status"),
                    "environment": readiness.get("environment"),
                    "credential_source": readiness.get("credential_source"),
                    "trade_permission_status": readiness.get("trade_permission_status"),
                    "trade_permission_verified": readiness.get("trade_permission_verified"),
                    "durable_storage": readiness.get("durable_storage"),
                    "event_store_readable": readiness.get("event_store_readable"),
                    "dashboard_auth_configured": readiness.get("dashboard_auth_configured"),
                    "unresolved_orders": readiness.get("unresolved_orders"),
                },
                "orders_submitted": False,
            })
            return 2

        with web_app.LOCK:
            before_cycles = int(web_app.BLOFIN_AUTOPILOT_STATE.get("cycles_started") or 0)

        web_app._blofin_demo_autopilot_worker(max_cycles=1)

        with web_app.LOCK:
            state = copy.deepcopy(web_app.BLOFIN_AUTOPILOT_STATE)

        after_cycles = int(state.get("cycles_started") or 0)
        finished_at = state.get("last_cycle_finished_at_utc")
        if after_cycles <= before_cycles or not finished_at:
            _emit({
                "status": "NOT_COMPLETED",
                "reason": "one complete cycle was not recorded",
                "before_cycles": before_cycles,
                "after_cycles": after_cycles,
                "orders_submitted": False,
                "persistence_status": state.get("persistence_status"),
            })
            return 3

        diagnostics = state.get("last_cycle_diagnostics") or {}
        payload = {
            "status": "CYCLE_COMPLETED" if state.get("status") != "ERROR" else "CYCLE_ERROR",
            "runtime_role": "WORKER",
            "environment": "BLOFIN_DEMO",
            "cycles_started": after_cycles,
            "last_cycle_status": state.get("last_cycle_status"),
            "last_cycle_started_at_utc": state.get("last_cycle_started_at_utc"),
            "last_cycle_finished_at_utc": finished_at,
            "last_stage": state.get("last_stage"),
            "instrument_ids": state.get("instrument_ids") or diagnostics.get("instrument_ids") or [],
            "signal": diagnostics.get("signal"),
            "orders_submitted": bool(diagnostics.get("orders_submitted")),
            "trade_executed": bool(diagnostics.get("trade_executed")),
            "open_positions": diagnostics.get("exchange_open_position_count"),
            "open_tpsl_orders": diagnostics.get("exchange_open_tpsl_order_count"),
            "persistence_status": state.get("persistence_status"),
            "restored_from_storage": bool(state.get("restored_from_storage")),
        }
        _emit(payload)
        _write_step_summary(payload)

        if payload["status"] != "CYCLE_COMPLETED" or payload["persistence_status"] != "DURABLE":
            return 3
        return 0
    except KeyboardInterrupt:
        _emit({"status": "STOPPED", "reason": "shutdown requested", "orders_submitted": False})
        return 130
    except Exception as exc:  # Do not print exception text; it may contain request details.
        _emit({
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "orders_submitted": False,
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
