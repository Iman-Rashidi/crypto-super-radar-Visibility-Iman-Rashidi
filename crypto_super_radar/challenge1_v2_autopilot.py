"""Read-only scheduled Challenge 1 V2 market scan. No order-writing imports."""
from __future__ import annotations
from datetime import datetime, timezone
import json, os, uuid
from collections.abc import Mapping
from typing import Any

SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
TABLE = "challenge1_v2_autopilot_cycles"

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def preflight_errors(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    errors = []
    if str(env.get("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED", "false")).strip().lower() != "true":
        errors.append("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED must be true")
    if not str(env.get("TURSO_DATABASE_URL", "")).strip():
        errors.append("missing TURSO_DATABASE_URL")
    if not str(env.get("TURSO_AUTH_TOKEN", "")).strip():
        errors.append("missing TURSO_AUTH_TOKEN")
    if str(env.get("BLOFIN_DEMO_SMOKE_TEST_ENABLED", "false")).strip().lower() == "true":
        errors.append("BLOFIN_DEMO_SMOKE_TEST_ENABLED must remain false")
    if str(env.get("BLOFIN_DEMO_TRADING_ENABLED", "false")).strip().lower() == "true":
        errors.append("BLOFIN_DEMO_TRADING_ENABLED must remain false for dry-run")
    return errors

def _safe_record(record: Mapping[str, Any]) -> dict[str, Any]:
    allowed = ("execution_profile", "signal", "status", "reason", "entry_price",
               "stop_loss", "take_profit", "regime", "market_data_proof",
               "contract_specs", "market_quote", "signal_id")
    return {key: record.get(key) for key in allowed if key in record}

def persist_result(connection: Any, *, cycle_id: str, symbol: str,
                   result: Mapping[str, Any], started_at: str, finished_at: str) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS challenge1_v2_autopilot_cycles (
        cycle_id TEXT NOT NULL, symbol TEXT NOT NULL,
        started_at_utc TEXT NOT NULL, finished_at_utc TEXT NOT NULL,
        environment TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
        signal TEXT, reason TEXT, orders_submitted INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL, PRIMARY KEY (cycle_id, symbol))""")
    safe = _safe_record(result)
    connection.execute("""INSERT OR REPLACE INTO challenge1_v2_autopilot_cycles
        (cycle_id, symbol, started_at_utc, finished_at_utc, environment, mode,
         status, signal, reason, orders_submitted, payload_json)
        VALUES (?, ?, ?, ?, 'BLOFIN_DEMO', 'DRY_RUN', ?, ?, ?, 0, ?)""",
        (cycle_id, symbol, started_at, finished_at,
         str(safe.get("status") or "UNKNOWN"), str(safe.get("signal") or "UNKNOWN"),
         str(safe.get("reason") or ""),
         json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)))

def run_cycle(client: Any, connection: Any, *, analyzer=None,
              symbols: tuple[str, ...] = SYMBOLS) -> dict[str, Any]:
    if analyzer is None:
        from .challenge1_v2_orchestration import analyze_challenge1_v2_market
        analyzer = analyze_challenge1_v2_market
    cycle_id, started_at = uuid.uuid4().hex, _utc_now()
    rows = []
    for symbol in symbols:
        try:
            result = analyzer(client, symbol)
            if not isinstance(result, Mapping):
                raise TypeError("V2 analyzer returned a non-mapping result")
            safe = _safe_record(result)
            safe.update(execution_profile="CHALLENGE_1_V2", orders_submitted=False,
                        mode="DRY_RUN", environment="BLOFIN_DEMO")
        except Exception as exc:
            safe = {"execution_profile":"CHALLENGE_1_V2", "signal":"NO_TRADE",
                    "status":"DATA_BLOCKED", "reason":type(exc).__name__,
                    "orders_submitted":False, "mode":"DRY_RUN", "environment":"BLOFIN_DEMO"}
        finished_at = _utc_now()
        persist_result(connection, cycle_id=cycle_id, symbol=symbol, result=safe,
                       started_at=started_at, finished_at=finished_at)
        rows.append({"symbol":symbol, **safe})
    connection.commit()
    blocked = sum(row.get("status") == "DATA_BLOCKED" for row in rows)
    return {"cycle_id":cycle_id, "status":"CYCLE_COMPLETED" if not blocked else "CYCLE_PARTIAL",
            "mode":"DRY_RUN", "environment":"BLOFIN_DEMO", "symbols":rows,
            "orders_submitted":False, "trade_executed":False,
            "open_position_limit":4, "position_limit_enforced":False,
            "tpsl_verified":False, "ai_reviews_performed":False, "persistence":"TURSO"}

def main() -> int:
    errors = preflight_errors()
    if errors:
        print(json.dumps({"status":"SKIPPED_OR_BLOCKED","reasons":errors,"orders_submitted":False}))
        return 0 if errors == ["CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED must be true"] else 2
    try:
        from .database import connect_database, storage_backend
        if storage_backend() != "TURSO_CLOUD_SQLITE":
            raise RuntimeError("Turso persistence is required")
        from .blofin_demo_readonly import BloFinDemoReadOnly
        connection = connect_database()
        try:
            payload = run_cycle(BloFinDemoReadOnly(), connection)
        finally:
            connection.close()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        return 0 if payload["status"] == "CYCLE_COMPLETED" else 3
    except Exception as exc:
        print(json.dumps({"status":"ERROR","error_type":type(exc).__name__,"orders_submitted":False}))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
