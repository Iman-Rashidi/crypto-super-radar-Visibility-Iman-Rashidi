"""Emergency reconciliation for one Demo position with an unlinked protective SL.

Never opens a position. It closes only an exact, journal-linked Demo position
when its durable V2 exit-management state is missing.
"""
from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from typing import Any, Mapping

from crypto_super_radar.blofin_demo_executor import BloFinDemoEventStore, BloFinDemoTradeClient
from crypto_super_radar.database import storage_backend

SYMBOLS = ("LINK-USDT", "XRP-USDT", "BTC-USDT", "ETH-USDT", "SOL-USDT")


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if isinstance(value.get("data"), list):
            value = value["data"]
        elif isinstance(value.get("data"), Mapping):
            value = [value["data"]]
        else:
            value = [value]
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _size(row: Mapping[str, Any]) -> Decimal:
    try:
        return Decimal(str(row.get("positions") or "0"))
    except Exception:
        return Decimal("0")


def _critical_orders(store: BloFinDemoEventStore) -> list[dict[str, Any]]:
    candidates = list(store.list_unresolved_orders(limit=100))
    latest = store.latest_order()
    if latest and latest.get("status") in {"FILLED", "PARTIALLY_FILLED"}:
        if all(x.get("client_order_id") != latest.get("client_order_id") for x in candidates):
            candidates.append(latest)
    return [
        dict(order) for order in candidates
        if str(order.get("instrument_id") or "").upper() in SYMBOLS
        and str(order.get("status") or "") in {"CRITICAL_UNPROTECTED", "FILLED", "PARTIALLY_FILLED"}
    ]


def _matching_positions(rows: list[Mapping[str, Any]], order: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    inst = str(order.get("instrument_id") or "").upper()
    pid = str(order.get("position_id") or "").strip()
    side = str(order.get("side") or "").upper()
    try:
        expected = Decimal(str(order.get("filled_size") or order.get("quantity") or "0"))
    except Exception:
        expected = Decimal("0")
    matches = []
    for row in rows:
        if str(row.get("instId") or "").upper() != inst or _size(row) == 0:
            continue
        if pid and str(row.get("positionId") or "").strip() != pid:
            continue
        signed = _size(row)
        if side == "LONG" and signed <= 0:
            continue
        if side == "SHORT" and signed >= 0:
            continue
        if expected > 0 and abs(signed) != expected:
            continue
        matches.append(row)
    return matches


def _active_sl_rows(client: BloFinDemoTradeClient, inst: str, position: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    pid = str(position.get("positionId") or "").strip()
    position_side = str(position.get("positionSide") or "").lower()
    size = abs(_size(position))
    result = []
    for row in _rows(client.get_open_tpsl_orders(inst_id=inst, limit=100)):
        if (
            str(row.get("instId") or "").upper() != inst
            or str(row.get("positionId") or "").strip() != pid
            or str(row.get("positionSide") or "").lower() != position_side
            or str(row.get("state") or "").lower() != "live"
            or row.get("slTriggerPrice") in (None, "")
        ):
            continue
        try:
            if any(value not in (None, "") and Decimal(str(value)) == size
                   for value in (row.get("size"), row.get("actualSize"))):
                result.append(row)
        except Exception:
            continue
    return result


def main() -> int:
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("Emergency reconciliation requires the Demo Turso journal.")
    store = BloFinDemoEventStore()
    client = BloFinDemoTradeClient()
    report: dict[str, Any] = {
        "environment": "BLOFIN_DEMO_ONLY",
        "mode": "EMERGENCY_RECONCILE_NO_NEW_ENTRIES",
        "orders_checked": 0,
        "positions_closed": 0,
        "results": [],
    }
    try:
        for order in _critical_orders(store):
            report["orders_checked"] += 1
            cid = str(order.get("client_order_id") or "")
            inst = str(order.get("instrument_id") or "").upper()
            pid = str(order.get("position_id") or "").strip()
            if not cid or not inst:
                report["results"].append({"instrument_id": inst or None, "status": "IDENTITY_MISSING"})
                continue
            if pid:
                state = store.get_v2_position_exit_state(pid)
                if state and str(state.get("status") or "").upper() != "CLOSED":
                    report["results"].append({
                        "instrument_id": inst, "position_id": pid,
                        "status": "EXIT_STATE_PRESENT_NOT_TOUCHED",
                    })
                    continue
            positions = _rows(client.get_positions(inst_id=inst))
            matches = _matching_positions(positions, order)
            if not matches:
                store.update_order(
                    cid, "CLOSED", position_id=pid or None,
                    critical_reason="Exchange is flat for the exact journal-linked position.",
                )
                report["results"].append({
                    "instrument_id": inst, "position_id": pid or None, "status": "ALREADY_FLAT",
                })
                continue
            if len(matches) != 1:
                report["results"].append({
                    "instrument_id": inst, "position_id": pid or None,
                    "status": "AMBIGUOUS_POSITION_IDENTITY", "matching_positions": len(matches),
                })
                continue
            position = matches[0]
            exact_pid = str(position.get("positionId") or "").strip()
            margin_mode = str(position.get("marginMode") or "").lower()
            position_side = str(position.get("positionSide") or "").lower()
            if not exact_pid or margin_mode != "isolated" or position_side not in {"net", "long", "short"}:
                report["results"].append({
                    "instrument_id": inst, "position_id": exact_pid or None,
                    "status": "UNSUPPORTED_POSITION_MODE",
                    "margin_mode": margin_mode, "position_side": position_side,
                })
                continue
            try:
                sl_rows = _active_sl_rows(client, inst, position)
            except Exception as exc:
                report["results"].append({
                    "instrument_id": inst, "position_id": exact_pid,
                    "status": "PROTECTION_READ_FAILED", "error_type": type(exc).__name__,
                })
                continue
            close_id = "R" + hashlib.sha256(f"{cid}|{exact_pid}|emergency-close".encode()).hexdigest()[:24]
            close_response = None
            close_error = None
            try:
                close_response = client.close_futures_position(
                    inst_id=inst, margin_mode=margin_mode, position_side=position_side,
                    client_order_id=close_id, position_id=exact_pid,
                )
            except Exception as exc:
                close_error = type(exc).__name__
            flat = False
            for _ in range(5):
                time.sleep(1)
                remaining = _rows(client.get_positions(inst_id=inst))
                flat = not any(
                    str(row.get("instId") or "").upper() == inst
                    and str(row.get("positionId") or "").strip() == exact_pid
                    and _size(row) != 0
                    for row in remaining
                )
                if flat:
                    break
            if flat:
                store.update_order(
                    cid, "CLOSED", position_id=exact_pid,
                    filled_size=str(abs(_size(position))),
                    critical_reason="Emergency close verified flat after V2 exit-state/SL-linkage failure.",
                )
                report["positions_closed"] += 1
                after_tpsl = _rows(client.get_open_tpsl_orders(inst_id=inst, limit=100))
                orphan_tpsl = [
                    row for row in after_tpsl
                    if str(row.get("positionId") or "").strip() == exact_pid
                    and str(row.get("state") or "").lower() == "live"
                ]
                report["results"].append({
                    "instrument_id": inst, "position_id": exact_pid,
                    "status": "EMERGENCY_CLOSED_AND_FLAT" if not orphan_tpsl else "FLAT_WITH_ORPHAN_TPSL_REVIEW",
                    "active_full_size_sl_found_before_close": len(sl_rows) == 1,
                    "active_sl_count_before_close": len(sl_rows),
                    "close_order_id": close_id,
                    "close_response_received": bool(close_response),
                    "close_error_type": close_error,
                    "remaining_position_size": "0",
                    "orphan_tpsl_count_after_close": len(orphan_tpsl),
                })
            else:
                report["results"].append({
                    "instrument_id": inst, "position_id": exact_pid,
                    "status": "CRITICAL_CLOSE_NOT_VERIFIED",
                    "active_sl_count_before_close": len(sl_rows),
                    "close_order_id": close_id, "close_error_type": close_error,
                })
        safe_statuses = {"ALREADY_FLAT", "EMERGENCY_CLOSED_AND_FLAT", "EXIT_STATE_PRESENT_NOT_TOUCHED"}
        report["status"] = (
            "SAFE_FLAT" if all(item.get("status") in safe_statuses for item in report["results"])
            else "MANUAL_REVIEW_REQUIRED"
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if report["status"] == "SAFE_FLAT" else 3
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
