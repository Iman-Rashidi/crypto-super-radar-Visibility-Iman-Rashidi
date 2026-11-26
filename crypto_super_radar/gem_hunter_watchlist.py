"""Permanent €100 Gem Hunter watchlist ledger backed by Turso/SQLite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from typing import Any, Mapping

from .gem_hunter_qualification import normalize_pump_phase, research_qualification_status
import os
import requests
import time

from .database import connect_database, storage_backend

TABLE = "gem_hunter_watchlist_ledger"
MAX_ACTIVE_TOKENS = 250
MAX_INACTIVE_DAYS = 30
MIN_SAFE_LIQUIDITY_USD = 5000.0
MIN_SAFE_VOLUME_24H_USD = 1000.0
DEXSCREENER_MAX_ATTEMPTS = 3
DEXSCREENER_BACKOFF_BASE_SECONDS = 0.5
EARLY_PHASE_CODES = {"EARLY_IGNITION", "MOMENTUM_BUILD"}

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
    if research_qualification_status(candidate) != "QUALIFIED_EARLY_WAVE":
        return False
    tier = candidate.get("gem_hunter_v3_status", candidate.get("tier"))
    if tier in {"TIER_1_VERIFIED_GEM", "TIER_2_EARLY_ALPHA"}:
        return True
    if tier != "EARLY_ALPHA_REVIEW":
        return False
    phase = (candidate.get("pump_phase_code") or (candidate.get("pump_dump_analysis") or {}).get("pump_phase_code")
             or candidate.get("pump_phase_fa") or (candidate.get("pump_dump_analysis") or {}).get("pump_phase_fa"))
    share = _number(candidate.get("buy_volume_share_pct"))
    if share is None:
        trades = candidate.get("pool_trade_evidence") or {}
        buy, sell = _nonnegative(trades.get("buy_volume_usd")), _nonnegative(trades.get("sell_volume_usd"))
        if buy is not None and sell is not None:
            share = 100.0 * buy / (buy + sell) if buy + sell else None
    if share is None:
        share = _number(candidate.get("narrative_buyer_share_1h_pct"))
    return normalize_pump_phase(phase) in EARLY_PHASE_CODES and share is not None and share >= 52.0

def _token_id(chain: str, address: str) -> str:
    return f"{chain.lower()}:{address if chain.lower() == 'solana' else address.lower()}"

def _row_value(row: Any, key: str, index: int):
    try:
        return row[key]
    except (TypeError, IndexError, KeyError):
        return row[index]


def _parse_utc(value: Any):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ensure_watchlist_schema(db) -> None:
    """Create the ledger and apply additive, idempotent Turso/SQLite migrations."""
    with db:
        db.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            token_id TEXT PRIMARY KEY, first_detected_utc TEXT NOT NULL,
            token_name TEXT NOT NULL, token_symbol TEXT NOT NULL, chain TEXT NOT NULL,
            token_address TEXT NOT NULL, category_fa TEXT, initial_price_usd REAL NOT NULL,
            latest_price_usd REAL NOT NULL, peak_price_usd REAL NOT NULL,
            detection_count INTEGER NOT NULL DEFAULT 1, last_seen_utc TEXT NOT NULL,
            price_updated_utc TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE',
            price_status TEXT NOT NULL DEFAULT 'UNKNOWN', liquidity_usd REAL, volume_24h_usd REAL
        )""")
        columns = {str(_row_value(row, "name", 1)) for row in db.execute(f"PRAGMA table_info({TABLE})").fetchall()}
        migrations = {"status": "TEXT NOT NULL DEFAULT 'ACTIVE'", "price_status": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
                      "liquidity_usd": "REAL", "volume_24h_usd": "REAL"}
        for column, definition in migrations.items():
            if column not in columns:
                db.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} {definition}")
        db.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_first_detected ON {TABLE}(first_detected_utc)")
        db.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_status_last_seen ON {TABLE}(status, last_seen_utc)")


def auto_prune_watchlist(db, *, now=None) -> dict[str, int]:
    """Archive expired/unsafe rows and enforce the active ceiling without deleting history."""
    ensure_watchlist_schema(db)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None: current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=MAX_INACTIVE_DAYS)
    rows = db.execute(f"SELECT token_id,last_seen_utc,liquidity_usd,volume_24h_usd FROM {TABLE} WHERE status='ACTIVE'").fetchall()
    archived_stale = archived_unsafe = 0
    with db:
        for row in rows:
            token_id = str(_row_value(row,"token_id",0))
            last_seen = _parse_utc(_row_value(row,"last_seen_utc",1))
            liquidity = _nonnegative(_row_value(row,"liquidity_usd",2))
            volume = _nonnegative(_row_value(row,"volume_24h_usd",3))
            if last_seen is not None and last_seen < cutoff:
                db.execute(f"UPDATE {TABLE} SET status='ARCHIVED' WHERE token_id=? AND status='ACTIVE'",(token_id,))
                archived_stale += 1
            elif ((liquidity is not None and liquidity < MIN_SAFE_LIQUIDITY_USD)
                  or (volume is not None and volume < MIN_SAFE_VOLUME_24H_USD)):
                db.execute(f"UPDATE {TABLE} SET status='ARCHIVED' WHERE token_id=? AND status='ACTIVE'",(token_id,))
                archived_unsafe += 1
        active = db.execute(f"SELECT token_id FROM {TABLE} WHERE status='ACTIVE' ORDER BY last_seen_utc DESC,first_detected_utc DESC").fetchall()
        for row in active[MAX_ACTIVE_TOKENS:]:
            db.execute(f"UPDATE {TABLE} SET status='ARCHIVED' WHERE token_id=? AND status='ACTIVE'",
                       (str(_row_value(row,"token_id",0)),))
    return {"archived_stale":archived_stale,"archived_unsafe":archived_unsafe,
            "archived_over_capacity":max(0,len(active)-MAX_ACTIVE_TOKENS),
            "active_tokens":min(len(active),MAX_ACTIVE_TOKENS)}


def record_watchlist_candidates(db, candidates: Any) -> int:
    ensure_watchlist_schema(db)
    inserted_or_seen = 0
    now = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    with db:
        for item in candidates if isinstance(candidates,list) else []:
            if not isinstance(item,Mapping) or not is_watchlist_eligible(item): continue
            chain = str(item.get("chain") or "").strip().lower()
            address = str(item.get("token_address") or "").strip()
            price = _number(item.get("price_usd"))
            if not chain or not address or price is None: continue
            token_id = _token_id(chain,address)
            name = str(item.get("token_name") or item.get("token_symbol") or "نامشخص")
            symbol = str(item.get("token_symbol") or "?")
            category = str(item.get("category_fa") or "دسته‌بندی نامشخص")
            market = item.get("market_reconciliation") or {}
            liquidity = _nonnegative(market.get("verified_liquidity_usd"))
            if liquidity is None: liquidity = _nonnegative(item.get("liquidity_usd"))
            volume = _nonnegative(market.get("verified_volume_24h_usd"))
            if volume is None: volume = _nonnegative(item.get("volume_24h_usd"))
            db.execute(f"""INSERT INTO {TABLE}
                (token_id,first_detected_utc,token_name,token_symbol,chain,token_address,category_fa,
                 initial_price_usd,latest_price_usd,peak_price_usd,detection_count,last_seen_utc,
                 status,price_status,liquidity_usd,volume_24h_usd)
                VALUES (?,?,?,?,?,?,?,?,?,?,1,?,'ACTIVE','UNKNOWN',?,?)
                ON CONFLICT(token_id) DO UPDATE SET
                  detection_count={TABLE}.detection_count+1,latest_price_usd=excluded.latest_price_usd,
                  peak_price_usd=CASE WHEN excluded.latest_price_usd>{TABLE}.peak_price_usd THEN excluded.latest_price_usd ELSE {TABLE}.peak_price_usd END,
                  last_seen_utc=excluded.last_seen_utc,status='ACTIVE',
                  liquidity_usd=COALESCE(excluded.liquidity_usd,{TABLE}.liquidity_usd),
                  volume_24h_usd=COALESCE(excluded.volume_24h_usd,{TABLE}.volume_24h_usd)""",
                (token_id,now,name,symbol,chain,address,category,price,price,price,now,liquidity,volume))
            inserted_or_seen += 1
    auto_prune_watchlist(db)
    return inserted_or_seen


def refresh_watchlist_prices(db, *, session=None, batch_size: int = 30,
                             max_attempts: int = DEXSCREENER_MAX_ATTEMPTS,
                             backoff_base_seconds: float = DEXSCREENER_BACKOFF_BASE_SECONDS) -> dict[str, Any]:
    """Refresh active rows in <=30-token batches with exponential retry and visible STALE state."""
    ensure_watchlist_schema(db)
    rows = db.execute(f"SELECT token_id,chain,token_address FROM {TABLE} WHERE status='ACTIVE' ORDER BY first_detected_utc").fetchall()
    updated = errors = attempts_total = stale = 0
    client = session or requests
    batch_limit = max(1,min(int(batch_size),30))
    attempts_limit = max(3,int(max_attempts))
    for offset in range(0,len(rows),batch_limit):
        batch = rows[offset:offset+batch_limit]
        addresses = [str(_row_value(row,"token_address",2)) for row in batch]
        payload = None
        for attempt in range(attempts_limit):
            attempts_total += 1
            try:
                response = client.get("https://api.dexscreener.com/latest/dex/tokens/"+",".join(addresses),timeout=20)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload,dict): raise ValueError("Invalid DexScreener JSON object")
                break
            except Exception:
                if attempt+1 >= attempts_limit:
                    errors += 1
                    with db:
                        for row in batch:
                            db.execute(f"UPDATE {TABLE} SET price_status='STALE' WHERE token_id=?",
                                       (str(_row_value(row,"token_id",0)),))
                            stale += 1
                else:
                    time.sleep(max(0.0,float(backoff_base_seconds))*(2**attempt))
        if payload is None: continue
        best = {}
        for pair in payload.get("pairs") or []:
            if not isinstance(pair,dict): continue
            base = pair.get("baseToken") or {}
            addr = str(base.get("address") or "")
            chain = str(pair.get("chainId") or "").lower()
            price = _number(pair.get("priceUsd"))
            if not addr or price is None: continue
            key = (chain,addr.lower())
            liquidity = _nonnegative((pair.get("liquidity") or {}).get("usd")) or 0.0
            volume = _nonnegative((pair.get("volume") or {}).get("h24"))
            if key not in best or liquidity > best[key][0]: best[key]=(liquidity,price,volume)
        with db:
            for row in batch:
                chain = str(_row_value(row,"chain",1)).lower()
                addr = str(_row_value(row,"token_address",2))
                token_id = str(_row_value(row,"token_id",0))
                hit = best.get((chain,addr.lower()))
                if hit:
                    db.execute(f"""UPDATE {TABLE} SET latest_price_usd=?,
                        peak_price_usd=CASE WHEN ?>peak_price_usd THEN ? ELSE peak_price_usd END,
                        price_updated_utc=?,price_status='OK',liquidity_usd=?,volume_24h_usd=? WHERE token_id=?""",
                        (hit[1],hit[1],hit[1],datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),hit[0],hit[2],token_id))
                    updated += 1
                else:
                    db.execute(f"UPDATE {TABLE} SET price_status='STALE' WHERE token_id=?",(token_id,))
                    stale += 1
    prune = auto_prune_watchlist(db)
    alerts = send_watchlist_profit_alerts(db,session=client)
    return {"status":"OK" if errors==0 and stale==0 else ("PARTIAL" if updated else "UNKNOWN"),
            "tokens_total":len(rows),"prices_updated":updated,"batch_errors":errors,
            "retry_attempts":attempts_total,"stale_tokens":stale,
            "archived_tokens":prune["archived_stale"]+prune["archived_unsafe"]+prune["archived_over_capacity"],
            "profit_alerts":alerts}


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
    """Send deduplicated Persian profit milestones and peak drawdown alerts."""
    token = (bot_token or os.getenv("GEM_HUNTER_TELEGRAM_BOT_TOKEN")
             or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return {"status": "NOT_CONFIGURED", "sent": 0, "duplicates": 0, "errors": 0}
    from .telegram_notifier import TelegramNotifier
    notifier = TelegramNotifier(token=token, connection=db, http_post=(session or requests).post)
    ensure_watchlist_schema(db)
    ensure_profit_alert_schema(db)
    rows = db.execute(
        f"SELECT token_id,first_detected_utc,token_name,token_symbol,chain,token_address,"
        f"initial_price_usd,latest_price_usd,peak_price_usd FROM {TABLE} "
        "WHERE status='ACTIVE' ORDER BY first_detected_utc"
    ).fetchall()
    sent = duplicates = errors = 0
    for row in rows:
        keys = ("token_id","first_detected_utc","token_name","token_symbol","chain",
                "token_address","initial_price_usd","latest_price_usd","peak_price_usd")
        item = dict(row) if isinstance(row, Mapping) else {key:row[i] for i,key in enumerate(keys)}
        initial = _number(item.get("initial_price_usd"))
        latest = _number(item.get("latest_price_usd"))
        peak = _number(item.get("peak_price_usd"))
        if initial is not None and latest is not None:
            change_pct = (latest / initial - 1.0) * 100.0
            for milestone in PROFIT_MILESTONES_PCT:
                if change_pct < milestone:
                    continue
                now = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
                with db:
                    reservation = db.execute(
                        f"INSERT OR IGNORE INTO {PROFIT_ALERT_TABLE} "
                        "(token_id,threshold_pct,reserved_at_utc) VALUES (?,?,?) RETURNING token_id",
                        (str(item["token_id"]),milestone,now))
                    reserved = reservation.fetchone()
                if reserved is None:
                    duplicates += 1
                    continue
                profit_eur = 100.0 * (latest / initial - 1.0)
                message = (
                    "💰 گزارش سود واچ‌لیست رادار\\n"
                    f"🎯 سطح سود ثبت‌شده: +{milestone}% (معادل +€{milestone} روی مبنای €100)\\n"
                    f"🪙 دارایی: {item.get('token_name') or 'نامشخص'} ({item.get('token_symbol') or '?'}) · {item.get('chain') or 'نامشخص'}\\n"
                    f"📈 تغییر از اولین کشف: +{change_pct:.2f}%\\n"
                    f"💶 ارزش فرضی €100: €{100.0+profit_eur:.2f} · سود: +€{profit_eur:.2f}\\n"
                    f"💵 قیمت اولیه: ${initial:.12g} · قیمت فعلی: ${latest:.12g}\\n"
                    f"🕒 اولین کشف UTC: {item.get('first_detected_utc') or 'نامشخص'}\\n"
                    f"آدرس: {item.get('token_address') or ''}\\n"
                    "⚠️ این گزارش تغییر قیمت از زمان کشف است؛ سود تحقق‌یافته نیست و کارمزد، مالیات و لغزش را لحاظ نمی‌کند. ربات هیچ معامله‌ای انجام نداده است."
                )
                try:
                    result = notifier.send_once(f"gem-hunter-watchlist-profit:{item['token_id']}:{milestone}:v1",message)
                    with db:
                        db.execute(
                            f"UPDATE {PROFIT_ALERT_TABLE} SET sent_at_utc=?,telegram_message_id=? "
                            "WHERE token_id=? AND threshold_pct=?",
                            (datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
                             str(result.get("chat_id") or ""),str(item["token_id"]),milestone))
                    if result.get("status") == "DUPLICATE_SKIPPED": duplicates += 1
                    else: sent += 1
                except Exception:
                    errors += 1
        if peak is None or latest is None or peak <= 0 or latest / peak > 0.70:
            continue
        now = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
        with db:
            reservation = db.execute(
                f"INSERT OR IGNORE INTO {PROFIT_ALERT_TABLE} "
                "(token_id,threshold_pct,reserved_at_utc) VALUES (?,-30,?) RETURNING token_id",
                (str(item["token_id"]),now))
            reserved = reservation.fetchone()
        if reserved is None:
            duplicates += 1
            continue
        drawdown_pct = (latest / peak - 1.0) * 100.0
        message = (
            "⚠️ هشدار افت از سقف واچ‌لیست Gem Hunter\\n"
            f"🪙 دارایی: {item.get('token_name') or 'نامشخص'} ({item.get('token_symbol') or '?'}) · {item.get('chain') or 'نامشخص'}\\n"
            f"📉 افت از سقف ثبت‌شده: {drawdown_pct:.2f}% (آستانه هشدار: -30%)\\n"
            f"🏔️ قیمت سقف: ${peak:.12g} · قیمت فعلی: ${latest:.12g}\\n"
            f"آدرس: {item.get('token_address') or ''}\\n"
            "این هشدار صرفاً اطلاع‌رسانی است؛ به معنی اجرای معامله یا تحقق زیان نیست."
        )
        try:
            result = notifier.send_once(f"gem-hunter-watchlist-drawdown:{item['token_id']}:30:v1",message)
            with db:
                db.execute(
                    f"UPDATE {PROFIT_ALERT_TABLE} SET sent_at_utc=?,telegram_message_id=? "
                    "WHERE token_id=? AND threshold_pct=-30",
                    (datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
                     str(result.get("chat_id") or ""),str(item["token_id"])))
            if result.get("status") == "DUPLICATE_SKIPPED": duplicates += 1
            else: sent += 1
        except Exception:
            errors += 1
    return {"status":"OK" if errors==0 else "PARTIAL","sent":sent,"duplicates":duplicates,"errors":errors}


def watchlist_snapshot(db) -> dict[str, Any]:
    ensure_watchlist_schema(db)
    raw = db.execute(f"SELECT * FROM {TABLE} WHERE status='ACTIVE' ORDER BY first_detected_utc DESC,token_name COLLATE NOCASE").fetchall()
    archived_count = int(_row_value(db.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE status='ARCHIVED'").fetchone(),"COUNT(*)",0))
    items = []
    columns = ["token_id","first_detected_utc","token_name","token_symbol","chain","token_address",
        "category_fa","initial_price_usd","latest_price_usd","peak_price_usd","detection_count",
        "last_seen_utc","price_updated_utc","status","price_status","liquidity_usd","volume_24h_usd"]
    for row in raw:
        item = dict(row) if isinstance(row,Mapping) else {key:row[i] for i,key in enumerate(columns)}
        initial,latest,peak = (_number(item.get(k)) for k in ("initial_price_usd","latest_price_usd","peak_price_usd"))
        if initial is None or latest is None: change=value=profit=None
        else:
            change=(latest/initial-1.0)*100.0
            value=100.0*latest/initial
            profit=value-100.0
        peak_change=(peak/initial-1.0)*100.0 if initial and peak else None
        peak_profit=100.0*peak/initial-100.0 if initial and peak else None
        drawdown=(latest/peak-1.0)*100.0 if latest and peak else None
        item.update({"current_change_pct":round(change,4) if change is not None else None,
            "eur_100_current_value":round(value,2) if value is not None else None,
            "eur_100_net_profit_eur":round(profit,2) if profit is not None else None,
            "peak_change_pct":round(peak_change,4) if peak_change is not None else None,
            "eur_100_peak_profit_eur":round(peak_profit,2) if peak_profit is not None else None,
            "drawdown_from_peak_pct":round(drawdown,4) if drawdown is not None else None})
        items.append(item)
    profitable=[x for x in items if x.get("eur_100_net_profit_eur") is not None and x["eur_100_net_profit_eur"]>0]
    best=max(items,key=lambda x:x.get("eur_100_net_profit_eur") if x.get("eur_100_net_profit_eur") is not None else float("-inf"),default=None)
    return {"status":"OK","read_only":True,"capital_basis_eur":100,"total_tokens":len(items),
        "active_tokens":len(items),"archived_tokens":archived_count,"profitable_tokens":len(profitable),
        "best_hunt":best,"items":items,"updated_at_utc":datetime.now(timezone.utc).isoformat().replace("+00:00","Z")}


def sync_watchlist_scan(candidates: Any) -> dict[str, Any]:
    if storage_backend() != "TURSO_CLOUD_SQLITE":
        raise RuntimeError("Watchlist ledger requires configured Turso Cloud storage.")
    db = connect_database()
    try:
        seen = record_watchlist_candidates(db, candidates)
        prices = refresh_watchlist_prices(db)
        prune = auto_prune_watchlist(db)
        snapshot = watchlist_snapshot(db)
        return {"records_seen_this_scan": seen, "price_refresh": prices, "auto_prune": prune,
                "total_tokens": snapshot["total_tokens"], "archived_tokens": snapshot["archived_tokens"],
                "profitable_tokens": snapshot["profitable_tokens"]}
    finally:
        db.close()
