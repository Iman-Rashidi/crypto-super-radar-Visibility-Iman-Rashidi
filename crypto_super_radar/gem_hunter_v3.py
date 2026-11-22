"""Gem Hunter V3 research-only lifecycle, exit-liquidity and alert primitives.

No function in this module submits trades or changes Futures automation.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import requests


SCHEMA_VERSION = "gem-hunter-v3"
DEFAULT_EXIT_ORDER_USD = 500.0
MAX_EXIT_SLIPPAGE_BPS = 100.0


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def estimate_exit_slippage(
    *, liquidity_usd: Any, order_usd: Any = DEFAULT_EXIT_ORDER_USD,
    fee_bps: float = 30.0, max_slippage_bps: float = MAX_EXIT_SLIPPAGE_BPS,
) -> dict[str, Any]:
    """Conservative constant-product estimate; liquidity_usd is assumed to be pool TVL.

    The pool is assumed balanced, so quote-side reserve is TVL/2. This is an
    estimate, not a routed quote. Missing/invalid reserves remain UNKNOWN.
    """
    liquidity = _finite(liquidity_usd)
    order = _finite(order_usd)
    fee = _finite(fee_bps)
    if liquidity is None or order is None or fee is None or liquidity <= 0 or order <= 0 or fee < 0:
        return {"status": "UNKNOWN", "estimated_slippage_bps": None,
                "reason": "نقدینگی، اندازه سفارش یا کارمزد معتبر نیست."}
    reserve = liquidity / 2.0
    if order >= reserve:
        return {"status": "FAIL", "estimated_slippage_bps": None,
                "reason": "اندازه سفارش به ذخیره فرضی سمت خروج می‌رسد یا از آن بیشتر است."}
    price_impact = order / (reserve + order)
    total_slippage = 1.0 - ((1.0 - price_impact) * (1.0 - fee / 10000.0))
    bps = round(total_slippage * 10000.0, 2)
    status = "PASS" if bps <= max_slippage_bps else "FAIL"
    return {
        "status": status,
        "estimated_slippage_bps": bps,
        "order_usd": order,
        "assumed_quote_reserve_usd": round(reserve, 2),
        "fee_bps": fee,
        "model": "CONSTANT_PRODUCT_BALANCED_POOL_ESTIMATE",
        "reason": ("لغزش تخمینی در محدوده مجاز است." if status == "PASS"
                   else "لغزش تخمینی از حد مجاز بیشتر است."),
        "trade_authorization": "NO_TRADE",
    }


def score_candidate_v3(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return independent momentum, safety and data-confidence scores."""
    trades = candidate.get("pool_trade_evidence") or {}
    technical = candidate.get("technical_evidence") or {}
    security = candidate.get("security_evidence") or {}
    exit_gate = candidate.get("exit_slippage_evidence") or {}
    reconciliation = candidate.get("market_reconciliation") or {}

    momentum_parts = []
    buy = _finite(trades.get("buy_volume_usd"))
    sell = _finite(trades.get("sell_volume_usd"))
    if buy is not None and sell is not None and buy >= 0 and sell >= 0 and buy + sell > 0:
        momentum_parts.append(max(0.0, min(100.0, 100.0 * buy / (buy + sell))))
    filters = technical.get("filters") if isinstance(technical, Mapping) else {}
    if isinstance(filters, Mapping):
        known = [filters.get(key) for key in ("trend", "momentum")]
        known = [100.0 if item == "PASS" else 0.0 for item in known if item in {"PASS", "FAIL"}]
        if known:
            momentum_parts.append(sum(known) / len(known))
    momentum = round(sum(momentum_parts) / len(momentum_parts), 2) if momentum_parts else None

    security_status = security.get("status")
    exit_status = exit_gate.get("status")
    safety = None
    if security_status == "FAIL" or exit_status == "FAIL":
        safety = 0.0
    elif security_status == "PASS" and exit_status == "PASS":
        slip = _finite(exit_gate.get("estimated_slippage_bps"))
        safety = round(max(0.0, 100.0 - (slip or 0.0) / MAX_EXIT_SLIPPAGE_BPS * 30.0), 2)

    confidence_parts = []
    if reconciliation.get("status") in {"PASS", "FAIL"}:
        confidence_parts.append(100.0 if reconciliation.get("status") == "PASS" else 0.0)
    if technical.get("coverage_status") in {"COMPLETE", "PARTIAL"}:
        confidence_parts.append(100.0 if technical.get("coverage_status") == "COMPLETE" else 50.0)
    if trades.get("coverage_status") in {"COMPLETE", "PARTIAL"}:
        confidence_parts.append(100.0 if trades.get("coverage_status") == "COMPLETE" else 50.0)
    if security_status in {"PASS", "FAIL"}:
        confidence_parts.append(100.0)
    confidence = round(sum(confidence_parts) / len(confidence_parts), 2) if confidence_parts else None

    qualified = (
        momentum is not None and momentum >= 70.0
        and safety is not None and safety >= 70.0
        and confidence is not None and confidence >= 75.0
        and security_status == "PASS" and exit_status == "PASS"
        and reconciliation.get("status") == "PASS"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "short_term_momentum_smart_money_score": momentum,
        "safety_exit_liquidity_score": safety,
        "data_confidence_score": confidence,
        "candidate_status": "QUALIFIED_GEM" if qualified else "RESEARCH_WATCH",
        "trade_authorization": "NO_TRADE",
        "score_semantics": "Evidence-based research ranking, not a probability of profit or trade instruction.",
    }


class TursoGemHunterStore:
    """Minimal Turso HTTP pipeline adapter; requires TURSO_DATABASE_URL/TURSO_AUTH_TOKEN."""

    def __init__(self, database_url: Optional[str] = None, auth_token: Optional[str] = None,
                 session: Any = None, timeout: float = 12.0):
        raw = database_url or os.getenv("TURSO_DATABASE_URL")
        self.auth_token = auth_token or os.getenv("TURSO_AUTH_TOKEN")
        if not raw or not self.auth_token:
            raise RuntimeError("Turso credentials are not configured")
        self.url = raw.replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
        self.session = session or requests.Session()
        self.timeout = timeout

    @staticmethod
    def _arg(value: Any) -> dict[str, str]:
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "integer", "value": "1" if value else "0"}
        if isinstance(value, int):
            return {"type": "integer", "value": str(value)}
        if isinstance(value, float):
            return {"type": "float", "value": repr(value)}
        return {"type": "text", "value": str(value)}

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any]:
        statement = {"sql": sql, "args": [self._arg(item) for item in args]}
        response = self.session.post(
            self.url,
            headers={"Authorization": f"Bearer {self.auth_token}", "Content-Type": "application/json"},
            json={"requests": [{"type": "execute", "stmt": statement}, {"type": "close"}]},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        result = (payload.get("results") or [{}])[0].get("response", {}).get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected Turso response")
        return result

    def initialize(self) -> None:
        self.execute("""CREATE TABLE IF NOT EXISTS gem_hunter_candidates (
            candidate_id TEXT PRIMARY KEY, chain TEXT NOT NULL, token_address TEXT NOT NULL,
            first_seen_utc TEXT NOT NULL, first_price_usd REAL, latest_price_usd REAL,
            latest_liquidity_usd REAL, candidate_status TEXT NOT NULL,
            evidence_json TEXT NOT NULL, updated_at_utc TEXT NOT NULL
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS gem_hunter_lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE,
            candidate_id TEXT NOT NULL, event_type TEXT NOT NULL,
            event_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_gem_hunter_events_candidate ON gem_hunter_lifecycle_events(candidate_id, event_at_utc)")

    def upsert_candidate(self, candidate: Mapping[str, Any]) -> None:
        chain = str(candidate.get("chain") or "").lower()
        address = str(candidate.get("token_address") or "")
        if not chain or not address:
            raise ValueError("candidate chain and token_address are required")
        candidate_id = f"{chain}:{address if chain == 'solana' else address.lower()}"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        price = _finite(candidate.get("price_usd"))
        liquidity = _finite(candidate.get("liquidity_usd"))
        status = str(candidate.get("candidate_status") or "DISCOVERED")
        evidence = json.dumps(dict(candidate), ensure_ascii=False, sort_keys=True, default=str)
        self.execute(
            """INSERT INTO gem_hunter_candidates
            (candidate_id, chain, token_address, first_seen_utc, first_price_usd,
             latest_price_usd, latest_liquidity_usd, candidate_status, evidence_json, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id) DO UPDATE SET
            latest_price_usd=excluded.latest_price_usd,
            latest_liquidity_usd=excluded.latest_liquidity_usd,
            candidate_status=excluded.candidate_status,
            evidence_json=excluded.evidence_json, updated_at_utc=excluded.updated_at_utc""",
            (candidate_id, chain, address, now, price, price, liquidity, status, evidence, now),
        )

    def append_event(self, candidate_id: str, event_type: str, payload: Mapping[str, Any],
                     event_key: Optional[str] = None) -> bool:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result = self.execute(
            """INSERT OR IGNORE INTO gem_hunter_lifecycle_events
            (event_key, candidate_id, event_type, event_at_utc, payload_json)
            VALUES (?, ?, ?, ?, ?)""",
            (event_key, candidate_id, event_type, now,
             json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)),
        )
        affected = result.get("rows_affected")
        if affected is None:
            raise RuntimeError("Turso did not confirm rows_affected; refusing to assume event was stored")
        return int(affected) == 1


def persist_candidate_lifecycle(store: Any, candidate: Mapping[str, Any],
                                previous_status: Optional[str] = None) -> str:
    chain = str(candidate.get("chain") or "").lower()
    address = str(candidate.get("token_address") or "")
    if not chain or not address:
        raise ValueError("candidate chain and token_address are required")
    candidate_id = f"{chain}:{address if chain == 'solana' else address.lower()}"
    store.upsert_candidate(candidate)
    status = str(candidate.get("candidate_status") or "DISCOVERED")
    store.append_event(candidate_id, "CANDIDATE_SNAPSHOT", dict(candidate))
    if previous_status != status:
        store.append_event(candidate_id, "STATUS_CHANGED", {
            "previous_status": previous_status, "new_status": status,
        })
    return candidate_id


def notify_qualified_gem(candidate: Mapping[str, Any], store: Any, *,
                         session: Any = None, bot_token: Optional[str] = None,
                         chat_id: Optional[str] = None, timeout: float = 10.0) -> dict[str, Any]:
    """Send one Telegram research alert per token, only after all required gates pass."""
    status = candidate.get("candidate_status")
    if candidate.get("gem_hunter_v3_status", status) != "QUALIFIED_GEM" or candidate.get("trade_authorization") != "NO_TRADE":
        return {"status": "SKIPPED", "reason": "Candidate is not a research-qualified gem."}
    security = candidate.get("security_evidence") or {}
    exit_gate = candidate.get("exit_slippage_evidence") or {}
    market = candidate.get("market_reconciliation") or {}
    if security.get("status") != "PASS" or exit_gate.get("status") != "PASS" or market.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": "Security, exit-liquidity or market validation is not PASS."}
    chain = str(candidate.get("chain") or "").lower()
    address = str(candidate.get("token_address") or "")
    candidate_id = f"{chain}:{address if chain == 'solana' else address.lower()}"
    event_key = f"TELEGRAM_QUALIFIED_GEM:{candidate_id}"
    inserted = store.append_event(candidate_id, "TELEGRAM_ALERT_RESERVED", {
        "candidate_status": status, "trade_authorization": "NO_TRADE"
    }, event_key=event_key)
    if not inserted:
        return {"status": "DUPLICATE_SUPPRESSED", "candidate_id": candidate_id}
    token = bot_token or os.getenv("GEM_HUNTER_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    destination = chat_id or os.getenv("GEM_HUNTER_TELEGRAM_CHAT_ID")
    if not token or not destination:
        return {"status": "NOT_CONFIGURED", "reason": "Telegram bot token or chat id is missing."}
    momentum = candidate.get("short_term_momentum_smart_money_score")
    text = (
        "🔎 <b>QUALIFIED GEM · RESEARCH ONLY</b>\n"
        f"<b>Token:</b> {candidate.get('token_name') or 'Unknown'} ({candidate.get('token_symbol') or '?'})\n"
        f"<b>Network:</b> {chain}\n<b>Contract:</b> <code>{address}</code>\n"
        f"<b>Price:</b> {candidate.get('price_usd') or 'UNKNOWN'} USD\n"
        f"<b>Liquidity:</b> {candidate.get('liquidity_usd') or 'UNKNOWN'} USD\n"
        f"<b>Momentum:</b> {momentum if momentum is not None else 'UNKNOWN'}/100\n"
        f"<b>Reason:</b> {candidate.get('qualification_reason') or 'Security, exit-liquidity and market evidence passed.'}\n"
        "<i>No order has been placed. Research alert only.</i>"
    )
    client = session or requests
    response = client.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": destination, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError("Telegram API did not confirm the message")
    return {"status": "SENT", "candidate_id": candidate_id, "message_id": (payload.get("result") or {}).get("message_id")}



def process_gem_hunter_v3_candidates(candidates: Any, *, notify: bool = True) -> list[dict[str, Any]]:
    """Enrich a discovery batch, persist its lifecycle when Turso is configured,
    and send deduplicated research alerts. This path never authorizes a trade.
    """
    rows = [dict(row) for row in candidates if isinstance(row, Mapping)] if isinstance(candidates, list) else []
    store = None
    storage_status = "NOT_CONFIGURED"
    if os.getenv("TURSO_DATABASE_URL") and os.getenv("TURSO_AUTH_TOKEN"):
        try:
            store = TursoGemHunterStore()
            store.initialize()
            storage_status = "READY"
        except Exception as exc:
            store = None
            storage_status = f"UNKNOWN:{type(exc).__name__}"

    for row in rows:
        reconciliation = row.get("market_reconciliation") or {}
        verified_liquidity = (
            reconciliation.get("verified_liquidity_usd")
            if reconciliation.get("status") == "PASS" else None
        )
        row["exit_slippage_evidence"] = estimate_exit_slippage(
            liquidity_usd=verified_liquidity,
            order_usd=_finite(os.getenv("GEM_HUNTER_EXIT_ORDER_USD")) or DEFAULT_EXIT_ORDER_USD,
        )
        row.update(score_candidate_v3(row))
        row["trade_authorization"] = "NO_TRADE"
        row["gem_hunter_storage_status"] = storage_status
        if store is not None:
            try:
                persist_candidate_lifecycle(store, row)
                row["gem_hunter_storage_status"] = "PERSISTED"
            except Exception as exc:
                row["gem_hunter_storage_status"] = f"UNKNOWN:{type(exc).__name__}"
        if notify and store is not None and row.get("gem_hunter_v3_status") == "QUALIFIED_GEM":
            try:
                row["telegram_alert_status"] = notify_qualified_gem(row, store)
            except Exception as exc:
                row["telegram_alert_status"] = {"status": "ERROR", "reason": type(exc).__name__}
        else:
            row["telegram_alert_status"] = {"status": "NOT_SENT"}
    return rows
