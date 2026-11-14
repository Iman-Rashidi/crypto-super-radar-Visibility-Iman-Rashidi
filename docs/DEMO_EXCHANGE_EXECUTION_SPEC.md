# Demo Exchange Execution Specification

**Status: Requirements captured; implementation and deployment are NOT complete.**  
**Target branch:** `feature/unlimited-human-demo-cycles`  
**Initial exchange target:** Binance Demo Trading (Spot + USDⓈ-M Futures)

This document records the corrected requirement. It does not enable exchange execution by itself.

## 1. Required operating behaviour

- There is no hard-coded lifetime order quota, per-service-start quota, per-session quota, or fixed maximum number of total Demo trades.
- Restarting or waking the Render service must not consume or permanently lock the ability to submit another Demo order.
- The bot may maintain multiple open positions concurrently. Remove the current single-active-position assumption from both Spot and Futures account managers.
- Each approved entry gets a unique client order ID and an independently auditable order lifecycle. Repeated submission of the same approved proposal must be idempotent.
- Same completed candle + same strategy/symbol/side must not create duplicate orders. A later candle or a new separately approved signal may create another entry.
- Order count and concurrent positions are bounded only by the Demo exchange account's available balances/margin, exchange filters, symbol rules, exchange open-order/conditional-order limits, and published rate limits. The application must not invent a smaller lifetime/session cap.
- The bot must never silently convert Demo execution into production execution.

## 2. Human supervision is mandatory

Every new entry must pass through a durable proposal queue:

1. Run the research engine and require exactly one PASS for each mandatory gate 1–12.
2. Require a fresh, valid Nemotron second opinion with `APPROVED` and `paper_entry_eligible=true`.
3. Revalidate exchange quote, account balance/margin, sizing, risk budget, stop/target ordering, and proposal expiry.
4. Show a human-readable order preview: exchange, Demo environment, market type, symbol, side, notional, quantity, leverage (Futures only), isolated/cross margin mode, entry type/price, stop loss, take profit, estimated fees, risk in USDT, and all 12 gate statuses.
5. Submit only after an authenticated human explicitly approves that exact proposal.
6. Reject, expire, or cancel the proposal without sending an order if the human rejects it, the market quote becomes stale, the price drift exceeds the configured bound, the proposal expires, or any gate/risk/account check no longer passes.

Approval must be single-use, bound to a proposal hash and expiry, and checked server-side. The approval endpoint must not be exposed without authentication/CSRF protection. API keys and secrets must never be sent to the browser, logs, GitHub, Notion, or chat.

## 3. Exchange environments

Use Binance Demo Trading, not the existing Spot Testnet URL:

- Spot Demo REST base: `https://demo-api.binance.com` (Spot API paths are used under this base).
- USDⓈ-M Futures Demo REST base: `https://demo-fapi.binance.com`.
- Store separate Spot Demo and Futures Demo API keys as server-side Render secrets.
- Reject production hostnames in the Demo executor, regardless of browser fields or request payloads.
- No withdrawal permission is needed. API keys should have trading/read access only and be restricted to the service IP where feasible.
- `CSR_BINANCE_ENV=production` must remain rejected by the bot. Do not add a user-facing switch to production.

## 4. Spot and Futures must remain distinct

### Spot Demo

- Market has no leverage. The UI must show leverage as `N/A (Spot)`, not 1x or another multiplier.
- Entry and exit orders must be visible in Binance Demo order/trade history.
- Once an entry is confirmed filled, place exchange-native protective stop-loss/take-profit orders for the actually filled quantity (use a supported Spot OCO/order-list workflow where valid).
- Reconcile order-list IDs, child order IDs, executed quantity, partial fills, cancellations, and balances from Binance.
- If the protective exit cannot be confirmed after entry, immediately halt additional entries, raise a critical alert, and execute the configured safe compensation/flatten procedure only after reconciliation. Never label the position protected until both exchange-side exits are confirmed.

### USDⓈ-M Futures Demo

- Support LONG and SHORT only after the 12 gates, AI review, and account/risk checks pass.
- Show leverage, margin mode, position side, margin used, mark price, entry price, liquidation price when supplied by Binance, unrealized/realized PnL, funding, stop loss and take profit.
- Use exchange-native conditional/algo orders for stop loss and take profit and reconcile their IDs/status independently from regular entry orders.
- Keep the leverage default at 1x until exchange-specific margin tiers, liquidation handling, and risk limits are verified. Any increase requires a separately configured hard cap and human confirmation.
- Support multiple symbols concurrently. Respect Binance's actual position-mode semantics: One-way mode aggregates exposure per symbol; Hedge mode separates LONG and SHORT sides but does not create unlimited independent sub-positions for the same symbol/side. The app may track individual strategy lots locally, but must reconcile their aggregate exchange position exactly.

## 5. Multiple-position accounting

Replace the singleton position records (`id=1`) in the current Spot and Futures paper ledgers with position/lot records that have stable unique IDs. Add migrations that preserve existing account and trade history.

- Account state and risk are portfolio-wide, not reset per symbol.
- Allocate available Spot balance / Futures margin across all open positions.
- Compute aggregate open risk, exposure, used margin, and available balance before each approval and again immediately before submission.
- Manage every open position on each service cycle, including stop, target, trailing stop, time exit, funding, and emergency state.
- Use database transactions and unique constraints to prevent duplicate entries under concurrent workers.
- Never allow closing one position to delete another position's records.
- A service restart must restore all pending proposals, open orders, protective orders, and positions from durable storage, then reconcile them with Binance before accepting new approvals.

## 6. Order lifecycle and failure handling

Persist exchange responses and events for:
`PROPOSED -> HUMAN_APPROVED -> SUBMITTING -> NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/UNKNOWN`.

- Never retry an order POST after timeout or ambiguous response.
- On `UNKNOWN`, query Binance using the unique client order ID before any further submit/cancel action.
- Reconcile partial fills and size protective exits to the actual filled quantity.
- Do not assume that a successful HTTP response means the position is protected.
- Stop all new entries if exchange data is stale, account reconciliation fails, or any open position lacks confirmed protective exits.
- Add a prominent global emergency pause. Closing/flattening must also require human confirmation unless a separately approved, narrowly-scoped emergency policy is implemented.

## 7. UI requirements

Provide a live supervisory panel with:

- Demo connection/environment indicator that cannot be mistaken for Production.
- Pending proposals with approve/reject actions.
- Multiple open Spot and Futures positions in separate rows.
- Per-position entry order, exit order(s), stop-loss, take-profit, leverage/margin (Futures), fills, fees, PnL, and exchange status.
- Open orders, completed trades, cancelled/rejected orders, and unresolved/unknown submissions.
- Human pause/resume and emergency stop.
- Direct exchange links to the relevant Binance Demo Spot/Futures interface where available.

## 8. Required acceptance tests

- More than one Spot position can remain open at once.
- More than one Futures position can remain open across different symbols, and both LONG and SHORT are handled according to the configured position mode.
- A second distinct approved signal is accepted while another position is open, subject only to exchange/account/risk constraints.
- Same proposal approval is idempotent and cannot submit twice.
- No lifetime or service-start counter blocks a valid new Demo proposal.
- Restart restores state and reconciles open positions/orders before resuming.
- Human rejection or missing authentication submits zero orders.
- Any FAIL/UNKNOWN gate or non-approved Nemotron response submits zero orders.
- Stale price, price drift, insufficient balance/margin, invalid quantity step, or exchange filter violation submits zero orders.
- Entry partial fill creates protective exits only for filled quantity.
- Failure to establish protective exits causes a critical halt and never reports the position as protected.
- Spot always reports leverage as N/A.
- All integration tests use mocked exchange responses or Binance Demo credentials only; tests must assert that production hostnames are unreachable.
- Test the demo order placement, cancel, query and OCO/algo protection flows against the Demo environment before enabling the service.

## 9. Known repository gap at requirement capture

The current feature branch has:
- Internal SQLite/Turso paper ledgers and simulated fills.
- A Binance Spot adapter targeting Spot Testnet by default, not Spot Demo.
- No completed exchange-side protective-exit lifecycle in that adapter.
- Singleton Spot and Futures open-position tables and one-position bot logic.
- No authenticated human order-approval queue connected to exchange order submission.

Therefore this request is **not implemented or deployed yet**. Do not describe internal Paper fills as exchange Demo orders.
