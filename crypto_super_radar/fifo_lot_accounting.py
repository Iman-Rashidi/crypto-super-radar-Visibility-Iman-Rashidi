"""FIFO cost-basis lots and realized P&L for the isolated multi-currency paper ledger.

All writes must run inside the caller's database transaction, together with the
underlying MultiCurrencyPaperLedger journal writes. This module is accounting
only: it does not create orders, fills, signals, or live-execution capability.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from typing import Any

from .multi_currency_ledger import (
    LedgerIdempotencyConflict,
    LedgerValidationError,
    MultiCurrencyPaperLedger,
)

_TOLERANCE = 1e-10
_INSTRUMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,95}$")


def _positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise LedgerValidationError(f"{field} must be a positive finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LedgerValidationError(f"{field} must be a positive finite number") from exc
    if not math.isfinite(result) or result <= 0:
        raise LedgerValidationError(f"{field} must be a positive finite number")
    return result


def _timestamp(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise LedgerValidationError(f"{field} must be a positive integer timestamp")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LedgerValidationError(f"{field} must be a positive integer timestamp") from exc
    if result <= 0 or str(value).strip() not in {str(result), f"{result}.0"}:
        raise LedgerValidationError(f"{field} must be a positive integer timestamp")
    return result


def _instrument(value: Any) -> str:
    result = str(value or "").strip()
    if not _INSTRUMENT_RE.fullmatch(result):
        raise LedgerValidationError("instrument_id is invalid")
    return result


class FIFOLotAccounting:
    """Append-only FIFO lot book layered over MultiCurrencyPaperLedger.

    BUY lots preserve their original unit price and FX provenance. SELLs
    allocate the oldest remaining lots and post inventory relief at historical
    carrying value, proceeds at sale-date FX, and realized gain/loss separately.
    Fees are intentionally excluded from this first stage and must be posted
    separately as expenses until fee-aware net P&L is implemented.
    """

    def __init__(
        self,
        connection: Any,
        ledger: MultiCurrencyPaperLedger,
        *,
        lock: threading.RLock | None = None,
    ):
        self._db = connection
        self._ledger = ledger
        self._lock = lock or threading.RLock()
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS paper_cost_basis_lots (
                    lot_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL UNIQUE,
                    journal_sequence INTEGER NOT NULL,
                    acquired_at INTEGER NOT NULL,
                    quantity REAL NOT NULL,
                    unit_price REAL NOT NULL,
                    quote_currency TEXT NOT NULL,
                    fx_rate_to_base REAL NOT NULL,
                    fx_source TEXT NOT NULL,
                    fx_observed_at INTEGER NOT NULL,
                    fx_quality TEXT NOT NULL
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS paper_cost_basis_allocations (
                    allocation_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    sale_transaction_id TEXT NOT NULL,
                    lot_id TEXT NOT NULL,
                    allocated_quantity REAL NOT NULL,
                    cost_basis_base REAL NOT NULL,
                    occurred_at INTEGER NOT NULL,
                    UNIQUE(sale_transaction_id, lot_id)
                )"""
            )
            for table, label in (
                ("paper_cost_basis_lots", "cost basis lots"),
                ("paper_cost_basis_allocations", "cost basis allocations"),
            ):
                self._db.execute(
                    f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{label} are append-only'); END"""
                )
                self._db.execute(
                    f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{label} are append-only'); END"""
                )

    def _fx(
        self,
        base_currency: str,
        quote_currency: str,
        occurred_at: int,
        fx_rate_to_base: float | None,
        fx_source: str | None,
        fx_observed_at: int | None,
        fx_quality: str | None,
    ) -> dict[str, Any]:
        if quote_currency == base_currency:
            return {
                "fx_rate_to_base": 1.0,
                "fx_source": "IDENTITY",
                "fx_observed_at": occurred_at,
                "fx_quality": "IDENTITY",
            }
        rate = _positive(fx_rate_to_base, "fx_rate_to_base")
        observed = _timestamp(fx_observed_at, "fx_observed_at")
        if observed > occurred_at:
            raise LedgerValidationError("FX observation cannot be later than the transaction")
        source = str(fx_source or "").strip()
        quality = str(fx_quality or "").strip().upper()
        if not source or quality not in {"OBSERVED", "MODELLED"}:
            raise LedgerValidationError("foreign-currency trades require FX source and OBSERVED/MODELLED quality")
        return {
            "fx_rate_to_base": rate,
            "fx_source": source,
            "fx_observed_at": observed,
            "fx_quality": quality,
        }

    @staticmethod
    def _request(side: str, instrument: str, quantity: float, price: float,
                 currency: str, occurred_at: int, fx: dict[str, Any]) -> dict[str, Any]:
        return {
            "method": "FIFO",
            "side": side,
            "instrument_id": instrument,
            "quantity": quantity,
            "unit_price": price,
            "quote_currency": currency,
            "occurred_at": occurred_at,
            **fx,
        }

    @staticmethod
    def _transaction_id(portfolio_id: str, idempotency_key: str) -> str:
        digest = hashlib.sha256(f"{portfolio_id}|{idempotency_key}".encode("utf-8")).hexdigest()[:32]
        return "ptx-" + digest

    def record_buy(
        self,
        portfolio_id: str,
        *,
        idempotency_key: str,
        instrument_id: str,
        quantity: float,
        unit_price: float,
        quote_currency: str,
        occurred_at: int,
        fx_rate_to_base: float | None = None,
        fx_source: str | None = None,
        fx_observed_at: int | None = None,
        fx_quality: str | None = None,
        reference: str | None = None,
    ) -> dict[str, Any]:
        """Post a paper buy and open one immutable FIFO lot."""
        instrument = _instrument(instrument_id)
        qty, price = _positive(quantity, "quantity"), _positive(unit_price, "unit_price")
        timestamp = _timestamp(occurred_at, "occurred_at")
        currency = str(quote_currency or "").strip().upper()
        portfolio = self._ledger.get_portfolio(portfolio_id)
        if portfolio is None:
            raise LedgerValidationError("unknown portfolio")
        fx = self._fx(str(portfolio["base_currency"]), currency, timestamp,
                      fx_rate_to_base, fx_source, fx_observed_at, fx_quality)
        metadata = {"lot_accounting": {"request": self._request(
            "BUY", instrument, qty, price, currency, timestamp, fx
        )}}
        result = self._ledger.post_transaction(
            portfolio_id=portfolio_id,
            idempotency_key=idempotency_key,
            transaction_type="TRADE",
            occurred_at=timestamp,
            source="FIFO_LOT_ACCOUNTING",
            reference=reference,
            metadata=metadata,
            entries=[
                {
                    "account_code": "ASSET:INVENTORY:POSITION",
                    "instrument_id": instrument,
                    "quantity": qty,
                    "unit_price": price,
                    "quote_currency": currency,
                    **fx,
                },
                {
                    "account_code": f"ASSET:CASH:{currency}",
                    "instrument_id": f"cash:{currency}",
                    "quantity": -(qty * price),
                    "unit_price": 1.0,
                    "quote_currency": currency,
                    **fx,
                },
            ],
        )
        transaction_id = str(result["transaction_id"])
        if result["inserted"]:
            row = self._db.execute(
                "SELECT sequence FROM paper_portfolio_transactions WHERE transaction_id=?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                raise LedgerValidationError("buy journal transaction is missing")
            lot_id = "lot-" + hashlib.sha256(f"{transaction_id}|1".encode()).hexdigest()[:32]
            self._db.execute(
                """INSERT INTO paper_cost_basis_lots (
                    lot_id, portfolio_id, instrument_id, transaction_id,
                    journal_sequence, acquired_at, quantity, unit_price,
                    quote_currency, fx_rate_to_base, fx_source, fx_observed_at, fx_quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (lot_id, portfolio_id, instrument, transaction_id, int(row["sequence"]),
                 timestamp, qty, price, currency, fx["fx_rate_to_base"],
                 fx["fx_source"], fx["fx_observed_at"], fx["fx_quality"]),
            )
        return {**result, "lot_id": "lot-" + hashlib.sha256(
            f"{transaction_id}|1".encode()).hexdigest()[:32], "method": "FIFO"}

    def _existing_sale(self, portfolio_id: str, key: str, request: dict[str, Any]) -> dict[str, Any] | None:
        row = self._db.execute(
            """SELECT transaction_id, payload_json FROM paper_portfolio_transactions
               WHERE portfolio_id=? AND idempotency_key=?""",
            (portfolio_id, key),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        stored = payload.get("metadata", {}).get("lot_accounting", {}).get("request")
        if stored != request:
            raise LedgerIdempotencyConflict("idempotency key was reused with a different FIFO sale request")
        allocation = payload.get("metadata", {}).get("lot_accounting", {})
        return {
            "inserted": False,
            "transaction_id": str(row["transaction_id"]),
            "method": "FIFO",
            "realized_pnl_base": float(allocation.get("realized_pnl_base", 0.0)),
            "cost_basis_base": float(allocation.get("cost_basis_base", 0.0)),
            "proceeds_base": float(allocation.get("proceeds_base", 0.0)),
            "allocations": allocation.get("allocations", []),
        }

    def record_sell(
        self,
        portfolio_id: str,
        *,
        idempotency_key: str,
        instrument_id: str,
        quantity: float,
        unit_price: float,
        quote_currency: str,
        occurred_at: int,
        fx_rate_to_base: float | None = None,
        fx_source: str | None = None,
        fx_observed_at: int | None = None,
        fx_quality: str | None = None,
        reference: str | None = None,
    ) -> dict[str, Any]:
        """Dispose FIFO lots, post historical inventory relief and realized P&L."""
        instrument = _instrument(instrument_id)
        qty, price = _positive(quantity, "quantity"), _positive(unit_price, "unit_price")
        timestamp = _timestamp(occurred_at, "occurred_at")
        currency = str(quote_currency or "").strip().upper()
        portfolio = self._ledger.get_portfolio(portfolio_id)
        if portfolio is None:
            raise LedgerValidationError("unknown portfolio")
        fx = self._fx(str(portfolio["base_currency"]), currency, timestamp,
                      fx_rate_to_base, fx_source, fx_observed_at, fx_quality)
        request = self._request("SELL", instrument, qty, price, currency, timestamp, fx)
        repeated = self._existing_sale(portfolio_id, idempotency_key, request)
        if repeated is not None:
            return repeated

        lots = self._db.execute(
            """SELECT l.*, l.quantity - COALESCE((
                   SELECT SUM(a.allocated_quantity) FROM paper_cost_basis_allocations a
                   WHERE a.lot_id=l.lot_id
               ), 0) AS remaining_quantity
               FROM paper_cost_basis_lots l
               WHERE l.portfolio_id=? AND l.instrument_id=?
               ORDER BY l.acquired_at, l.journal_sequence, l.lot_id""",
            (portfolio_id, instrument),
        ).fetchall()
        remaining = qty
        allocations: list[dict[str, Any]] = []
        inventory_entries: list[dict[str, Any]] = []
        cost_basis = 0.0
        for lot in lots:
            available = max(0.0, float(lot["remaining_quantity"]))
            if available <= _TOLERANCE or remaining <= _TOLERANCE:
                continue
            used = min(available, remaining)
            lot_cost = used * float(lot["unit_price"]) * float(lot["fx_rate_to_base"])
            cost_basis += lot_cost
            allocations.append({
                "lot_id": str(lot["lot_id"]),
                "quantity": used,
                "cost_basis_base": round(lot_cost, 12),
            })
            inventory_entries.append({
                "account_code": "ASSET:INVENTORY:POSITION",
                "instrument_id": instrument,
                "quantity": -used,
                "unit_price": float(lot["unit_price"]),
                "quote_currency": str(lot["quote_currency"]),
                "fx_rate_to_base": float(lot["fx_rate_to_base"]),
                "fx_source": str(lot["fx_source"]),
                "fx_observed_at": int(lot["fx_observed_at"]),
                "fx_quality": str(lot["fx_quality"]),
            })
            remaining -= used
        if remaining > max(_TOLERANCE, qty * 1e-10):
            raise LedgerValidationError(
                f"insufficient open FIFO inventory: requested {qty}, available {qty - remaining}"
            )

        proceeds = qty * price * fx["fx_rate_to_base"]
        realized = proceeds - cost_basis
        entries = [{
            "account_code": f"ASSET:CASH:{currency}",
            "instrument_id": f"cash:{currency}",
            "quantity": qty * price,
            "unit_price": 1.0,
            "quote_currency": currency,
            **fx,
        }, *inventory_entries]
        if realized > _TOLERANCE:
            entries.append({
                "account_code": "INCOME:REALIZED_PNL",
                "instrument_id": instrument,
                "quantity": -realized,
                "unit_price": 1.0,
                "quote_currency": str(portfolio["base_currency"]),
                "fx_rate_to_base": 1.0,
                "fx_source": "IDENTITY",
                "fx_observed_at": timestamp,
                "fx_quality": "IDENTITY",
            })
        elif realized < -_TOLERANCE:
            entries.append({
                "account_code": "EXPENSE:REALIZED_PNL",
                "instrument_id": instrument,
                "quantity": -realized,
                "unit_price": 1.0,
                "quote_currency": str(portfolio["base_currency"]),
                "fx_rate_to_base": 1.0,
                "fx_source": "IDENTITY",
                "fx_observed_at": timestamp,
                "fx_quality": "IDENTITY",
            })

        metadata = {"lot_accounting": {
            "request": request,
            "allocations": allocations,
            "cost_basis_base": round(cost_basis, 12),
            "proceeds_base": round(proceeds, 12),
            "realized_pnl_base": round(realized, 12),
        }}
        result = self._ledger.post_transaction(
            portfolio_id=portfolio_id,
            idempotency_key=idempotency_key,
            transaction_type="TRADE",
            occurred_at=timestamp,
            source="FIFO_LOT_ACCOUNTING",
            reference=reference,
            metadata=metadata,
            entries=entries,
        )
        if result["inserted"]:
            sale_id = str(result["transaction_id"])
            for index, item in enumerate(allocations, start=1):
                allocation_id = "alloc-" + hashlib.sha256(
                    f"{sale_id}|{item['lot_id']}".encode()
                ).hexdigest()[:32]
                self._db.execute(
                    """INSERT INTO paper_cost_basis_allocations (
                        allocation_id, portfolio_id, instrument_id,
                        sale_transaction_id, lot_id, allocated_quantity,
                        cost_basis_base, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (allocation_id, portfolio_id, instrument, sale_id,
                     item["lot_id"], item["quantity"], item["cost_basis_base"], timestamp),
                )
        return {
            **result,
            "method": "FIFO",
            "allocations": allocations,
            "cost_basis_base": round(cost_basis, 12),
            "proceeds_base": round(proceeds, 12),
            "realized_pnl_base": round(realized, 12),
        }

    def open_lots(self, portfolio_id: str, instrument_id: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [portfolio_id]
        filter_sql = ""
        if instrument_id is not None:
            filter_sql = " AND l.instrument_id=?"
            params.append(_instrument(instrument_id))
        rows = self._db.execute(
            f"""SELECT l.*, l.quantity - COALESCE((
                    SELECT SUM(a.allocated_quantity) FROM paper_cost_basis_allocations a
                    WHERE a.lot_id=l.lot_id
                ), 0) AS remaining_quantity
                FROM paper_cost_basis_lots l
                WHERE l.portfolio_id=?{filter_sql}
                ORDER BY l.acquired_at, l.journal_sequence, l.lot_id""",
            tuple(params),
        ).fetchall()
        return [{**dict(row), "remaining_quantity": round(float(row["remaining_quantity"]), 12)}
                for row in rows if float(row["remaining_quantity"]) > _TOLERANCE]

    def replay(self, portfolio_id: str | None = None) -> dict[str, Any]:
        """Validate immutable lots and allocation references/quantities."""
        where = " WHERE portfolio_id=?" if portfolio_id is not None else ""
        params = (portfolio_id,) if portfolio_id is not None else ()
        errors: list[str] = []
        lots = self._db.execute(f"SELECT * FROM paper_cost_basis_lots{where}", params).fetchall()
        allocations = self._db.execute(
            f"SELECT * FROM paper_cost_basis_allocations{where}", params
        ).fetchall()
        lot_by_id = {str(row["lot_id"]): row for row in lots}
        used: dict[str, float] = {}
        for item in allocations:
            lot_id = str(item["lot_id"])
            lot = lot_by_id.get(lot_id)
            if lot is None:
                errors.append(f"{item['allocation_id']}: referenced lot is missing")
                continue
            if str(item["instrument_id"]) != str(lot["instrument_id"]):
                errors.append(f"{item['allocation_id']}: instrument mismatch")
            used[lot_id] = used.get(lot_id, 0.0) + float(item["allocated_quantity"])
            if float(item["allocated_quantity"]) <= 0 or float(item["cost_basis_base"]) < 0:
                errors.append(f"{item['allocation_id']}: invalid allocation values")
            sale = self._db.execute(
                "SELECT transaction_id FROM paper_portfolio_transactions WHERE transaction_id=? AND portfolio_id=?",
                (item["sale_transaction_id"], item["portfolio_id"]),
            ).fetchone()
            if sale is None:
                errors.append(f"{item['allocation_id']}: sale journal transaction is missing")
        for lot_id, lot in lot_by_id.items():
            if used.get(lot_id, 0.0) > float(lot["quantity"]) + _TOLERANCE:
                errors.append(f"{lot_id}: allocations exceed acquired quantity")
        return {
            "status": "PASS" if not errors else "FAIL",
            "portfolio_id": portfolio_id,
            "lot_count": len(lots),
            "allocation_count": len(allocations),
            "open_quantity": round(sum(
                float(row["quantity"]) - used.get(str(row["lot_id"]), 0.0)
                for row in lots
            ), 12),
            "errors": errors,
        }
