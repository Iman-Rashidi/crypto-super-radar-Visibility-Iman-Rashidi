# Gate-A — Persistent EventStore Integrity Audit

## Scope

Audited the existing implementation on the `challenge/ch26-persistent-event-store` source commit. Gate-A verification is isolated to the existing SQLite event store. No parallel event-store implementation was created.

## Existing architecture

```text
PersistentEventStore
  -> EventStoreRegistration
  -> EventStoreTransitions
  -> EventStoreRecovery
  -> EventStoreStorage
  -> SQLite
```

The public store is assembled from focused mixins. The storage layer owns the SQLite schema and transactions; registration and transition mixins write immutable event history; recovery replays and verifies it.

## Schema and invariant findings

- `trade_intents.intent_id` is the aggregate primary key.
- `trade_intents.client_order_id` is unique.
- `trade_events.event_id` is the primary key (globally unique).
- `trade_events.intent_id` references `trade_intents.intent_id`.
- `(intent_id, seq_num)` is unique and `seq_num` must be positive.
- Event UPDATE and DELETE are rejected by SQLite triggers.
- Intent identity columns are protected by a separate trigger.
- Canonical JSON and SHA-256 are used for payload and event hashes.
- Each write uses `BEGIN IMMEDIATE`; intent registration and its first event are committed atomically.
- Recovery checks sequence continuity, previous hash, event hash, payload hash, timestamp order, transition legality, exchange-order continuity, and projection agreement.
- File-backed databases enable WAL during initialization.

## Important boundary: Prediction lineage

The current schema has **no predictions table and no `prediction_id` / `parent_prediction_id` field**. Its real parent relationship is `trade_events.intent_id -> trade_intents.intent_id`. The Gate-A test suite verifies this implemented foreign-key relationship and rejects orphan events.

This does **not** prove a Prediction-to-Intent lineage. No synthetic Prediction relationship or parallel table was added. Prediction lineage remains a separate integration requirement and must be designed against the project's actual prediction record source before it can be truthfully tested.

## New automated checks

`tests/test_gate_a_integrity.py` contains ten pytest test cases:

1. Event ID uniqueness and conflicting replay rejection.
2. Previous-event-hash tampering detection.
3. Payload tampering detection.
4. Deleted-event detection.
5. Event-order tampering detection.
6. Parent-intent foreign-key enforcement.
7. File-backed SQLite close/reopen recovery using pytest `tmp_path`.
8. Full valid-chain verification.
9. Registered evaluation-protocol SHA-256 verification.
10. Protocol tampering and unregistered thresholds block evaluation.

Existing unittest coverage in `tests/test_event_store.py`, `tests/test_recovery.py`, and `tests/test_transitions.py` remains in place.

## Evaluation protocol status

`docs/evaluation_protocol.yaml` is intentionally marked `DRAFT_BLOCKED`. The SHA-256 sidecar is committed and verified by `scripts/verify_evaluation_protocol.py`.

Numeric thresholds, purge/embargo lengths, minimum sample counts, Sharpe confidence level, and regime-specific sample requirements are still unset. The verifier therefore refuses evaluation readiness even when the protocol hash is valid. No arbitrary performance thresholds were invented.

Run the integrity check:

```bash
python -m pytest -q tests/test_gate_a_integrity.py
python scripts/verify_evaluation_protocol.py
```

Check whether the protocol is ready for model evaluation (expected to remain blocked until numeric registration is completed):

```bash
python scripts/verify_evaluation_protocol.py --require-ready
```

## Safety and release boundary

- Tests use local temporary SQLite files and synthetic fixtures only.
- No network exchange adapter is invoked by the Gate-A tests.
- No Demo or Live order is placed.
- No Render service, environment variable, database secret, or Production setting is changed.
- No deployment approval is implied by a passing local or GitHub Actions test run.

## Execution evidence

- GitHub Actions workflow: `gate-a-eventstore-integrity`
- First CI run: `43778888103`
- Tested commit: `0c96b4ad68dde9c33a5bdffd2175e4d63e905ef3`
- Result: **10 pytest tests passed in 0.23s**
- Protocol SHA-256 verification: **PASS** (`0d4546b158436d139dc39b80683ab03dc130b6410ccc45be671b079adc19a468`)
- Evaluation readiness check: **BLOCKED as expected** because the protocol is still `DRAFT_BLOCKED` and numeric thresholds are unset.

## Gate status

**Gate-A remains OPEN / NOT ACCEPTED.** The ten local SQLite integrity tests pass, but the Prediction lineage requirement is not represented in the current schema. Local CI also does not prove persistence on Render or connection to the running trading bot. No test result is being promoted into a claim of end-to-end memory integration.
