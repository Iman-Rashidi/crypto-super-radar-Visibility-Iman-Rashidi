# Demo Dual-Engine and Learning Memory — implementation status

Updated: 2026-11-15

## Scope deployed to the dedicated staging branch

This milestone adds a durable-memory API layer, policy definitions for the two future Demo strategy profiles, and a read-only Binance Demo account preflight module.

### Implemented in code

- `learning_memory.py`: SQLite/Turso-backed episode records, engine attribution, timestamped snapshot hashes, one-time outcome attachment, and lessons that start as PENDING and require explicit validation.
- Existing Spot Paper and Futures Paper cycle workers record a decision-summary episode. These records are explicitly labelled as summaries, not raw OHLCV snapshots.
- `engine_profiles.py`: Aggressive (1.0% strategy risk) and Strict (0.5%) policy profiles with a shared 2.0% portfolio risk cap. The Aggressive profile skips only the 12-gate strategy screen; all universal guards remain mandatory. This module returns eligibility only and never authorizes orders.
- `binance_demo.py`: signed GET-only account preflight using separate Spot Demo and USD-M Futures Demo host allowlists and separate credentials. It does not implement order submission, cancellation, OCO/bracket protection, or any live endpoint.
- Dashboard read-only panels show memory persistence status and Demo readiness. No public endpoint exposes individual learning episodes or credentials.
- CI workflow now runs on pushes to `feature/unlimited-human-demo-cycles`.

### Important boundaries / not implemented yet

- The two profiles are policy definitions, not complete independent autonomous engines.
- Existing internal Paper bots still have a single open position each and require their current 12-gate process. Multi-position Binance Demo execution and the unlimited-lifetime Demo cycle/order policy are not yet implemented.
- The Binance adapter is read-only. No Demo order, cancel request, transfer, or live order was sent.
- Protective orders confirmed by Binance for actual filled quantities, partial-fill reconciliation, aggregate portfolio risk, STOP BOT runtime, and safe restart recovery are not implemented. Order submission must remain locked until these are tested.
- BOT_ONLY and BOT_WITH_AI_MENTOR pairing is enforced by the memory API, but the actual model-training and supervised comparison workflow is not yet wired into the decision engines.
- The Paper worker currently stores a decision summary, not a full time-aligned OHLCV/feature snapshot. Training-grade snapshots and outcome labelling by trade lifecycle remain follow-up work.
- On Render Free, local SQLite is ephemeral. For persistence across restarts/redeploys, both `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` must be configured as Render secrets. This build does not infer or fabricate these values.
- Binance Demo credentials are not read from the existing Render service, copied, or added by this change. When configured, use separate Demo-only USER_DATA/read-only keys; never put credentials in GitHub, Notion, or chat.

## Acceptance sequence

1. CI passes on this branch.
2. Deploy only to the separate `crypto-super-radar-unlimited-demo-staging` Render service.
3. Confirm `/health`, `/learning-memory/status`, and `/demo-readiness`.
4. Configure Turso secrets and restart-test memory persistence.
5. Configure separate read-only Binance Demo credentials and validate Spot/Futures account preflight.
6. Implement and test multi-position strategy ledgers, Demo order lifecycle, exchange-side protective exits, idempotency, shared portfolio risk, STOP BOT, and reconciliation before any Demo order submission is enabled.
7. Wire BOT_ONLY / BOT_WITH_AI_MENTOR, immutable market snapshots, outcome labelling, leakage-safe ML training, and walk-forward promotion.

No production service, BloFin service, or real-money execution is part of this milestone.
