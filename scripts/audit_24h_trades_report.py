"""Read-only 24h BloFin Demo/Turso audit. No order-writing API is imported or called."""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crypto_super_radar.database import connect_database, storage_backend
from crypto_super_radar.blofin_demo_readonly import BloFinDemoReadOnly
from crypto_super_radar.telegram_notifier import TelegramNotifier

SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
START_LOCAL = datetime(2026, 11, 25, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))
START_MS = int(START_LOCAL.timestamp() * 1000)
AUDIT_KEY = "audit-24h:2026-11-25T00:00:00+01:00:v2"

def rows(value):
    if isinstance(value, dict):
        value = value.get("data", value)
        if isinstance(value, dict):
            value = [value]
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []

def num(value):
    try:
        x = float(value)
        return x if x == x and abs(x) != float("inf") else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0

def main():
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("Turso is required; local SQLite fallback is forbidden.")
    db = connect_database()
    try:
        client = BloFinDemoReadOnly()
        local_rows = db.execute(
            """SELECT client_order_id,instrument_id,side,status,position_id,created_at,outcome_recorded_at
               FROM blofin_demo_orders WHERE created_at >= ? ORDER BY created_at""",
            (START_MS // 1000,),
        ).fetchall()
        local = [dict(x) for x in local_rows]
        local_by_pid = {str(x.get("position_id")): x for x in local if x.get("position_id")}
        current_positions = rows(client.get_positions())
        current_open = [
            x for x in current_positions
            if abs(num(x.get("positions") or x.get("pos") or x.get("positionSize") or x.get("availablePositions") or x.get("availPos"))) > 0
        ]
        history = []
        for symbol in SYMBOLS:
            history.extend(rows(client.get_positions_history(inst_id=symbol, limit=100)))
        filtered = []
        for item in history:
            ts = num(item.get("createTime") or item.get("updateTime") or item.get("cTime") or item.get("uTime"))
            if 0 < ts < 10_000_000_000:
                ts *= 1000
            if ts >= START_MS:
                filtered.append(item)
        grouped = {}
        for item in filtered:
            pid = str(item.get("positionId") or item.get("posId") or "")
            if not pid:
                continue
            rec = grouped.setdefault(pid, {
                "position_id": pid, "instrument_id": str(item.get("instId") or ""),
                "states": set(), "realized_before_fees": 0.0, "fees": 0.0,
                "close_positions": 0.0, "max_positions": 0.0,
                "history_rows": 0, "local": local_by_pid.get(pid),
            })
            rec["close_positions"] += num(item.get("closePositions"))
            rec["max_positions"] = max(rec["max_positions"], num(item.get("maxPositions")))
            rec["realized_before_fees"] += num(item.get("realizedPnl"))
            rec["fees"] += abs(num(item.get("fee")))
            rec["history_rows"] += 1
        trades = []
        for rec in grouped.values():
            tolerance = max(1e-9, rec["max_positions"] * 1e-6)
            rec["states"] = ({"CLOSED"} if rec["max_positions"] > 0 and rec["close_positions"] + tolerance >= rec["max_positions"] else {"PARTIALLY_CLOSED"})
            trades.append({
                "position_id": rec["position_id"], "instrument_id": rec["instrument_id"],
                "states": sorted(rec["states"]), "local_order_matched": rec["local"] is not None,
                "local_order_status": (rec["local"] or {}).get("status"),
                "history_rows": rec["history_rows"],
                "closed_quantity": round(rec["close_positions"], 12),
                "maximum_position_quantity": round(rec["max_positions"], 12),
                "realized_pnl_before_fees_usdt": round(rec["realized_before_fees"], 8),
                "trading_fees_usdt": round(rec["fees"], 8),
                "net_after_reported_trading_fees_usdt": round(rec["realized_before_fees"] - rec["fees"], 8),
                "funding_included": False,
            })
        closed = [x for x in trades if "CLOSED" in x["states"]]
        waiting = [
            x for x in local
            if x.get("status") in {"ACCEPTED", "PARTIALLY_FILLED", "FILLED", "PENDING_RECONCILE"}
            and x.get("outcome_recorded_at") is None
        ]
        payload = {
            "status": "OK", "read_only_exchange_access": True, "orders_submitted": False,
            "window_start_local": START_LOCAL.isoformat(),
            "window_start_utc": START_LOCAL.astimezone(ZoneInfo("UTC")).isoformat(),
            "generated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
            "current_open_position_count": len(current_open),
            "current_open_positions": [{
                "instrument_id": x.get("instId"),
                "position_id": x.get("positionId") or x.get("posId"),
                "side": x.get("posSide") or x.get("side"),
                "size": x.get("positions") or x.get("pos") or x.get("positionSize") or x.get("availablePositions") or x.get("availPos"),
                "floating_pnl_usdt": num(x.get("upl") or x.get("unrealizedPnl")),
            } for x in current_open],
            "local_orders_created_in_window": len(local),
            "exchange_history_position_count": len(trades),
            "closed_position_count": len(closed),
            "closed_local_matched_count": sum(1 for x in closed if x["local_order_matched"]),
            "closed_unmatched_count": sum(1 for x in closed if not x["local_order_matched"]),
            "closed_local_matched_net_after_fees_usdt": round(sum(x["net_after_reported_trading_fees_usdt"] for x in closed if x["local_order_matched"]), 8),
            "closed_net_after_reported_trading_fees_usdt": round(sum(x["net_after_reported_trading_fees_usdt"] for x in closed), 8),
            "funding_included": False,
            "waiting_for_final_reconciliation_count": len(waiting),
            "waiting_for_final_reconciliation": [{
                "instrument_id": x.get("instrument_id"), "position_id": x.get("position_id"),
                "client_order_id": x.get("client_order_id"), "status": x.get("status"),
            } for x in waiting],
            "trades": trades,
            "limitations": [
                "Funding is excluded; net means realized PnL less reported trading fees.",
                "This read-only audit does not mutate unresolved reconciliation records.",
                "Exchange position history is limited to 100 rows per configured symbol.",
                "Only exchange position IDs matched against Turso are locally reconciled.",
            ],
        }
        # A single-use report key is reserved in Turso. No credentials enter the artifact.
        db.execute("""CREATE TABLE IF NOT EXISTS csr_audit_report_snapshots (
            event_key TEXT PRIMARY KEY, generated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL
        )""")
        if db.execute("SELECT event_key FROM csr_audit_report_snapshots WHERE event_key=?", (AUDIT_KEY,)).fetchone():
            raise RuntimeError("The one-shot audit key has already been used.")
        db.execute(
            "INSERT INTO csr_audit_report_snapshots(event_key,generated_at_utc,payload_json) VALUES(?,?,?)",
            (AUDIT_KEY, payload["generated_at_utc"], json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        db.commit()
        message = (
            "🧾 ممیزی فقط‌خواندنی BloFin Demo از ۲۵ نوامبر\n"
            f"پوزیشن باز فعلی: {len(current_open)}\n"
            f"پوزیشن بسته‌شده در تاریخچه صرافی: {len(closed)}\n"
            f"PnL پس از کارمزد معاملاتی، بدون Funding: {payload['closed_net_after_reported_trading_fees_usdt']:.4f} USDT\n"
            f"در انتظار تطبیق: {len(waiting)}\nسفارش جدید: خیر؛ فقط درخواست‌های GET صرافی."
        )
        notifier = TelegramNotifier(connection_factory=connect_database)
        sent = notifier.send_once(AUDIT_KEY, message)
        payload["telegram_status"] = sent.get("status")
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    finally:
        db.close()

if __name__ == "__main__":
    main()
