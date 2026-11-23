"""Permanent €100 Gem Hunter watchlist ledger backed by Turso/SQLite."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any, Mapping
import os
import requests

from .database import connect_database, storage_backend

TABLE = "gem_hunter_watchlist_ledger"
EARLY_PHASES = {"🟢 فاز ۱: شروع پامپ (Early Ignition)", "🟡 فاز ۲: میانه موج صعودی"}

def _number(value: Any):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 and result < float("inf") else None

def _nonnegative(value: Any):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 and result < float("inf") else None

def is_watchlist_eligible(candidate: Mapping[str, Any]) -> bool:
    tier = candidate.get("gem_hunter_v3_status", candidate.get("tier"))
    if tier in {"TIER_1_VERIFIED_GEM", "TIER_2_EARLY_ALPHA"}:
        return True
    if tier != "EARLY_ALPHA_REVIEW":
        return False
    phase = candidate.get("pump_phase_fa") or (candidate.get("pump_dump_analysis") or {}).get("pump_phase_fa")
    share = _number(candidate.get("buy_volume_share_pct"))
    if share is None:
        trades = candidate.get("pool_trade_evidence") or {}
        buy, sell = _nonnegative(trades.get("buy_volume_usd")), _nonnegative(trades.get("sell_volume_usd"))
        if buy is not None and sell is not None:
            share = 100.0 * buy / (buy + sell) if buy + sell else None
    if share is None:
        share = _number(candidate.get("narrative_buyer_share_1h_pct"))
    return phase in EARLY_PHASES and share is not None and share >= 52.0

def _token_id(chain: str, address: str) -> str:
    return f"{chain.lower()}:{address if chain.lower() == 'solana' else address.lower()}"

def ensure_watchlist_schema(db) -> None:
    with db:
        db.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            token_id TEXT PRIMARY KEY,
            first_detected_utc TEXT NOT NULL,
            token_name TEXT NOT NULL,
            token_symbol TEXT NOT NULL,
            chain TEXT NOT NULL,
            token_address TEXT NOT NULL,
            category_fa TEXT,
            initial_price_usd REAL NOT NULL,
            latest_price_usd REAL NOT NULL,
            peak_price_usd REAL NOT NULL,
            detection_count INTEGER NOT NULL DEFAULT 1,
            last_seen_utc TEXT NOT NULL,
            price_updated_utc TEXT
        )""")
        db.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_first_detected ON {TABLE}(first_detected_utc)")

def record_watchlist_candidates(db, candidates: Any) -> int:
    ensure_watchlist_schema(db)
    inserted_or_seen = 0
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with db:
        for item in candidates if isinstance(candidates, list) else []:
            if not isinstance(item, Mapping) or not is_watchlist_eligible(item):
                continue
            chain = str(item.get("chain") or "").strip().lower()
            address = str(item.get("token_address") or "").strip()
            price = _number(item.get("price_usd"))
            if not chain or not address or price is None:
                continue
            token_id = _token_id(chain, address)
            name = str(item.get("token_name") or item.get("token_symbol") or "نامشخص")
            symbol = str(item.get("token_symbol") or "?")
            category = str(item.get("category_fa") or "دسته‌بندی نامشخص")
            db.execute(f"""INSERT INTO {TABLE}
                (token_id, first_detected_utc, token_name, token_symbol, chain, token_address,
                 category_fa, initial_price_usd, latest_price_usd, peak_price_usd,
                 detection_count, last_seen_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                  detection_count={TABLE}.detection_count+1,
                  latest_price_usd=excluded.latest_price_usd,
                  peak_price_usd=CASE WHEN excluded.latest_price_usd>{TABLE}.peak_price_usd
                                      THEN excluded.latest_price_usd ELSE {TABLE}.peak_price_usd END,
                  last_seen_utc=excluded.last_seen_utc""",
                (token_id, now, name, symbol, chain, address, category, price, price, price, now))
            inserted_or_seen += 1
    return inserted_or_seen

def refresh_watchlist_prices(db, *, session=None, batch_size: int = 30) -> dict[str, Any]:
    ensure_watchlist_schema(db)
    rows = db.execute(f"SELECT token_id, chain, token_address FROM {TABLE} ORDER BY first_detected_utc").fetchall()
    updated = 0
    errors = 0
    client = session or requests
    for offset in range(0, len(rows), max(1, min(int(batch_size), 30))):
        batch = rows[offset:offset + max(1, min(int(batch_size), 30))]
        addresses = [str(row["token_address"] if isinstance(row, Mapping) else row[2]) for row in batch]
        if not addresses:
            continue
        try:
            response = client.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(addresses), timeout=20)
            response.raise_for_status()
            payload = response.json()
            pairs = payload.get("pairs") or []
            best = {}
            for pair in pairs:
                if not isinstance(pair, dict):
                    continue
                base = pair.get("baseToken") or {}
                addr = str(base.get("address") or "")
                chain = str(pair.get("chainId") or "").lower()
                price = _number(pair.get("priceUsd"))
                if not addr or price is None:
                    continue
                key = (chain, addr.lower())
                liquidity = _number((pair.get("liquidity") or {}).get("usd")) or 0
                if key not in best or liquidity > best[key][0]:
                    best[key] = (liquidity, price)
            with db:
                for row in batch:
                    chain = str(row["chain"] if isinstance(row, Mapping) else row[1]).lower()
                    addr = str(row["token_address"] if isinstance(row, Mapping) else row[2])
                    hit = best.get((chain, addr.lower()))
                    if hit:
                        db.execute(f"""UPDATE {TABLE} SET
                            latest_price_usd=?,
                            peak_price_usd=CASE WHEN ?>peak_price_usd THEN ? ELSE peak_price_usd END,
                            price_updated_utc=?
                            WHERE token_id=?""",
                            (hit[1], hit[1], hit[1], datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
                             str(row["token_id"] if isinstance(row, Mapping) else row[0])))
                        updated += 1
        except Exception:
            errors += 1
    alerts = send_watchlist_profit_alerts(db, session=client)
    return {"status": "OK" if errors == 0 else ("PARTIAL" if updated else "UNKNOWN"),
            "tokens_total": len(rows), "prices_updated": updated, "batch_errors": errors,
            "profit_alerts": alerts}


PROFIT_MILESTONES_PCT = (25, 50, 100)
PROFIT_ALERT_TABLE = "gem_hunter_watchlist_profit_alerts"


def ensure_profit_alert_schema(db) -> None:
    with db:
        db.execute(f"""CREATE TABLE IF NOT EXISTS {PROFIT_ALERT_TABLE} (
            token_id TEXT NOT NULL,
            threshold_pct INTEGER NOT NULL,
            reserved_at_utc TEXT NOT NULL,
            sent_at_utc TEXT,
            telegram_message_id TEXT,
            PRIMARY KEY (token_id, threshold_pct)
        )""")


def send_watchlist_profit_alerts(db, *, session=None, bot_token=None, chat_id=None) -> dict[str, Any]:
    """Send one Persian Telegram alert per token/milestone, based on current price vs first discovery."""
    token = (bot_token or os.getenv("GEM_HUNTER_TELEGRAM_BOT_TOKEN")
             or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    destination = (chat_id or os.getenv("GEM_HUNTER_TELEGRAM_CHAT_ID")
                   or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not destination:
        return {"status": "NOT_CONFIGURED", "sent": 0, "duplicates": 0, "errors": 0}
    ensure_watchlist_schema(db)
    ensure_profit_alert_schema(db)
    rows = db.execute(
        f"SELECT token_id, first_detected_utc, token_name, token_symbol, chain, token_address, "
        f"initial_price_usd, latest_price_usd FROM {TABLE} ORDER BY first_detected_utc"
    ).fetchall()
    client = session or requests
    sent = duplicates = errors = 0
    for row in rows:
        item = dict(row) if isinstance(row, Mapping) else {
            key: row[index] for index, key in enumerate((
                "token_id", "first_detected_utc", "token_name", "token_symbol", "chain",
                "token_address", "initial_price_usd", "latest_price_usd"
            ))
        }
        initial, latest = _number(item.get("initial_price_usd")), _number(item.get("latest_price_usd"))
        if initial is None or latest is None:
            continue
        change_pct = (latest / initial - 1.0) * 100.0
        for milestone in PROFIT_MILESTONES_PCT:
            if change_pct < milestone:
                continue
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with db:
                reservation = db.execute(
                    f"INSERT OR IGNORE INTO {PROFIT_ALERT_TABLE} "
                    "(token_id, threshold_pct, reserved_at_utc) VALUES (?, ?, ?)",
                    (str(item["token_id"]), milestone, now),
                )
            if getattr(reservation, "rowcount", 0) == 0:
                duplicates += 1
                continue
            profit_eur = 100.0 * (latest / initial - 1.0)
            current_value = 100.0 + profit_eur
            name = html_escape(str(item.get("token_name") or "نامشخص"))
            symbol = html_escape(str(item.get("token_symbol") or "?"))
            chain = html_escape(str(item.get("chain") or "نامشخص"))
            address = html_escape(str(item.get("token_address") or ""))
            text = (
                "💰 <b>گزارش سود واچ‌لیست رادار</b>\\n"
                f"🎯 <b>سطح سود ثبت‌شده:</b> +{milestone}%  (معادل +€{milestone} روی مبنای €100)\\n"
                f"🪙 <b>دارایی:</b> {name} ({symbol}) · {chain}\\n"
                f"📈 <b>تغییر از اولین کشف:</b> +{change_pct:.2f}%\\n"
                f"💶 <b>ارزش فرضی €100:</b> €{current_value:.2f} · <b>سود:</b> +€{profit_eur:.2f}\\n"
                f"💵 <b>قیمت اولیه:</b> ${initial:.12g} · <b>قیمت فعلی:</b> ${latest:.12g}\\n"
                f"🕒 <b>اولین کشف UTC:</b> {html_escape(str(item.get('first_detected_utc') or 'نامشخص'))}\\n"
                f"<code>{address}</code>\\n"
                "⚠️ این گزارش تغییر قیمت از زمان کشف است؛ سود تحقق‌یافته نیست و کارمزد، مالیات و لغزش را لحاظ نمی‌کند. ربات هیچ معامله‌ای انجام نداده است."
            )
            try:
                response = client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": destination, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=15,
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    raise RuntimeError("Telegram API did not confirm the watchlist profit alert")
                message_id = str((payload.get("result") or {}).get("message_id") or "")
                with db:
                    db.execute(
                        f"UPDATE {PROFIT_ALERT_TABLE} SET sent_at_utc=?, telegram_message_id=? "
                        "WHERE token_id=? AND threshold_pct=?",
                        (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                         message_id, str(item["token_id"]), milestone),
                    )
                sent += 1
            except Exception:
                with db:
                    db.execute(
                        f"DELETE FROM {PROFIT_ALERT_TABLE} WHERE token_id=? AND threshold_pct=? AND sent_at_utc IS NULL",
                        (str(item["token_id"]), milestone),
                    )
                errors += 1
    return {"status": "OK" if errors == 0 else "PARTIAL",
            "sent": sent, "duplicates": duplicates, "errors": errors}

def watchlist_snapshot(db) -> dict[str, Any]:
    ensure_watchlist_schema(db)
    raw = db.execute(f"SELECT * FROM {TABLE} ORDER BY first_detected_utc DESC, token_name COLLATE NOCASE").fetchall()
    items = []
    for row in raw:
        item = dict(row) if isinstance(row, Mapping) else {
            key: row[index] for index, key in enumerate(
                ["token_id","first_detected_utc","token_name","token_symbol","chain","token_address",
                 "category_fa","initial_price_usd","latest_price_usd","peak_price_usd",
                 "detection_count","last_seen_utc","price_updated_utc"])
        }
        initial = _number(item.get("initial_price_usd"))
        latest = _number(item.get("latest_price_usd"))
        peak = _number(item.get("peak_price_usd"))
        if initial is None or latest is None:
            change = value = profit = None
        else:
            change = (latest / initial - 1.0) * 100.0
            value = 100.0 * latest / initial
            profit = value - 100.0
        peak_change = (peak / initial - 1.0) * 100.0 if initial and peak else None
        peak_profit = 100.0 * peak / initial - 100.0 if initial and peak else None
        item.update({
            "current_change_pct": round(change, 4) if change is not None else None,
            "eur_100_current_value": round(value, 2) if value is not None else None,
            "eur_100_net_profit_eur": round(profit, 2) if profit is not None else None,
            "peak_change_pct": round(peak_change, 4) if peak_change is not None else None,
            "eur_100_peak_profit_eur": round(peak_profit, 2) if peak_profit is not None else None,
        })
        items.append(item)
    profitable = [x for x in items if x.get("eur_100_net_profit_eur") is not None and x["eur_100_net_profit_eur"] > 0]
    best = max(items, key=lambda x: x.get("eur_100_net_profit_eur") if x.get("eur_100_net_profit_eur") is not None else float("-inf"), default=None)
    return {
        "status": "OK", "read_only": True, "capital_basis_eur": 100,
        "total_tokens": len(items), "profitable_tokens": len(profitable),
        "best_hunt": best, "items": items,
        "updated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

def sync_watchlist_scan(candidates: Any) -> dict[str, Any]:
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("Watchlist ledger requires configured Turso Cloud storage.")
    db = connect_database()
    try:
        seen = record_watchlist_candidates(db, candidates)
        prices = refresh_watchlist_prices(db)
        snapshot = watchlist_snapshot(db)
        return {"records_seen_this_scan": seen, "price_refresh": prices,
                "total_tokens": snapshot["total_tokens"], "profitable_tokens": snapshot["profitable_tokens"]}
    finally:
        db.close()
