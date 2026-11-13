# Multi-Asset Paper Ledger — Stage 1

## Status and safety boundary

This document describes the first accounting foundation for Crypto Super Radar. The new module is an isolated, append-only paper journal. It is **not connected to the active Paper Bot**, does not create positions or signals, does not call exchange or broker APIs, and cannot submit orders.

The existing bot remains unchanged:

- Mode: PAPER_ONLY.
- Live orders: disabled.
- Existing crypto Paper flow: unchanged.
- US/EU equities, ETFs, FX and metals: research-only and not eligible for Paper execution.
- Risk limits remain 0.5% of equity per trade and 10% of equity maximum notional.
- The USD 10,000 initial balance in the existing Paper Bot is not migrated or changed by this module.

## Implemented contract

Module: crypto_super_radar/multi_currency_ledger.py

Class: MultiCurrencyPaperLedger

The module provides:

1. Separate portfolio identities, each with one immutable functional base currency (for example USD or EUR).
2. Signed native quantities for cash balances and asset inventory.
3. Double-entry transactions with base-currency balancing.
4. Per-line FX rate, source, observation timestamp and quality (OBSERVED, MODELLED, or base-currency IDENTITY).
5. An append-only transaction journal and append-only entry rows.
6. A deterministic transaction ID and per-portfolio idempotency key.
7. Native quantity and historical book-value account balances.
8. Trial-balance reconciliation and journal replay checks.
9. A capital-contribution helper for opening a paper portfolio without an order.

### Amount and FX convention

For each entry:

    base_amount = quantity × unit_price × fx_rate_to_base

- quantity is signed: positive increases the named account; negative decreases it.
- unit_price is denominated in quote_currency.
- fx_rate_to_base means one unit of quote currency expressed in the portfolio's base currency.
- The sum of all base_amount values in one transaction must equal zero within an absolute tolerance of 1e-8 base-currency units (or a proportional tolerance for large transactions).
- For the portfolio base currency, FX is fixed to 1.0 and quality must be IDENTITY.
- Foreign-currency entries require a positive FX rate, explicit source, observation timestamp no later than the transaction timestamp, and quality OBSERVED or MODELLED. The module records this provenance; it does not yet enforce a maximum FX age.

Accounts use one of five prefixes: ASSET, LIABILITY, EQUITY, INCOME, EXPENSE. The usual journal convention applies: positive base amounts are debits and negative amounts are credits. The account hierarchy after the prefix is application-defined.

## Example: separate USD cash, EUR cash and a European equity position

The following example is illustrative. It does not imply that the instrument is approved for trading.

1. Create a portfolio with USD as its functional currency.
2. Record USD 10,000 as cash against contributed capital.
3. Convert USD 990 into EUR 900 using a timestamped EUR/USD rate of 1.10 USD per EUR.
4. Record a hypothetical purchase of five SAP shares at EUR 180 each, using the same timestamped EUR/USD rate.

The journal then reports the residual USD cash, EUR cash, and five SAP shares with a historical carrying value of USD 990. The EUR cash becomes zero after the example purchase. The journal's trial balance must remain balanced after every transaction.

## Transactional and idempotency rules

Every write operation must be called inside the caller's database transaction, so the journal can be committed atomically with a future account/position projection. The ledger intentionally does not commit on behalf of the caller.

The caller must provide a stable idempotency_key and explicit occurred_at timestamp. Repeating the same key with the same canonical payload returns the original transaction ID without inserting duplicate rows. Reusing the key with different content raises LedgerIdempotencyConflict.

Portfolio definitions, transactions and entry rows have database triggers that reject updates and deletes. replay() checks payload hashes, header-to-payload consistency, entry-to-payload consistency, deterministic transaction IDs, orphan entry rows and transaction balance. This detects inconsistent persisted data; it is not a cryptographic signature against an actor who can rewrite the entire database and its hashes.

## Current reporting limits

account_balances() returns the net native quantity and summed historical base-currency carrying value for each account/instrument/quote-currency combination.

trial_balance() verifies total debits equal total credits. The response explicitly sets mark_to_market=false.

The module does **not yet** calculate:

- FIFO or average-cost lot allocation.
- Realized P&L on asset disposal.
- Unrealized P&L from current market marks.
- Net asset value from fresh market prices.
- Dividends, splits, mergers, symbol changes or other corporate actions beyond accepting transaction-type labels.
- FX exposure limits, stale-FX thresholds, trading calendars or market-specific costs.
- Margin, borrowing, short inventory or financing eligibility.

A balanced trial balance is an accounting invariant, not proof of profitable trading, accurate market valuation or trade eligibility. Missing marks and FX inputs must remain unknown in later valuation stages rather than being silently treated as zero.

## Validation

Focused tests are in tests/test_multi_currency_ledger.py. They cover base-currency immutability, separate USD/EUR portfolios, cash contributions, an EUR conversion and EU SAP inventory example, balance rejection, FX provenance and timestamp controls, idempotent retries, append-only triggers, database reconnect and tamper detection through replay.

The full GitHub Actions offline suite remains the acceptance gate. Live-provider acceptance is a separate data-readiness gate and must not be weakened to make accounting tests pass.

## Next controlled steps

1. Connect the journal to the current crypto Paper flow using atomic dual-write and reconciliation while preserving the existing single-position behavior.
2. Implement cost-basis lots and realized P&L, then mark-to-market snapshots with explicit quote and FX provenance.
3. Add independent price/FX validation, freshness gates and corporate-action processing.
4. Add exchange calendars and per-market cost models before any non-crypto Paper path is enabled.
5. Run replay, restart, backtest and walk-forward/OOS validation before enabling any additional Paper asset class.

Until those steps pass, this module remains an accounting foundation only. It must not be interpreted as permission to trade stocks, ETFs, FX, metals or crypto beyond the active controlled Paper flow.
