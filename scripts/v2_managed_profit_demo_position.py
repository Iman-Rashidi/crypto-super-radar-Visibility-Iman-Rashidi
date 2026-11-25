"""One-shot CHALLENGE_1_V2 BloFin Demo position opener.

This runner ranks five live 15m momentum feeds, then analyzes only the Top-1
candidate with the existing V2 engine. V2 owns TP1/partial-close/BE/time-exit;
the exchange receives only the full-size protective SL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sys
from typing import Any, Mapping

from crypto_super_radar.database import connect_database, storage_backend
from crypto_super_radar.blofin_demo_readonly import DEMO_REST_URL
from crypto_super_radar.blofin_demo_executor import (
    BloFinDemoExecutor, BloFinDemoTradeClient,
)
from crypto_super_radar.challenge1_v2_autopilot import (
    SYMBOLS, build_v2_demo_risk_policy, run_cycle,
)
from crypto_super_radar.challenge1_v2_orchestration import analyze_challenge1_v2_market
from crypto_super_radar.telegram_notifier import TelegramNotifier

LOCK_KEY = "v2-managed-profit-demo-position-2026-11-25"
BAR_MS = 15 * 60 * 1000


class _NoopNotifier:
    def notify_async(self, *_args, **_kwargs):
        return None


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("data", value)
        if isinstance(value, Mapping):
            value = [value]
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def rank_top1_momentum(client: Any, *, now_ms: int | None = None) -> list[dict[str, Any]]:
    """Rank every requested market from four contiguous, completed 15m candles."""
    ranked = []
    for order, symbol in enumerate(SYMBOLS):
        candles = client.get_candles(symbol, bar="15m", limit=12)
        if not isinstance(candles, list):
            raise RuntimeError(f"{symbol}: candle response is not a list")
        closed = []
        for row in candles:
            if not isinstance(row, (list, tuple)) or len(row) < 9:
                raise RuntimeError(f"{symbol}: malformed candle row")
            if str(row[8]) != "1":
                continue
            try:
                stamp, close = int(row[0]), Decimal(str(row[4]))
            except Exception as exc:
                raise RuntimeError(f"{symbol}: invalid candle timestamp/close") from exc
            if stamp <= 0 or not close.is_finite() or close <= 0:
                raise RuntimeError(f"{symbol}: invalid candle timestamp/close")
            closed.append((stamp, close))
        closed.sort(key=lambda item: item[0])
        if len(closed) < 4:
            raise RuntimeError(f"{symbol}: fewer than four completed 15m candles")
        last_four = closed[-4:]
        if any(b[0] - a[0] != BAR_MS for a, b in zip(last_four, last_four[1:])):
            raise RuntimeError(f"{symbol}: completed 15m candles are not contiguous")
        tickers = client.get_tickers(inst_id=symbol)
        if not isinstance(tickers, list) or not tickers or not isinstance(tickers[0], Mapping):
            raise RuntimeError(f"{symbol}: current ticker unavailable")
        try:
            ticker_ts = int(tickers[0]["ts"])
        except Exception as exc:
            raise RuntimeError(f"{symbol}: ticker timestamp unavailable") from exc
        local_now = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
        latest_close = last_four[-1][0] + BAR_MS
        if ticker_ts <= 0 or abs(local_now - ticker_ts) > 60_000:
            raise RuntimeError(f"{symbol}: current ticker is stale")
        if latest_close > ticker_ts or ticker_ts - latest_close > BAR_MS:
            raise RuntimeError(f"{symbol}: completed candle history is stale")
        change = (last_four[-1][1] / last_four[0][1] - Decimal("1")) * Decimal("100")
        if change == 0:
            direction = "FLAT"
        else:
            direction = "LONG" if change > 0 else "SHORT"
        ranked.append({
            "symbol": symbol, "direction": direction,
            "momentum_pct": format(change, "f"),
            "absolute_momentum_pct": format(abs(change), "f"),
            "latest_closed_candle_ms": last_four[-1][0],
            "ticker_ts_ms": ticker_ts, "symbol_order": order,
        })
    ranked.sort(key=lambda item: (-Decimal(item["absolute_momentum_pct"]), item["symbol_order"]))
    if not ranked or Decimal(ranked[0]["absolute_momentum_pct"]) <= 0:
        raise RuntimeError("No non-zero directional momentum candidate exists")
    return ranked


def _claim_once(db: Any) -> bool:
    db.execute("""CREATE TABLE IF NOT EXISTS csr_managed_demo_position_once (
        lock_key TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        report_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.commit()
    cursor = db.execute(
        "INSERT OR IGNORE INTO csr_managed_demo_position_once(lock_key,status,report_json) VALUES(?,?,?)",
        (LOCK_KEY, "IN_PROGRESS", "{}"),
    )
    db.commit()
    return int(getattr(cursor, "rowcount", 0) or 0) == 1


def _save_report(db: Any, report: dict[str, Any], status: str) -> None:
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)
    db.execute(
        "UPDATE csr_managed_demo_position_once SET status=?,report_json=?,updated_at=CURRENT_TIMESTAMP WHERE lock_key=?",
        (status, encoded, LOCK_KEY),
    )
    db.commit()
    row = db.execute(
        "SELECT status,report_json FROM csr_managed_demo_position_once WHERE lock_key=?",
        (LOCK_KEY,),
    ).fetchone()
    if row is None or str(row[0]) != status or str(row[1]) != encoded:
        raise RuntimeError("Turso one-shot report read-back mismatch")


def _verify_groq(signal: Mapping[str, Any]) -> dict[str, Any]:
    reviews = signal.get("ai_reviews")
    if not isinstance(reviews, list) or len(reviews) != 1 or not isinstance(reviews[0], Mapping):
        raise RuntimeError("V2 requires exactly one Groq reviewer record")
    item = reviews[0]
    review = item.get("review")
    if not isinstance(review, Mapping):
        raise RuntimeError("Groq reviewer payload is missing")
    expected = "openai/gpt-oss-20b"
    if not (
        str(item.get("reviewer_id") or "").lower() == "groq"
        and review.get("http_status") == 200
        and review.get("request_attempted") is True
        and review.get("response_source") == "LIVE_API"
        and review.get("response_validated") is True
        and review.get("model_verified") is True
        and str(review.get("model") or "").split(":", 1)[0] == expected
        and str(review.get("returned_model") or "").split(":", 1)[0] == expected
    ):
        raise RuntimeError("Groq HTTP 200 / exact model identity verification failed")
    return {
        "status": "VERIFIED", "http_status": 200,
        "model": review.get("model"), "returned_model": review.get("returned_model"),
        "model_verified": True, "request_attempted": True,
        "response_source": "LIVE_API",
    }


def _tp1_price(entry: str, stop: str, side: str) -> str:
    e, s = Decimal(entry), Decimal(stop)
    risk = abs(e - s)
    target = e + risk * Decimal("1.2") if side == "LONG" else e - risk * Decimal("1.2")
    return format(target.normalize(), "f")


def main() -> int:
    report: dict[str, Any] = {
        "idempotency_key": LOCK_KEY,
        "environment": "BLOFIN_DEMO_ONLY",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "strategy": "CHALLENGE_1_V2",
        "exchange_protection": "SL_ONLY_FULL_FILLED_SIZE",
        "tp1_management": "V2_MANAGE_OPEN_POSITIONS",
    }
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("Turso cloud persistence is mandatory")
    if os.getenv("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED", "").lower() != "true":
        raise RuntimeError("V2 Demo autopilot is not explicitly enabled")
    if os.getenv("BLOFIN_DEMO_TRADING_ENABLED", "").lower() != "true":
        raise RuntimeError("BloFin Demo trading is not explicitly enabled")
    if os.getenv("BLOFIN_DEMO_AUTOPILOT_ENABLED", "false").lower() != "false":
        raise RuntimeError("Legacy autopilot must remain disabled")
    if os.getenv("BLOFIN_DEMO_SMOKE_TEST_ENABLED", "false").lower() != "false":
        raise RuntimeError("BloFin smoke-test path must remain disabled")
    if not os.getenv("GROQ_API_KEY", "").strip():
        raise RuntimeError("GROQ_API_KEY is missing")

    db = connect_database()
    executor = None
    try:
        if not _claim_once(db):
            print(json.dumps({"status": "DUPLICATE_SKIPPED", "idempotency_key": LOCK_KEY}))
            return 0
        client = BloFinDemoTradeClient()
        if client.base_url != DEMO_REST_URL:
            raise RuntimeError("Exchange host is not the pinned BloFin Demo REST endpoint")
        executor = BloFinDemoExecutor(client=client, policy=build_v2_demo_risk_policy(), trading_enabled=True)
        recovered = executor.recover_unresolved_orders_from_journal()
        if recovered.get("status") != "RECONCILED" or int(recovered.get("remaining_unresolved", -1)) != 0:
            raise RuntimeError("Unresolved Demo order journal blocks one-shot entry")
        open_positions = _rows(client.get_positions())
        pending_orders = _rows(client.get_open_orders())
        active_positions = [
            row for row in open_positions
            if Decimal(str(row.get("positions") or "0")) != 0
        ]
        if active_positions or pending_orders:
            raise RuntimeError("Existing Demo positions or pending orders block one-shot entry")

        ranked = rank_top1_momentum(client)
        winner = ranked[0]
        report["momentum_ranking"] = ranked
        report["top1"] = winner
        _save_report(db, report, "TOP1_SELECTED")

        def analyze_selected(exchange: Any, symbol: str) -> Mapping[str, Any]:
            if symbol != winner["symbol"]:
                raise RuntimeError("Runner attempted to analyze a non-Top-1 symbol")
            signal = analyze_challenge1_v2_market(exchange, symbol)
            if str(signal.get("signal") or "").upper() != winner["direction"]:
                signal = dict(signal)
                signal.update({
                    "signal": "NO_TRADE", "status": "NO_TRADE",
                    "reason": "Top-1 momentum direction does not match the formal V2 signal",
                    "orders_submitted": False,
                })
            return signal

        cycle = run_cycle(
            client, db, analyzer=analyze_selected, symbols=(winner["symbol"],),
            executor=executor, environ=os.environ, notifier=_NoopNotifier(),
        )
        report["v2_cycle"] = cycle
        rows = cycle.get("symbols") or []
        if len(rows) != 1:
            raise RuntimeError("V2 runner did not return exactly one candidate record")
        selected = rows[0]
        report["groq"] = _verify_groq(selected)
        execution = selected.get("execution_result")
        if not selected.get("orders_submitted") or not isinstance(execution, Mapping):
            raise RuntimeError("Formal V2 signal/Groq approval did not authorize a Demo entry")
        if execution.get("environment") != "BLOFIN_DEMO":
            raise RuntimeError("Execution result is not explicitly BloFin Demo")
        if execution.get("status") != "FILLED":
            raise RuntimeError("BloFin Demo entry did not reach FILLED state")
        protection = execution.get("protection") or {}
        if protection.get("sl_verified") is not True or protection.get("tp_required") is not False:
            raise RuntimeError("Full-size SL-only exchange protection was not verified")

        inst = winner["symbol"]
        entry_cid = str(execution.get("client_order_id") or "")
        position_id = str(execution.get("position_id") or "")
        entry_order_id = str(execution.get("exchange_order_id") or "")
        if not all((entry_cid, position_id, entry_order_id)):
            raise RuntimeError("Entry fill lacks exchange order/position identity")
        active_tpsl = _rows(client.get_open_tpsl_orders(inst_id=inst, client_order_id=entry_cid, limit=100))
        sl_matches = [
            row for row in active_tpsl
            if str(row.get("positionId") or "") == position_id
            and str(row.get("state") or "").lower() == "live"
            and row.get("slTriggerPrice") not in (None, "")
            and str(row.get("tpslId") or "")
        ]
        if len(sl_matches) != 1:
            raise RuntimeError("Exact live exchange SL order could not be independently read back")
        sl = sl_matches[0]
        if sl.get("tpTriggerPrice") not in (None, ""):
            raise RuntimeError("Unexpected exchange TP was attached; V2 owns TP1")
        entry_price = str(execution.get("entry_price") or execution.get("entry_reference_price") or "")
        stop_loss = str(execution.get("stop_loss") or "")
        side = str(execution.get("side") or winner["direction"]).upper()
        tp1 = _tp1_price(entry_price, stop_loss, side)
        report["position"] = {
            "instrument_id": inst, "side": side,
            "position_id": position_id, "entry_order_id": entry_order_id,
            "client_order_id": entry_cid, "entry_price_reference": entry_price,
            "filled_size": str(execution.get("filled_size") or ""),
            "leverage": 1, "margin_mode": "isolated",
            "risk_budget_usdt": execution.get("risk_budget_usdt"),
            "stop_loss": stop_loss, "sl_tpsl_id": str(sl.get("tpslId")),
            "sl_trigger_price": str(sl.get("slTriggerPrice")),
            "tp1_managed_target": tp1,
            "protection_readback": "SL_VERIFIED_FULL_SIZE; NO_EXCHANGE_TP",
        }
        _save_report(db, report, "POSITION_OPEN_SL_VERIFIED")

        telegram_message = (
            f"🚀 پوزیشن آزمایشی جدید در BloFin Demo\n"
            f"ارز: {inst}\nجهت: {side}\n"
            f"قیمت ورود مرجع: {entry_price}\n"
            f"حجم پرشده: {execution.get('filled_size')} قرارداد\n"
            f"اهرم: ایزوله 1x\n"
            f"حد ضرر SL: {stop_loss}\n"
            f"هدف TP1 (مدیریت داخلی V2): {tp1}\n"
            f"Position ID: {position_id}\n"
            "حفاظت SL روی صرافی بازخوانی و تأیید شد. TP1/بستن ۵۰٪، انتقال SL به سربه‌سر و Time Exit توسط مدیر V2 انجام می‌شود."
        )
        notifier = TelegramNotifier(connection_factory=connect_database)
        telegram = notifier.send_once(LOCK_KEY + ":entry-telegram", telegram_message)
        if telegram.get("status") != "SENT":
            raise RuntimeError("Telegram did not confirm SENT")
        report["telegram"] = telegram
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["status"] = "POSITION_OPEN_SL_VERIFIED_TELEGRAM_SENT"
        _save_report(db, report, "COMPLETE")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str), flush=True)
        return 0
    except Exception as exc:
        report["status"] = "FAILED_OR_UNKNOWN_NO_RETRY"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:400]}
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            row = db.execute("SELECT COUNT(*) FROM csr_managed_demo_position_once WHERE lock_key=?", (LOCK_KEY,)).fetchone()
            if row and int(row[0]) == 1:
                _save_report(db, report, "FAILED_OR_UNKNOWN_NO_RETRY")
        except Exception:
            pass
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str), file=sys.stderr, flush=True)
        return 1
    finally:
        if executor is not None:
            try:
                executor.store.close()
            except Exception:
                pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
