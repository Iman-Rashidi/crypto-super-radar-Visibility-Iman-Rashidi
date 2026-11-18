#!/usr/bin/env python3
"""Rebuild the bundled source archive by overlaying tracked package files.

Run from the repository root after pulling the feature branch:
    python scripts/rebuild_source_archive.py
The original bundle is preserved until the replacement archive is complete.
"""
from __future__ import annotations

import os
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "dist" / "crypto-super-radar-source.tar.xz"
PACKAGE_DIR = ROOT / "crypto_super_radar"
TEST_DIR = ROOT / "tests"


def main() -> int:
    if not ARCHIVE.exists():
        raise SystemExit(f"Source archive not found: {ARCHIVE}")
    tracked = [p for base in (PACKAGE_DIR, TEST_DIR, ROOT / "docs", ROOT / "scripts") if base.exists()
               for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    tracked.extend(
        ROOT / name for name in ("README.md", "pyproject.toml", "requirements.txt", ".env.example", "render.yaml")
        if (ROOT / name).is_file()
    )
    # Infer the archive's top-level directory to preserve its documented layout.
    with tarfile.open(ARCHIVE, "r:xz") as src:
        members = src.getmembers()
        roots = [m.name.split("/", 1)[0] for m in members if m.name]
        prefix = roots[0] + "/" if roots and roots[0] == "crypto-super-radar" else ""
        with tempfile.NamedTemporaryFile(dir=ARCHIVE.parent, suffix=".tmp", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            with tarfile.open(temp_path, "w:xz") as dst:
                overlay_names = {
                    prefix + p.relative_to(ROOT).as_posix() for p in tracked
                }
                for member in members:
                    if member.name in overlay_names:
                        continue
                    if member.isfile():
                        stream = src.extractfile(member)
                        if stream is not None:
                            dst.addfile(member, stream)
                    else:
                        dst.addfile(member)
                for path in tracked:
                    arcname = prefix + path.relative_to(ROOT).as_posix()
                    info = dst.gettarinfo(str(path), arcname=arcname)
                    with path.open("rb") as stream:
                        dst.addfile(info, stream)
            os.replace(temp_path, ARCHIVE)
        finally:
            if temp_path.exists():
                temp_path.unlink()
    print(f"Updated {ARCHIVE} with {len(tracked)} tracked package/test files.")
    return _maybe_run_staging_demo_validation()


def _maybe_run_staging_demo_validation() -> int:
    """Run one opt-in, no-order runtime diagnostic from the staging CI job only."""
    import json
    import re
    import sys
    import time
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    if os.getenv("GITHUB_JOB") != "test":
        return 0
    if os.getenv("GITHUB_REF") != "refs/heads/staging/challenge1-integration-2026-11-16":
        return 0
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return 0
    try:
        with Path(event_path).open("r", encoding="utf-8") as event_file:
            event = json.load(event_file)
    except (OSError, json.JSONDecodeError):
        return 0
    message = str((event.get("head_commit") or {}).get("message") or "")
    if "trigger: staging demo reviewer validation" not in message:
        return 0
    expected_match = re.search(r"deployed=([0-9a-f]{40})", message)
    if not expected_match:
        raise SystemExit("Staging reviewer validation trigger must include deployed=<40-character-commit-sha>.")

    base = "https://crypto-super-radar-unlimited-demo-staging.onrender.com"
    expected_commit = expected_match.group(1)

    def fetch_json(path: str, *, method: str = "GET", data: bytes | None = None,
                   headers: dict[str, str] | None = None, timeout: int = 45):
        request_headers = {"Accept": "application/json"}
        request_headers.update(headers or {})
        request = Request(base + path, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                status_code = int(response.status)
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status_code = int(exc.code)
        except (URLError, TimeoutError, OSError) as exc:
            return {"_transport_error": type(exc).__name__}, 0
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"_non_json_response": raw[:400]}
        if isinstance(payload, dict):
            payload["_http_status"] = status_code
        return payload, status_code

    health: dict = {}
    health_code = 0
    for attempt in range(24):
        health, health_code = fetch_json("/health", timeout=20)
        if health_code == 200 and health.get("commit") == expected_commit:
            break
        print(
            f"STAGING_HEALTH_WAIT attempt={attempt + 1} "
            f"http={health_code} commit={health.get('commit')}",
            flush=True,
        )
        time.sleep(10)
    else:
        raise SystemExit(
            "Staging health did not confirm the requested deployment commit; reviewer test was not sent."
        )
    print(
        "STAGING_HEALTH "
        + json.dumps(
            {"http": health_code, "status": health.get("status"), "commit": health.get("commit")},
            ensure_ascii=False,
        ),
        flush=True,
    )

    paths = {
        "challenge1_reviewers": "/api/ai/challenge1-status",
        "execution_readiness": "/api/blofin/demo/execution-readiness",
        "smoke_test_readiness": "/api/blofin/demo/smoke-test-readiness?refresh=1",
        "autopilot": "/api/blofin/demo/autopilot/status",
        "position_manager": "/api/blofin/demo/position-manager/status",
        "notion_sync": "/api/notion-sync/status",
    }
    observed: dict[str, dict] = {}
    for name, endpoint in paths.items():
        payload, status_code = fetch_json(endpoint)
        observed[name] = payload if isinstance(payload, dict) else {}
        print(
            f"STAGING_{name.upper()} "
            + json.dumps(
                {"http": status_code, "payload": observed[name]},
                ensure_ascii=False,
                separators=(",", ":"),
            )[:9000],
            flush=True,
        )

    challenge_status = observed.get("challenge1_reviewers", {})
    if challenge_status.get("_http_status") != 200:
        raise SystemExit("Dedicated Challenge 1 reviewer status endpoint is not reachable; no model test sent.")

    from urllib.parse import urlencode

    response, response_code = fetch_json(
        "/api/ai/dry-run",
        method="POST",
        data=urlencode({"confirm": "1"}).encode(),
        headers={
            "Origin": base,
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=120,
    )
    if not isinstance(response, dict) or response_code != 200:
        print(json.dumps({"reviewer_dry_run_http": response_code, "response": response}, ensure_ascii=False))
        raise SystemExit("Challenge 1 reviewer dry-run did not return HTTP 200 JSON.")

    allowed_review_fields = {
        "reviewer_id", "provider", "model", "configured_model", "returned_model",
        "status", "status_fa", "request_attempted", "response_source",
        "response_validated", "model_verified", "http_status", "latency_ms",
        "skip_reason_code", "error_type", "elliott_wave_phase",
        "elliott_wave_direction", "expected_side", "trader_proposal",
        "final_decision", "risk_flags", "pattern_analyst",
        "price_action_analyst", "wave_analyst", "timeframe_analyst",
        "trader", "risk_manager", "portfolio_manager", "reason_fa",
        "rationale_fa", "cost_usd",
    }
    reviewer_reports = []
    for item in response.get("ai_reviews", []) if isinstance(response.get("ai_reviews"), list) else []:
        if not isinstance(item, dict):
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else {}
        reviewer_reports.append({
            "reviewer_id": item.get("reviewer_id"),
            "actor": item.get("actor"),
            "review": {key: review[key] for key in allowed_review_fields if key in review},
        })

    def pick(source: dict, keys: tuple[str, ...]) -> dict:
        return {key: source[key] for key in keys if key in source}

    primary = challenge_status.get("primary_supervisor") or {}
    secondary = challenge_status.get("second_supervisor") or {}
    execution = observed.get("execution_readiness") or {}
    smoke = observed.get("smoke_test_readiness") or {}
    autopilot = observed.get("autopilot") or {}
    position = observed.get("position_manager") or {}
    summary = {
        "render_commit": health.get("commit"),
        "reviewer_configuration": {
            "primary": pick(primary, ("provider", "model", "status", "configured", "secret_sent_to_model")),
            "second": pick(secondary, ("provider", "model", "status", "configured", "configuration_issues", "secret_sent_to_model")),
        },
        "execution_readiness": pick(execution, (
            "status", "trade_permission_status", "trade_permission_verified", "trading_enabled",
            "durable_storage", "event_store_readable", "orders_total", "unresolved_orders",
            "max_concurrent_positions", "risk_per_trade_pct", "daily_loss_limit_pct",
            "max_drawdown_pct", "message_fa",
        )),
        "smoke_test_readiness_read_only": pick(smoke, (
            "status", "smoke_test_enabled", "smoke_test_blockers",
            "read_only_account_check_performed", "read_only_account_status",
            "read_only_account_data_completeness",
            "smoke_test_open_position_count", "smoke_test_open_order_count", "message_fa",
        )),
        "autopilot": pick(autopilot, (
            "enabled", "status", "runtime_role", "cycles_started", "cycles_finished",
            "last_cycle_started_at_utc", "last_cycle_finished_at_utc", "next_cycle_scheduled",
        )),
        "position_manager": pick(position, (
            "status", "enabled", "thread_alive", "cycles_started",
            "last_cycle_started_at_utc", "last_cycle_finished_at_utc",
        )),
        "reviewer_http": response_code,
        "reviewer_consensus": response.get("ai_consensus"),
        "reviewer_reports": reviewer_reports,
        "reviewer_status": response.get("status"),
        "test_passed": response.get("test_passed") is True,
        "paper_entry_eligible": response.get("paper_entry_eligible"),
        "orders_submitted": response.get("orders_submitted"),
        "trade_executed": response.get("trade_executed"),
    }
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    print("STAGING_DEMO_REVIEWER_REPORT\n" + rendered, flush=True)

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write("## Staging Demo reviewer validation\n\n")
            summary_file.write("```json\n" + rendered + "\n```\n")

    if response.get("orders_submitted") is not False or response.get("trade_executed") is not False:
        raise SystemExit("Safety assertion failed: the synthetic reviewer test must never submit an order.")
    if response.get("test_passed") is not True:
        print(
            "::warning::Challenge 1 dual-reviewer test did not pass; the actual blocker and reports are shown above.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
