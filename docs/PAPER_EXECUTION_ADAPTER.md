# PaperExecutionAdapter — deterministic fill simulation

## Scope

\`PaperExecutionAdapter\` is a local, deterministic simulator. It accepts an explicit market-book snapshot from its caller and writes simulated order lifecycle/fill events to \`PaperExecutionEventLedger\`. It has no HTTP client, exchange credential, signing capability, or live-order endpoint.

This adapter is not yet wired to the production \`PaperTradingBot\` cycle. The current bot still uses its existing immediate paper-fill path. Integration must wait until the provider layer supplies timestamped bid/ask depth and a tested mapping from venue depth to base-asset liquidity.

## Lifecycle

An internal paper intent moves through \`CREATED → VALIDATED → SUBMITTED → ACCEPTED\`. Supplied book snapshots can then append \`PARTIALLY_FILLED\` events until cumulative filled quantity equals the requested quantity, at which point the order becomes \`FILLED\`.

Each snapshot requires a stable \`snapshot_id\`. Reprocessing the same snapshot is idempotent; reusing its ID with different fill inputs is rejected by the event ledger. A stale quote produces \`UNKNOWN\` without a fill. Zero available liquidity produces \`NO_FILL\` and leaves the state unchanged.

## Fill model

- BUY fills reference best ask; SELL fills reference best bid.
- Adverse slippage is applied in basis points in the trade direction.
- Fees are calculated per incremental fill.
- Filled base quantity cannot exceed \`available_liquidity_qty × max_participation_pct\`.
- Participation is bounded to 100% of the explicitly supplied liquidity.
- The event ledger stores cumulative filled quantity; \`fee_paid_usd\` and \`fill_price\` belong to the incremental fill event.

The simulator does not infer hidden depth, volume, or exchange queue position. The quality of its results is bounded by the freshness and correctness of the supplied snapshot.

## Validation

Run the isolated tests with:

\`\`\`bash
python -m unittest tests.test_paper_execution_adapter -v
\`\`\`

Tests cover partial-to-full fill progression, participation caps, per-fill fees, adverse BUY/SELL slippage, stale quotes, empty liquidity, invalid/crossed books, submit retry, and duplicate snapshot idempotency.

## Release boundary

F-49 remains PARTIAL until this adapter is integrated with a real, timestamped public-data depth adapter and its Paper account projection is updated atomically for each incremental fill. No Render Cron, Production cycle, or live execution is enabled by this module.
