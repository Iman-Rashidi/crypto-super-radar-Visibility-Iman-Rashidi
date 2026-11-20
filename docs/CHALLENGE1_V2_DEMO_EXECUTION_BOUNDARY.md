# Challenge 1 V2 to BloFin Demo execution boundary

Status: **BLOCKED / NOT INTEGRATED**

## Current contract mismatch

- `crypto_super_radar/challenge1_v2.py::analyze_signal` is an analysis-only engine. It uses a 1H regime, 4H alignment, and a completed 15M entry candle. Its signal record includes `signal`, `signal_id`, `instrument_id`, `entry`, `stop_loss`, `take_profit`, `atr_15m`, confluence fields, and `orders_submitted=false`.
- `crypto_super_radar/blofin_demo_executor.py::build_demo_order_plan` currently requires `execution_profile=CHALLENGE_1` and validates the separate `challenge1_strategy.py::challenge1_entry_evidence_ready` contract. That gate requires 5M-only candle/pattern/Elliott evidence, explicitly requires `higher_timeframes_used=false`, and binds the signal ID to the 5M confirmation candle.
- The Demo executor additionally requires the exact two-reviewer live approval quorum, durable Turso storage, verified Demo Trade permission, account-wide exposure checks, fresh BloFin Demo quotes, risk/margin reservation, and an explicit per-call Demo opt-in.

The V2 multi-timeframe result cannot be copied into the existing 5M-only contract. Doing so would misrepresent the evidence and weaken the safety boundary.

## Current protection

- No automatic adapter maps V2 output to the legacy executor.
- No change was made to the two-reviewer quorum or the 5M evidence validator.
- A regression test explicitly verifies that a V2-shaped analysis record is rejected by the current Demo order-plan builder.
- `analyze_signal` remains analysis-only; no order is submitted by this validation.

## Required design resolution before any Demo adapter

1. Select one canonical Challenge 1 decision contract for Demo execution, or explicitly version and support both contracts.
2. Define a V2 evidence validator that checks its actual 1H/4H/15M closed-candle inputs, timestamps, instrument identity, and derived SL/TP without fabricating the 5M-only evidence fields.
3. Bind both independent reviewer approvals to the exact V2 `signal_id`, direction, timeframe set, and evidence hash. Observers remain post-decision auditors, not execution authorities.
4. Preserve all existing Demo host, credentials, Turso, risk, aggregate margin, concurrency, reconciliation, and cancellation gates.
5. Add full unit/regression tests before runtime wiring. Start with analysis-only adapter tests; do not place test orders.
6. Separately add read-back verification for attached TP/SL and position/order identity. A successful order receipt alone does not prove that protection is active.

Until those requirements are implemented and independently audited, the V2-to-Demo adapter remains **BLOCKED** and no V2 signal may enter the existing Demo executor.
