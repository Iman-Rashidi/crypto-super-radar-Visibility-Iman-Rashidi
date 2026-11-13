#!/usr/bin/env python3
"""Execute one bounded, read-only live CRYPTO SUPER RADAR research scan.

This runner is intended for explicit operator/CI acceptance runs. It only uses
public market/news/RPC endpoints and always records the decision gates. It does
not connect wallets, load secrets, sign transactions, or submit exchange orders.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from crypto_super_radar.integrated_engine import WhaleAwareConfirmationEngine
from crypto_super_radar.models import AssetSpec

ALLOWED_ASSET = re.compile(r"^(?:cex|perp):[A-Z0-9]{3,30}$")


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def run_scan(
    asset_input: str,
    *,
    engine_factory=WhaleAwareConfirmationEngine,
) -> dict[str, Any]:
    """Run one read-only scan and return the complete, serializable evidence record."""
    if not isinstance(asset_input, str) or not ALLOWED_ASSET.fullmatch(asset_input):
        raise ValueError("Only a CEX spot/perpetual symbol such as cex:BTCUSDT or perp:BTCUSDT is allowed.")
    asset = AssetSpec.parse(asset_input)
    engine = engine_factory()
    result = engine.analyze(asset)
    record = result.to_record()
    gates = record.get("gates") or []
    gate_statuses = [str(item.get("status", "UNKNOWN")) for item in gates]
    overall = (
        "PASS" if len(gates) == 12 and all(status == "PASS" for status in gate_statuses)
        else "BLOCKED" if any(status == "FAIL" for status in gate_statuses)
        else "INCONCLUSIVE"
    )
    if record.get("signal") != "NO TRADE" and overall != "PASS":
        raise RuntimeError("Fail-closed invariant violated: a non-NO-TRADE result has a non-PASS mandatory gate.")
    return {
        "project": "CRYPTO SUPER RADAR",
        "test_type": "LIVE_READ_ONLY_INTEGRATED_RESEARCH_SCAN",
        "asset": asset_input,
        "execution_status": "COMPLETED",
        "gate_status": overall,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "safety": {
            "read_only": True,
            "public_data_only": True,
            "wallet_credentials_used": False,
            "private_exchange_credentials_used": False,
            "orders_submitted": False,
            "transactions_signed": False,
        },
        "gate_summary": {
            "count": len(gates),
            "pass": gate_statuses.count("PASS"),
            "fail": gate_statuses.count("FAIL"),
            "unknown": gate_statuses.count("UNKNOWN"),
        },
        "decision": {
            "signal": record.get("signal"),
            "safety_status": record.get("safety_status"),
            "summary_en": record.get("summary_en"),
            "summary_fa": record.get("summary_fa"),
            "decision_blockers": record.get("decision_blockers"),
        },
        "result": record,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="cex:BTCUSDT", help="One read-only CEX market to scan (default: cex:BTCUSDT)")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_scan(args.asset)
        exit_code = 0
    except Exception as exc:
        report = {
            "project": "CRYPTO SUPER RADAR",
            "test_type": "LIVE_READ_ONLY_INTEGRATED_RESEARCH_SCAN",
            "asset": args.asset,
            "execution_status": "ERROR",
            "gate_status": "INCONCLUSIVE",
            "generated_at_utc": datetime.now().astimezone().isoformat(),
            "safety": {
                "read_only": True,
                "orders_submitted": False,
                "transactions_signed": False,
            },
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
        exit_code = 1

    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=json_default)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
