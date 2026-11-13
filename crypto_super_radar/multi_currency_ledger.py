"""Append-only, double-entry multi-asset paper accounting foundation.

The journal is deliberately separate from the active Paper Bot. It stores
balanced base-currency values and native asset quantities, but it does not
value open positions, calculate FIFO realized P&L, or authorize execution.
No exchange or broker API is imported or called here.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

ACCOUNT_TYPES = frozenset({"ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"})
TRANSACTION_TYPES = frozenset({
    "CAPITAL_CONTRIBUTION",
    "CAPITAL_WITHDRAWAL",
    "TRADE",
    "FEE",
    "FX_CONVERSION",
    "DIVIDEND",
    "INTEREST",
    "FUNDING",
    "CORPORATE_ACTION",
    "TAX",
    "ADJUSTMENT",
    "MARK_TO_MARKET",
})
FX_QUALITIES = frozenset({"IDENTITY", "OBSERVED", "MODELLED"})
BALANCE_TOLERANCE = 1e-8

_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_PORTFOLIO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_ACCOUNT_PART_RE = re.compile(r"^[A-Z0-9][A-Z0-9._/-]{0,31}$")
_INSTRUMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,95}$")


class LedgerValidationError(ValueError):
    """A portfolio or journal entry violates the ledger contract."""


class LedgerIdempotencyConflict(LedgerValidationError):
    """An idempotency key was reused with different transaction content."""


class PortfolioConfigurationConflict(LedgerValidationError):
    """An existing portfolio was requested with a different base currency."""


def _finite_number(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    non_zero: bool = False,
) -> float:
    if isinstance(value, bool) or value is None:
        raise LedgerValidationError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LedgerValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise LedgerValidationError(f"{field} must be a finite number")
    if positive and number <= 0:
        raise LedgerValidationError(f"{field} must be greater than zero")
    if non_zero and number == 0:
        raise LedgerValidationError(f"{field} must not be zero")
    return number


def _currency_code(value: Any, field: str) -> str:
    code = str(value or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(code):
        raise LedgerValidationError(f"{field} must be a 3–12 character currency or commodity code")
    return code


def _timestamp(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise LedgerValidationError(f"{field} must be a positive integer Unix timestamp")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LedgerValidationError(f"{field} must be a positive integer Unix timestamp") from exc
    if result <= 0 or str(value).strip() not in {str(result), f"{result}.0"}:
        raise LedgerValidationError(f"{field} must be a positive integer Unix timestamp")
    return result


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError("transaction metadata must be finite JSON data") from exc


def _account_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    parts = code.split(":")
    if len(parts) < 2 or parts[0] not in ACCOUNT_TYPES:
        raise LedgerValidationError(
            "account_code must start with ASSET, LIABILITY, EQUITY, INCOME or EXPENSE"
        )
    if any(not _ACCOUNT_PART_RE.fullmatch(part) for part in parts[1:]):
        raise LedgerValidationError("account_code contains an invalid segment")
    return code


class MultiCurrencyPaperLedger:
    """Append-only double-entry journal for isolated paper portfolios.

    All writes must be wrapped in the caller's database transaction (for
    example, a connection context manager). This allows callers to commit the
    journal and their own state projection atomically. The ledger never commits
    on behalf of the caller.
    """

    def __init__(self, connection: Any, *, lock: threading.RLock | None = None):
        self._db = connection
        self._lock = lock or threading.RLock()
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS paper_portfolios (
                    portfolio_id TEXT PRIMARY KEY,
                    base_currency TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS paper_portfolio_transactions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL,
                    transaction_type TEXT NOT NULL,
                    base_currency TEXT NOT NULL,
                    occurred_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    reference TEXT,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    UNIQUE(portfolio_id, idempotency_key)
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS paper_portfolio_entries (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    account_code TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    unit_price REAL NOT NULL,
                    quote_currency TEXT NOT NULL,
                    fx_rate_to_base REAL NOT NULL,
                    fx_source TEXT NOT NULL,
                    fx_observed_at INTEGER NOT NULL,
                    fx_quality TEXT NOT NULL,
                    base_amount REAL NOT NULL,
                    UNIQUE(transaction_id, line_number)
                )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_portfolio_entries_account "
                "ON paper_portfolio_entries(portfolio_id, account_code, instrument_id)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_portfolio_transactions_sequence "
                "ON paper_portfolio_transactions(portfolio_id, sequence)"
            )
            for table, label in (
                ("paper_portfolios", "portfolio definitions"),
                ("paper_portfolio_transactions", "portfolio transactions"),
                ("paper_portfolio_entries", "portfolio entries"),
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

    def create_portfolio(
        self,
        portfolio_id: str,
        base_currency: str,
        *,
        created_at: int,
    ) -> dict[str, Any]:
        """Create an immutable portfolio identity and functional base currency."""
        portfolio = str(portfolio_id or "").strip()
        if not _PORTFOLIO_RE.fullmatch(portfolio):
            raise LedgerValidationError("portfolio_id is required and must be at most 64 safe characters")
        base = _currency_code(base_currency, "base_currency")
        timestamp = _timestamp(created_at, "created_at")
        with self._lock:
            existing = self._db.execute(
                "SELECT portfolio_id, base_currency, created_at "
                "FROM paper_portfolios WHERE portfolio_id=?",
                (portfolio,),
            ).fetchone()
            if existing:
                if str(existing["base_currency"]) != base:
                    raise PortfolioConfigurationConflict(
                        "portfolio base_currency is immutable once created"
                    )
                return {
                    "portfolio_id": portfolio,
                    "base_currency": base,
                    "created_at": int(existing["created_at"]),
                    "inserted": False,
                }
            self._db.execute(
                "INSERT INTO paper_portfolios(portfolio_id, base_currency, created_at) "
                "VALUES (?, ?, ?)",
                (portfolio, base, timestamp),
            )
        return {
            "portfolio_id": portfolio,
            "base_currency": base,
            "created_at": timestamp,
            "inserted": True,
        }

    def get_portfolio(self, portfolio_id: str) -> dict[str, Any] | None:
        portfolio = str(portfolio_id or "").strip()
        with self._lock:
            row = self._db.execute(
                "SELECT portfolio_id, base_currency, created_at "
                "FROM paper_portfolios WHERE portfolio_id=?",
                (portfolio,),
            ).fetchone()
        return dict(row) if row else None

    def _normalise_entries(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        base_currency: str,
        occurred_at: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(entries, (list, tuple)) or not 2 <= len(entries) <= 500:
            raise LedgerValidationError("entries must contain between 2 and 500 journal lines")
        normalized: list[dict[str, Any]] = []
        distinct_accounts: set[str] = set()

        for index, raw in enumerate(entries, start=1):
            if not isinstance(raw, Mapping):
                raise LedgerValidationError(f"entry {index} must be a mapping")
            account = _account_code(raw.get("account_code"))
            instrument = str(raw.get("instrument_id") or "").strip()
            if not _INSTRUMENT_RE.fullmatch(instrument):
                raise LedgerValidationError(f"entry {index} has an invalid instrument_id")
            quantity = _finite_number(raw.get("quantity"), f"entry {index} quantity", non_zero=True)
            unit_price = _finite_number(
                raw.get("unit_price"), f"entry {index} unit_price", positive=True
            )
            quote_currency = _currency_code(raw.get("quote_currency"), f"entry {index} quote_currency")

            if quote_currency == base_currency:
                fx_rate = _finite_number(
                    raw.get("fx_rate_to_base", 1.0),
                    f"entry {index} fx_rate_to_base",
                    positive=True,
                )
                if not math.isclose(fx_rate, 1.0, rel_tol=0.0, abs_tol=1e-12):
                    raise LedgerValidationError(
                        f"entry {index} must use fx_rate_to_base=1 for the portfolio base currency"
                    )
                fx_quality = str(raw.get("fx_quality") or "IDENTITY").strip().upper()
                if fx_quality != "IDENTITY":
                    raise LedgerValidationError(
                        f"entry {index} in the base currency must use fx_quality=IDENTITY"
                    )
                fx_source = str(raw.get("fx_source") or "IDENTITY").strip()
                fx_observed_at = _timestamp(
                    raw.get("fx_observed_at", occurred_at),
                    f"entry {index} fx_observed_at",
                )
            else:
                fx_rate = _finite_number(
                    raw.get("fx_rate_to_base"),
                    f"entry {index} fx_rate_to_base",
                    positive=True,
                )
                fx_quality = str(raw.get("fx_quality") or "").strip().upper()
                if fx_quality not in {"OBSERVED", "MODELLED"}:
                    raise LedgerValidationError(
                        f"entry {index} foreign-currency FX quality must be OBSERVED or MODELLED"
                    )
                fx_source = str(raw.get("fx_source") or "").strip()
                if not fx_source or len(fx_source) > 120:
                    raise LedgerValidationError(
                        f"entry {index} foreign-currency FX source is required"
                    )
                fx_observed_at = _timestamp(
                    raw.get("fx_observed_at"),
                    f"entry {index} fx_observed_at",
                )
                if fx_observed_at > occurred_at:
                    raise LedgerValidationError(
                        f"entry {index} FX observation cannot be later than the transaction"
                    )

            if not fx_source or len(fx_source) > 120:
                raise LedgerValidationError(f"entry {index} fx_source is invalid")

            base_amount = round(quantity * unit_price * fx_rate, 12)
            if not math.isfinite(base_amount) or base_amount == 0:
                raise LedgerValidationError(f"entry {index} base_amount is not representable")

            normalized.append({
                "line_number": index,
                "account_code": account,
                "instrument_id": instrument,
                "quantity": quantity,
                "unit_price": unit_price,
                "quote_currency": quote_currency,
                "fx_rate_to_base": fx_rate,
                "fx_source": fx_source,
                "fx_observed_at": fx_observed_at,
                "fx_quality": fx_quality,
                "base_amount": base_amount,
            })
            distinct_accounts.add(account)

        if len(distinct_accounts) < 2:
            raise LedgerValidationError("a balanced transaction must touch at least two accounts")
        signed_total = math.fsum(entry["base_amount"] for entry in normalized)
        gross_total = math.fsum(abs(entry["base_amount"]) for entry in normalized)
        tolerance = max(BALANCE_TOLERANCE, gross_total * 1e-12)
        if abs(signed_total) > tolerance:
            raise LedgerValidationError(
                f"transaction is not balanced in {base_currency}: "
                f"signed base amount is {signed_total:.12f}"
            )
        return normalized

    def post_transaction(
        self,
        *,
        portfolio_id: str,
        idempotency_key: str,
        transaction_type: str,
        occurred_at: int,
        source: str,
        entries: Sequence[Mapping[str, Any]],
        reference: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one balanced transaction or return its identical prior copy.

        The FX rate on each line means units of portfolio base currency per
        one unit of that line's quote currency. Native quantities are signed:
        positive increases the named account; negative decreases it.
        """
        portfolio = str(portfolio_id or "").strip()
        if not _PORTFOLIO_RE.fullmatch(portfolio):
            raise LedgerValidationError("portfolio_id is invalid")
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 160:
            raise LedgerValidationError("idempotency_key is required and must be at most 160 characters")
        kind = str(transaction_type or "").strip().upper()
        if kind not in TRANSACTION_TYPES:
            raise LedgerValidationError(f"unsupported transaction_type: {kind}")
        timestamp = _timestamp(occurred_at, "occurred_at")
        source_value = str(source or "").strip()
        if not source_value or len(source_value) > 120:
            raise LedgerValidationError("source is required and must be at most 120 characters")
        reference_value = None if reference is None else str(reference).strip()
        if reference_value == "":
            reference_value = None
        if reference_value is not None and len(reference_value) > 180:
            raise LedgerValidationError("reference must be at most 180 characters")
        if metadata is None:
            metadata_value: dict[str, Any] = {}
        elif isinstance(metadata, Mapping):
            metadata_value = dict(metadata)
        else:
            raise LedgerValidationError("metadata must be a mapping")

        with self._lock:
            portfolio_row = self._db.execute(
                "SELECT base_currency FROM paper_portfolios WHERE portfolio_id=?",
                (portfolio,),
            ).fetchone()
            if portfolio_row is None:
                raise LedgerValidationError("portfolio must be created before posting transactions")
            base_currency = str(portfolio_row["base_currency"])
            normalized_entries = self._normalise_entries(
                entries,
                base_currency=base_currency,
                occurred_at=timestamp,
            )
            payload = {
                "portfolio_id": portfolio,
                "idempotency_key": key,
                "transaction_type": kind,
                "base_currency": base_currency,
                "occurred_at": timestamp,
                "source": source_value,
                "reference": reference_value,
                "metadata": metadata_value,
                "entries": normalized_entries,
            }
            payload_json = _canonical_json(payload)
            payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            transaction_id = "ptx-" + hashlib.sha256(
                f"{portfolio}|{key}".encode("utf-8")
            ).hexdigest()[:32]

            existing = self._db.execute(
                "SELECT transaction_id, payload_hash FROM paper_portfolio_transactions "
                "WHERE portfolio_id=? AND idempotency_key=?",
                (portfolio, key),
            ).fetchone()
            if existing:
                if str(existing["payload_hash"]) != payload_hash:
                    raise LedgerIdempotencyConflict(
                        "idempotency_key already exists with different transaction content"
                    )
                return {
                    "portfolio_id": portfolio,
                    "transaction_id": str(existing["transaction_id"]),
                    "idempotency_key": key,
                    "base_currency": base_currency,
                    "entry_count": len(normalized_entries),
                    "inserted": False,
                }

            self._db.execute(
                """INSERT INTO paper_portfolio_transactions(
                    portfolio_id, transaction_id, idempotency_key, transaction_type,
                    base_currency, occurred_at, source, reference, payload_json, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    portfolio, transaction_id, key, kind, base_currency,
                    timestamp, source_value, reference_value, payload_json, payload_hash,
                ),
            )
            self._db.executemany(
                """INSERT INTO paper_portfolio_entries(
                    portfolio_id, transaction_id, line_number, account_code,
                    instrument_id, quantity, unit_price, quote_currency,
                    fx_rate_to_base, fx_source, fx_observed_at, fx_quality, base_amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        portfolio, transaction_id, entry["line_number"],
                        entry["account_code"], entry["instrument_id"], entry["quantity"],
                        entry["unit_price"], entry["quote_currency"],
                        entry["fx_rate_to_base"], entry["fx_source"],
                        entry["fx_observed_at"], entry["fx_quality"], entry["base_amount"],
                    )
                    for entry in normalized_entries
                ],
            )

        return {
            "portfolio_id": portfolio,
            "transaction_id": transaction_id,
            "idempotency_key": key,
            "base_currency": base_currency,
            "entry_count": len(normalized_entries),
            "inserted": True,
        }

    def record_capital_contribution(
        self,
        portfolio_id: str,
        *,
        idempotency_key: str,
        amount: float,
        occurred_at: int,
        currency: str | None = None,
        fx_rate_to_base: float | None = None,
        fx_source: str | None = None,
        fx_observed_at: int | None = None,
        fx_quality: str | None = None,
        reference: str | None = None,
    ) -> dict[str, Any]:
        """Record opening paper cash against contributed capital, without orders."""
        portfolio = self.get_portfolio(portfolio_id)
        if portfolio is None:
            raise LedgerValidationError("portfolio must be created before adding capital")
        cash_amount = _finite_number(amount, "amount", positive=True)
        quote_currency = _currency_code(
            currency or portfolio["base_currency"], "currency"
        )
        if quote_currency == portfolio["base_currency"]:
            rate = 1.0
            quality = "IDENTITY"
            rate_source = "IDENTITY"
            rate_time = _timestamp(occurred_at, "occurred_at")
        else:
            rate = _finite_number(fx_rate_to_base, "fx_rate_to_base", positive=True)
            quality = str(fx_quality or "").strip().upper()
            rate_source = str(fx_source or "").strip()
            rate_time = _timestamp(fx_observed_at, "fx_observed_at")
            if quality not in {"OBSERVED", "MODELLED"} or not rate_source:
                raise LedgerValidationError(
                    "foreign-currency capital requires FX quality and source"
                )
        cash_instrument = f"cash:{quote_currency}"
        common_fx = {
            "unit_price": 1.0,
            "quote_currency": quote_currency,
            "fx_rate_to_base": rate,
            "fx_source": rate_source,
            "fx_observed_at": rate_time,
            "fx_quality": quality,
        }
        return self.post_transaction(
            portfolio_id=portfolio_id,
            idempotency_key=idempotency_key,
            transaction_type="CAPITAL_CONTRIBUTION",
            occurred_at=occurred_at,
            source="PAPER_ACCOUNTING",
            reference=reference,
            entries=[
                {
                    "account_code": f"ASSET:CASH:{quote_currency}",
                    "instrument_id": cash_instrument,
                    "quantity": cash_amount,
                    **common_fx,
                },
                {
                    "account_code": "EQUITY:CAPITAL",
                    "instrument_id": cash_instrument,
                    "quantity": -cash_amount,
                    **common_fx,
                },
            ],
        )

    def account_balances(self, portfolio_id: str) -> list[dict[str, Any]]:
        """Return native-unit balances and historical base-currency book values."""
        portfolio = self.get_portfolio(portfolio_id)
        if portfolio is None:
            raise LedgerValidationError("unknown portfolio")
        with self._lock:
            rows = self._db.execute(
                """SELECT account_code, instrument_id, quote_currency,
                          SUM(quantity) AS quantity,
                          SUM(base_amount) AS book_value_base,
                          COUNT(*) AS entry_count
                   FROM paper_portfolio_entries
                   WHERE portfolio_id=?
                   GROUP BY account_code, instrument_id, quote_currency
                   ORDER BY account_code, instrument_id, quote_currency""",
                (portfolio_id,),
            ).fetchall()
        result = []
        for row in rows:
            quantity = round(float(row["quantity"]), 12)
            book_value = round(float(row["book_value_base"]), 12)
            if abs(quantity) <= BALANCE_TOLERANCE and abs(book_value) <= BALANCE_TOLERANCE:
                continue
            result.append({
                "account_code": str(row["account_code"]),
                "account_type": str(row["account_code"]).split(":", 1)[0],
                "instrument_id": str(row["instrument_id"]),
                "quote_currency": str(row["quote_currency"]),
                "quantity": quantity,
                "book_value_base": book_value,
                "base_currency": str(portfolio["base_currency"]),
                "entry_count": int(row["entry_count"]),
            })
        return result

    def trial_balance(self, portfolio_id: str) -> dict[str, Any]:
        """Return debit/credit totals; this is not a mark-to-market equity report."""
        portfolio = self.get_portfolio(portfolio_id)
        if portfolio is None:
            raise LedgerValidationError("unknown portfolio")
        balances = self.account_balances(portfolio_id)
        account_totals: dict[str, float] = {}
        type_totals: dict[str, float] = {}
        for row in balances:
            amount = float(row["book_value_base"])
            account_totals[row["account_code"]] = round(
                account_totals.get(row["account_code"], 0.0) + amount, 12
            )
            account_type = str(row["account_type"])
            type_totals[account_type] = round(
                type_totals.get(account_type, 0.0) + amount, 12
            )
        debit_total = math.fsum(max(amount, 0.0) for amount in account_totals.values())
        credit_total = math.fsum(max(-amount, 0.0) for amount in account_totals.values())
        difference = round(debit_total - credit_total, 12)
        return {
            "status": "PASS" if abs(difference) <= BALANCE_TOLERANCE else "FAIL",
            "portfolio_id": portfolio_id,
            "base_currency": str(portfolio["base_currency"]),
            "debits_base": round(debit_total, 12),
            "credits_base": round(credit_total, 12),
            "difference_base": difference,
            "account_type_totals_base": type_totals,
            "account_balances_base": account_totals,
            "mark_to_market": False,
        }

    def recent_transactions(self, portfolio_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 500:
            raise LedgerValidationError("limit must be between 1 and 500")
        with self._lock:
            rows = self._db.execute(
                """SELECT transaction_id, idempotency_key, transaction_type,
                          base_currency, occurred_at, source, reference
                   FROM paper_portfolio_transactions
                   WHERE portfolio_id=?
                   ORDER BY sequence DESC LIMIT ?""",
                (portfolio_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def replay(self, portfolio_id: str | None = None) -> dict[str, Any]:
        """Verify journal payload hashes, entries, portfolio config and balance."""
        errors: list[str] = []
        params: tuple[Any, ...] = ()
        where = ""
        if portfolio_id is not None:
            where = "WHERE portfolio_id=?"
            params = (portfolio_id,)
        with self._lock:
            transactions = self._db.execute(
                f"SELECT * FROM paper_portfolio_transactions {where} ORDER BY sequence",
                params,
            ).fetchall()
            orphan_sql = (
                "SELECT COUNT(*) FROM paper_portfolio_entries e "
                "LEFT JOIN paper_portfolio_transactions t "
                "ON t.transaction_id=e.transaction_id "
                + ("WHERE t.transaction_id IS NULL AND e.portfolio_id=?" if portfolio_id is not None
                   else "WHERE t.transaction_id IS NULL")
            )
            orphan_count = int(self._db.execute(orphan_sql, params).fetchone()[0])
            checked_entries = 0
            for transaction in transactions:
                transaction_id = str(transaction["transaction_id"])
                raw_payload = str(transaction["payload_json"])
                stored_hash = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
                if stored_hash != str(transaction["payload_hash"]):
                    errors.append(f"{transaction_id}: payload hash mismatch")
                    continue
                try:
                    payload = json.loads(raw_payload)
                except (TypeError, ValueError):
                    errors.append(f"{transaction_id}: payload JSON is invalid")
                    continue

                portfolio = self._db.execute(
                    "SELECT base_currency FROM paper_portfolios WHERE portfolio_id=?",
                    (transaction["portfolio_id"],),
                ).fetchone()
                if portfolio is None or str(portfolio["base_currency"]) != str(transaction["base_currency"]):
                    errors.append(f"{transaction_id}: portfolio base currency mismatch")

                expected_header = {
                    "portfolio_id": payload.get("portfolio_id"),
                    "idempotency_key": payload.get("idempotency_key"),
                    "transaction_type": payload.get("transaction_type"),
                    "base_currency": payload.get("base_currency"),
                    "occurred_at": payload.get("occurred_at"),
                    "source": payload.get("source"),
                    "reference": payload.get("reference"),
                }
                actual_header = {
                    "portfolio_id": str(transaction["portfolio_id"]),
                    "idempotency_key": str(transaction["idempotency_key"]),
                    "transaction_type": str(transaction["transaction_type"]),
                    "base_currency": str(transaction["base_currency"]),
                    "occurred_at": int(transaction["occurred_at"]),
                    "source": str(transaction["source"]),
                    "reference": transaction["reference"],
                }
                if expected_header != actual_header:
                    errors.append(f"{transaction_id}: header does not match its payload")
                expected_id = "ptx-" + hashlib.sha256(
                    f"{transaction['portfolio_id']}|{transaction['idempotency_key']}".encode("utf-8")
                ).hexdigest()[:32]
                if expected_id != transaction_id:
                    errors.append(f"{transaction_id}: deterministic transaction identity mismatch")

                rows = self._db.execute(
                    "SELECT line_number, account_code, instrument_id, quantity, unit_price, "
                    "quote_currency, fx_rate_to_base, fx_source, fx_observed_at, fx_quality, base_amount "
                    "FROM paper_portfolio_entries WHERE transaction_id=? ORDER BY line_number",
                    (transaction_id,),
                ).fetchall()
                actual_entries = [{
                    "line_number": int(row["line_number"]),
                    "account_code": str(row["account_code"]),
                    "instrument_id": str(row["instrument_id"]),
                    "quantity": float(row["quantity"]),
                    "unit_price": float(row["unit_price"]),
                    "quote_currency": str(row["quote_currency"]),
                    "fx_rate_to_base": float(row["fx_rate_to_base"]),
                    "fx_source": str(row["fx_source"]),
                    "fx_observed_at": int(row["fx_observed_at"]),
                    "fx_quality": str(row["fx_quality"]),
                    "base_amount": float(row["base_amount"]),
                } for row in rows]
                checked_entries += len(actual_entries)
                if actual_entries != payload.get("entries"):
                    errors.append(f"{transaction_id}: journal entries do not match their payload")
                total = math.fsum(item["base_amount"] for item in actual_entries)
                gross = math.fsum(abs(item["base_amount"]) for item in actual_entries)
                tolerance = max(BALANCE_TOLERANCE, gross * 1e-12)
                if abs(total) > tolerance:
                    errors.append(f"{transaction_id}: entries do not balance in base currency")
        if orphan_count:
            errors.append(f"{orphan_count} orphan journal entries found")
        return {
            "status": "PASS" if not errors else "FAIL",
            "portfolio_id": portfolio_id,
            "transaction_count": len(transactions),
            "entry_count": checked_entries,
            "orphan_entry_count": orphan_count,
            "errors": errors,
        }
