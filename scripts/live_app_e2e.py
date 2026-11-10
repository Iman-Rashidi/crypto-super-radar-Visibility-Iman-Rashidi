#!/usr/bin/env python3
"""One-shot read-only application E2E acceptance against the deployed Render app.

This submits exactly one paper-only cycle and one public-data research scan.
It never uses credentials, wallets, exchange order endpoints or signed transactions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_BASE_URL = "https://crypto-super-radar.onrender.com"
JOB_ID_RE = re.compile(r"const job='([a-f0-9]{32})'")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def get_json(session: requests.Session, url: str, timeout: float = 45.0) -> tuple[requests.Response, dict[str, Any]]:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Expected JSON from {url}, received non-JSON content") from exc
    require(isinstance(payload, dict), f"Expected an object from {url}")
    return response, payload


def wait_for_health(
    session: requests.Session,
    base_url: str,
    expected_commit: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "service has not responded yet"
    while time.monotonic() < deadline:
        try:
            _, health = get_json(session, f"{base_url}/health", timeout=10)
            if health.get("status") != "ok":
                last_error = f"unexpected health payload: {health}"
            else:
                deployed_commit = health.get("commit")
                if not expected_commit or not deployed_commit or deployed_commit == expected_commit:
                    health["expected_commit"] = expected_commit
                    health["commit_match"] = (
                        deployed_commit == expected_commit if deployed_commit else None
                    )
                    return health
                last_error = (
                    f"Render still serves commit {deployed_commit}; waiting for {expected_commit}"
                )
        except (requests.RequestException, RuntimeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(8)
    raise RuntimeError(f"Render health/deployment check timed out: {last_error}")


def submit_and_wait(
    session: requests.Session,
    base_url: str,
    route: str,
    asset: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    response = session.post(
        f"{base_url}{route}",
        data={"asset": asset},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
        allow_redirects=False,
    )
    require(
        response.status_code == 202,
        f"{route} returned HTTP {response.status_code}, expected 202: {response.text[:800]}",
    )
    match = JOB_ID_RE.search(response.text)
    require(match is not None, f"{route} did not return a pollable job ID")
    job_id = match.group(1)

    deadline = time.monotonic() + timeout_seconds
    last_job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _, last_job = get_json(session, f"{base_url}/jobs/{job_id}", timeout=45)
        state = last_job.get("status")
        if state in {"completed", "failed"}:
            return {"http_status": response.status_code, "job_id": job_id, **last_job}
        time.sleep(2.5)
    raise RuntimeError(
        f"{route} job {job_id} timed out; last state={last_job.get('status')}"
    )


def validate_safety(safety: Any, label: str, paper_only: bool = False) -> None:
    require(isinstance(safety, dict), f"{label}: missing safety metadata")
    require(safety.get("read_only") is True, f"{label}: read_only must be true")
    require(safety.get("orders_submitted") is False, f"{label}: orders_submitted must be false")
    require(safety.get("transactions_signed") is False, f"{label}: transactions_signed must be false")
    require(safety.get("wallet_credentials_used") is False, f"{label}: wallet credentials were used")
    require(
        safety.get("private_exchange_credentials_used") is False,
        f"{label}: private exchange credentials were used",
    )
    if paper_only:
        require(safety.get("paper_only") is True, f"{label}: paper_only must be true")


def run(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url.rstrip("/")
    report: dict[str, Any] = {
        "project": "CRYPTO SUPER RADAR",
        "test_type": "LIVE_RENDER_APP_END_TO_END",
        "started_at_utc": utc_now(),
        "base_url": base_url,
        "expected_commit": args.expected_commit,
        "actions": {
            "paper_cycles_submitted": 0,
            "research_scans_submitted": 0,
            "real_orders_submitted": False,
            "transactions_signed": False,
            "wallets_connected": False,
            "private_exchange_credentials_used": False,
        },
    }
    session = requests.Session()
    session.headers.update({"User-Agent": "crypto-super-radar-e2e/1.0"})
    try:
        report["stage"] = "health"
        print("[E2E] Checking Render deployment and health", flush=True)
        report["health"] = wait_for_health(
            session, base_url, args.expected_commit, args.deploy_timeout
        )

        report["stage"] = "home"
        print("[E2E] Checking dashboard and form markup", flush=True)
        home = session.get(base_url + "/", timeout=60)
        home.raise_for_status()
        page = home.text
        require('lang="fa" dir="rtl"' in page, "Persian dashboard markup is missing")
        require('action="/paper-bot/tick"' in page, "Paper Trading form is missing")
        require('action="/scan"' in page, "Research scan form is missing")
        require('id="crypto-market-search"' in page, "Market search control is missing")
        require('id="crypto-market-sort"' in page, "Market sorting control is missing")
        report["home"] = {
            "http_status": home.status_code,
            "persian_dashboard": True,
            "paper_form": True,
            "scan_form": True,
        }

        report["stage"] = "market_board"
        print("[E2E] Fetching cross-validated market board", flush=True)
        _, markets = get_json(session, base_url + "/api/crypto-markets", timeout=240)
        market_items = markets.get("items", [])
        report["market_board"] = {
            "status": markets.get("status"),
            "item_count": len(market_items),
            "items": [
                {
                    "symbol": item.get("symbol"),
                    "status": item.get("status"),
                    "price_usd": item.get("price_usd"),
                    "providers": item.get("providers"),
                    "reason": item.get("reason"),
                }
                for item in market_items
            ],
        }
        require(len(market_items) >= 20, f"Market board returned only {len(market_items)} instruments; expected at least 20")

        report["stage"] = "dex_search"
        print("[E2E] Checking DEX discovery endpoint", flush=True)
        _, dex = get_json(session, base_url + "/api/dex-search?q=PEPE", timeout=75)
        report["dex_search"] = {
            "status": dex.get("status"),
            "result_count": dex.get("result_count"),
            "networks": dex.get("supported_networks"),
        }

        report["stage"] = "paper_status_before"
        print("[E2E] Checking paper account safety state", flush=True)
        _, before = get_json(session, base_url + "/paper-bot/status")
        require(before.get("mode") == "PAPER_ONLY", "Paper bot mode is not PAPER_ONLY")
        require(before.get("orders_submitted") is False, "Paper bot reports live orders enabled")
        require(before.get("live_orders_enabled") is False, "Paper bot live-order switch is enabled")
        report["paper_before"] = {
            "mode": before.get("mode"),
            "equity_usd": (before.get("account") or {}).get("equity_usd"),
            "open_position": before.get("open_position"),
            "storage": before.get("storage"),
        }

        report["stage"] = "paper_cycle"
        print("[E2E] Submitting exactly one paper-only cycle", flush=True)
        report["actions"]["paper_cycles_submitted"] = 1
        paper_job = submit_and_wait(
            session, base_url, "/paper-bot/tick", "cex:BTCUSDT", args.job_timeout
        )
        require(paper_job.get("status") == "completed", f"Paper job failed: {paper_job}")
        paper_result = paper_job.get("result") or {}
        validate_safety(paper_result.get("safety"), "paper cycle", paper_only=True)
        cycle = paper_result.get("cycle") or {}
        paper_state = paper_result.get("paper_state") or {}
        require(paper_state.get("mode") == "PAPER_ONLY", "Paper cycle changed the mode")
        require(paper_state.get("orders_submitted") is False, "Paper cycle reports a live order")
        report["paper_cycle"] = {
            "job_id": paper_job.get("job_id"),
            "status": cycle.get("status"),
            "reason": cycle.get("reason"),
            "asset": cycle.get("asset"),
            "open_position": paper_state.get("open_position"),
            "account": paper_state.get("account"),
            "risk_controls": paper_state.get("risk_controls"),
            "safety": paper_result.get("safety"),
        }

        report["stage"] = "spot_scan"
        print("[E2E] Submitting exactly one BTC Spot research scan", flush=True)
        report["actions"]["research_scans_submitted"] = 1
        scan_job = submit_and_wait(
            session, base_url, "/scan", "cex:BTCUSDT", args.job_timeout
        )
        require(scan_job.get("status") == "completed", f"Research scan failed: {scan_job}")
        scan = scan_job.get("result") or {}
        validate_safety(scan.get("safety"), "research scan")
        gates = scan.get("gates") or []
        require(len(gates) == 12, f"Expected 12 gates in the scan response; got {len(gates)}")
        gate_counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
        for gate in gates:
            gate_status = str(gate.get("status", "UNKNOWN")).upper()
            require(gate_status in gate_counts, f"Unexpected gate status: {gate_status}")
            gate_counts[gate_status] += 1
        signal = str(scan.get("signal", "NO TRADE")).upper()
        require(signal in {"LONG", "SHORT", "NO TRADE"}, f"Unexpected signal: {signal}")
        report["spot_scan"] = {
            "job_id": scan_job.get("job_id"),
            "asset": scan.get("asset"),
            "execution_status": scan_job.get("status"),
            "signal": signal,
            "confirmation_score": scan.get("confirmation_score"),
            "estimated_risk_score": scan.get("estimated_risk_score"),
            "gate_counts": gate_counts,
            "gates": gates,
            "diagnosis": scan.get("diagnosis"),
            "safety": scan.get("safety"),
        }

        report["stage"] = "paper_status_after"
        print("[E2E] Verifying post-cycle paper account", flush=True)
        _, after = get_json(session, base_url + "/paper-bot/status")
        require(after.get("mode") == "PAPER_ONLY", "Paper mode changed after scan")
        require(after.get("orders_submitted") is False, "Post-test account reports real orders")
        report["paper_after"] = {
            "mode": after.get("mode"),
            "account": after.get("account"),
            "open_position": after.get("open_position"),
            "recent_trades": after.get("recent_trades"),
            "storage": after.get("storage"),
            "risk_controls": after.get("risk_controls"),
        }

        report["stage"] = "history"
        print("[E2E] Checking scan history endpoint", flush=True)
        _, history = get_json(
            session, base_url + "/history?asset=cex%3ABTCUSDT"
        )
        report["history_after"] = history
        report["overall_status"] = "PASS"
        report["trading_readiness"] = (
            "READY_FOR_PAPER_OBSERVATION"
            if gate_counts["PASS"] == 12 and signal in {"LONG", "SHORT"}
            else "BLOCKED_NO_TRADE"
        )
        report["stage"] = "completed"
        return report
    except Exception as exc:
        report["overall_status"] = "FAIL"
        report["failed_stage"] = report.get("stage", "unknown")
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        report["finished_at_utc"] = utc_now()
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("CSR_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--expected-commit", default=os.getenv("CSR_EXPECTED_COMMIT") or None)
    parser.add_argument("--deploy-timeout", type=int, default=600)
    parser.add_argument("--job-timeout", type=int, default=360)
    parser.add_argument("--output", default="reports/render-app-e2e.json")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = run(args)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("overall_status") == "PASS" else 1
    except Exception as exc:
        failed = {
            "project": "CRYPTO SUPER RADAR",
            "test_type": "LIVE_RENDER_APP_END_TO_END",
            "overall_status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at_utc": utc_now(),
            "note": "Inspect the last completed action; no real orders or signed transactions are permitted.",
        }
        output.write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(failed, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
