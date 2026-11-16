"""Standalone process entry point for the BloFin Demo Challenge 1 worker.

Runs the existing guarded Challenge 1 loop without starting the dashboard.
Requires an explicit WORKER role and durable Turso storage.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping

_TRUE = {"1", "true", "yes", "on"}


def validate_worker_environment(environ: Mapping[str, str] | None = None) -> None:
    """Fail closed unless this process is explicitly configured as Demo worker."""
    env = os.environ if environ is None else environ

    role = str(env.get("BLOFIN_DEMO_AUTOPILOT_ROLE", "")).strip().upper()
    if role != "WORKER":
        raise RuntimeError("BLOFIN_DEMO_AUTOPILOT_ROLE must be WORKER.")

    if str(env.get("BLOFIN_DEMO_AUTOPILOT_ENABLED", "")).strip().lower() not in _TRUE:
        raise RuntimeError("BLOFIN_DEMO_AUTOPILOT_ENABLED must be true.")

    if str(env.get("BLOFIN_DEMO_TRADING_ENABLED", "")).strip().lower() not in _TRUE:
        raise RuntimeError("BLOFIN_DEMO_TRADING_ENABLED must be true.")

    if not str(env.get("TURSO_DATABASE_URL", "")).strip() or not str(env.get("TURSO_AUTH_TOKEN", "")).strip():
        raise RuntimeError("Both Turso durable-storage settings are required.")

    if not str(env.get("BLOFIN_DASHBOARD_TOKEN", "")).strip():
        raise RuntimeError("BLOFIN_DASHBOARD_TOKEN is required.")

    read_keys = (
        str(env.get("BLOFIN_DEMO_API_KEY", "")).strip(),
        str(env.get("BLOFIN_DEMO_API_SECRET", "")).strip(),
        str(env.get("BLOFIN_DEMO_PASSPHRASE", "")).strip(),
    )
    trade_keys = (
        str(env.get("BLOFIN_DEMO_TRADE_API_KEY", "")).strip(),
        str(env.get("BLOFIN_DEMO_TRADE_API_SECRET", "")).strip(),
        str(env.get("BLOFIN_DEMO_TRADE_PASSPHRASE", "")).strip(),
    )

    read_any, read_all = any(read_keys), all(read_keys)
    trade_any, trade_all = any(trade_keys), all(trade_keys)
    if (read_any and not read_all) or (trade_any and not trade_all):
        raise RuntimeError("BloFin Demo credential groups must be complete.")
    if not read_all and not trade_all:
        raise RuntimeError("A complete BloFin Demo credential group is required.")


def main() -> int:
    try:
        validate_worker_environment()
    except RuntimeError as exc:
        print(f"CHALLENGE1_WORKER_BLOCKED: {exc}", file=sys.stderr, flush=True)
        return 2

    from . import web_app

    if web_app.storage_backend() != "TURSO_CLOUD_SQLITE":
        print("CHALLENGE1_WORKER_BLOCKED: durable Turso storage is unavailable.", file=sys.stderr, flush=True)
        return 2

    if not web_app._restore_blofin_autopilot_state():
        print("CHALLENGE1_WORKER_BLOCKED: the durable event store could not be restored.", file=sys.stderr, flush=True)
        return 2

    print(
        "CHALLENGE1_WORKER_STARTED: BloFin Demo only; Strategy 1 only; "
        "five configured instruments; no dashboard server in this process.",
        flush=True,
    )
    try:
        # Run the same guarded loop in the foreground so systemd supervises it.
        web_app._blofin_demo_autopilot_worker()
    except KeyboardInterrupt:
        print("CHALLENGE1_WORKER_STOPPED: shutdown requested.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
