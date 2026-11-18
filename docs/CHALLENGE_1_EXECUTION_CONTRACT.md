# Challenge 1 · 5m Elliott-aware Demo execution contract

This records the Challenge 1 implementation contract. It does not replace the
general twelve-gate CRYPTO SUPER RADAR architecture or change other profiles.

## Evidence and setup

1. The strategy reads only confirmed, closed BloFin Demo 5m OHLCV candles.
2. Candle-pattern detection evaluates the completed candle sequence. Chart
   structure uses confirmed, non-repainting pivots.
3. Elliott impulse-wave monitoring runs on the same 5m close series. Challenge 1
   supplies an intraday reversal floor of 0.15% and a 3% cap. The generic Radar
   monitor retains its existing slower-candle defaults.
4. A candle/chart trigger is necessary but not sufficient. A rule-valid
   directional Elliott prefix and invalidation price must be confirmed and
   agree with the trigger direction. An unknown, ambiguous, unconfirmed, or
   opposing wave count produces NO_TRADE. A wave count alone never creates
   an entry.
5. Evidence preserves pivot timestamps and confirmation timestamps, wave
   phase, direction, reversal threshold, and invalidation level. No 15m/1h feed
   is requested or required. The historical v4 signal id remains in place to
   protect existing idempotency reservations.

## Independent supervision and execution

1. Two independently configured AI supervisors receive the same sanitized
   closed-5m candle, chart-pattern, chart-structure, Elliott-wave, and
   protective-price evidence. They do not receive each other's response.
2. Each supervisor reviews the selected pattern, wave phase/direction, 5m
   chronology, trade proposal, risk plan, and portfolio constraints. Returned
   phase and direction must exactly match the deterministic evidence.
3. A missing, invalid, unverified, non-independent, opposing, or unavailable
   review fails closed. Both reviewers must return a verified APPROVED result
   for the same side and exact Elliott phase/direction.
4. Only after this quorum may the BloFin Demo Executor apply exchange,
   position, margin, size, idempotency, TP/SL, and account-risk guards.
   Supervisors have no exchange tools and cannot submit, cancel, or modify
   orders.
5. A blocked quorum is recorded as not executed. Successful execution preserves
   both complete reviews and consensus in the job result/event journal. The
   independent Position Manager monitors linked Demo positions and protective
   orders.

## Scope and test requirements

- This is the Challenge 1 BloFin Demo path only. The general twelve-gate
  architecture, other execution profiles, and production services are unchanged.
- CI repeats Elliott, chart-pattern, Challenge 1 strategy, AI-review, and
  web-execution regression files three times, then runs the full suite and
  Render-overlay/HTTP and Streamlit smoke checks.
- CI success proves code/test behavior only. Challenge 1 acceptance additionally
  requires a verified Staging deployment, durable unattended cycles, actual
  BloFin Demo order/fill reconciliation, AI request/response evidence, durable
  learning/event records, Notion synchronization, and Position Manager outcomes.
  No deployment or live order is authorized by this document.


## Staging runtime validation record

Expected deployed Staging commit: `3da0a9c1472e3857b09eb235d8caba0a21c96be8`.

A guarded, one-shot CI check reads the Challenge 1 reviewer reports and Demo readiness. Its synthetic AI dry-run is not permitted to submit orders.
