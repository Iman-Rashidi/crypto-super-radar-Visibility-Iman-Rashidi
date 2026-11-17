"""Durable, idempotent market-analysis and Demo-trade journal for Notion.

This projection is separate from the operational event outbox. Turso stores the
latest lifecycle snapshot per learning experience; Notion receives an auditable
activity row with authentic completed-candle OHLCV, a generated chart image,
and exchange-confirmed execution/outcome fields only when available.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import requests
from PIL import Image, ImageDraw, ImageFont

from .database import connect_database, storage_backend

NOTION_ACTIVITY_DATA_SOURCE_ID = "41a6c893-0218-81f8-ace2-000ba7b9f063"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2025-09-03"
MAX_SINGLE_UPLOAD_BYTES = 4_500_000
CHART_WIDTH = 1280
CHART_HEIGHT = 820


def _text(value: Any, limit: int = 1800) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _normalise_candles(rows: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    output: list[dict[str, Any]] = []
    for row in rows[-limit:]:
        if not isinstance(row, Mapping):
            continue
        stamp = _iso(row.get("time_utc") or row.get("timestamp_utc") or row.get("time"))
        values = {key: _finite(row.get(key)) for key in ("open", "high", "low", "close", "volume", "quote_volume")}
        if not stamp or any(values[key] is None for key in ("open", "high", "low", "close", "volume")):
            continue
        if min(values[key] for key in ("open", "high", "low", "close")) <= 0 or values["volume"] < 0:
            continue
        if values["high"] < max(values["open"], values["close"], values["low"]):
            continue
        if values["low"] > min(values["open"], values["close"], values["high"]):
            continue
        output.append({"time_utc": stamp, **values})
    output.sort(key=lambda item: item["time_utc"])
    return output


def _normalise_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise ValueError("activity snapshot must be a mapping")
    experience_id = _text(snapshot.get("experience_id") or snapshot.get("activity_id"), 180)
    if not experience_id:
        raise ValueError("experience_id is required")
    candles_5m = _normalise_candles(snapshot.get("candles_5m"), limit=160)
    candles_1h = _normalise_candles(snapshot.get("candles_1h"), limit=96)
    entry = _finite(snapshot.get("entry_price"))
    stop = _finite(snapshot.get("stop_loss"))
    target = _finite(snapshot.get("take_profit"))
    actual_entry = _finite(snapshot.get("actual_entry_price"))
    actual_exit = _finite(snapshot.get("actual_exit_price"))
    realized_net = _finite(snapshot.get("realized_net_pnl_usdt"))
    return {
        "experience_id": experience_id,
        "job_id": _text(snapshot.get("job_id"), 180),
        "symbol": _text(snapshot.get("symbol"), 60).upper(),
        "signal": _text(snapshot.get("signal"), 20).upper(),
        "status": _text(snapshot.get("status"), 40).upper(),
        "reason_fa": _text(snapshot.get("reason_fa"), 1800),
        "analysis_time_utc": _iso(snapshot.get("analysis_time_utc") or snapshot.get("occurred_at_utc")),
        "signal_bar_open_utc": _iso(snapshot.get("signal_bar_open_utc")),
        "engine_version": _text(snapshot.get("engine_version") or "CHALLENGE_1_LOCAL_RULES_V1", 100),
        "patterns": list(snapshot.get("patterns") or [])[:20] if isinstance(snapshot.get("patterns"), list) else [],
        "chart_structure": _text(snapshot.get("chart_structure"), 120),
        "volume_confirmation": dict(snapshot.get("volume_confirmation") or {}) if isinstance(snapshot.get("volume_confirmation"), Mapping) else {},
        "evidence": dict(snapshot.get("evidence") or {}) if isinstance(snapshot.get("evidence"), Mapping) else {},
        "candles_5m": candles_5m,
        "candles_1h": candles_1h,
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit": target,
        "actual_entry_price": actual_entry,
        "actual_entry_time_utc": _iso(snapshot.get("actual_entry_time_utc")),
        "actual_exit_price": actual_exit,
        "actual_exit_time_utc": _iso(snapshot.get("actual_exit_time_utc")),
        "realized_net_pnl_usdt": realized_net,
        "reported_trading_fees_usdt": _finite(snapshot.get("reported_trading_fees_usdt")),
        "order_id": _text(snapshot.get("order_id"), 180),
        "position_id": _text(snapshot.get("position_id"), 180),
        "trade_executed": snapshot.get("trade_executed") is True,
        "orders_submitted": snapshot.get("orders_submitted") is True,
        "closed_confirmed": snapshot.get("closed_confirmed") is True,
        "ai_status": _text(snapshot.get("ai_status"), 40),
        "ai_model": _text(snapshot.get("ai_model"), 160),
        "ai_reason": _text(snapshot.get("ai_reason"), 1600),
        "source_url": _text(snapshot.get("source_url"), 1000),
    }


def render_candlestick_chart(snapshot: Mapping[str, Any]) -> bytes:
    """Render a real OHLCV chart. No candle, price level, or exit is fabricated."""
    candles = _normalise_candles(snapshot.get("candles_5m"), limit=120)
    if len(candles) < 5:
        raise ValueError("At least five completed 5m candles are needed for a chart.")
    width, height = CHART_WIDTH, CHART_HEIGHT
    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    title_font = ImageFont.load_default()
    left, right = 86, width - 120
    price_top, price_bottom = 100, 565
    volume_top, volume_bottom = 615, 745
    plot_width = right - left
    low_values = [row["low"] for row in candles]
    high_values = [row["high"] for row in candles]
    levels = {
        "ENTRY": _finite(snapshot.get("actual_entry_price")) or _finite(snapshot.get("entry_price")),
        "SL": _finite(snapshot.get("stop_loss")),
        "TP": _finite(snapshot.get("take_profit")),
        "EXIT": _finite(snapshot.get("actual_exit_price")) if snapshot.get("closed_confirmed") is True else None,
    }
    low = min(low_values + [value for value in levels.values() if value is not None])
    high = max(high_values + [value for value in levels.values() if value is not None])
    span = max(high - low, abs(high) * 0.001, 1e-9)
    low -= span * 0.08
    high += span * 0.08
    draw.text((left, 26), f"{_text(snapshot.get('symbol'), 40)}  |  5m completed candles  |  CHALLENGE 1", fill="#111827", font=title_font)
    draw.text((left, 48), f"Decision: {_text(snapshot.get('signal'), 20)}  |  {(_text(snapshot.get('signal_bar_open_utc'), 40) or 'timestamp unavailable')}", fill="#374151", font=font)
    draw.line((left, price_top - 12, right, price_top - 12), fill="#d1d5db", width=1)
    draw.line((left, price_bottom, right, price_bottom), fill="#9ca3af", width=1)
    for step in range(6):
        y = price_top + (price_bottom - price_top) * step / 5
        price = high - (high - low) * step / 5
        draw.line((left, int(y), right, int(y)), fill="#e5e7eb", width=1)
        draw.text((8, int(y) - 6), f"{price:.6g}", fill="#4b5563", font=font)
    maximum_volume = max((row["volume"] for row in candles), default=0.0) or 1.0
    count = len(candles)
    cell = plot_width / count
    body_width = max(2, min(8, int(cell * 0.62)))

    def y_price(value: float) -> int:
        return int(price_top + (high - value) * (price_bottom - price_top) / (high - low))

    decision_x = None
    for index, row in enumerate(candles):
        center_x = int(left + (index + 0.5) * cell)
        if index == count - 1:
            decision_x = center_x
        bullish = row["close"] >= row["open"]
        tone = "#059669" if bullish else "#dc2626"
        draw.line((center_x, y_price(row["high"]), center_x, y_price(row["low"])), fill=tone, width=2)
        body_top = y_price(max(row["open"], row["close"]))
        body_bottom = y_price(min(row["open"], row["close"]))
        if body_bottom - body_top <= 1:
            body_bottom = body_top + 2
        draw.rectangle((center_x - body_width // 2, body_top, center_x + body_width // 2, body_bottom), fill=tone, outline=tone)
        vol_height = int((row["volume"] / maximum_volume) * (volume_bottom - volume_top - 6))
        draw.rectangle((center_x - body_width // 2, volume_bottom - vol_height, center_x + body_width // 2, volume_bottom), fill=tone)
    if decision_x is not None:
        draw.line((decision_x, price_top, decision_x, volume_bottom), fill="#2563eb", width=2)
        draw.text((max(left, decision_x - 60), price_top - 26), "DECISION", fill="#1d4ed8", font=font)

    line_colors = {"ENTRY": "#2563eb", "SL": "#dc2626", "TP": "#059669", "EXIT": "#7c3aed"}
    for label, value in levels.items():
        if value is None:
            continue
        y = y_price(value)
        draw.line((left, y, right, y), fill=line_colors[label], width=2)
        draw.text((right + 8, y - 6), f"{label} {value:.6g}", fill=line_colors[label], font=font)

    draw.text((left, 584), "VOLUME", fill="#374151", font=font)
    draw.line((left, volume_bottom, right, volume_bottom), fill="#9ca3af", width=1)
    tick_indices = sorted(set([0, count // 3, (2 * count) // 3, count - 1]))
    for index in tick_indices:
        x = int(left + (index + 0.5) * cell)
        stamp = candles[index]["time_utc"].replace("T", " ").replace("Z", "")
        draw.text((max(left, min(right - 100, x - 45)), volume_bottom + 8), stamp[:16], fill="#4b5563", font=font)
    footer_y = 790
    patterns = ", ".join(_text(item.get("name_en") or item.get("name"), 40) for item in snapshot.get("patterns", [])[:4] if isinstance(item, Mapping))
    draw.text((left, footer_y), f"Patterns: {patterns or 'none confirmed'}  |  Source: BloFin Demo completed OHLCV", fill="#374151", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_ohlcv_csv(snapshot: Mapping[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["timeframe", "time_utc", "open", "high", "low", "close", "volume", "quote_volume"])
    for timeframe, rows in (("5m", snapshot.get("candles_5m") or []), ("1H", snapshot.get("candles_1h") or [])):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            writer.writerow([
                timeframe, row.get("time_utc"), row.get("open"), row.get("high"),
                row.get("low"), row.get("close"), row.get("volume"), row.get("quote_volume"),
            ])
    return output.getvalue().encode("utf-8")


class NotionActivityJournal:
    """Turso-backed, idempotent activity journal; Notion is a retryable projection."""

    def __init__(self, connection: Any | None = None) -> None:
        self._owns_db = connection is None
        self._db = connection or connect_database()
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS csr_notion_activity_journal (
                experience_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                sync_status TEXT NOT NULL DEFAULT 'PENDING',
                notion_page_id TEXT,
                last_error_type TEXT,
                last_http_status INTEGER,
                synced_at_utc TEXT
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_csr_notion_activity_pending
            ON csr_notion_activity_journal(sync_status, next_attempt_at, updated_at_utc)
        """)
        self._db.commit()

    def enqueue(self, snapshot: Mapping[str, Any]) -> bool:
        payload = _normalise_snapshot(snapshot)
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._db:
            cursor = self._db.execute(
                """INSERT INTO csr_notion_activity_journal
                   (experience_id, payload_json, created_at_utc, updated_at_utc,
                    attempts, next_attempt_at, sync_status)
                   VALUES (?, ?, ?, ?, 0, 0, 'PENDING')
                   ON CONFLICT(experience_id) DO UPDATE SET
                     payload_json=excluded.payload_json,
                     updated_at_utc=excluded.updated_at_utc,
                     next_attempt_at=0,
                     sync_status='PENDING',
                     last_error_type=NULL,
                     last_http_status=NULL""",
                (payload["experience_id"], _canonical(payload), now, now),
            )
        return bool(getattr(cursor, "rowcount", 0))

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VERSION,
        }

    def _request_json(self, method: str, url: str, token: str, *, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        headers = self._headers(token)
        headers["Content-Type"] = "application/json"
        response = requests.request(method, url, headers=headers, json=dict(body or {}), timeout=25)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Notion API returned a non-object response")
        return data

    def _upload(self, token: str, filename: str, content_type: str, payload: bytes) -> str:
        if not payload or len(payload) > MAX_SINGLE_UPLOAD_BYTES:
            raise ValueError(f"Notion single-part upload size invalid for {filename}")
        created = self._request_json(
            "POST",
            f"{NOTION_API_BASE}/file_uploads",
            token,
            body={"mode": "single_part", "filename": filename, "content_type": content_type},
        )
        upload_id = _text(created.get("id"), 80)
        if not upload_id:
            raise RuntimeError("Notion file upload response omitted id")
        response = requests.post(
            f"{NOTION_API_BASE}/file_uploads/{upload_id}/send",
            headers=self._headers(token),
            files={"file": (filename, payload, content_type)},
            timeout=45,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError("Notion file upload did not return a file id")
        return _text(result["id"], 80)

    @staticmethod
    def _rich_text(value: Any, limit: int = 1800) -> dict[str, Any]:
        text = _text(value, limit)
        return {"rich_text": [{"text": {"content": text}}]} if text else {"rich_text": []}

    @staticmethod
    def _number(value: Any) -> dict[str, Any]:
        number = _finite(value)
        return {"number": number} if number is not None else {"number": None}

    @staticmethod
    def _date(value: Any) -> dict[str, Any]:
        iso = _iso(value)
        return {"date": {"start": iso}} if iso else {"date": None}

    @staticmethod
    def _select(value: str | None) -> dict[str, Any]:
        return {"select": {"name": value}} if value else {"select": None}

    def _properties(
        self,
        snapshot: Mapping[str, Any],
        *,
        chart_upload_id: str | None,
        ohlcv_upload_id: str | None,
    ) -> dict[str, Any]:
        signal = snapshot.get("signal") if snapshot.get("signal") in {"LONG", "SHORT", "NO_TRADE"} else None
        trade_executed = snapshot.get("trade_executed") is True
        orders_submitted = snapshot.get("orders_submitted") is True
        closed = snapshot.get("closed_confirmed") is True
        status = str(snapshot.get("status") or "").upper()
        if trade_executed:
            activity_type = "معامله دمو"
            execution_status = "بسته شد" if closed else "پرشده"
        elif signal in {"LONG", "SHORT"}:
            activity_type = "پیش‌بینی بدون معامله"
            execution_status = "رد شد" if any(word in status for word in ("BLOCKED", "REJECTED")) else (
                "سفارش ارسال شد" if orders_submitted else "پیش‌بینی بدون سفارش"
            )
        elif status in {"DATA_ERROR", "ERROR", "UNKNOWN"}:
            activity_type = "خطای داده"
            execution_status = "نامشخص"
        else:
            activity_type = "عدم معامله"
            execution_status = "پیش‌بینی بدون سفارش"

        title = f"CH1 · {snapshot.get('symbol') or 'UNKNOWN'} · {snapshot.get('signal') or 'UNKNOWN'} · {snapshot.get('experience_id')}"
        properties: dict[str, Any] = {
            "فعالیت": {"title": [{"text": {"content": title[:1800]}}]},
            "شناسه تجربه": self._rich_text(snapshot.get("experience_id"), 180),
            "نماد": self._rich_text(snapshot.get("symbol"), 60),
            "بازار": {"select": {"name": "Futures"}},
            "محیط": {"select": {"name": "BloFin Demo"}},
            "منبع داده بازار": {"select": {"name": "BloFin Demo"}},
            "نوع فعالیت": {"select": {"name": activity_type}},
            "وضعیت اجرا": {"select": {"name": execution_status}},
            "زمان ثبت": self._date(snapshot.get("analysis_time_utc")),
            "تصمیم ربات": self._select(signal),
            "تایم‌فریم اجرا": {"select": {"name": "5m"}},
            "تایم‌فریم زمینه": {"select": {"name": "1H"}},
            "نسخه موتور": self._rich_text(snapshot.get("engine_version"), 120),
            "الگوی کندلی": self._rich_text(", ".join(
                _text(item.get("name_en") or item.get("name"), 80)
                for item in snapshot.get("patterns", []) if isinstance(item, Mapping)
            ), 1200),
            "تحلیل امواج الیوت": self._rich_text("در این نسخه از Challenge 1 محاسبه نشده است؛ مقدار ساختگی ثبت نمی‌شود.", 300),
            "وضعیت تحلیل الیوت": {"select": {"name": "نامشخص"}},
            "شرط فعال شدن ورود": self._rich_text(snapshot.get("reason_fa"), 1800),
            "شرط ابطال سناریو": self._rich_text(
                f"سطح ابطال ساختاری/حد ضرر: {snapshot.get('stop_loss')}" if snapshot.get("stop_loss") is not None else "",
                300,
            ),
            "قیمت ورود پیش‌بینی": self._number(snapshot.get("entry_price")),
            "حد ضرر پیش‌بینی": self._number(snapshot.get("stop_loss")),
            "هدف سود ۱": self._number(snapshot.get("take_profit")),
            "قیمت ورود واقعی": self._number(snapshot.get("actual_entry_price")),
            "زمان ورود واقعی": self._date(snapshot.get("actual_entry_time_utc")),
            "قیمت خروج واقعی": self._number(snapshot.get("actual_exit_price")),
            "زمان خروج واقعی": self._date(snapshot.get("actual_exit_time_utc")),
            "PnL تحقق‌یافته USDT": self._number(snapshot.get("realized_net_pnl_usdt")),
            "شناسه سفارش BloFin": self._rich_text(snapshot.get("order_id"), 180),
            "گزارش شواهد": self._rich_text(json.dumps({
                "signal_bar_open_utc": snapshot.get("signal_bar_open_utc"),
                "reason_fa": snapshot.get("reason_fa"),
                "chart_structure": snapshot.get("chart_structure"),
                "volume_confirmation": snapshot.get("volume_confirmation"),
                "ai_status": snapshot.get("ai_status"),
                "ai_model": snapshot.get("ai_model"),
                "ai_reason": snapshot.get("ai_reason"),
                "order_id": snapshot.get("order_id"),
                "position_id": snapshot.get("position_id"),
                "closed_confirmed": closed,
                "realized_net_pnl_usdt": snapshot.get("realized_net_pnl_usdt"),
                "evidence": snapshot.get("evidence"),
            }, ensure_ascii=False, sort_keys=True), 1800),
            "بازبینی مستقل": {"select": {"name": "در انتظار"}},
        }
        if snapshot.get("signal_bar_open_utc"):
            properties["زمان ورود فرضی"] = self._date(snapshot.get("signal_bar_open_utc"))
        if snapshot.get("ai_status"):
            properties["نقطه ضعف"] = self._rich_text(
                f"بازبینی AI: {snapshot.get('ai_status')} · {_text(snapshot.get('ai_reason'), 500)}",
                650,
            )
        if chart_upload_id:
            properties["تصویر چارت"] = {"files": [{
                "name": f"{snapshot.get('symbol')}-CH1-{snapshot.get('experience_id')[:12]}.png",
                "type": "file_upload",
                "file_upload": {"id": chart_upload_id},
            }]}
        if ohlcv_upload_id:
            properties["داده OHLCV"] = {"files": [{
                "name": f"{snapshot.get('symbol')}-OHLCV-{snapshot.get('experience_id')[:12]}.csv",
                "type": "file_upload",
                "file_upload": {"id": ohlcv_upload_id},
            }]}
        return properties

    def _find_page(self, token: str, experience_id: str) -> str | None:
        response = self._request_json(
            "POST",
            f"{NOTION_API_BASE}/data_sources/{NOTION_ACTIVITY_DATA_SOURCE_ID}/query",
            token,
            body={
                "page_size": 1,
                "filter": {
                    "property": "شناسه تجربه",
                    "rich_text": {"equals": experience_id},
                },
            },
        )
        rows = response.get("results")
        if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
            return _text(rows[0].get("id"), 80) or None
        return None

    def _sync_row(self, token: str, snapshot: Mapping[str, Any], page_id: str | None) -> str:
        chart_upload_id = None
        ohlcv_upload_id = None
        candles = snapshot.get("candles_5m") or []
        if len(candles) >= 5:
            chart = render_candlestick_chart(snapshot)
            ohlcv = render_ohlcv_csv(snapshot)
            chart_upload_id = self._upload(
                token, f"{snapshot.get('symbol')}-CH1-{snapshot.get('experience_id')[:12]}.png",
                "image/png", chart,
            )
            ohlcv_upload_id = self._upload(
                token, f"{snapshot.get('symbol')}-OHLCV-{snapshot.get('experience_id')[:12]}.csv",
                "text/csv", ohlcv,
            )
        properties = self._properties(
            snapshot, chart_upload_id=chart_upload_id, ohlcv_upload_id=ohlcv_upload_id,
        )
        if page_id:
            response = self._request_json(
                "PATCH", f"{NOTION_API_BASE}/pages/{page_id}", token,
                body={"properties": properties},
            )
            return _text(response.get("id") or page_id, 80)
        response = self._request_json(
            "POST", f"{NOTION_API_BASE}/pages", token,
            body={
                "parent": {"type": "data_source_id", "data_source_id": NOTION_ACTIVITY_DATA_SOURCE_ID},
                "properties": properties,
            },
        )
        page = _text(response.get("id"), 80)
        if not page:
            raise RuntimeError("Notion activity page response omitted id")
        return page

    def flush_pending(self, *, limit: int = 5, now: int | None = None) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        token = (os.getenv("NOTION_API_TOKEN") or "").strip()
        if not token:
            return {"status": "NOT_CONFIGURED", "pending": self.pending_count(), "synced": 0, "failed": 0}
        current = int(time.time()) if now is None else int(now)
        rows = self._db.execute(
            """SELECT experience_id, payload_json, attempts, notion_page_id
               FROM csr_notion_activity_journal
               WHERE sync_status='PENDING' AND next_attempt_at<=?
               ORDER BY updated_at_utc ASC LIMIT ?""",
            (current, min(limit, 20)),
        ).fetchall()
        synced = failed = 0
        for row in rows:
            experience_id = str(row["experience_id"])
            attempts = int(row["attempts"] or 0) + 1
            page_id = _text(row["notion_page_id"], 80) or None
            try:
                snapshot = json.loads(row["payload_json"])
                if page_id is None:
                    page_id = self._find_page(token, experience_id)
                saved_page_id = self._sync_row(token, snapshot, page_id)
                with self._db:
                    self._db.execute(
                        """UPDATE csr_notion_activity_journal SET
                           attempts=?, sync_status='SYNCED', notion_page_id=?,
                           synced_at_utc=?, last_error_type=NULL, last_http_status=NULL
                           WHERE experience_id=?""",
                        (attempts, saved_page_id, datetime.now(timezone.utc).isoformat(), experience_id),
                    )
                synced += 1
            except requests.HTTPError as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                delay = min(3600, 5 * (2 ** min(attempts - 1, 9)))
                with self._db:
                    self._db.execute(
                        """UPDATE csr_notion_activity_journal SET attempts=?, next_attempt_at=?,
                           last_error_type=?, last_http_status=? WHERE experience_id=?""",
                        (attempts, current + delay, type(exc).__name__, status_code, experience_id),
                    )
                failed += 1
            except Exception as exc:
                delay = min(3600, 5 * (2 ** min(attempts - 1, 9)))
                with self._db:
                    self._db.execute(
                        """UPDATE csr_notion_activity_journal SET attempts=?, next_attempt_at=?,
                           last_error_type=? WHERE experience_id=?""",
                        (attempts, current + delay, type(exc).__name__, experience_id),
                    )
                failed += 1
        state = self.status_snapshot()
        state.update({"synced_this_run": synced, "failed_this_run": failed})
        return state

    def pending_count(self) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM csr_notion_activity_journal WHERE sync_status='PENDING'"
        ).fetchone()
        return int(row[0] or 0)

    def status_snapshot(self) -> dict[str, Any]:
        row = self._db.execute(
            """SELECT
               SUM(CASE WHEN sync_status='PENDING' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN sync_status='SYNCED' THEN 1 ELSE 0 END) AS synced,
               SUM(CASE WHEN sync_status='FAILED' THEN 1 ELSE 0 END) AS failed,
               MAX(synced_at_utc) AS last_success_utc
               FROM csr_notion_activity_journal"""
        ).fetchone()
        latest = self._db.execute(
            """SELECT experience_id, sync_status, attempts, last_error_type,
                      last_http_status, updated_at_utc
               FROM csr_notion_activity_journal
               ORDER BY updated_at_utc DESC LIMIT 1"""
        ).fetchone()
        return {
            "status": "CONFIGURED" if bool((os.getenv("NOTION_API_TOKEN") or "").strip()) else "NOT_CONFIGURED",
            "storage_backend": storage_backend(),
            "pending": int(row["pending"] or 0),
            "synced": int(row["synced"] or 0),
            "failed": int(row["failed"] or 0),
            "last_success_utc": row["last_success_utc"],
            "latest_experience_id": latest["experience_id"] if latest else None,
            "latest_sync_status": latest["sync_status"] if latest else None,
            "latest_attempts": int(latest["attempts"] or 0) if latest else 0,
            "last_error_type": latest["last_error_type"] if latest else None,
            "last_http_status": latest["last_http_status"] if latest else None,
            "latest_updated_at_utc": latest["updated_at_utc"] if latest else None,
        }

    def get_snapshot(self, experience_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT payload_json FROM csr_notion_activity_journal WHERE experience_id=?",
            (_text(experience_id, 180),),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def close(self) -> None:
        if self._owns_db:
            self._db.close()
