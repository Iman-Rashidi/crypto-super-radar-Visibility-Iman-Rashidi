# Four-repository source audit and adaptation plan

**Review date:** 2026-11-11  
**Target:** `feature/multi-stage-confirmation-engine`  
**Target project:** Crypto Super Radar (Python)  
**Policy:** adapt concepts and verified algorithms; do not transplant applications, secrets, execution paths, or unverified claims.

## Executive decision

The four repositories are useful, but they do not provide four production-ready modules that can be copied into Crypto Super Radar. Several useful capabilities are already present in the target branch. The remaining gaps are explicit and are tracked below.

| Reference | Useful contribution | Existing target implementation | Remaining gap / decision |
|---|---|---|---|
| [HyperData Terminal](https://github.com/Co-Messi/HyperData-Terminal) | Per-venue CVD/order flow, funding and OI normalization, liquidation evidence classes, freshness and coverage | `providers.py`, `market_intelligence.py`, `feed_health.py`, `walk_forward.py`, new `order_flow.py` | A transport-agnostic per-venue CVD/coverage contract and confirmed-vs-estimated liquidation classifier now exist with regression tests. They are not yet connected to live provider streams, the integrated decision engine or dashboard. No live CVD/liquidation coverage is claimed. |
| [million](https://github.com/Kelows/million) | Wallet ⇄ token discovery, wallet quality, owner/cluster inference, event-driven ingestion | `wallet_graph.py`, `whale_intelligence.py`, `solana_whales.py`, `whale_store.py`, Gem Hunter discovery modules | Existing Solana scan is deliberately partial. No claim of full token-lifetime wallet history. Adopt robust event ingestion and reconciliation only after durable/idempotent design. |
| [TradeClaw](https://github.com/tradeclaw-labs/tradeclaw) | Strategy simulation skeleton, trade safety limits, simulation-first workflow | `walk_forward.py`, `signal_performance.py`, `paper_trading.py`, `indicators.py` | Target already has stronger cost-aware OOS and paper-ledger metrics. TradeClaw's inspected backtest engine does not model fees or slippage; do not use its raw return calculations as a performance benchmark. |
| [Solbeam](https://github.com/CodeMuscle/solbeam) | Explainable weighted score, hard disqualifiers, holder and deployer risk concepts | `gem_hunter_security.py`, `evm_security.py`, `wallet_graph.py`, `solana_whales.py`, mandatory security gates | Reimplement only independently from documented concepts. The reviewed repository has no declared license, and its token route contains placeholder/default inputs; no source code may be copied. |

## 1. HyperData Terminal — market microstructure and data integrity

**Source reviewed:** `docs/DATA_INTEGRITY.md`, `data_layer/orderflow_engine.py`, `data_layer/liquidation_processing.py`, `data_layer/funding_rates.py`, `data_layer/health_monitor.py`.

### Patterns worth adopting

- Keep CVD split by venue; never present an unexplained aggregate when one venue is silent or partial.
- Deduplicate trades using venue-native identifiers (for example, Hyperliquid trade ID and Binance aggregate-trade ID).
- Use exchange event timestamps for event ordering and deduplication, not local receive time.
- Track per-venue states such as `connecting`, `ok`, `partial`, `silent`, `frozen`, `stale`, and `disconnected`.
- Attach window coverage to every CVD timeframe; do not treat a newly started 4-hour bucket as a 4-hour signal.
- Separate confirmed liquidations from estimates/large prints. Report the collection method and source limitations for every venue.
- Use a shared data-health report for freshness, completeness, cross-source drift and consistency.

### Target status

- **Already present:** cross-provider funding/OI retrieval and checks in `providers.py`; CoinGlass funding median, OI and large Hyperliquid position evidence in `market_intelligence.py`; market-board freshness/coverage in `feed_health.py`; historical funding-aware walk-forward validation in `walk_forward.py`.
- **Implemented as an isolated contract (not live functionality):** `order_flow.py` normalizes per-venue trade events, deduplicates venue-native IDs, uses exchange event time for window membership, tracks connection coverage/freshness, freezes on malformed payloads, excludes events from uncovered reconnect gaps, and suppresses usable CVD until coverage passes. Multi-venue directional agreement becomes UNKNOWN unless every configured venue is LIVE. The liquidation summarizer keeps confirmed liquidation events separate from estimated large-print proxies.
- **Still not verified/integrated:** websocket/provider adapters, live trade subscriptions, durable deduplication across process restarts, historical replay/backfill, event-stream persistence, integrated-engine gates, dashboard display, and real liquidation event feeds. Unit tests were added but GitHub Actions currently fails before its first step, so no CI PASS is claimed.
- **Do not infer:** CoinGlass OI/funding snapshots are not a substitute for trade-level CVD or a complete liquidation tape.

### Proposed acceptance tests

1. Duplicate venue trade IDs do not change cumulative CVD.
2. A dead venue cannot be hidden by another venue remaining live.
3. A timeframe below the configured coverage threshold returns `WARMING_UP`, not a directional signal.
4. A parser/schema failure transitions that venue to `FROZEN`.
5. Estimated large prints never enter confirmed liquidation totals.
6. Missing event timestamps, unknown contract multipliers or unsized contracts remain `UNKNOWN`/excluded with a reason.
7. Cross-venue disagreement and regional fallback retain source provenance.

## 2. million — smart-money discovery and event integrity

**Source reviewed previously:** `apps/api/prisma/schema.prisma`, analysis/ledger/metrics, live feed/controller/flows, wallet ingestion and crawler paths. Repository reviewed at `main`, HEAD `c89ad07caaf70773d40b45943e136eadc4f946a5`.

### Patterns worth adopting

- Bidirectional wallet-to-token and token-to-wallet discovery with bounded budgets.
- Wallet quality scoring based on realized history and behaviour, not one profitable trade.
- Detect related wallets using multiple evidence types; do not equate a common public exchange funder with common ownership.
- Record a decision log explaining why a wallet/token was included or rejected.
- Treat the live event stream and periodic analysis as concurrent writers that need idempotency and reconciliation.

### Previously identified source-level risks

- `ObservedTrade` is a performance summary, not a reconstructable execution ledger tied to entry/exit transaction signatures.
- Multi-wallet transaction attribution is incomplete in the reviewed webhook path: the first matching wallet is selected rather than emitting one event per subscribed wallet.
- Live position writes and periodic roster/position seeding can race.
- Webhook acceptance is not a durable acknowledgement; missing secret handling and background processing need fail-closed/durable semantics.
- Some live transaction paths select only one mint; fallback SOL pricing can create apparently numeric PnL without a verified contemporaneous price.

### Target status

- **Already present:** bounded wallet graph analytics, event deduplication within analysis, wallet-flow classification, GoPlus top-holder samples, and explicit partial coverage for Solana largest-account scans.
- **Not yet present:** a complete token-lifetime Solana swap index, durable webhook inbox, wallet+signature idempotency, chain-order reconciliation and a complete owner graph.
- Keep all wallet findings as on-chain address relationships, not verified human identity.

### Proposed acceptance tests

Use the ten wallet/live-event regression tests already recorded in the project tracking page: multi-wallet attribution, replay deduplication, webhook secret, durable acknowledgement, out-of-order events, concurrent seed/live writes, atomic observed ledger, invalid/stale price, multi-mint/router legs, and feed-gap detection.

## 3. TradeClaw — backtesting and execution discipline

**Source reviewed:** `trading-engine/src/backtesting/engine.ts`, `scripts/backtest.py`, `trading-engine/tests/strategies.test.ts`, `trading-engine/package.json`, `LICENSE`.

### Useful ideas

- Keep simulation as the default operating mode.
- Validate maximum trade size, slippage ceiling, daily trade count and safety state before any hypothetical execution.
- Report individual trades and explicit exit reasons, not only a headline return.

### Important limitations in the inspected backtester

- The inspected TypeScript and Python backtest paths calculate price return from CoinGecko historical prices without deducting trading fees or slippage.
- The TypeScript result counts buy and sell records in `totalTrades`, while win/loss statistics are derived from completed-return observations.
- An open position is marked to the final price but the close is not consistently represented as a normal exit record in the returned trade history.
- The Sharpe-like calculation is based on per-trade returns and is not a properly annualized periodic return series.

Therefore, **do not port the reported return, win rate or Sharpe values as validated performance evidence**.

### Target status

- **Already present:** chronological walk-forward folds, fee/slippage assumptions, mandatory historical funding for perpetual validation, cost-aware closed-trade expectancy/profit factor/drawdown, and paper-only ledger/risk controls.
- **Remaining improvement:** persist execution assumptions and actual modeled fill components (spread, fee, slippage, funding) per trade so net performance is independently reconstructable.

## 4. Solbeam — token risk and holder analysis

**Source reviewed:** `lib/signal-engine.ts`, `lib/disqualifiers.ts`, `app/api/tokens/[mint]/route.ts`, `lib/helius.ts`, `app/api/webhooks/helius/route.ts`, `lib/backtester-stats.ts`, initial Supabase schema and tests.

### Useful ideas

- Explainable score components instead of one opaque token score.
- Hard disqualifiers for authority risk, holder concentration, bundled launches, deployer history and suspicious launch behaviour.
- Preserve score history and compare forward outcomes by score cohort.
- Separate token risk from momentum and smart-money evidence.

### Source-level cautions

- The token API route supplies hardcoded defaults for several risk inputs (including sniper share, developer selling, deployer rug count, bundled launch, holder growth and clustering). These are not measured observations.
- Missing top-holder concentration is defaulted to zero in the disqualifier input, which can silently make missing evidence look safe.
- The smart-money count is based on transaction rows rather than a verified distinct-wallet count.
- The route calls the tier classifier with a constant multiplier (`1`), not a measured token outcome.
- The webhook parser selects the first non-SOL transfer and uses the fee payer as the wallet identity; that is not a general multi-leg, multi-wallet attribution model.
- The schema makes transaction signature globally unique, so one signature cannot naturally represent multiple tracked-wallet observations.
- Repository metadata did not declare a license at review time.

### Decision

No Solbeam source code is copied. Its publicly described ideas may be independently implemented, but missing evidence must remain `UNKNOWN`; no placeholder zero, fixed default or unmeasured score may authorize a trade.

## Implementation order for Crypto Super Radar

1. **Preserve the existing architecture.** Continue using Python provider adapters, typed models, hard gates, paper ledger and test suite. Do not import the Node/NestJS, Next.js or Supabase application stacks.
2. **Market flow extension.** The transport-agnostic CVD/evidence-class contract is now implemented and regression tests are added. Next: connect one public provider at a time, preserve provider-native timestamps/IDs, add live acceptance tests, and only then connect an explicit engine gate.
3. **Wallet event integrity.** Add durable, replay-safe wallet events and reconciliation before expanding smart-money discovery claims.
4. **Risk evidence completeness.** Ensure every holder/deployer/authority field carries value, source, timestamp, coverage and status; missing inputs block rather than improve a score.
5. **Backtest evidence.** Extend per-trade cost attribution and chronological OOS validation; reject fee-free headline metrics.
6. **Acceptance.** Run unit, integration and offline smoke tests; compare source evidence and test outputs. CI currently has an unresolved failure with unavailable job logs, so no new feature is release-ready until the independent test path is green.

## Scope and safety

- Research and paper trading only. No real orders, signing, private keys, seed phrases or production wallet access.
- No merge to `main`, no production migration, no secrets/config changes and no Render deploy as part of this reference audit.
- `UNKNOWN`, stale, partial or conflicting evidence never becomes zero, `PASS` or `BUY`.
- This document records a source audit and an evolving implementation plan. A standalone CVD/evidence-class contract is now in the Python codebase, but no live CVD stream, connected liquidation feed, durable wallet event pipeline or full-history wallet graph is claimed.


## Implementation progress — 2026-11-12

### TradeClaw-inspired paper-performance correction (F-34)

- Updated `crypto_super_radar/signal_performance.py`: fee components are now treated as unknown when absent or invalid, rather than substituted with zero.
- Complete fee totals and fee-to-gross-profit ratios are emitted only when every valid closed trade has both valid fee components. Partial known fee components remain separately visible as `known_recorded_fees_usd`; `total_recorded_fees_usd` remains null when coverage is incomplete.
- Added tests for missing fee, explicitly zero fees and invalid fee values.
- Local isolated logic checks passed for four fee-attribution scenarios. Full project test suite remains unverified because the GitHub Actions runner jobs terminate before their first step.

### HyperData-inspired per-venue order flow contract

- Added `crypto_super_radar/order_flow.py` and `tests/test_order_flow.py`.
- The module is transport-agnostic and accepts normalized base quantity, venue-native trade ID, source and exchange event timestamp. It does not fetch data or open network connections.
- CVD is kept per venue. Deduplication is per venue; event time determines window membership; receive time determines freshness; reconnect gaps are not filled with old observations; warm-up, stale, frozen, silent and disconnected states suppress usable CVD.
- Cross-venue alignment is UNKNOWN unless every configured venue is LIVE. No aggregate CVD is emitted.
- Liquidation records must be explicitly classified. `ESTIMATED_LARGE_PRINT` is kept separate and is never added to confirmed liquidation notional.
- The new tests cover duplicate trade IDs, out-of-order arrivals, warm-up, stale venues, malformed sides, future timestamps, missing base quantity, reconnect gaps and confirmed/estimated liquidation separation.

### Commits on the feature branch

- `fdf6e280ba0f74215c09e5c431c82032b923396b` — fail-closed fee completeness.
- `d64ff88d496e22b5115ff6bbde327e245ff01e2b` — fee regression tests.
- `80a8c16840254a89a38c63c359b78bf46658a4d9` — remove aggregation of unknown fee rows.
- `6ceb81b8e5f29391fb753c7ecfa97d600b9f078e` / `357017b2757827f8dd524a1878dd30a504beb783` — initial order-flow contract and tests.
- `e0ab2615d611f9b89621b4f10f56261038177db7` / `7dd449c464dc280a50ac7da7a2b02523b367e40b` — reconnect-gap correctness and regression tests. Current feature-branch HEAD at this documentation point: `7dd449c464dc280a50ac7da7a2b02523b367e40b`.

### Validation status and boundary

- GitHub Actions on HEAD `7dd449c464dc280a50ac7da7a2b02523b367e40b`: offline-tests #818 and runner-diagnostic #20 failed before any step; their jobs expose `steps=null` and `logs_url=null`. The live-provider workflow also failed before usable job evidence.
- Consequently, no automated unit/integration/Streamlit CI pass is claimed. The local fee logic checks are narrow, not a substitute for repository tests.
- No provider adapter, integrated engine, dashboard, database schema, secrets, Render configuration or deployed service was changed. No deploy, merge, live order, wallet connection or signing occurred.
- Next acceptance sequence: restore a functioning runner; run all offline tests and Streamlit smoke tests on the exact HEAD; then wire one provider at a time to the new order-flow contract with timestamp, source and coverage tests; only afterwards add an explicit engine gate and UI view.


## Follow-on review — 2026-11-12 (post initial implementation)

- Liquidation summarization now returns `ESTIMATED_ONLY` when the input contains only large-trade proxies and no confirmed liquidation records. Invalid-only input is `PARTIAL`, not `NO_DATA`; empty input alone is `NO_DATA`. Confirmed notional stays null when no confirmed liquidation record exists.
- Added regression tests for estimated-only, invalid-only and empty liquidation inputs.
- The `wallet_graph.py` implementation already applies several million-inspired safeguards: chain-aware address normalization, bounded candidate generation, event-level deduplication, temporal/amount evidence and no ownership inference from a public exchange funder alone. It remains bounded analysis, not a durable live event ledger.
- `gem_hunter_security.py` / `evm_security.py` already reflect Solbeam-inspired fail-closed security semantics: missing mandatory EVM evidence yields UNKNOWN; missing Solana authority evidence does not produce PASS; top-10 token-account concentration is explicitly not described as independent wallet ownership. No source code was copied from Solbeam.
- Current feature-branch HEAD: `e83365b6ebfa6af57f7023b4fdf547181dd68e18`.
- Latest checks on that HEAD: [offline-tests #820](https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi/actions/runs/43153821049) and [runner-diagnostic #25](https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi/actions/runs/43153821039) failed before the first step; their jobs expose `steps=null` and `logs_url=null`. No full test pass is claimed.


## UI integration and latest runner observation — 2026-11-12

- The Paper Performance dashboard now renders `fee_data_status`, `fee_coverage_pct`, `fee_incomplete_rows`, and a separately labeled partial sum when fee coverage is incomplete. The full total remains null when any valid trade row lacks a valid fee component.
- `tests/test_web_app.py` now asserts that the homepage includes the fee-coverage disclosure and that the empty paper ledger reports `fee_data_status=NO_DATA`.
- `summarize_liquidation_events` now rejects non-mapping rows safely; a malformed row is counted as invalid and produces `PARTIAL`, not a crash or `NO_DATA`.
- Follow-up commits: `9337d26b9bfc0baa48aa534d455ce4260b32dcda` (UI fee coverage), `7217e62f928b5f678fbc60ea9a26d5ef6cb2f8e1` (UI regression checks), `84a9f21b02c30e6ae70e6b2067238a2a63bf0b37` (malformed liquidation handling), and `5ace4d470c439d42f65735252ab11eda7cb35d7f` (regression test).
- Current Feature HEAD: `5ace4d470c439d42f65735252ab11eda7cb35d7f`. PR #1 remains Open/Draft against `main` and mergeable; PR #2 remains Open/Draft and not mergeable.
- Latest GitHub Actions runs associated with the current head: [offline-tests #825](https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi/actions/runs/43154603929), [runner-diagnostic #34](https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi/actions/runs/43154572330), [live-provider-acceptance #282](https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi/actions/runs/43154572355), and [offline-tests #824](https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi/actions/runs/43154572256). All jobs returned `steps=null` and `logs_url=null`; no project test step is evidenced. Do not claim these runs tested the new source.
- Render's attached service still has Auto Deploy off and remains live on `97df9f7854a5df3f99213f019df95cd97ec77a7d`. No new deploy occurred; current Feature commits are not live.


## Live trade-stream adapter groundwork — 2026-11-12

- Added `crypto_super_radar/order_flow_adapters.py` for raw Binance Spot `@aggTrade` and Bybit V5 Spot `publicTrade.{symbol}` WebSocket messages.
- Binance `m=true` is normalized as SELL aggressor (buyer is maker); `T` remains the exchange trade timestamp, `a` the venue-native aggregate trade ID, and `q` the base-asset quantity.
- Bybit `S` is the taker side; `T` is the exchange trade timestamp, `i` is the venue-native trade ID, and `v` is the spot trade size in base units. Messages are validated against the configured topic and symbol.
- Malformed adapter payloads explicitly freeze the affected venue until reconnect. Valid adapter messages do not automatically connect a venue, mark an unobserved heartbeat, or bypass the existing warm-up/freshness checks.
- Added `tests/test_order_flow_adapters.py` for maker/taker direction, timestamp provenance, symbol/topic validation, native-ID deduplication, malformed-message freeze, and disconnected behavior.
- **Boundary:** these are message normalizers/ingestion bridges only. No WebSocket client, reconnect supervisor, event persistence, UI, or 12-gate integration is added in this step. A continuous consumer must own connection lifecycle and feed messages before CVD can become LIVE.
- Provider contracts reviewed against the official [Binance Spot aggregate-trade stream](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md) and [Bybit V5 public trade stream](https://bybit-exchange.github.io/docs/v5/websocket/public/trade).

- Additional safety boundary: both provider adapters now require an explicit `channel_type="spot"` from the owning connection configuration. This prevents reusing a Bybit/ Binance Spot quantity normalizer against Linear/Perpetual contract-size messages.
