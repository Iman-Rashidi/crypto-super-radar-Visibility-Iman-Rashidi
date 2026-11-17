"""Read-only BloFin Demo trade-history and AI-operation observability.

This module never creates, changes, cancels, or closes exchange orders. It polls
exchange history and appends immutable events to the existing Turso/Notion outbox.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from .blofin_demo_executor import BloFinDemoEventStore
from .blofin_demo_readonly import BloFinDemoReadOnly
from .notion_event_sync import NotionEventOutbox

_DEFAULT_SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
_STATE_LOCK = threading.RLock()
_START_LOCK = threading.Lock()
_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None
_STATE: dict[str, Any] = {
    "enabled": False,
    "status": "NOT_STARTED",
    "environment": "BLOFIN_DEMO",
    "read_only": True,
    "interval_seconds": 60,
    "last_cycle_started_at_utc": None,
    "last_cycle_finished_at_utc": None,
    "last_success_at_utc": None,
    "symbols": [],
    "fills_observed": 0,
    "closed_positions_observed": 0,
    "events_queued": 0,
    "events_duplicate": 0,
    "errors": [],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event_time(value: Any) -> str:
    """Convert BloFin epoch milliseconds/seconds or ISO timestamp to UTC."""
    if value in (None, ""):
        return _utc_now()
    try:
        raw = str(value).strip()
        if raw.isdigit():
            epoch = int(raw)
            if epoch > 100_000_000_000:
                epoch /= 1000
            return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return _utc_now()


def _stable_row_id(row: Mapping[str, Any]) -> str:
    try:
        raw = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        raw = repr(sorted((str(k), str(v)) for k, v in row.items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _details(row: Mapping[str, Any], fields: tuple[str, ...]) -> str:
    payload = {key: row.get(key) for key in fields if key in row}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def build_fill_event(instrument_id: str, fill: Mapping[str, Any]) -> dict[str, Any]:
    """Represent one exchange-confirmed fill; a fill is not a closed trade."""
    symbol = str(instrument_id or fill.get("instId") or "").strip().upper()
    trade_id = str(fill.get("tradeId") or _stable_row_id(fill))
    order_id = str(fill.get("orderId") or "")
    timestamp = fill.get("ts") or fill.get("createTime") or fill.get("updateTime")
    fields = (
        "tradeId", "orderId", "instId", "side", "positionSide", "fillPrice",
        "fillSize", "fillPnl", "fee", "ts", "clientOrderId",
    )
    return {
        "event_id": f"blofin:fill:{symbol}:{trade_id}"[:180],
        "cycle_id": f"blofin-history:{symbol}",
        "occurred_at_utc": _event_time(timestamp),
        "event_type": "ORDER_FILLED",
        "actor": "BloFin Demo",
        "status": "SUCCESS",
        "market": "Futures",
        "symbol": symbol,
        "timeframe": "",
        "signal": "UNKNOWN",
        "ai_decision": "NOT_CALLED",
        "order_id": order_id,
        "reason": "Fill از تاریخچهٔ خصوصی و فقط‌خواندنی BloFin Demo مشاهده شد؛ به‌تنهایی به معنی بسته‌شدن پوزیشن نیست.",
        "details": _details(fill, fields),
        "source_url": "https://crypto-super-radar-blofin-demo.onrender.com/api/blofin/demo/autopilot/status",
    }


def build_closed_position_event(
    instrument_id: str, position: Mapping[str, Any]
) -> dict[str, Any]:
    """Represent one exchange-confirmed closed-position history row."""
    symbol = str(instrument_id or position.get("instId") or "").strip().upper()
    history_id = str(position.get("historyId") or _stable_row_id(position))
    position_id = str(position.get("positionId") or "")
    timestamp = position.get("updateTime") or position.get("createTime")
    fields = (
        "historyId", "positionId", "instId", "side", "positionSide",
        "closePositions", "maxPositions", "openAveragePrice", "closeAveragePrice",
        "createTime", "updateTime", "leverage", "realizedPnl",
        "realizedPnlRatio", "fee", "fundingFee",
    )
    return {
        "event_id": f"blofin:position-closed:{symbol}:{history_id}"[:180],
        "cycle_id": f"blofin-history:{symbol}",
        "occurred_at_utc": _event_time(timestamp),
        "event_type": "POSITION_CLOSED",
        "actor": "BloFin Demo",
        "status": "SUCCESS",
        "market": "Futures",
        "symbol": symbol,
        "timeframe": "",
        "signal": "UNKNOWN",
        "ai_decision": "NOT_CALLED",
        "order_id": "",
        "position_id": position_id,
        "reason": "بسته‌شدن پوزیشن در تاریخچهٔ رسمی BloFin Demo مشاهده شد؛ PnL از realizedPnl صرافی گرفته شده است.",
        "details": _details(position, fields),
        "source_url": "https://crypto-super-radar-blofin-demo.onrender.com/api/blofin/demo/autopilot/status",
    }


def build_heartbeat_event(
    *,
    positions: list[Mapping[str, Any]],
    symbols: list[str],
    status: str,
    fills_observed: int,
    closed_positions_observed: int,
    errors: list[str],
    bucket: int,
) -> dict[str, Any]:
    """Compact periodic snapshot. Bucketed event IDs prevent one page per poll."""
    compact_positions = []
    for row in positions[:8]:
        compact_positions.append({
            key: row.get(key) for key in (
                "instId", "positionSide", "positions", "averagePrice",
                "markPrice", "unrealizedPnl", "leverage", "marginMode",
            ) if key in row
        })
    detail = {
        "monitor_status": status,
        "read_only": True,
        "symbols_checked": symbols,
        "open_positions": compact_positions,
        "open_position_count": len(positions),
        "fills_observed_in_cycle": fills_observed,
        "closed_positions_observed_in_cycle": closed_positions_observed,
        "errors": errors[:8],
    }
    return {
        "event_id": f"blofin:heartbeat:{bucket}",
        "cycle_id": f"blofin-history-heartbeat:{bucket}",
        "occurred_at_utc": _utc_now(),
        "event_type": "HEARTBEAT",
        "actor": "BloFin Demo",
        "status": "SUCCESS" if status == "OK" else "WARNING",
        "market": "Futures",
        "symbol": "ACCOUNT",
        "timeframe": "",
        "signal": "UNKNOWN",
        "ai_decision": "NOT_CALLED",
        "reason": "گزارش دوره‌ای فقط‌خواندنی از وضعیت پوزیشن‌ها و تاریخچهٔ معاملات حساب Demo.",
        "details": json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str),
        "source_url": "https://crypto-super-radar-blofin-demo.onrender.com/api/notion-sync/status",
    }


def build_manual_action_event(route: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create an auditable Operator event for an explicitly requested account action."""
    body = dict(payload)
    status_value = str(body.get("status") or "UNKNOWN").upper()
    instrument = str(body.get("instrument_id") or body.get("instrument") or "").upper()
    order_id = str(body.get("exchange_order_id") or body.get("order_id") or "")
    position_id = str(body.get("position_id") or "")
    if route.endswith("/manual-open"):
        event_type = "ORDER_FILLED" if status_value == "FILLED" else (
            "ORDER_SUBMITTED" if status_value in {"ACCEPTED", "PARTIALLY_FILLED"} else "MANUAL_ACTION_FAILED"
        )
        event_status = "SUCCESS" if status_value in {"FILLED", "ACCEPTED"} else "WARNING"
        reason = f"عملیات ورود دستی توسط اپراتور؛ وضعیت گزارش‌شده از BloFin Demo: {status_value}."
    elif route.endswith("/manual-cancel-order"):
        event_type = "MANUAL_ORDER_CANCELED" if status_value == "CANCELED" else "MANUAL_ACTION_FAILED"
        event_status = "SUCCESS" if status_value == "CANCELED" else "WARNING"
        reason = f"درخواست لغو دستی سفارش مشخص؛ نتیجهٔ تأییدشده: {status_value}."
    else:
        event_type = (
            "MANUAL_POSITION_CLOSE_REQUESTED"
            if status_value == "CLOSE_ACCEPTED" else "MANUAL_ACTION_FAILED"
        )
        event_status = "PENDING" if status_value == "CLOSE_ACCEPTED" else "WARNING"
        reason = (
            "درخواست بستن دستی پوزیشن پذیرفته شد؛ هنوز بسته‌شدن نهایی یا PnL تحقق‌یافته ثابت نشده است. "
            "فقط پس از تطبیق Position History صرافی رویداد POSITION_CLOSED ثبت می‌شود."
            if status_value == "CLOSE_ACCEPTED" else
            f"درخواست بستن دستی پوزیشن با وضعیت {status_value}؛ بسته‌شدن نهایی تأیید نشده است."
        )
    identity = order_id or position_id or str(body.get("client_order_id") or uuid.uuid4().hex)
    event_id = f"manual:{route.rsplit('/', 1)[-1]}:{instrument}:{identity}"[:180]
    safe = {
        key: body.get(key) for key in (
            "status", "instrument_id", "instrument", "direction", "side", "size",
            "quantity_contracts", "exchange_order_id", "order_id", "client_order_id",
            "position_id", "position_side", "average_fill_price", "filled_size",
            "stop_loss", "take_profit", "realized_pnl_usdt", "unrealized_pnl_usdt_at_confirmation",
            "message_fa", "loss_making_position",
        ) if key in body
    }
    return {
        "event_id": event_id,
        "cycle_id": event_id,
        "occurred_at_utc": _utc_now(),
        "event_type": event_type,
        "actor": "Operator",
        "status": event_status,
        "market": "Futures",
        "symbol": instrument,
        "timeframe": "",
        "signal": "UNKNOWN",
        "ai_decision": "NOT_CALLED",
        "order_id": order_id,
        "position_id": position_id,
        "reason": reason,
        "details": json.dumps(
            {"manual_action": route.rsplit("/", 1)[-1], **safe},
            ensure_ascii=False, sort_keys=True, default=str,
        ),
        "source_url": "https://crypto-super-radar-blofin-demo.onrender.com/",
    }

def queue_manual_action(route: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Best-effort append of an already-confirmed manual action to the durable outbox."""
    outbox = None
    try:
        outbox = NotionEventOutbox()
        inserted = outbox.enqueue(build_manual_action_event(route, payload))
        return {"status": "QUEUED" if inserted else "DUPLICATE"}
    except Exception as exc:
        return {"status": "QUEUE_FAILED", "error_type": type(exc).__name__}
    finally:
        if outbox is not None:
            outbox.close()


def _configured_symbols() -> list[str]:
    raw = os.getenv("BLOFIN_DEMO_AUTOPILOT_INSTRUMENTS", ",".join(_DEFAULT_SYMBOLS))
    symbols = []
    for item in raw.split(","):
        symbol = item.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{2,20}-USDT", symbol) and symbol not in symbols:
            symbols.append(symbol)
    return symbols or list(_DEFAULT_SYMBOLS)


def _poll_once() -> dict[str, Any]:
    """Poll the read-only exchange API and enqueue stable, de-duplicated history events."""
    started = _utc_now()
    client = None
    store = None
    outbox = None
    errors: list[str] = []
    symbols = _configured_symbols()
    fills_count = closed_count = queued = duplicate = 0
    open_positions: list[dict[str, Any]] = []
    try:
        client = BloFinDemoReadOnly(timeout=12.0)
        try:
            raw_positions = client.get_positions()
            if not isinstance(raw_positions, list):
                raise ValueError("positions response was not a list")
            open_positions = [
                row for row in raw_positions
                if isinstance(row, dict) and abs(float(row.get("positions") or 0)) > 0
            ]
            for row in open_positions:
                symbol = str(row.get("instId") or "").strip().upper()
                if re.fullmatch(r"[A-Z0-9]{2,20}-USDT", symbol) and symbol not in symbols:
                    symbols.append(symbol)
        except Exception as exc:
            errors.append("positions:" + type(exc).__name__)

        store = BloFinDemoEventStore()
        try:
            for item in store.recent_all_events(limit=200):
                payload = item.get("payload") if isinstance(item, dict) else None
                symbol = str((payload or {}).get("instrument_id") or "").strip().upper()
                if re.fullmatch(r"[A-Z0-9]{2,20}-USDT", symbol) and symbol not in symbols:
                    symbols.append(symbol)
        except Exception as exc:
            errors.append("local_events:" + type(exc).__name__)

        outbox = NotionEventOutbox()
        for symbol in symbols:
            try:
                raw_fills = client.get_fills_history(inst_id=symbol, limit=100)
                if not isinstance(raw_fills, list):
                    raise ValueError("fills response was not a list")
                for row in raw_fills:
                    if not isinstance(row, dict):
                        continue
                    event = build_fill_event(symbol, row)
                    if outbox.enqueue(event):
                        queued += 1
                    else:
                        duplicate += 1
                    fills_count += 1
            except Exception as exc:
                errors.append(f"{symbol}:fills:{type(exc).__name__}")

            try:
                raw_closed = client.get_positions_history(
                    inst_id=symbol, state="closed", limit=100
                )
                if not isinstance(raw_closed, list):
                    raise ValueError("closed positions response was not a list")
                for row in raw_closed:
                    if not isinstance(row, dict):
                        continue
                    event = build_closed_position_event(symbol, row)
                    if outbox.enqueue(event):
                        queued += 1
                    else:
                        duplicate += 1
                    closed_count += 1
                try:
                    partial = client.get_positions_history(
                        inst_id=symbol, state="partially_closed", limit=100
                    )
                except Exception:
                    partial = []
                if store is not None:
                    store.record_closed_position_outcome(
                        instrument_id=symbol,
                        history_rows=raw_closed,
                        partial_rows=partial if isinstance(partial, list) else [],
                    )
            except Exception as exc:
                errors.append(f"{symbol}:closed_positions:{type(exc).__name__}")

        status = "PARTIAL" if errors else "OK"
        bucket = int(time.time() // 300)
        heartbeat = build_heartbeat_event(
            positions=open_positions,
            symbols=symbols,
            status=status,
            fills_observed=fills_count,
            closed_positions_observed=closed_count,
            errors=errors,
            bucket=bucket,
        )
        if outbox.enqueue(heartbeat):
            queued += 1
        else:
            duplicate += 1

        finished = _utc_now()
        snapshot = {
            "enabled": True,
            "status": status,
            "environment": "BLOFIN_DEMO",
            "read_only": True,
            "interval_seconds": _interval_seconds(),
            "last_cycle_started_at_utc": started,
            "last_cycle_finished_at_utc": finished,
            "last_success_at_utc": finished if not errors else None,
            "symbols": symbols,
            "open_position_count": len(open_positions),
            "fills_observed": fills_count,
            "closed_positions_observed": closed_count,
            "events_queued": queued,
            "events_duplicate": duplicate,
            "errors": errors[:20],
        }
        with _STATE_LOCK:
            previous_success = _STATE.get("last_success_at_utc")
            _STATE.update(snapshot)
            if errors and previous_success:
                _STATE["last_success_at_utc"] = previous_success
        return snapshot
    finally:
        if outbox is not None:
            outbox.close()
        if store is not None:
            store.close()
        if client is not None:
            session = getattr(client, "session", None)
            close = getattr(session, "close", None)
            if callable(close):
                close()


def _interval_seconds() -> int:
    try:
        configured = int(os.getenv("BLOFIN_DEMO_HISTORY_SYNC_INTERVAL_SECONDS", "60"))
    except (TypeError, ValueError):
        configured = 60
    return max(30, min(configured, 300))


def _worker() -> None:
    with _STATE_LOCK:
        _STATE.update({"enabled": True, "status": "STARTING", "interval_seconds": _interval_seconds()})
    while not _STOP_EVENT.is_set():
        try:
            _poll_once()
        except Exception as exc:
            with _STATE_LOCK:
                _STATE.update({
                    "enabled": True,
                    "status": "ERROR",
                    "last_cycle_finished_at_utc": _utc_now(),
                    "errors": [type(exc).__name__],
                })
        _STOP_EVENT.wait(_interval_seconds())


def start_demo_history_monitor() -> None:
    """Start one independent read-only history watcher, even when AutoPilot is off."""
    global _THREAD
    with _START_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP_EVENT.clear()
        _THREAD = threading.Thread(
            target=_worker,
            daemon=True,
            name="blofin-demo-readonly-history-monitor",
        )
        _THREAD.start()


def demo_history_monitor_status() -> dict[str, Any]:
    """Return a safe operational snapshot; never exposes API credentials."""
    with _STATE_LOCK:
        return copy.deepcopy(_STATE)
