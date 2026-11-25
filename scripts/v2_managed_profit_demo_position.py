"""One-shot CHALLENGE_1_V2 BloFin Demo position opener.

This runner ranks five live 15m momentum feeds, then analyzes only the Top-1
candidate with the existing V2 engine. V2 owns TP1/partial-close/BE/time-exit;
the exchange receives only the full-size protective SL.
"""
from __future__ import annotations

from dataclasses import replace
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

BAR_MS = 15 * 60 * 1000


class _NoopNotifier:
    def notify_async(self, *_args, **_kwargs):
        return None


class _SingleEntryExecutor:
    """Guard run_cycle so this one-shot runner can attempt at most one entry."""
    def __init__(self, executor: Any):
        self._executor = executor
        self.store = executor.store
        self.entry_attempted = False

    def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.entry_attempted:
            raise RuntimeError("One-shot Demo entry attempt limit has already been consumed")
        self.entry_attempted = True
        return self._executor.execute(*args, **kwargs)


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
            "momentum_pct": format(change.normalize(), "f"),
            "absolute_momentum_pct": format(abs(change).normalize(), "f"),
            "latest_closed_candle_ms": last_four[-1][0],
            "ticker_ts_ms": ticker_ts, "symbol_order": order,
        })
    ranked.sort(key=lambda item: (-Decimal(item["absolute_momentum_pct"]), item["symbol_order"]))
    if not ranked or Decimal(ranked[0]["absolute_momentum_pct"]) <= 0:
        raise RuntimeError("No non-zero directional momentum candidate exists")
    return ranked


def _report_proves_no_entry(report_text: str) -> bool:
    try:
        report = json.loads(report_text or "{}")
        cycle = report.get("v2_cycle") or {}
        rows = cycle.get("symbols") or []
        if cycle.get("orders_submitted") is not False or cycle.get("trade_executed") is not False:
            return False
        if not rows or len(rows) > len(SYMBOLS):
            return False
        for selected in rows:
            if not isinstance(selected, Mapping):
                return False
            if selected.get("orders_submitted") is not False or selected.get("execution_result") is not None:
                return False
            if str(selected.get("status") or "") not in {"AI_REVIEW_BLOCKED", "NO_TRADE", "SIGNAL_BLOCKED"}:
                return False
        return True
    except Exception:
        return False


def build_candle_idempotency_key(symbol: str, completed_candle_open_ms: int) -> str:
    """One idempotency scope per market and completed 15m candle open time."""
    normalized = str(symbol or "").strip().upper()
    if normalized not in SYMBOLS:
        raise ValueError(f"Unsupported idempotency symbol: {normalized or 'EMPTY'}")
    stamp = int(completed_candle_open_ms)
    if stamp <= 0:
        raise ValueError("Completed candle open timestamp must be positive")
    candle_open_utc = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return f"v2-managed-profit:{normalized}:{candle_open_utc}"


def _claim_once(db: Any, lock_key: str, *, safe_to_retry_no_entry: bool = False) -> bool:
    db.execute("""CREATE TABLE IF NOT EXISTS csr_managed_demo_position_once (
        lock_key TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        report_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.commit()
    row = db.execute(
        "SELECT status,report_json FROM csr_managed_demo_position_once WHERE lock_key=?",
        (lock_key,),
    ).fetchone()
    if row is None:
        cursor = db.execute(
            "INSERT OR IGNORE INTO csr_managed_demo_position_once(lock_key,status,report_json) VALUES(?,?,?)",
            (lock_key, "IN_PROGRESS", "{}"),
        )
        db.commit()
        return int(getattr(cursor, "rowcount", 0) or 0) == 1
    if (
        safe_to_retry_no_entry
        and str(row[0]) in {"FAILED_OR_UNKNOWN_NO_RETRY", "NO_TRADE_RETRYABLE"}
        and _report_proves_no_entry(str(row[1] or ""))
    ):
        db.execute(
            "UPDATE csr_managed_demo_position_once SET status='IN_PROGRESS',report_json='{}',updated_at=CURRENT_TIMESTAMP WHERE lock_key=?",
            (lock_key,),
        )
        db.commit()
        check = db.execute(
            "SELECT status FROM csr_managed_demo_position_once WHERE lock_key=?", (lock_key,)
        ).fetchone()
        return bool(check and str(check[0]) == "IN_PROGRESS")
    return False


def _save_report(db: Any, report: dict[str, Any], status: str, lock_key: str) -> None:
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)
    db.execute(
        "UPDATE csr_managed_demo_position_once SET status=?,report_json=?,updated_at=CURRENT_TIMESTAMP WHERE lock_key=?",
        (status, encoded, lock_key),
    )
    db.commit()
    row = db.execute(
        "SELECT status,report_json FROM csr_managed_demo_position_once WHERE lock_key=?",
        (lock_key,),
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


def _one_position_risk_policy():
    """Use the V2 risk caps but limit this one-shot runner to one concurrent position."""
    return replace(build_v2_demo_risk_policy(), max_concurrent_positions=1)


def _tp1_price(entry: str, stop: str, side: str) -> str:
    e, s = Decimal(entry), Decimal(stop)
    risk = abs(e - s)
    target = e + risk * Decimal("1.2") if side == "LONG" else e - risk * Decimal("1.2")
    return format(target.normalize(), "f")


def main() -> int:
    report: dict[str, Any] = {
        "idempotency_key": None,
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
    lock_key: str | None = None
    try:
        client = BloFinDemoTradeClient()
        if client.base_url != DEMO_REST_URL:
            raise RuntimeError("Exchange host is not the pinned BloFin Demo REST endpoint")
        executor = BloFinDemoExecutor(client=client, policy=_one_position_risk_policy(), trading_enabled=True)
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
        lock_key = build_candle_idempotency_key(
            winner["symbol"], int(winner["latest_closed_candle_ms"])
        )
        candle_open_utc = datetime.fromtimestamp(
            int(winner["latest_closed_candle_ms"]) / 1000, tz=timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        report["idempotency_key"] = lock_key
        report["completed_candle_open_utc"] = candle_open_utc
        report["momentum_ranking"] = ranked
        report["top1"] = winner
        if not _claim_once(db, lock_key, safe_to_retry_no_entry=True):
            print(json.dumps({
                "status": "DUPLICATE_SKIPPED",
                "idempotency_key": lock_key,
                "symbol": winner["symbol"],
                "completed_candle_open_utc": candle_open_utc,
            }, ensure_ascii=False))
            return 0
        _save_report(db, report, "TOP1_SELECTED", lock_key)

        direction_by_symbol = {item["symbol"]: item["direction"] for item in ranked}
        single_entry_executor = _SingleEntryExecutor(executor)

        def analyze_selected(exchange: Any, symbol: str) -> Mapping[str, Any]:
            if single_entry_executor.entry_attempted:
                return {
                    "signal": "NO_TRADE", "status": "NO_TRADE",
                    "reason": "The one-shot runner has already consumed its single entry attempt",
                    "orders_submitted": False,
                }
            expected_direction = direction_by_symbol.get(symbol)
            if expected_direction not in {"LONG", "SHORT"}:
                raise RuntimeError("Runner attempted to analyze a symbol outside the ranked Demo candidate set")
            signal = analyze_challenge1_v2_market(exchange, symbol)
            if str(signal.get("signal") or "").upper() != expected_direction:
                signal = dict(signal)
                signal.update({
                    "signal": "NO_TRADE", "status": "NO_TRADE",
                    "reason": "Ranked momentum direction does not match the formal V2 signal",
                    "orders_submitted": False,
                })
            return signal

        cycle = run_cycle(
            client, db, analyzer=analyze_selected,
            symbols=tuple(item["symbol"] for item in ranked),
            executor=single_entry_executor, environ=os.environ, notifier=_NoopNotifier(),
        )
        report["v2_cycle"] = cycle
        rows = cycle.get("symbols") or []
        if len(rows) != len(ranked):
            raise RuntimeError("V2 runner did not return one record per ranked candidate")
        executed = [
            row for row in rows
            if row.get("orders_submitted") is True and isinstance(row.get("execution_result"), Mapping)
        ]
        if len(executed) > 1:
            raise RuntimeError("One-shot Demo runner observed more than one submitted entry")
        if not executed:
            reviewed = next((row for row in rows if row.get("ai_reviews")), rows[0] if rows else {})
            review_items = reviewed.get("ai_reviews") or []
            first_review = review_items[0].get("review") if review_items and isinstance(review_items[0], Mapping) else {}
            report["candidate_outcomes"] = [
                {
                    "symbol": row.get("symbol"),
                    "signal": row.get("signal"),
                    "status": row.get("status"),
                    "reason": row.get("reason"),
                    "entry_skipped_reason": row.get("entry_skipped_reason"),
                    "orders_submitted": row.get("orders_submitted"),
                }
                for row in rows
            ]
            report["groq"] = {
                "status": "NOT_APPROVED_OR_NOT_CALLED",
                "request_attempted": bool(isinstance(first_review, Mapping) and first_review.get("request_attempted") is True),
                "http_status": first_review.get("http_status") if isinstance(first_review, Mapping) else None,
                "model_verified": bool(isinstance(first_review, Mapping) and first_review.get("model_verified") is True),
                "skip_reason_code": first_review.get("skip_reason_code") if isinstance(first_review, Mapping) else None,
            }
            report["status"] = "NO_TRADE_RETRYABLE"
            report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
            _save_report(db, report, "NO_TRADE_RETRYABLE", lock_key)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str), flush=True)
            return 0
        selected = executed[0]
        winner = next(item for item in ranked if item["symbol"] == selected["symbol"])
        report["selected_candidate"] = winner
        execution = selected["execution_result"]
        report["groq"] = _verify_groq(selected)
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
        _save_report(db, report, "POSITION_OPEN_SL_VERIFIED", lock_key)

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
        telegram = notifier.send_once(lock_key + ":entry-telegram", telegram_message)
        if telegram.get("status") != "SENT":
            raise RuntimeError("Telegram did not confirm SENT")
        report["telegram"] = telegram
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["status"] = "POSITION_OPEN_SL_VERIFIED_TELEGRAM_SENT"
        _save_report(db, report, "COMPLETE", lock_key)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str), flush=True)
        return 0
    except Exception as exc:
        report["status"] = "FAILED_OR_UNKNOWN_NO_RETRY"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:400]}
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            if lock_key:
                row = db.execute("SELECT COUNT(*) FROM csr_managed_demo_position_once WHERE lock_key=?", (lock_key,)).fetchone()
                if row and int(row[0]) == 1:
                    _save_report(db, report, "FAILED_OR_UNKNOWN_NO_RETRY", lock_key)
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
