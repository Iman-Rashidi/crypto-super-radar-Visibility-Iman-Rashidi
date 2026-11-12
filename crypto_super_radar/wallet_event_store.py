"""Append-only, idempotent wallet-event ledger for on-chain research.

A transaction may legitimately touch multiple tracked wallets and multiple
token legs. The composite key preserves each wallet attribution and leg.
Solana Base58 addresses are case-sensitive; EVM addresses are normalized.
"""
from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping
from typing import Any

_EVENT_TYPES = {"BUY", "SELL", "TRANSFER_IN", "TRANSFER_OUT", "MINT", "BURN", "SWAP", "OTHER"}
_EVIDENCE = {"OBSERVED", "INFERRED", "UNKNOWN"}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _address(chain: str, value: Any, field: str) -> str:
    address = _required_text(value, field)
    return address.lower() if chain in {"base", "ethereum", "arbitrum", "optimism", "polygon", "bsc", "avalanche"} else address


def _finite_optional(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _normalize(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ValueError("event must be a mapping")
    chain = _required_text(event.get("chain"), "chain").lower()
    tx = _required_text(event.get("tx_signature"), "tx_signature")
    wallet = _address(chain, event.get("wallet_address"), "wallet_address")
    token = _address(chain, event.get("token_address"), "token_address")
    event_type = _required_text(event.get("event_type"), "event_type").upper()
    if event_type not in _EVENT_TYPES:
        raise ValueError("unsupported event_type")
    index = event.get("event_index")
    timestamp = event.get("event_time_ms")
    received = event.get("received_at_ms")
    for field, value in (("event_index", index), ("event_time_ms", timestamp), ("received_at_ms", received)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    slot = event.get("slot")
    if slot is not None and (isinstance(slot, bool) or not isinstance(slot, int) or slot < 0):
        raise ValueError("slot must be a non-negative integer or null")
    evidence = str(event.get("evidence_status") or "OBSERVED").strip().upper()
    if evidence not in _EVIDENCE:
        raise ValueError("unsupported evidence_status")
    source = _required_text(event.get("source"), "source")
    quote_symbol = event.get("quote_symbol")
    if quote_symbol is not None:
        quote_symbol = _required_text(quote_symbol, "quote_symbol").upper()
    return {
        "chain": chain, "tx_signature": tx, "wallet_address": wallet,
        "token_address": token,
        "counterparty_address": (
            _address(chain, event.get("counterparty_address"), "counterparty_address")
            if event.get("counterparty_address") not in (None, "") else None
        ),
        "event_type": event_type, "event_index": index,
        "event_time_ms": timestamp, "received_at_ms": received, "slot": slot,
        "token_delta": _finite_optional(event.get("token_delta"), "token_delta"),
        "quote_delta": _finite_optional(event.get("quote_delta"), "quote_delta"),
        "quote_symbol": quote_symbol, "source": source, "evidence_status": evidence,
    }


class WalletEventStore:
    """Append-only SQLite-compatible wallet observation ledger."""

    def __init__(self, connection: Any) -> None:
        if connection is None or not callable(getattr(connection, "execute", None)):
            raise ValueError("a SQLite-compatible connection is required")
        self._db = connection
        self._lock = threading.RLock()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_event_observations (
                chain TEXT NOT NULL,
                tx_signature TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                event_index INTEGER NOT NULL,
                token_address TEXT NOT NULL,
                counterparty_address TEXT,
                event_type TEXT NOT NULL,
                event_time_ms INTEGER NOT NULL,
                received_at_ms INTEGER NOT NULL,
                slot INTEGER,
                token_delta REAL,
                quote_delta REAL,
                quote_symbol TEXT,
                source TEXT NOT NULL,
                evidence_status TEXT NOT NULL,
                PRIMARY KEY (chain, tx_signature, wallet_address, event_index)
            )
            """
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(wallet_event_observations)")}
        if "counterparty_address" not in columns:
            self._db.execute(
                "ALTER TABLE wallet_event_observations ADD COLUMN counterparty_address TEXT"
            )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_wallet_event_history
            ON wallet_event_observations (chain, wallet_address, event_time_ms, slot, event_index)
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_token_event_history
            ON wallet_event_observations (chain, token_address, event_time_ms, slot, event_index)
            """
        )
        self._db.commit()

    def append_many(self, events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
        if isinstance(events, (str, bytes, Mapping)):
            raise ValueError("events must be an iterable of event mappings")
        rows = [_normalize(item) for item in events]
        if not rows:
            return {"inserted": 0, "duplicates": 0}
        inserted = 0
        with self._lock:
            cursor = self._db.cursor()
            try:
                for row in rows:
                    cursor.execute(
                    """
                    INSERT OR IGNORE INTO wallet_event_observations (
                        chain, tx_signature, wallet_address, event_index,
                        token_address, counterparty_address, event_type, event_time_ms, received_at_ms,
                        slot, token_delta, quote_delta, quote_symbol, source, evidence_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        tuple(row[key] for key in (
                            "chain", "tx_signature", "wallet_address", "event_index",
                            "token_address", "counterparty_address", "event_type", "event_time_ms", "received_at_ms",
                            "slot", "token_delta", "quote_delta", "quote_symbol", "source", "evidence_status"
                        )),
                    )
                    inserted += max(0, int(cursor.rowcount or 0))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return {"inserted": inserted, "duplicates": len(rows) - inserted}

    def _read(self, where: str, values: tuple[Any, ...], *, limit: int) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            cursor = self._db.execute(
                f"""
            SELECT chain, tx_signature, wallet_address, event_index, token_address,
                   counterparty_address, event_type, event_time_ms, received_at_ms, slot, token_delta,
                   quote_delta, quote_symbol, source, evidence_status
            FROM wallet_event_observations
            WHERE {where}
            ORDER BY event_time_ms ASC, COALESCE(slot, -1) ASC, event_index ASC, wallet_address ASC
            LIMIT ?
            """,
            (*values, min(limit, 100_000)),
        )
            names = [col[0] for col in (cursor.description or ())]
            return [dict(row) if isinstance(row, Mapping) else dict(zip(names, row)) for row in cursor.fetchall()]

    def wallet_history(self, *, chain: str, wallet_address: str, limit: int = 5000) -> list[dict[str, Any]]:
        chain_name = _required_text(chain, "chain").lower()
        wallet = _address(chain_name, wallet_address, "wallet_address")
        return self._read("chain = ? AND wallet_address = ?", (chain_name, wallet), limit=limit)

    def token_events(self, *, chain: str, token_address: str, limit: int = 5000) -> list[dict[str, Any]]:
        chain_name = _required_text(chain, "chain").lower()
        token = _address(chain_name, token_address, "token_address")
        return self._read("chain = ? AND token_address = ?", (chain_name, token), limit=limit)
