# FIFO Cost Basis and Realized P&L — Stage 2

## Status and safety boundary

This stage adds an isolated FIFO lot-accounting layer over `MultiCurrencyPaperLedger`. It is not connected to the active Paper Bot and does not create orders, fills, signals, or broker/exchange requests.

- PAPER_ONLY remains unchanged.
- Live orders remain disabled.
- The existing USD 10,000 Paper Bot account is not migrated or modified.
- Per-trade risk remains capped at 0.5%; maximum notional remains 10% of equity.
- US/EU equities, ETFs, FX and metals remain research-only.
- No execution allowlist or provider readiness gate is changed.

## Accounting method

The method is **FIFO (First-In, First-Out)**. Each accepted buy creates one immutable acquisition lot. A sale consumes the oldest available lot first, ordered by acquisition timestamp, journal sequence, then lot ID for deterministic tie-breaking.

Each lot retains:
- Original acquired quantity and acquisition timestamp.
- Original quote-currency unit cost.
- Original FX rate, source, observation timestamp and quality.
- Source journal transaction and sequence.

Partial disposals allocate only the consumed quantity; the unconsumed quantity remains open. A sale larger than open inventory is rejected before a journal transaction is written.

## Journal treatment

For a buy, the journal debits inventory at acquisition-date base-currency cost and credits cash at the same amount.

For a sale:
1. Cash is debited at sale proceeds translated using the sale-date FX observation.
2. Inventory is credited at the historical base-currency cost of the FIFO lots consumed, using each lot's original FX rate.
3. The difference is posted to `INCOME:REALIZED_PNL` for a gain or `EXPENSE:REALIZED_PNL` for a loss.

Therefore:

`realized_pnl_base = sale_proceeds_base - fifo_cost_basis_base`

A sale may consume lots acquired in different quote currencies or at different FX rates. Each inventory-relief journal line preserves the source lot's historical FX provenance.

## Persistence and atomicity

Module: `crypto_super_radar/fifo_lot_accounting.py`

Class: `FIFOLotAccounting`

Append-only tables:
- `paper_cost_basis_lots`: immutable acquisition records.
- `paper_cost_basis_allocations`: immutable sale-to-lot allocations.

The module never commits on behalf of its caller. The caller must wrap the journal transaction, lot creation/allocation, and any account projection in the same database transaction (for SQLite, use the connection context manager). This is required for atomicity.

Stable idempotency keys are mandatory. Repeating an identical buy or sale returns the original transaction; reusing a sale key with a different request raises `LedgerIdempotencyConflict`. Lot and allocation tables reject UPDATE and DELETE operations.

`FIFOLotAccounting.replay()` checks allocation references, instrument consistency, positive allocation quantities, and that no lot is over-allocated. The underlying journal's `replay()` independently verifies journal payload hashes, entry integrity and double-entry balance.

## Deliberate exclusions

This stage reports **gross realized P&L before fees**. Trading fees, spread, slippage, funding, borrow and holding costs are not included in the lot cost or realized P&L yet. They must be separately posted as expenses until the fee-aware cost model is implemented.

Not included:
- Unrealized P&L, mark-to-market snapshots or NAV.
- Corporate actions (splits, mergers, dividends, symbol changes).
- FX freshness policy beyond timestamp/provenance validation.
- Trading calendars, tax-lot elections or jurisdiction-specific tax treatment.
- Short positions, margin, leverage or negative inventory.
- Active Paper Bot integration.

Missing or stale prices/FX must remain unknown in the future valuation layer; they must not be silently replaced with zero or stale values.

## Tests

`tests/test_fifo_lot_accounting.py` covers:
- FIFO consumption across multiple acquisition lots.
- Partial lot depletion.
- Realized gain and loss postings.
- Acquisition-date FX versus sale-date FX.
- Oversell rejection with no journal write.
- Idempotent buy/sale retries and conflicting retries.
- Append-only lot/allocation tables.
- Required foreign-currency FX provenance.
- Journal trial balance and replay reconciliation.

This is an isolated accounting stage only. It does not authorize any new asset class or change the active Paper Bot execution path.
