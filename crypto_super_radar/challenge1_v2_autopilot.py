"""Scheduled Challenge 1 V2 Demo autopilot with guarded entry and exit managers."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import logging
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from typing import Any

from .ai_second_opinion import GROQ_DEFAULT_MODEL, GROQ_FALLBACK_MODEL, GROQ_ALLOWED_MODELS\nfrom .position_manager import PositionManager, ActionType

SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
TABLE = "challenge1_v2_autopilot_cycles"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enabled(value: Any) -> bool:
    return str(value or "").strip().lower() == "true"


def preflight_errors(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    errors = []
    if not _enabled(env.get("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED")):
        errors.append("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED must be true")
    if not str(env.get("TURSO_DATABASE_URL", "")).strip():
        errors.append("missing TURSO_DATABASE_URL")
    if not str(env.get("TURSO_AUTH_TOKEN", "")).strip():
        errors.append("missing TURSO_AUTH_TOKEN")
    if _enabled(env.get("BLOFIN_DEMO_SMOKE_TEST_ENABLED")):
        errors.append("BLOFIN_DEMO_SMOKE_TEST_ENABLED must remain false")
    if _enabled(env.get("BLOFIN_DEMO_TRADING_ENABLED")):
        if str(env.get("BLOFIN_DEMO_AUTOPILOT_ENABLED", "")).strip().lower() != "false":
            errors.append("legacy BLOFIN_DEMO_AUTOPILOT_ENABLED must remain false")
        from .blofin_demo_readonly import DEMO_TRADE_CREDENTIAL_ENV
        missing = [
            field for field, aliases in DEMO_TRADE_CREDENTIAL_ENV.items()
            if not any(str(env.get(alias, "")).strip() for alias in aliases)
        ]
        if missing:
            errors.append("missing BloFin Demo trade credentials: " + ",".join(missing))
    return errors


def build_v2_demo_risk_policy():
    """Return the four-slot Demo policy with evenly allocated risk and margin."""
    from .blofin_demo_executor import BloFinDemoRiskPolicy
    return BloFinDemoRiskPolicy(
        risk_per_trade_pct=0.0025,
        daily_loss_limit_pct=0.02,
        max_drawdown_pct=0.05,
        max_margin_usage_pct=0.25,
        max_aggregate_risk_pct=0.01,
        max_position_notional_usdt=5000.0,
        max_leverage=3,
        max_concurrent_positions=4,
        min_reward_risk=1.4,
    )


def _safe_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Credential-free per-symbol audit allowlist; missing proof stays missing."""
    allowed = (
        "execution_profile", "signal", "status", "reason", "reason_fa", "analysis_reason_fa",
        "entry_price", "entry", "stop_loss", "take_profit", "regime", "market_data_proof",
        "contract_specs", "market_quote", "signal_id", "signal_candle_open_utc",
        "ai_review", "second_ai_review", "ai_reviews", "ai_consensus", "ai_status",
        "ai_review_reason", "ai_request_attempted", "ai_model", "ai_decision", "ai_reason_fa",
        "ai_response_validated", "ai_model_verified", "ai_error_type", "ai_error_code",
        "ai_http_status", "ai_skip_reason_code", "orders_submitted", "execution_result",
        "entry_skipped_reason", "mode", "exit_manager_summary", "demo_entry_context",
        "easy_demo_entry_mode", "entry_gate_policy", "elliott_rejection_reason",
        "elliott_evidence_snapshot", "challenge1_v2_evidence", "challenge1_evidence",
        "elliott_wave", "wave_evidence", "trend_evidence", "filter_evidence",
        "confirmation_filters", "confirmed_filters", "failed_filters", "opposing_factors",
        "risk_factors", "reasons_fa", "score_parts", "confidence_score", "confirmation_score",
        "risk_score", "gross_rr", "net_reward_risk", "atr", "atr_value", "funding_rate",
        "spread_fraction", "rejection_code", "block_reason_code", "data_quality",
        "trade_context", "market_data_diagnostics", "fast_trigger_diagnostic",
        "early_w3_diagnostic", "wave4_fibonacci", "rsi_divergence_guard", "funding",
        "funding_classification", "entry_skipped_reason_code", "filter_results", "gate_results",
    )
    return {key: record.get(key) for key in allowed if key in record}


def persist_result(connection: Any, *, cycle_id: str, symbol: str, result: Mapping[str, Any],
                   started_at: str, finished_at: str) -> None:
    safe = _safe_record(result)

    def write_symbol_record():
        connection.execute("""CREATE TABLE IF NOT EXISTS challenge1_v2_autopilot_cycles (
            cycle_id TEXT NOT NULL, symbol TEXT NOT NULL, started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT NOT NULL, environment TEXT NOT NULL, mode TEXT NOT NULL,
            status TEXT NOT NULL, signal TEXT, reason TEXT, orders_submitted INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL, PRIMARY KEY (cycle_id, symbol))""")
        connection.execute("""INSERT OR REPLACE INTO challenge1_v2_autopilot_cycles
            (cycle_id,symbol,started_at_utc,finished_at_utc,environment,mode,status,signal,reason,orders_submitted,payload_json)
            VALUES (?,?,?,?, 'BLOFIN_DEMO',?,?,?,?,?,?)""",
            (cycle_id,symbol,started_at,finished_at,str(result.get("mode") or "DRY_RUN"),
             str(safe.get("status") or "UNKNOWN"),str(safe.get("signal") or "UNKNOWN"),
             str(safe.get("reason") or ""),int(bool(result.get("orders_submitted"))),
             json.dumps(safe,ensure_ascii=False,sort_keys=True,default=str)))

    retry_transaction = getattr(connection, "run_transaction", None)
    if callable(retry_transaction):
        retry_transaction(write_symbol_record, retries=3)
    else:
        write_symbol_record()
        connection.commit()


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("data", value)
        if isinstance(value, Mapping):
            value = [value]
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _exit_id(prefix: str, position_id: str) -> str:
    return (prefix + hashlib.sha256(position_id.encode()).hexdigest()[:20])[:32]


def manage_open_positions(client: Any, store: Any, *, now_ms: int | None = None, notifier: Any | None = None) -> dict[str, Any]:
    """Manage exact Turso-journaled V2 positions; ambiguous state never triggers a write."""
    now_ms = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    states = store.list_v2_position_exit_states(active_only=True, limit=100)
    results = []
    for wrapped in states:
        pid = ""
        try:
            state = dict(wrapped.get("payload") or wrapped)
            pid, inst = str(state.get("position_id") or ""), str(state.get("instrument_id") or "").upper()
            if not pid or not inst or state.get("status") in {"HALTED", "CLOSED"}:
                continue
            positions = _rows(client.get_positions(inst_id=inst))
            exact = [p for p in positions if str(p.get("positionId") or "") == pid
                and str(p.get("instId") or "").upper() == inst
                and str(p.get("positionSide") or "").lower() == "net"
                and Decimal(str(p.get("positions") or "0")) != 0]
            if len(exact) != 1:
                # A missing live position is not proof of closure. Reconcile exact
                # positionId against Demo partial/closed history before recording.
                if not exact:
                    try:
                        partial_rows = _rows(client.get_positions_history(inst_id=inst, state="partially_closed", limit=100))
                        closed_rows = _rows(client.get_positions_history(inst_id=inst, state="closed", limit=100))
                        outcome = store.record_closed_position_outcome(
                            instrument_id=inst, history_rows=closed_rows, partial_rows=partial_rows,
                            client_order_id=str(state.get("client_order_id") or ""), position_id=pid,
                        )
                        if isinstance(outcome, Mapping) and outcome.get("status") == "RECORDED":
                            history = [r for r in (partial_rows + closed_rows)
                                if str(r.get("positionId") or "") == pid]
                            raw_reason = " ".join(str(
                                next((r.get(k) for r in reversed(history) if r.get(k)),
                                     state.get("exit_reason") or "")
                            ) for k in ("closeType", "closeReason", "reason", "exitType")).strip().upper()
                            if str(state.get("status") or "").upper() == "TIME_EXIT_PENDING" or str(state.get("exit_order_id") or "").startswith("TIME"):
                                exit_reason = "TIME_EXIT"
                            elif any(term in raw_reason for term in ("STOP", "SL", "LOSS")):
                                exit_reason = "SL"
                            elif any(term in raw_reason for term in ("TAKE", "TP", "PROFIT")):
                                exit_reason = "TP"
                            else:
                                exit_reason = raw_reason[:48] or "EXCHANGE_CLOSED"
                            state.update(status="CLOSED", exit_reason=exit_reason,
                                closed_history_ids=outcome.get("history_ids", []))
                            store.save_v2_position_exit_state(state)
                            if notifier is not None:
                                notifier.notify_async(
                                    f"challenge1-v2:closed:{pid}",
                                    f"🏁 معامله {inst} پایان یافت\nجهت: {state.get('side')}\nدلیل خروج: {exit_reason}\n"
                                    f"PnL پس از کارمزد معاملاتی (پیش از Funding): {outcome.get('realized_pnl_after_reported_trading_fees')} USDT",
                                )
                            results.append({"position_id":pid,"status":"CLOSED_RECONCILED","exit_reason":exit_reason})
                            continue
                        results.append({"position_id":pid,"status":str((outcome or {}).get("status") or "CLOSE_HISTORY_NOT_RECONCILED")})
                        continue
                    except Exception as exc:
                        results.append({"position_id":pid,"status":"CLOSE_HISTORY_CHECK_FAILED","error_type":type(exc).__name__})
                        continue
                results.append({"position_id":pid,"status":"POSITION_NOT_EXACTLY_IDENTIFIED"})
                continue
            marks = _rows(client.get_mark_price(inst_id=inst))
            if len(marks) != 1 or not marks[0].get("markPrice"):
                results.append({"position_id":pid,"status":"MARK_PRICE_UNAVAILABLE"})
                continue
            mark, entry, stop = Decimal(str(marks[0]["markPrice"])), Decimal(str(state["entry_price"])), Decimal(str(state["initial_stop_loss"]))
            risk, side = abs(entry-stop), str(state["side"]).upper()
            if risk <= 0 or side not in {"LONG","SHORT"}:
                results.append({"position_id":pid,"status":"INVALID_RISK_STATE"})
                continue
            target = entry + risk*Decimal("1.2") if side=="LONG" else entry-risk*Decimal("1.2")
            reached = mark >= target if side=="LONG" else mark <= target
            if reached and not state.get("tp1_executed",False) and state.get("status") not in {"TIME_EXIT_PENDING","TIME_EXIT_MARKET_PENDING"}:
                cid = _exit_id("TP1",pid)
                if state.get("status") != "TP1_PENDING":
                    # Persist intent before the exchange write. A restart never blindly resubmits.
                    state.update(status="TP1_PENDING",tp1_exit_order_id=cid)
                    store.save_v2_position_exit_state(state)
                    client.partial_close_reduce_only(inst_id=inst,client_order_id=cid,position_id=pid)
                details = _rows(client.get_order_detail(inst,client_order_id=cid))
                detail = next((d for d in details if str(d.get("clientOrderId") or "")==cid),None)
                half = Decimal(str(state["original_size"]))/Decimal("2")
                if not detail or str(detail.get("state") or "").lower() not in {"filled","partially_filled"} or Decimal(str(detail.get("filledSize") or "0")) != half:
                    results.append({"position_id":pid,"status":"TP1_FILL_UNCONFIRMED"})
                    continue
                net_be, tpsl = state.get("net_break_even_price"), str(state.get("sl_tpsl_id") or "")
                if not net_be or not tpsl:
                    results.append({"position_id":pid,"status":"NET_BE_OR_TPSL_ID_MISSING"})
                    continue
                amended = client.amend_or_replace_sl(inst_id=inst,tpsl_id=tpsl,new_stop_loss=str(net_be),
                    request_id=_exit_id("BE",pid),position_id=pid)
                if amended.get("sl_verified") is not True:
                    results.append({"position_id":pid,"status":"SL_AMENDMENT_UNVERIFIED"})
                    continue
                state.update(tp1_executed=True,remaining_size=str(Decimal(str(state["original_size"]))-half),
                    status="TP1_EXECUTED",break_even_stop_loss=str(net_be),tp1_exit_order_id=cid)
                store.save_v2_position_exit_state(state)
                if notifier is not None:
                    notifier.notify_async(
                        f"challenge1-v2:tp1:{pid}",
                        f"🎯 TP1 تأیید شد: {inst}\nجهت: {side}\nپرشدن: ۵۰٪ حجم\n"
                        f"حد ضرر به نقطه سربه‌سر منتقل و تأیید شد: {net_be}",
                    )
                results.append({"position_id":pid,"status":"TP1_EXECUTED_SL_VERIFIED"})
                continue
            candles = client.get_candles(inst,bar="15m",limit=30)
            entry_ms, closed = int(state.get("entry_time_ms") or 0), set()
            for candle in candles if isinstance(candles,list) else []:
                try:
                    ts=int(candle[0])
                    if str(candle[8])=="1" and ts>=entry_ms and ts+900000<=now_ms:
                        closed.add(ts)
                except (IndexError,TypeError,ValueError):
                    continue
            state["candles_elapsed"]=max(int(state.get("candles_elapsed") or 0),len(closed))\n            # Quantitative PositionManager is wired into the live Demo monitor;\n            # existing exchange order identity/verification remains authoritative.\n            if candles and len(candles) >= 16:\n                try:\n                    rows = [x for x in candles if isinstance(x, (list, tuple)) and len(x) >= 6 and str(x[8] if len(x)>8 else "1")=="1"]\n                    closes = [float(x[4]) for x in rows]\n                    highs = [float(x[2]) for x in rows]\n                    lows = [float(x[3]) for x in rows]\n                    if len(closes) >= 15:\n                        deltas = [closes[i]-closes[i-1] for i in range(1,len(closes))]\n                        gains = sum(max(v,0) for v in deltas[-14:])/14\n                        losses = sum(max(-v,0) for v in deltas[-14:])/14\n                        rsi_now = 100.0 if losses==0 else 100-(100/(1+gains/losses))\n                        prev_deltas = deltas[:-1][-14:]\n                        pg = sum(max(v,0) for v in prev_deltas)/14 if prev_deltas else 0\n                        pl = sum(max(-v,0) for v in prev_deltas)/14 if prev_deltas else 0\n                        rsi_prev = 100.0 if pl==0 else 100-(100/(1+pg/pl))\n                        tr = [max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]\n                        atr14 = sum(tr[-14:])/14\n                        pm_action = PositionManager().on_tick({"side":side,"entry_price":float(entry),"initial_stop_loss":float(state["initial_stop_loss"]),"stop_loss":float(stop),"entry_time":datetime.fromtimestamp(int(state.get("entry_time_ms") or now_ms)/1000,timezone.utc).isoformat()}, {"close":float(closes[-1])}, {"rsi14":rsi_now,"previous_rsi14":rsi_prev,"atr14":atr14,"last_confirmed_swing_low":min(lows[-7:-2]) if len(lows)>=7 else None,"last_confirmed_swing_high":max(highs[-7:-2]) if len(highs)>=7 else None,"now":datetime.fromtimestamp(now_ms/1000,timezone.utc).isoformat()})\n                        if pm_action.action == ActionType.CLOSE:\n                            cid=_exit_id("PMX",pid); state.update(status="POSITION_MANAGER_CLOSE_PENDING",exit_order_id=cid,exit_reason=pm_action.reason); store.save_v2_position_exit_state(state)\n                            receipt=client.place_market_exit_reduce_only(inst_id=inst,client_order_id=cid,position_id=pid); state["position_manager_close_receipt"]=receipt; store.save_v2_position_exit_state(state)\n                            results.append({"position_id":pid,"status":"POSITION_MANAGER_CLOSE_SUBMITTED","reason":pm_action.reason}); continue\n                        if pm_action.action == ActionType.MOVE_STOP and pm_action.stop_loss is not None:\n                            tpsl=str(state.get("sl_tpsl_id") or "")\n                            if tpsl:\n                                amended=client.amend_or_replace_sl(inst_id=inst,tpsl_id=tpsl,new_stop_loss=str(pm_action.stop_loss),request_id=_exit_id("PMS",pid),position_id=pid)\n                                if amended.get("sl_verified") is True:\n                                    state.update(stop_loss=pm_action.stop_loss,position_manager_last_action=pm_action.reason); store.save_v2_position_exit_state(state)\n                except Exception as pm_exc:\n                    state["position_manager_error_type"]=type(pm_exc).__name__\n
            current_status=str(state.get("status") or "").upper()
            if current_status=="TIME_EXIT_MARKET_PENDING":
                market_cid=_exit_id("TMKT",pid)
                market_details=_rows(client.get_order_detail(inst,client_order_id=market_cid))
                existing=next((d for d in market_details if str(d.get("clientOrderId") or "")==market_cid),None)
                results.append({"position_id":pid,"status":"TIME_EXIT_MARKET_PENDING" if existing else "TIME_EXIT_MARKET_UNRESOLVED"})
                continue
            if current_status=="TIME_EXIT_PENDING":
                pending_cycles=int(state.get("time_exit_pending_cycles") or 0)+1
                pending_candles=max(0,state["candles_elapsed"]-int(state.get("time_exit_started_candles") or state["candles_elapsed"]))
                state.update(time_exit_pending_cycles=pending_cycles,time_exit_pending_candles=pending_candles)
                store.save_v2_position_exit_state(state)
                if pending_candles>=1 or pending_cycles>=2:
                    old_cid=str(state.get("exit_order_id") or "")
                    receipt=state.get("time_exit_receipt") or {}
                    receipt_data=receipt.get("data") if isinstance(receipt,Mapping) else None
                    receipt_row=receipt_data[0] if isinstance(receipt_data,list) and receipt_data and isinstance(receipt_data[0],Mapping) else {}
                    old_order_id=str(receipt_row.get("orderId") or "") or None
                    canceled=client.cancel_order(inst_id=inst,client_order_id=old_cid,order_id=old_order_id)
                    if str(canceled.get("status") or "")=="ALREADY_FILLED":
                        results.append({"position_id":pid,"status":"TIME_EXIT_LIMIT_ALREADY_FILLED"})
                        continue
                    market_cid=_exit_id("TMKT",pid)
                    state.update(status="TIME_EXIT_MARKET_PENDING",time_exit_market_order_id=market_cid,
                        time_exit_cancel_receipt=canceled)
                    store.save_v2_position_exit_state(state)
                    market_receipt=client.place_market_exit_reduce_only(
                        inst_id=inst,client_order_id=market_cid,position_id=pid)
                    state.update(time_exit_market_receipt=market_receipt)
                    store.save_v2_position_exit_state(state)
                    results.append({"position_id":pid,"status":"TIME_EXIT_MARKET_SUBMITTED"})
                else:
                    results.append({"position_id":pid,"status":"TIME_EXIT_ALREADY_PENDING",
                        "pending_cycles":pending_cycles,"pending_candles":pending_candles})
                continue
            if state["candles_elapsed"]>=12:
                cid=_exit_id("TIME",pid)
                state.update(status="TIME_EXIT_PENDING",exit_order_id=cid,
                    time_exit_pending_cycles=0,time_exit_pending_candles=0,
                    time_exit_started_candles=state["candles_elapsed"])
                store.save_v2_position_exit_state(state)
                instrument=client.get_tradable_instrument(inst)
                tick=Decimal(str(instrument["tickSize"]))
                close_side="sell" if side=="LONG" else "buy"
                rounding=ROUND_FLOOR if close_side=="sell" else ROUND_CEILING
                limit_price=(mark/tick).to_integral_value(rounding=rounding)*tick
                receipt=client.place_limit_exit_reduce_only(inst_id=inst,limit_price=format(limit_price,"f"),
                    client_order_id=cid,position_id=pid)
                state.update(time_exit_receipt=receipt)
                store.save_v2_position_exit_state(state)
                results.append({"position_id":pid,"status":"TIME_EXIT_SUBMITTED","limit_price":format(limit_price,"f")})
            else:
                store.save_v2_position_exit_state(state)
                results.append({"position_id":pid,"status":"MONITORED","candles_elapsed":state["candles_elapsed"]})
        except Exception as exc:
            results.append({"position_id":pid or "UNKNOWN","status":"EXIT_MANAGEMENT_ERROR","error_type":type(exc).__name__})
    return {"positions_checked":len(states),"results":results}


def _persist_entry_state(client: Any, store: Any, execution: Mapping[str, Any],
                         signal: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    pid=str(execution.get("position_id") or "")
    cid=str(execution.get("client_order_id") or "")
    inst=str(execution.get("instrument_id") or signal.get("instrument_id") or "").upper()
    if not pid or not cid or not inst:
        raise RuntimeError("Entry fill lacks exact position/order identity.")
    tpsl=_rows(client.get_open_tpsl_orders(inst_id=inst,limit=100))
    side=str(execution.get("side") or signal.get("signal") or "").upper()
    close_side="sell" if side=="LONG" else "buy"
    qty=str(execution.get("filled_size") or execution.get("quantity_contracts") or "")
    stop=str(execution.get("stop_loss") or "")
    active=[]
    for row in tpsl:
        if str(row.get("instId") or "").upper()!=inst:
            continue
        if str(row.get("positionSide") or "").lower()!="net":
            continue
        if str(row.get("marginMode") or "").lower()!="isolated":
            continue
        if str(row.get("side") or "").lower()!=close_side:
            continue
        if str(row.get("state") or "").lower()!="live":
            continue
        if str(row.get("slTriggerPriceType") or "").lower()!="mark":
            continue
        if row.get("slTriggerPrice") in (None,""):
            continue
        try:
            if Decimal(str(row.get("slTriggerPrice"))) != Decimal(stop):
                continue
            sizes=[v for v in (row.get("size"),row.get("actualSize")) if v not in (None,"")]
            if not sizes or any(Decimal(str(v)) != Decimal(qty) for v in sizes):
                continue
        except Exception:
            continue
        active.append(row)
    if len(active)!=1:
        raise RuntimeError("Exact full-size active protective SL could not be linked by supported exchange fields.")
    state={"position_id":pid,"client_order_id":cid,"instrument_id":inst,
        "side":side,
        "entry_price":str(execution.get("entry_price") or execution.get("entry_reference_price") or ""),
        "initial_stop_loss":stop,
        "original_size":qty,"remaining_size":qty,"tp1_executed":False,"candles_elapsed":0,
        "entry_time_ms":now_ms,"sl_tpsl_id":str(active[0].get("tpslId") or ""),
        "net_break_even_price":execution.get("net_break_even_price"),"status":"OPEN"}
    if not all(state[k] for k in ("side","entry_price","initial_stop_loss","original_size","sl_tpsl_id","net_break_even_price")):
        raise RuntimeError("Entry result lacks fee-adjusted net break-even or protective-order evidence.")
    return store.save_v2_position_exit_state(state)


def run_cycle(client: Any, connection: Any, *, analyzer=None, symbols: tuple[str,...]=SYMBOLS,
              executor: Any | None=None, environ: Mapping[str,str] | None=None,
              notifier: Any | None=None) -> dict[str,Any]:
    if analyzer is None:
        from .challenge1_v2_orchestration import analyze_challenge1_v2_market
        analyzer=analyze_challenge1_v2_market
    env=os.environ if environ is None else environ
    trading_active=_enabled(env.get("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED")) and _enabled(env.get("BLOFIN_DEMO_TRADING_ENABLED"))
    easy_demo_entry_mode = trading_active and _enabled(env.get("CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE"))
    if trading_active and executor is None:
        raise RuntimeError("Demo trading flags enabled without BloFinDemoExecutor.")
    cycle_id,started_at=uuid.uuid4().hex,_utc_now()
    if notifier is None and str(env.get("TELEGRAM_BOT_TOKEN") or "").strip():
        try:
            from .telegram_notifier import TelegramNotifier
            from .database import connect_database
            notifier = TelegramNotifier(
                token=str(env.get("TELEGRAM_BOT_TOKEN") or ""),
                connection_factory=connect_database,
            )
            try:
                recipient = notifier.resolve_recipient()
                logging.getLogger(__name__).info(
                    "TELEGRAM_RECIPIENT_LOCK_READY username=@%s chat_id=%s",
                    recipient["username"], recipient["chat_id"],
                )
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "TELEGRAM_RECIPIENT_LOCK_PENDING error_type=%s",
                    type(exc).__name__,
                )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "TELEGRAM_NOTIFIER_INIT_FAILED error_type=%s",
                type(exc).__name__,
            )
            notifier = None
    exit_summary=manage_open_positions(client,executor.store,notifier=notifier) if trading_active else {"positions_checked":0,"results":[]}
    rows=[]
    manage_only=_enabled(env.get("CHALLENGE_1_V2_MANAGE_ONLY"))
    for symbol in symbols:
        try:
            if manage_only:
                result={"execution_profile":"CHALLENGE_1_V2","signal":"NO_TRADE",
                    "status":"MANAGEMENT_ONLY","reason":"New entries disabled; existing V2 positions remain under exit management"}
            else:
                result=analyzer(client,symbol)
            if not isinstance(result,Mapping): raise TypeError("V2 analyzer returned a non-mapping result")
            safe=_safe_record(result)
            safe.update(execution_profile="CHALLENGE_1_V2",orders_submitted=False,
                mode="MANAGEMENT_ONLY" if manage_only else "DRY_RUN",
                exit_manager_summary=exit_summary.get("results", []))
            if easy_demo_entry_mode:
                safe.update(
                    easy_demo_entry_mode=True,
                    entry_gate_policy="EASY_DEMO_ENTRY_STRICT_EXIT",
                )
            consensus=safe.get("ai_consensus")
            approved = (
                isinstance(consensus, Mapping)
                and consensus.get("status") == "APPROVED_BY_GROQ"
                and consensus.get("single_reviewer_approval") is True
                and consensus.get("reviewer_id") == "groq"
            )
            if trading_active and approved and str(safe.get("signal") or "").upper() in {"LONG","SHORT"}:
                inst=str(result.get("instrument_id") or result.get("inst_id") or symbol).upper()
                execution=executor.execute(result,inst_id=inst,allow_demo_orders=True,
                    leverage=int(env.get("CHALLENGE_1_V2_LEVERAGE","2")))
                entry_state = _persist_entry_state(client,executor.store,execution,result,
                    now_ms=int(datetime.now(timezone.utc).timestamp()*1000))
                if notifier is not None:
                    notifier.notify_async(
                        f"challenge1-v2:entry:{entry_state['position_id']}",
                        f"🚀 معامله جدید تأیید شد: {inst}\nجهت: {entry_state['side']}\n"
                        f"قیمت ورود: {entry_state['entry_price']}\nحد ضرر SL: {entry_state['initial_stop_loss']}\n"
                        f"هدف اول TP1: {format((Decimal(entry_state['entry_price']) + (Decimal(entry_state['entry_price']) - Decimal(entry_state['initial_stop_loss'])) * Decimal('1.2') if entry_state['side'] == 'LONG' else Decimal(entry_state['entry_price']) - (Decimal(entry_state['initial_stop_loss']) - Decimal(entry_state['entry_price'])) * Decimal('1.2')).normalize(), 'f')}\n"
                        f"شناسه موقعیت: {entry_state['position_id']}\nسفارش حفاظتی SL: فعال و تأییدشده",
                    )
                safe.update(execution_result=dict(execution),orders_submitted=True,mode="DEMO_AUTOPILOT")
            elif trading_active and not manage_only:
                safe["entry_skipped_reason"]="GROQ_APPROVAL_NOT_PRESENT"
        except Exception as exc:
            # Preserve a sanitized, actionable provider/data error in the audit record.
            # Known adapters already emit credential-free messages; never serialize headers or env.
            detail = " ".join(str(exc).split())[:240]
            safe={"execution_profile":"CHALLENGE_1_V2","signal":"NO_TRADE","status":"DATA_BLOCKED",
                "reason":f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__,
                "orders_submitted":False,"mode":"DRY_RUN"}
            rejection = getattr(exc, "elliott_rejection_reason", None)
            snapshot = getattr(exc, "elliott_evidence_snapshot", None)
            if rejection:
                safe["elliott_rejection_reason"] = str(rejection)[:2000]
            if isinstance(snapshot, Mapping):
                safe["elliott_evidence_snapshot"] = dict(snapshot)
        finished_at=_utc_now()
        persist_result(connection,cycle_id=cycle_id,symbol=symbol,result=safe,
            started_at=started_at,finished_at=finished_at)
        rows.append({"symbol":symbol,**safe})
    connection.commit()
    blocked=sum(row.get("status")=="DATA_BLOCKED" for row in rows)
    submitted=any(bool(row.get("orders_submitted")) for row in rows)
    return {"cycle_id":cycle_id,"status":"CYCLE_COMPLETED" if not blocked else "CYCLE_PARTIAL",
        "mode":"DEMO_AUTOPILOT" if trading_active else "DRY_RUN","environment":"BLOFIN_DEMO",
        "symbols":rows,"orders_submitted":submitted,"trade_executed":submitted,
        "open_position_limit":4,"position_limit_enforced":trading_active,
        "entry_mode":"EASY_DEMO_ENTRY_STRICT_EXIT" if easy_demo_entry_mode else "STANDARD",
        "entry_gate_policy":{
            "opposed_4h":"ADVISORY" if easy_demo_entry_mode else "STANDARD",
            "btc_master_guard":"ADVISORY" if easy_demo_entry_mode else "STANDARD",
            "elliott_wave":"ADVISORY" if easy_demo_entry_mode else "STANDARD",
            "m15_candle_momentum":"ADVISORY" if easy_demo_entry_mode else "STANDARD",
            "m15_rsi_alignment":"ADVISORY" if easy_demo_entry_mode else "STANDARD",
            "hard_safety_gates":["fresh_market_data","funding_freshness","spread_cap",
                "atr_shock_cap","valid_stop_geometry","groq_signal_approval",
                "isolated_margin","isolated_max_3x","full_size_exchange_sl",
                "four_position_capacity","same_candle_idempotency"],
        },
        "risk_policy":{"risk_per_trade_pct":0.0025,"max_aggregate_risk_pct":0.01,
            "daily_loss_limit_pct":0.02,"max_margin_usage_pct":0.25,"leverage":2,
            "max_concurrent_positions":4,"min_reward_risk":1.4},
        "tpsl_verified":trading_active,"exit_manager":exit_summary,
        "ai_reviews_performed":any(bool(row.get("ai_reviews")) for row in rows),
        "ai_model_policy":{"provider":"Groq","primary_model":GROQ_DEFAULT_MODEL,
            "fallback_model":GROQ_FALLBACK_MODEL,"allowlist":sorted(GROQ_ALLOWED_MODELS)},
        "persistence":"TURSO"}


def main() -> int:
    errors=preflight_errors()
    if errors:
        print(json.dumps({"status":"SKIPPED_OR_BLOCKED","reasons":errors,"orders_submitted":False}))
        return 0 if errors==["CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED must be true"] else 2
    try:
        from .database import connect_database,storage_backend
        if storage_backend()!="TURSO_CLOUD_SQLITE": raise RuntimeError("Turso persistence is required")
        from .blofin_demo_readonly import BloFinDemoReadOnly
        trading_active=_enabled(os.environ.get("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED")) and _enabled(os.environ.get("BLOFIN_DEMO_TRADING_ENABLED"))
        market_client=BloFinDemoReadOnly()
        executor=None
        if trading_active:
            from .blofin_demo_executor import BloFinDemoExecutor,BloFinDemoTradeClient
            executor=BloFinDemoExecutor(client=BloFinDemoTradeClient(),policy=build_v2_demo_risk_policy())
            executor.recover_unresolved_orders_from_journal()
            market_client=executor.client
        connection=connect_database()
        try:
            payload=run_cycle(market_client,connection,executor=executor)
        finally:
            connection.close()
        print(json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str))
        return 0 if payload["status"]=="CYCLE_COMPLETED" else 3
    except Exception as exc:
        detail = " ".join(str(exc).split())[:240]
        for secret in (os.getenv("TURSO_AUTH_TOKEN", ""), os.getenv("BLOFIN_DEMO_TRADE_API_SECRET", ""),
                       os.getenv("BLOFIN_DEMO_TRADE_PASSPHRASE", "")):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        detail = re.sub(r"(?i)(?:libsql|https?)://\S+", "[ENDPOINT_REDACTED]", detail)
        detail = re.sub(r"(?i)(authorization|token|password|secret)\s*[:=]\s*[^,\s]+",
                        r"\1=[REDACTED]", detail)
        print(json.dumps({
            "status":"ERROR","error_type":type(exc).__name__,
            "error_detail":detail[:240],"orders_submitted":False,
        }))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
