#!/usr/bin/env python3
"""Short, read-only live acceptance probe for BloFin Demo Futures public WebSocket.

No API key is loaded. The probe only calls the unauthenticated Demo instruments
endpoint and subscribes to public market-data channels. It never opens a private
socket, submits orders, or touches account state.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

import requests

from crypto_super_radar.blofin_demo_futures_ws import (
    DEFAULT_PUBLIC_CHANNELS,
    BloFinDemoFuturesPublicEventStore,
    BloFinDemoFuturesPublicWorker,
)

DEMO_REST_URL = "https://demo-trading-openapi.blofin.com"


def parse_contract_value(payload: Any, inst_id: str) -> float:
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise ValueError("BloFin Demo instruments endpoint returned an API error or invalid envelope.")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("BloFin Demo instruments response data is not a list.")
    matches = [
        row for row in rows
        if isinstance(row, dict) and row.get("instId") == inst_id
    ]
    if len(matches) != 1:
        raise ValueError("BloFin Demo did not return exactly one matching instrument.")
    row = matches[0]
    if (
        row.get("state") != "live"
        or row.get("contractType") != "linear"
        or row.get("settleCurrency") != "USDT"
    ):
        raise ValueError("Instrument is not a live USDT-linear contract.")
    value = row.get("contractValue")
    if isinstance(value, bool):
        raise ValueError("Contract value is invalid.")
    try:
        contract_value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Contract value is missing or invalid.") from exc
    if not math.isfinite(contract_value) or contract_value <= 0:
        raise ValueError("Contract value must be finite and positive.")
    return contract_value


def classify_ws_acceptance(worker_report: dict[str, Any]) -> dict[str, Any]:
    """PASS only when public subscription ACKs and core live market data are observed."""
    acks = worker_report.get("subscription_ack_counts")
    observations = worker_report.get("channel_observation_counts")
    acks = acks if isinstance(acks, dict) else {}
    observations = observations if isinstance(observations, dict) else {}
    expected = list(DEFAULT_PUBLIC_CHANNELS)
    acknowledged = [channel for channel in expected if int(acks.get(channel, 0) or 0) > 0]
    missing_acks = [channel for channel in expected if channel not in acknowledged]
    required_observation_channels = ("books5", "tickers", "mark-price-candle1m")
    missing_observations = [
        channel for channel in required_observation_channels
        if int(observations.get(channel, 0) or 0) <= 0
    ]
    last_error = str(worker_report.get("last_error") or "")
    protocol_error = last_error.startswith("BloFinDemoWebSocketError:")
    if protocol_error:
        status = "FAIL"
    elif not missing_acks and not missing_observations:
        status = "PASS"
    else:
        status = "INCONCLUSIVE"
    return {
        "status": status,
        "expected_public_channels": expected,
        "acknowledged_channels": acknowledged,
        "missing_subscription_acks": missing_acks,
        "observation_counts": {channel: int(observations.get(channel, 0) or 0) for channel in expected},
        "required_channels_without_observation": missing_observations,
        "trade_observation": (
            "OBSERVED" if int(observations.get("trades", 0) or 0) > 0
            else "NO_TRADE_SEEN_DURING_PROBE"
        ),
        "funding_rate_observation": (
            "OBSERVED" if int(observations.get("funding-rate", 0) or 0) > 0
            else "NO_FUNDING_PUSH_DURING_PROBE"
        ),
        "last_error": last_error or None,
    }


async def _run_probe(inst_id: str, contract_value: float, duration_seconds: float) -> dict[str, Any]:
    db = sqlite3.connect(":memory:")
    try:
        store = BloFinDemoFuturesPublicEventStore(db)
        stop = asyncio.Event()
        worker = BloFinDemoFuturesPublicWorker(
            inst_id=inst_id,
            contract_value=contract_value,
            event_store=store,
        )

        async def stop_after_duration() -> None:
            await asyncio.sleep(duration_seconds)
            stop.set()

        timer = asyncio.create_task(stop_after_duration())
        try:
            result = await worker.run(stop)
        finally:
            timer.cancel()
            try:
                await timer
            except asyncio.CancelledError:
                pass
        return {
            **classify_ws_acceptance(result),
            "worker": result,
            "stored_event_count": sum(
                len(store.read_recent(inst_id=inst_id, channel=channel, limit=1000))
                for channel in DEFAULT_PUBLIC_CHANNELS
            ),
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--duration-seconds", type=float, default=25.0)
    parser.add_argument("--output", default="reports/blofin-demo-public-ws-acceptance.json")
    args = parser.parse_args()
    inst_id = str(args.symbol or "").strip().upper()
    if not inst_id.endswith("-USDT") or not inst_id.isascii() or not (1 <= args.duration_seconds <= 120):
        parser.error("symbol must be a USDT instrument and duration must be between 1 and 120 seconds.")

    report: dict[str, Any] = {
        "project": "CRYPTO SUPER RADAR",
        "test_type": "BLOFIN_DEMO_PUBLIC_FUTURES_WEBSOCKET_READ_ONLY",
        "environment": "BLOFIN_DEMO",
        "instrument_id": inst_id,
        "duration_seconds": args.duration_seconds,
        "generated_at_utc": None,
        "safety": {
            "public_endpoints_only": True,
            "private_websocket_used": False,
            "api_credentials_used": False,
            "account_state_read": False,
            "orders_submitted": False,
            "trade_authorization": "NO_TRADE",
        },
    }
    exit_code = 2
    try:
        with requests.Session() as session:
            response = session.get(
                f"{DEMO_REST_URL}/api/v1/market/instruments",
                params={"instId": inst_id},
                headers={"Accept": "application/json", "User-Agent": "CRYPTO-SUPER-RADAR-READONLY/1.0"},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        contract_value = parse_contract_value(payload, inst_id)
        result = asyncio.run(_run_probe(inst_id, contract_value, args.duration_seconds))
        report.update({
            "contract_value_base_per_contract": contract_value,
            "acceptance": result,
            "status": result["status"],
        })
        exit_code = 0 if result["status"] == "PASS" else 1 if result["status"] == "FAIL" else 2
    except requests.RequestException as exc:
        report.update({
            "status": "INCONCLUSIVE",
            "reason_code": "DEMO_PUBLIC_REST_UNAVAILABLE",
            "error_type": type(exc).__name__,
        })
    except Exception as exc:
        report.update({
            "status": "FAIL" if type(exc).__name__ == "BloFinDemoWebSocketError" else "INCONCLUSIVE",
            "reason_code": type(exc).__name__,
            "error": str(exc)[:240],
        })
        exit_code = 1 if report["status"] == "FAIL" else 2

    from datetime import datetime, timezone
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
