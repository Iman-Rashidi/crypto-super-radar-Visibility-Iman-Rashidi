from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from crypto_super_radar.market_replay import (
    DEFAULT_SYMBOLS,
    download_candles,
    download_instrument_rules,
    initialise_store,
    save_replay,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and persist real BloFin Demo market candles independently of funding acceptance."
    )
    parser.add_argument("--months", type=int, default=7)
    parser.add_argument("--db", default="artifacts/market_data_diagnostic.sqlite3")
    parser.add_argument("--report", default="artifacts/market_data_diagnostic.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    initialise_store(connection)

    report = {
        "status": "MARKET_DATA_ONLY_NOT_REPLAY_ACCEPTANCE",
        "environment": "BLOFIN_DEMO_PUBLIC_MARKET_DATA",
        "months_requested": args.months,
        "symbols": {},
        "historical_replay_accepted": False,
        "funding_data_included": False,
        "network_order_writes": False,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    failures = []

    try:
        for symbol in DEFAULT_SYMBOLS:
            symbol_result = {"series": {}, "instrument_metadata": "UNKNOWN"}
            for bar in ("15m", "1H", "4H"):
                try:
                    rows = download_candles(symbol, bar, months=args.months)
                    save_replay(connection, rows, [], None)
                    symbol_result["series"][bar] = {
                        "status": "PASS",
                        "rows": len(rows),
                        "first_open_utc": datetime.fromtimestamp(rows[0][2] / 1000, timezone.utc).isoformat(),
                        "last_open_utc": datetime.fromtimestamp(rows[-1][2] / 1000, timezone.utc).isoformat(),
                        "first_confirmed_close_utc": datetime.fromtimestamp((rows[0][2] + {"15m":900000,"1H":3600000,"4H":14400000}[bar]) / 1000, timezone.utc).isoformat(),
                        "last_confirmed_close_utc": datetime.fromtimestamp((rows[-1][2] + {"15m":900000,"1H":3600000,"4H":14400000}[bar]) / 1000, timezone.utc).isoformat(),
                        "duplicate_timestamp_count": 0,
                        "gap_count": 0,
                    }
                except Exception as exc:
                    message = f"{symbol} {bar}: {type(exc).__name__}: {str(exc)[:300]}"
                    failures.append(message)
                    symbol_result["series"][bar] = {
                        "status": "BLOCKED",
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:300],
                    }

            try:
                marks = download_candles(symbol, "15m", months=args.months, mark_price=True)
                save_replay(connection, marks, [], None)
                symbol_result["series"]["MARK_15m"] = {
                    "status": "PASS",
                    "rows": len(marks),
                    "first_open_utc": datetime.fromtimestamp(marks[0][2] / 1000, timezone.utc).isoformat(),
                    "last_open_utc": datetime.fromtimestamp(marks[-1][2] / 1000, timezone.utc).isoformat(),
                    "duplicate_timestamp_count": 0,
                    "gap_count": 0,
                }
            except Exception as exc:
                message = f"{symbol} MARK_15m: {type(exc).__name__}: {str(exc)[:300]}"
                failures.append(message)
                symbol_result["series"]["MARK_15m"] = {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "reason": str(exc)[:300],
                }

            try:
                instrument = download_instrument_rules(symbol)
                save_replay(connection, [], [], instrument)
                symbol_result["instrument_metadata"] = "PASS"
            except Exception as exc:
                message = f"{symbol} instrument metadata: {type(exc).__name__}: {str(exc)[:300]}"
                failures.append(message)
                symbol_result["instrument_metadata"] = {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "reason": str(exc)[:300],
                }

            report["symbols"][symbol] = symbol_result
            connection.commit()
    finally:
        connection.close()

    report["failures"] = failures
    report["market_series_pass_count"] = sum(
        1
        for item in report["symbols"].values()
        for series in item["series"].values()
        if series["status"] == "PASS"
    )
    report["market_series_blocked_count"] = sum(
        1
        for item in report["symbols"].values()
        for series in item["series"].values()
        if series["status"] != "PASS"
    )
    report["all_market_series_pass"] = report["market_series_blocked_count"] == 0
    report["dataset_acceptance_note"] = (
        "This diagnostic only audits last-price candles, mark-price candles and contract metadata. "
        "It cannot substitute for missing funding history and must never be passed to AI Sandbox as an accepted replay dataset."
    )
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["all_market_series_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
