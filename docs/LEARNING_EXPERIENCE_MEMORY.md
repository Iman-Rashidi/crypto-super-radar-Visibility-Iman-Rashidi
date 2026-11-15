# Learning Experience Memory — v1

## Purpose

This module is the learning record for CRYPTO SUPER RADAR. It is separate in
purpose from the existing order execution event ledger:

- The order ledger records order lifecycle, fills and execution states.
- This memory records the bot's original decision, the data observed at that
  moment, the final labelled outcome, AI Mentor feedback and candidate lessons
  that are independently tested.

The module is not an ML trainer, a model registry or an exchange adapter. It
does not send, amend or cancel orders.

## Implementation

Module: crypto_super_radar/learning_experience_memory.py  
Class: LearningExperienceMemory  
Table: learning_experience_events

The module reuses the application's existing DB-API connection. It must not
create a second database or a separate local file. Instantiate it with the
connection returned by crypto_super_radar.database.connect_database().

The caller owns transaction boundaries. Each logical write must be wrapped in
the caller's database transaction so a related application write and memory
event can commit or roll back together.

## Stored learning cycle

1. DECISION_RECORDED: immutable decision, engine ID, challenge ID, cycle ID,
   BOT_ONLY or BOT_WITH_AI_MENTOR mode, environment, market, symbol, timeframe,
   model/dataset version and input snapshot.
2. OUTCOME_LABELLED: only a final outcome supported by evidence. Open PnL and
   incomplete outcomes cannot be used as training labels.
3. MENTOR_FEEDBACK_RECORDED: only after the BOT_ONLY outcome is locked.
4. LESSON_PROPOSED: candidate learning, linked to earlier evidence event IDs.
5. LESSON_VALIDATION_RECORDED: independent dataset, sample count, metrics and
   pass/fail evidence. A candidate lesson is not trusted merely because it was
   proposed.

The mentor-assisted decision is a separate experience and must reference the
final-labelled BOT_ONLY baseline from the same engine, challenge and cycle, with
mentor feedback already recorded.

## Four decision/outcome cases

The outcome record distinguishes:

- CORRECT_PROFIT
- CORRECT_LOSS
- INCORRECT_PROFIT_LUCK
- INCORRECT_LOSS

It also distinguishes NOT_EXECUTED and outcomes whose decision correctness is
UNKNOWN. A lucky profitable result from an incorrect decision is not recorded
as a successful decision.

## Integrity and idempotency

- Each event has a stable event ID and a per-experience sequence.
- Duplicate identical event IDs return the original event.
- Reuse of an event ID with changed content is rejected.
- Payloads are canonical JSON and SHA-256 hashed.
- Event hashes include the preceding event hash.
- A unique cycle index prevents duplicate BOT_ONLY or mentor-assisted root
  decisions for the same engine/challenge/cycle/mode.
- SQLite triggers reject UPDATE and DELETE of event rows.
- Verification checks sequence, metadata, timestamps, payload hashes, lifecycle,
  previous hashes and event hashes.
- Credential-like fields are rejected before persistence.

The hash chain detects accidental or partial tampering. It is not a trusted
cryptographic anchor against an administrator who can rewrite the entire
database and recompute the chain. Stronger assurance requires protected backups
and externally signed or anchored checkpoints.

## Durability boundary

Durability depends on the configured storage backend:

- LOCAL_SQLITE is durable only while the configured database path and its
  underlying disk persist. A local file on an ephemeral container is not
  durable across container replacement.
- TURSO_CLOUD_SQLITE uses the application's configured cloud database when
  both required credentials are present.

This module does not inspect, print or change credentials, Render variables,
service settings, deployment settings or database configuration. Verify the
actual configured backend before calling the deployed memory permanent.

## Current scope boundary

This is an isolated memory module and test suite. It is not yet wired into the
active Challenge 1 BOT_ONLY decision path, the paper bot, the BloFin Demo
executor or the existing order ledger. No production behavior changes until a
separate integration is reviewed and accepted.

Engine identities must remain separate. The 12-gate engine and EASY engine must
not share experience IDs, model versions or learning outcomes. Spot and Futures
datasets/environments must also remain distinguishable.

## Tests

Run the focused suite:

    python -m unittest tests.test_learning_experience_memory -v

Run the repository suite:

    python -m unittest discover -s tests -v

Coverage includes database reconnect/recovery, idempotency, event ID conflicts,
the four decision/outcome cases, final-label enforcement, BOT_ONLY and mentor
ordering, separate mentor-assisted experience, lesson proposals and
independent validation, secret rejection, timestamp ordering, append-only
triggers, tamper detection, transaction rollback and replay from a second
database connection.
