# Paper Execution Model

## Scope

The Paper execution path is an offline simulation only. It does not connect to exchange trading endpoints, sign transactions, or submit orders. `PAPER_ONLY` remains the only supported mode.

## Deterministic fill simulation

`crypto_super_radar.paper_fill_simulator.PaperFillSimulator` accepts an explicit quote and explicit base-asset liquidity quantity. It will not infer missing liquidity or substitute a stale observed quote.

- Observed quotes require a timestamp and must be within the configured freshness window (5 seconds by default, with a 2-second future-skew tolerance).
- Modelled quotes are labelled `MODELLED`; they are never represented as observed market data.
- The fillable amount is `min(requested_quantity, available_liquidity_quantity * max_participation_rate)`.
- `fill_fractions` must be positive and sum to 1.0. They deterministically split the fillable amount into one or more fill slices.
- BUY fills use best ask plus adverse impact; SELL fills use best bid minus adverse impact.
- Spread, adverse slippage and fee are calculated separately for each slice through the existing execution-cost calculator. Spot funding is explicitly `NOT_APPLICABLE`.
- Simulated latency is timestamp metadata only. The simulator does not sleep or delay execution in wall-clock time.
- An unfilled remainder stays `PARTIALLY_FILLED` unless the caller explicitly requests cancellation; zero-liquidity orders remain `ACCEPTED` or become `CANCELED` under that explicit policy.

## Event persistence

When an execution event ledger is supplied, all event writes must be performed inside the caller's database transaction. The ledger intentionally does not commit, so callers can make event history and account projections atomic. A standalone use should wrap the simulation in `with db:` and commit only after the complete simulated operation succeeds.

The lifecycle follows the existing state machine:
`CREATED → VALIDATED → SUBMITTED → ACCEPTED → PARTIALLY_FILLED/FILLED`.
Cumulative fill quantity and cumulative average price are stored on each fill event. Event IDs and idempotency keys are deterministic per order and slice.

## Projection reconciliation

`PaperLedger.reconciliation()` compares:

1. Open position entry fill against the entry event.
2. Every closed paper trade's entry and exit fills against their journal events.
3. Actual paper cash against initial equity + realized PnL - open-position debit.
4. The append-only journal's replay integrity.

Statuses:
- `PASS`: all checked projections and event evidence reconcile.
- `INCOMPLETE`: a legacy projection or an event has no corresponding record; no synthetic evidence is created.
- `FAIL`: journal integrity, fill values, identity, or account cash disagree.
- `NO_DATA`: there is no open position, no closed trade, and no execution event to reconcile.

## Current acceptance boundary

The simulator and reconciliation tests are offline deterministic regression tests. The simulator is not yet integrated into the main Paper Bot entry/exit sizing path; that integration must explicitly account for partial positions and remaining order quantity before it is enabled in the scheduled cycle. Production Turso write/readback/reconnect and current-head Python 3.14 CI remain separate release gates.
