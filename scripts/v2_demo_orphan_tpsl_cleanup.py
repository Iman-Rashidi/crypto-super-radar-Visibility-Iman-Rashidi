"""Cancel exact orphaned Demo TPSL orders after an emergency position close.

This utility never opens or closes positions. It only cancels matching live
SL/TP triggers for the most recent journaled emergency-closed position.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Mapping

from crypto_super_radar.blofin_demo_executor import BloFinDemoEventStore, BloFinDemoTradeClient
from crypto_super_radar.database import storage_backend

EMERGENCY_REASON = "Emergency close verified flat after V2 exit-state/SL-linkage failure."


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("data", value)
    if isinstance(value, Mapping):
        value = [value]
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _matches(row: Mapping[str, Any], order: Mapping[str, Any]) -> bool:
    inst = str(order.get("instrument_id") or "").upper()
    side = str(order.get("side") or "").upper()
    close_side = "sell" if side == "LONG" else "buy"
    try:
        size = Decimal(str(order.get("filled_size") or order.get("quantity") or "0"))
        stop = Decimal(str(order.get("stop_loss") or "0"))
        row_size = Decimal(str(row.get("size") or "0"))
        row_stop = Decimal(str(row.get("slTriggerPrice") or "0"))
    except Exception:
        return False
    actual_size = row.get("actualSize")
    if actual_size not in (None, ""):
        try:
            if Decimal(str(actual_size)) != size:
                return False
        except Exception:
            return False
    return (
        str(row.get("instId") or "").upper() == inst
        and str(row.get("marginMode") or "").lower() == "isolated"
        and str(row.get("positionSide") or "").lower() == "net"
        and str(row.get("side") or "").lower() == close_side
        and str(row.get("state") or "").lower() == "live"
        and str(row.get("slTriggerPriceType") or "").lower() == "mark"
        and row_size == size
        and row_stop == stop
        and bool(row.get("tpslId"))
    )


def main() -> int:
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("Orphan TPSL cleanup requires the Demo Turso journal.")
    store = BloFinDemoEventStore()
    client = BloFinDemoTradeClient()
    try:
        order = store.latest_order()
        if not order or order.get("status") != "CLOSED" or EMERGENCY_REASON not in str(order.get("critical_reason") or ""):
            report = {"environment": "BLOFIN_DEMO_ONLY", "status": "NO_EMERGENCY_CLOSED_ORDER", "cancelled": 0}
            print(json.dumps(report, sort_keys=True), flush=True)
            return 0
        inst = str(order.get("instrument_id") or "").upper()
        active = [row for row in _rows(client.get_open_tpsl_orders(inst_id=inst, limit=100)) if _matches(row, order)]
        canceled = []
        for row in active:
            tpsl_id = str(row["tpslId"])
            client.cancel_tpsl_order(inst_id=inst, tpsl_id=tpsl_id)
            canceled.append(tpsl_id)
        remaining = [
            row for row in _rows(client.get_open_tpsl_orders(inst_id=inst, limit=100))
            if _matches(row, order)
        ]
        report = {
            "environment": "BLOFIN_DEMO_ONLY",
            "instrument_id": inst,
            "status": "ORPHAN_TPSL_CLEAN" if not remaining else "ORPHAN_TPSL_REMAINS",
            "found": len(active),
            "cancelled": len(canceled),
            "remaining": len(remaining),
            "cancelled_tpsl_ids": canceled,
        }
        print(json.dumps(report, sort_keys=True), flush=True)
        return 0 if not remaining else 3
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
