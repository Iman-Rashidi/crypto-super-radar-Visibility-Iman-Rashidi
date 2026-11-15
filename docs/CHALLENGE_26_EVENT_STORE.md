# Challenge 26 — Persistent Event Store

## Scope and release boundary

This is an isolated, exchange-agnostic SQLite implementation for order-intent lifecycle persistence. It makes no HTTP calls, has no exchange credentials, and cannot submit, amend, cancel, or close orders. It is not wired into the current paper or BloFin execution cycle and is not a deployment approval.

## Architecture

\`OrderStateMachine -> TransitionValidator -> PersistentEventStore -> SQLite\`

- \`OrderStateMachine\` is the allow-list for legal order and position state changes.
- \`TransitionValidator\` enforces reconciliation evidence for transitions out of \`UNKNOWN\` and independent TP/SL proof before \`POSITION_OPEN_UNPROTECTED -> POSITION_MANAGED\`.
- \`PersistentEventStore\` owns each write transaction, immutable intent identity, idempotency checks, per-intent sequence numbers, event hash chaining, projection updates, and replay verification.
- SQLite stores \`trade_intents\` separately from \`trade_events\`.

## Invariants

1. Intent registration and the first \`NONE -> INTENT_RECORDED\` event commit in one \`BEGIN IMMEDIATE\` transaction.
2. \`intent_id\` and \`client_order_id\` are unique; an existing intent must match both its client ID and canonical JSON SHA-256 payload hash.
3. Every event has a stable caller-supplied \`event_id\`, a unique \`seq_num\` within its intent, fixed client ID and payload hash, \`from_state\`, \`to_state\`, UTC millisecond timestamp, and exchange order ID when known.
4. Replaying an identical \`event_id\` returns the stored event. Reusing it with different content fails.
5. Event rows are protected from UPDATE and DELETE by SQLite triggers. Event hashes include the prior event hash and canonical event fields.
6. Recovery checks sequence continuity, initial state, transition legality, stable identity/hash, exchange-order-ID continuity, UTC timestamp ordering, hash-chain integrity, and agreement between the final event and \`trade_intents.current_state\`.
7. \`UNKNOWN\` cannot move to \`SUBMITTING\` or \`SUBMITTED\`. It can leave only with \`RECONCILIATION\` event type plus verified evidence that identifies the observed exchange status and order.
8. \`POSITION_OPEN_UNPROTECTED\` cannot become \`POSITION_MANAGED\` without evidence for the same position ID, matching side and size, distinct active TP/SL order IDs, and trigger prices on the correct side of entry.
9. WAL is set during initialization for file-backed SQLite databases, not for each operation. Each write starts with \`BEGIN IMMEDIATE\`; failures roll back the event and intent projection together.

## Current unit-test scope

- atomic intent registration and rollback;
- duplicate intent, changed payload, and client-ID collision;
- duplicate event retries and conflicting event-ID reuse;
- two-connection concurrency;
- restart recovery;
- append-only triggers and manually corrupted history detection;
- sequence gaps and projection/event disagreement;
- legal and illegal transitions, terminal states, and evidence-gated \`UNKNOWN\` recovery;
- TP/SL position, size, side, and trigger validation.

Run the isolated suite:

\`\`\`bash
python -m unittest discover -s tests -p 'test_*.py' -v
\`\`\`

## Explicit limitations

- This is a new isolated module, not a migration of the existing \`PaperExecutionEventLedger\`.
- No exchange adapter currently supplies or independently validates the evidence fields; unit tests use synthetic fixtures only.
- A local hash chain detects accidental changes and inconsistent history, but it is not a cryptographic trust anchor against an actor who can rewrite the database and recompute every hash. Stronger tamper evidence would require signed or externally anchored checkpoints and protected backups.
- No Render, production, environment variable, secret, or live/demo order flow is changed by this module.
