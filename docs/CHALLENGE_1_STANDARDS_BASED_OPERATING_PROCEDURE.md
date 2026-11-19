# Challenge 1 — Standards-Based Supervisor Operating Procedure
**Version:** 0.1-draft  
**Date:** 2026-11-19  
**Status:** Design proposal only. This document does not change runtime behavior, deploy services, or authorize orders.

## 1. Purpose and scope

This procedure establishes a traceable engineering baseline for CRYPTO SUPER RADAR Challenge 1 on BloFin Demo. It separates three different timing responsibilities that must not be represented by one scheduler:

1. A fast local control loop that evaluates the newest already-available state.
2. Exchange data transport and periodic reconciliation.
3. External LLM supervisor requests and their audit records.

This is a Demo-only procedure. Production execution, withdrawals, transfers, and production credentials remain out of scope.

## 2. Source-of-truth and authority

The following authority order applies:

1. **Exchange facts:** BloFin Demo responses and authenticated stream events are the source of truth for order, fill, position, and protective-order state. A local prediction is not proof of exchange acceptance.
2. **Durable evidence:** append-only Challenge 1 audit events and the durable event store preserve what was known, when it was known, and which decision used it. Notion is a projection/reporting surface, not the execution source of truth.
3. **Deterministic risk/execution policy:** code-controlled checks own position limits, order permissions, idempotency, protection reconciliation, and exit authorization.
4. **LLM supervisors:** models provide bounded, schema-validated analysis of sanitized evidence. A post-decision observer has no order, position, TP/SL, or exit authority. A new-entry approval gate, if retained, must be specified separately and must never override deterministic hard gates.

### Policy conflict requiring resolution (P0)

The current repository contains conflicting descriptions:
- `docs/CHALLENGE_1_EXECUTION_CONTRACT.md` describes two AI reviews as a pre-entry quorum.
- `run_challenge1_observer_reviews()` in `crypto_super_radar/ai_second_opinion.py` describes post-decision, non-operative reviews.
- `docs/audit/2026-11-19-blofin-position-supervisor-audit.md` states that the open-position reviewers are report-only and identifies the conflicting policy language.

These are different functions and must not be conflated. The release owner must approve one explicit entry-gate policy and one explicit open-position observer policy. Until the documents, call paths, tests, and UI agree, the system must not claim that a position was AI-approved merely because a reviewer was configured or a request was queued.

## 3. Timing policy

**A three-second interval is a project requirement to be validated, not a universal trading-bot standard.** It is appropriate for a lightweight local watchdog operating on fresh stream snapshots. It is not a safe default for repeatedly calling two hosted LLM APIs.

| Layer | Proposed/known cadence | Work allowed | Work explicitly excluded |
|---|---:|---|---|
| Local watchdog / deterministic state evaluator | Target: every 3 seconds | Evaluate cached latest mark/position/order/protection state, freshness, hard limits, and state transitions; emit a heartbeat | No LLM request and no REST polling on every tick |
| BloFin market/account transport | Event-driven WebSocket | Consume authenticated position, order, and algo-order updates plus relevant public market data; track sequence/time/freshness and reconnect gaps | Do not infer a missing event as a healthy state |
| REST reconciliation | Keep the currently implemented 15-second manager cadence until stream handling is implemented and measured; then tune from endpoint budget and reconciliation tests | Reconcile positions and protective orders against exact IDs; repair stream gaps | Do not multiply REST reads merely to simulate a 3-second LLM review |
| AI supervisor review | Event-driven, evidence-versioned, bounded by provider quota | Review on a new material evidence version: e.g. a new closed 5m candle, material risk/protection change, entry decision (only if the separately approved policy requires it), or post-exit audit | No unconditional dual-model request every 3 seconds |
| Durable audit | Every state transition and actual request/result; aggregate repetitive heartbeats for presentation | Record timestamps, evidence hash, reason code, request attempt, provider/model, latency, validation, verdict, and execution effect | Never record NOT_CALLED as REVIEWED or infer a response |

### Why external LLM calls cannot be the three-second heartbeat

The current code sets a three-hour default in `challenge1_position_observers.py` and clamps `BLOFIN_DEMO_POSITION_AI_REVIEW_INTERVAL_SECONDS` to a minimum of 3,600 seconds (one hour). The current position manager itself sleeps for 15 seconds. Therefore, setting the environment variable to 3 does not produce three-second calls.

The position-observer adapter itself sets `_OBSERVER_DAILY_CAP = 35` and `_OBSERVER_MINUTE_CAP = 18` per reviewer ID. Thus an attempted call every three seconds (20/minute per reviewer) already exceeds the application's own per-reviewer minute cap. The 35/day per-reviewer app cap would be consumed in about 105 seconds at that rate. For two reviewers, the app-level maximum is 70 calls/day across the pair, assuming both remain configured and reachable.

The currently documented OpenRouter free `:free` limit is 20 requests/minute and 50 requests/day. One request every three seconds to a single free model would consume 20/minute and 28,800/day, 576 times the published daily allowance. Two reviewer calls each three seconds would mean 40 total provider calls/minute across the pair. The current observer adapter can wait up to 40 seconds for the two results, so a three-second schedule would also create overlapping/in-flight work unless explicitly prevented. A failed request can count against the free daily cap.

The second provider has its own quota and availability policy. Its limit must be obtained from the provider's current official account/model documentation; it must not be guessed or copied from OpenRouter. The observer's 35/day and 18/minute application counters are held in process memory (`_OBSERVER_REQUESTS`) and reset on process restart; they are not a durable cross-instance quota ledger. Upstream provider limits remain separate, but the application-level counters need durable shared storage before multi-instance or restart-safe quota enforcement can be claimed.

## 4. Proposed event-driven supervisor procedure

For every tracked Demo position:

1. Load the exact exchange `positionId`, instrument, side, size, average fill, linked entry order, and durable experience ID. Missing/ambiguous linkage means `UNKNOWN`; no supervisor verdict may be attached to another position.
2. Build a sanitized snapshot from confirmed market data and exact linked exchange state. Include UTC event time, source, freshness, closed-candle timestamp, mark price, unrealized PnL, holding duration, TP/SL state and IDs, and deterministic HOLD/EXIT_ELIGIBLE state.
3. Compute a stable evidence hash and an event version. If evidence has not materially changed, do not call a model again solely because a timer fired.
4. Enforce one in-flight review batch per position and event version. Invoke the two providers independently and in parallel, with separate provider identifiers, model identifiers, keys, timeouts, and rate-limit accounting.
5. Persist a request event before dispatch. Persist each reviewer result separately. Record `NOT_CALLED`, `PENDING`, `RATE_LIMITED`, `TIMEOUT`, `INVALID_RESPONSE`, `REVIEWED`, and `UNKNOWN` explicitly with a stable reason code.
6. Apply bounded retry with exponential backoff and jitter for retryable failures, honoring `Retry-After`. Do not retry immediately on every three-second watchdog tick. An ambiguous request outcome must be reconciled before resending.
7. Compare reviewer responses only after schema validation and model/provider identity checks. Agreement is not proof of correctness; disagreement remains visible and is never silently averaged away.
8. Keep the observer's execution effect equal to `NONE`. The deterministic position manager and exchange-side protective orders remain responsible for risk actions.
9. On a new closed candle or a material state change, create a new evidence version and schedule a fresh review if the per-provider budget permits. A stale snapshot is `UNKNOWN`, not permission to reuse an old approval.
10. After exit, reconcile actual exchange fill history and realized PnL, then run the post-mortem audit against the original entry-time snapshot. Do not rewrite the original decision using future information.

## 5. Three-second watchdog contract

The target watchdog should meet all of the following:

- Tick period: 3 seconds, measured with a monotonic clock. Record scheduled time, actual start, completion, and drift.
- Input: the newest validated in-memory state snapshot, not a newly fetched REST response on every tick.
- Freshness: each input carries source timestamp and receive timestamp. Stale, missing, discontinuous, or unlinked data yields `UNKNOWN`.
- Idempotency: each state transition has a stable event key; duplicate ticks must not create duplicate actions.
- Non-blocking: AI calls, Notion sync, and historical reconciliation cannot block the local watchdog.
- Fail-safe: missing data or watchdog lag disables new entries. Existing exchange-side TP/SL remains active; the watchdog must not fabricate a successful protective order.
- Separation: the watchdog can emit an alert or make a deterministic state transition, but the post-decision LLM observer cannot submit, cancel, resize, or close an order.
- Observability: expose last heartbeat, last validated market event, snapshot age, loop duration, missed ticks, WebSocket state, reconciliation age, AI request age, and each reviewer's last status.

**Implementation prerequisite:** the Staging branch currently exposes BloFin WebSocket URL/login argument helpers, but the inspected position-manager path uses REST reads and a 15-second sleep. The inspected source tree does not contain an implemented BloFin WebSocket position/order consumer. Therefore the three-second local watchdog is a target design, not a currently verified runtime capability. Implement and test the stream consumer before shortening REST polling.

## 6. Rate, latency, and resilience controls

- Maintain independent budgets per provider, model, account, and service instance where the upstream quota is defined that way.
- Reserve budget atomically before sending a request.
- Honour provider rate-limit responses and `Retry-After`; use exponential backoff with jitter. Do not retry non-retryable authentication/schema failures automatically.
- Use a circuit breaker for repeated provider failures. A circuit-open state is a visible `NOT_CALLED`/blocked state, not an invented model answer.
- Use one in-flight batch per position/evidence version and a durable lease/recovery record so a process restart cannot trigger duplicate calls.
- Maintain a global REST request budget below the exchange's published limits. Account for every endpoint called per managed position, not just the top-level loop frequency.
- Reconcile WebSocket state with REST state after connection loss, sequence gaps, stale heartbeat, or process restart.
- Keep API secrets server-side and never write them, full prompts, private account data, or raw secret-bearing provider bodies to audit logs.

## 7. Required evidence for acceptance

Challenge 1 cannot pass based solely on a green offline CI run. The following evidence is required:

1. **Policy traceability:** one approved source of truth for pre-entry AI gating versus post-decision observation; matching code, tests, UI labels, and runbook.
2. **Three-second watchdog:** test evidence showing 3-second heartbeat, bounded drift, no overlapping ticks, no network call per tick, and correct stale-data behavior.
3. **Stream correctness:** authenticated BloFin Demo subscription, initial snapshot, position/order/algo-order changes, reconnect, ping/pong, gap detection, and REST reconciliation against exact IDs.
4. **Provider evidence:** for each reviewer, actual request attempt, provider, model, request/response UTC timestamps, latency, HTTP state, model verification, schema validation, verdict, reason, and evidence hash.
5. **Quota resilience:** simulated 429, 401, timeout, malformed JSON, slow responses, unavailable provider, daily cap, and process restart. No tight retry loop.
6. **Execution isolation:** tests proving report-only observers cannot modify position, order, leverage, margin, TP/SL, or exit decision.
7. **Position reconciliation:** actual BloFin Demo position and TP/SL order IDs, fill history, exit fill, fee, and realized PnL linked to the same experience.
8. **Durability:** write → restart/deploy → read-back with unchanged event IDs and hashes; Notion outbox retry and projection verified separately.
9. **Operational acceptance:** sustained Demo observation with measured latency, API request rate, quota consumption, missed ticks, stale-data rate, supervisor coverage, and error/recovery rate.
10. **Release safety:** exact commit, test artifacts, deployment ID, environment confirmation, rollback plan, and explicit Demo-only permission proof.

## 8. Ordered implementation roadmap

- **P0 — Resolve authority and policy:** reconcile README, execution contract, audit report, entry supervisor, and open-position observer. Mark each AI function as ENTRY_GATE or POST_DECISION_AUDIT.
- **P1 — Architecture and requirements baseline:** adopt this procedure; add a system context diagram, sequence diagram, state machine, requirements-to-test traceability matrix, provider/API quota registry, and risk register.
- **P2 — Transport:** implement BloFin Demo WebSocket position, order, algo-order, and market-data consumers with authentication, ping/pong, reconnect, gap detection, and durable event ingestion.
- **P3 — Three-second local watchdog:** evaluate fresh cached snapshots using deterministic rules and a monotonic scheduler; measure latency and drift. Keep REST reconciliation separate.
- **P4 — Supervisor orchestration:** implement evidence-version triggers, independent quotas, single-flight, bounded backoff, response validation, explicit failure states, and immutable reviewer reports.
- **P5 — Verification:** add unit, integration, fault-injection, replay, concurrency, recovery, and Demo contract tests. Verify no network access in offline CI.
- **P6 — Demo acceptance:** deploy only to the designated Staging/Demo service after review; validate real provider calls and exchange outcomes, then run sustained observation. Keep Challenge 1 open until all evidence passes.

## 9. Standards and research references

These references provide process, architecture, quality, security, and AI-risk guidance. They do not prescribe a universal three-second LLM polling interval, and this project must not claim certification or conformance solely by citing them.

| Reference | Use in this project | Applicability note |
|---|---|---|
| ISO/IEC/IEEE 12207:2026 — Software life-cycle processes | Lifecycle governance from conception through development, operation, maintenance, and retirement | Framework; does not mandate one delivery methodology or scheduler interval |
| ISO/IEC/IEEE 42010:2022 — Architecture description | System context, stakeholders, viewpoints, architectural views, and rationale | Defines requirements for architecture descriptions, not the process used to create them |
| ISO/IEC/IEEE 42020:2019 — Architecture processes | Architecture governance and architecting process | Useful process companion to 42010 |
| ISO/IEC/IEEE 15289:2019 — Life-cycle information items | Structure and content of plans, procedures, reports, records, specifications, and descriptions | Documentation reference; check edition/compatibility when mapping to 12207:2026 |
| ISO/IEC 23894:2023 — AI risk management | Identify, analyse, treat, monitor, and communicate AI-related risks across the lifecycle | Guidance, customizable to context |
| NIST AI RMF 1.0 and NIST AI 600-1 GenAI Profile | GOVERN, MAP, MEASURE, MANAGE; identify generative-AI-specific failure modes and controls | Voluntary frameworks; NIST states AI RMF 1.0 is being revised |
| NIST SP 800-218 SSDF v1.1 | Secure software development practices integrated into the SDLC | Security development framework, not a trading strategy standard |
| ISO/IEC 25010:2023 — Product quality model | Define and measure reliability, security, performance efficiency, maintainability, and other product-quality characteristics | Quality model, not a guarantee of trading outcomes |
| EU Delegated Regulation 2017/589 (RTS 6), Articles 12–17; ESMA 2026 Algorithmic Trading Supervisory Briefing | Useful benchmark for kill functionality, surveillance, monitoring, pre-trade controls, testing, governance, and outsourcing | Legal scope depends on MiFID II applicability and the type of entity/activity; do not assume it directly governs this private Demo project |

### Academic research (not a standard)

- Xiao et al., **TradingAgents: Multi-Agents LLM Financial Trading Framework** (arXiv:2412.20138, 2024). Useful as a research example of role-specialized agents and bull/bear/risk-team coordination; it is not a certified implementation or proof that this project's agents are profitable.
- Nguyen & Pham, **Toward Reliable Evaluation of LLM-Based Financial Multi-Agent Systems** (arXiv:2603.27539, 2026). Useful as a current research warning about look-ahead bias, survivorship bias, backtest overfitting, transaction-cost neglect, and regime-shift blindness; the paper itself describes its coordination hypothesis as requiring further validation.

### Official source links

- ISO/IEC/IEEE 12207:2026: https://www.iso.org/standard/90219.html
- ISO/IEC/IEEE 42010:2022: https://www.iso.org/standard/74393.html
- ISO/IEC/IEEE 42020:2019: https://www.iso.org/standard/68982.html
- ISO/IEC/IEEE 15289:2019: https://www.iso.org/standard/74909.html
- ISO/IEC 23894:2023: https://www.iso.org/standard/77304.html
- ISO/IEC 25010:2023: https://www.iso.org/standard/78176.html
- NIST AI RMF: https://www.nist.gov/itl/ai-risk-management-framework
- NIST SSDF SP 800-218: https://csrc.nist.gov/pubs/sp/800/218/final
- OpenRouter free-model limits: https://openrouter.ai/collections/free-models
- BloFin API and WebSocket documentation: https://docs.blofin.com/index.html
- ESMA 2026 briefing: https://www.esma.europa.eu/press-news/esma-news/esma-issues-supervisory-briefing-algorithmic-trading
- EU RTS 6: https://eur-lex.europa.eu/eli/reg_del/2017/589/oj/eng
- TradingAgents: https://arxiv.org/abs/2412.20138
- LLM financial multi-agent evaluation survey: https://arxiv.org/abs/2603.27539
