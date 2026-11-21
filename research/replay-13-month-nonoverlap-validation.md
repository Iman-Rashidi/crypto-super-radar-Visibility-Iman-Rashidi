# Replay historical range extension — 2026-11-21

## Purpose
Research-only attempt to obtain a genuinely non-overlapping six-month historical window ending before Replay #109 starts (2026-04-21 16:00 UTC).

## Change
- `crypto_super_radar/market_replay.py`: public candle and funding history request bounds extended from 12 to 13 calendar months.
- `.github/workflows/challenge1-v2-historical-replay.yml`: historical replay download request changed to 13 months.
- No strategy defaults changed; no order writes; no Render deploy; no secrets or environment edits.

## Validation status
- Code change commit: `3abb98d4f005b753b402a01a453c8d6453ab3ac0`.
- A GitHub Actions historical replay run for this 13-month request has not yet been observed.
- Independent data availability, exact date range, SQLite hash, and Funding quality remain UNVERIFIED until the workflow artifact is inspected.
- The request must fail closed if BloFin public history does not cover the full requested span.

## Safety
- Replay only, public market data only.
- No Demo or Live order writes.
- `feature/blofin-demo-adapter`, Production, Render, API credentials, and strategy defaults are untouched.
- Challenge 1 remains NOT PASSED.
