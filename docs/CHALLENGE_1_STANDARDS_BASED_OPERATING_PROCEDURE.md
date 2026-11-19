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
4. **LLM supervisors (dual responsibility):** both supervisors are active decision participants and mandatory audit/report producers. They must report their observations and structured decisions before entry, during the open position, at exit, and after exit. Accepted decisions flow through the constrained coordinator and Demo-only execution gateway. The reporting duty is never disabled when intervention is enabled, and no intervention may occur without a linked audit record. Model endpoints never receive exchange credentials or direct order tools. Entry approval and open-position management remain distinct workflows, but both write to the same durable, append-only audit ledger.

### Policy conflict requiring resolution (P0)

The current repository contains conflicting descriptions:
- `docs/CHALLENGE_1_EXECUTION_CONTRACT.md` describes two AI reviews as a pre-entry quorum.
- `run_challenge1_observer_reviews()` in `crypto_super_radar/ai_second_opinion.py` describes post-decision, non-operative reviews.
- `docs/audit/2026-11-19-blofin-position-supervisor-audit.md` states that the open-position reviewers are report-only and identifies the conflicting policy language.

The current code path and the target design must be distinguished without discarding either responsibility: the current open-position reviewer is report-only, while the required target is an active supervisor that also preserves and expands reporting. The release baseline must define both (a) the actions a validated AI decision may cause through the guarded coordinator and (b) the complete audit events every supervisor cycle must produce. Until documents, call paths, tests, and UI agree, the system must not claim an intervention occurred merely because a reviewer was configured or a request was queued. Nor may the system suppress reports after active intervention is implemented.

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

## 4. Proposed event-driven active-supervisor and mandatory reporting procedure

For every tracked Demo position:

1. Load the exact exchange `positionId`, instrument, side, size, average fill, linked entry order, and durable experience ID. Missing/ambiguous linkage means `UNKNOWN`; no supervisor verdict may be attached to another position.
2. Build a sanitized snapshot from confirmed market data and exact linked exchange state. Include UTC event time, source, freshness, closed-candle timestamp, mark price, unrealized PnL, holding duration, TP/SL state and IDs, and deterministic HOLD/EXIT_ELIGIBLE state.
3. Compute a stable evidence hash and an event version. If evidence has not materially changed, do not call a model again solely because a timer fired.
4. Enforce one in-flight review batch per position and event version. Invoke the two providers independently and in parallel, with separate provider identifiers, model identifiers, keys, timeouts, and rate-limit accounting.
5. Persist a request event before dispatch. Persist each reviewer result separately. Record `NOT_CALLED`, `PENDING`, `RATE_LIMITED`, `TIMEOUT`, `INVALID_RESPONSE`, `REVIEWED`, and `UNKNOWN` explicitly with a stable reason code.
6. Apply bounded retry with exponential backoff and jitter for retryable failures, honoring `Retry-After`. Do not retry immediately on every three-second watchdog tick. An ambiguous request outcome must be reconciled before resending.
7. Compare reviewer responses only after schema validation and model/provider identity checks. Agreement is not proof of correctness; disagreement remains visible and is never silently averaged away.
8. Treat each supervisor as both decision-maker and reporter. Each validated decision must be passed to the deterministic coordinator, which may authorize a bounded Demo action. The supervisor itself never sends an exchange order. Record the decision, coordinator result, execution guard checks, exchange response, and final reconciled state as separate linked audit events. The deterministic hard-risk engine and exchange-side protective orders remain authoritative safety controls.
9. On a new closed candle or a material state change, create a new evidence version and schedule a fresh review if the per-provider budget permits. A stale snapshot is `UNKNOWN`, not permission to reuse an old approval.
10. After exit, reconcile actual exchange fill history and realized PnL, then run the post-mortem audit against the original entry-time snapshot. Do not rewrite the original decision using future information. Preserve the full sequence of pre-entry reports, in-position reports, interventions, non-interventions, errors, recovery actions, exit reports, and post-exit findings so defects can be located and corrected.

## 5. Three-second watchdog contract

The target watchdog should meet all of the following:

- Tick period: 3 seconds, measured with a monotonic clock. Record scheduled time, actual start, completion, and drift.
- Input: the newest validated in-memory state snapshot, not a newly fetched REST response on every tick.
- Freshness: each input carries source timestamp and receive timestamp. Stale, missing, discontinuous, or unlinked data yields `UNKNOWN`.
- Idempotency: each state transition has a stable event key; duplicate ticks must not create duplicate actions.
- Non-blocking: AI calls, Notion sync, and historical reconciliation cannot block the local watchdog.
- Fail-safe: missing data or watchdog lag disables new entries. Existing exchange-side TP/SL remains active; the watchdog must not fabricate a successful protective order.
- Dual responsibility: every watchdog cycle records the state of both supervisors, whether each was called, the age/hash of its last accepted decision, and why a fresh call was or was not due. When a new inference is due, the supervisors report their findings and the coordinator may authorize only a bounded Demo action. Models still cannot directly submit, cancel, resize, or close an exchange order.
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
6. **Guarded intervention and complete reporting:** tests proving models have no direct exchange credentials or order tools, while valid decisions can reach only the Demo-only gateway through deterministic checks; every attempted, accepted, rejected, blocked, failed, or completed intervention must have a complete linked audit trail.
7. **Position reconciliation:** actual BloFin Demo position and TP/SL order IDs, fill history, exit fill, fee, and realized PnL linked to the same experience.
8. **Durability:** write → restart/deploy → read-back with unchanged event IDs and hashes; Notion outbox retry and projection verified separately.
9. **Operational acceptance:** sustained Demo observation with measured latency, API request rate, quota consumption, missed ticks, stale-data rate, supervisor coverage, and error/recovery rate.
10. **Release safety:** exact commit, test artifacts, deployment ID, environment confirmation, rollback plan, and explicit Demo-only permission proof.

## 8. Ordered implementation roadmap

- **P0 — Resolve authority and policy:** reconcile README, execution contract, audit report, entry supervisor, and open-position observer. Mark each AI function as ENTRY_GATE or POST_DECISION_AUDIT.
- **P1 — Architecture and requirements baseline:** adopt this procedure; add a system context diagram, sequence diagram, state machine, requirements-to-test traceability matrix, provider/API quota registry, and risk register.
- **P2 — Transport:** implement BloFin Demo WebSocket position, order, algo-order, and market-data consumers with authentication, ping/pong, reconnect, gap detection, and durable event ingestion.
- **P3 — Three-second local watchdog:** evaluate fresh cached snapshots using deterministic rules and a monotonic scheduler; measure latency and drift. Keep REST reconciliation separate.
- **P4 — Supervisor orchestration and reporting:** implement evidence-version triggers, independent quotas, single-flight, bounded backoff, response validation, explicit failure states, immutable reviewer reports, and a durable heartbeat/audit event for every three-second coordinator cycle, including cycles where no LLM request is made.
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


---

## 10. Superseding target design — AI supervisors with active trade authority

**This section does not remove or reduce reporting.** It supersedes only the interpretation that supervisors should remain report-only. The target is a dual-function model: both supervisors actively participate in bounded trade decisions **and** continuously produce comprehensive reports/audit evidence. Every intervention, disagreement, non-intervention, missed review, data problem, failure, retry, guard rejection, execution result, and post-exit finding must remain reportable. The existing Staging implementation is still report-only; this section defines the intended next architecture and is not a claim that active intervention is already deployed.

### 10.1 Operating decision

Challenge 1 shall use two independent AI supervisors as active decision participants at three stages:

1. **Before entry:** review and vote on whether the proposed trade may be opened.
2. **While a position is open:** continuously reassess the live position and issue bounded management decisions.
3. **At exit and after exit:** decide whether the position should be held, protected, reduced, or closed; then audit the actual fill and realized PnL.

The supervisors are not decorative second opinions or silent execution agents. Each one must produce both (a) a structured operational decision and (b) a structured report of the evidence reviewed, important observations, risk flags, data gaps, action rationale, and dissent. Their structured decisions must feed an explicit decision coordinator. The coordinator converts accepted decisions into a constrained command for the deterministic execution gateway. The models themselves never receive exchange credentials or direct order tools. Reporting must continue even when the correct operational decision is HOLD, NO_ACTION, VETO, or REQUEST_DATA.

### 10.2 Two different supervisor mandates

| Supervisor | Primary mandate | Must independently examine |
|---|---|---|
| Supervisor A — Market / Trade Thesis | Is the original LONG/SHORT thesis still valid? | Closed 5m candles, setup pattern, chart structure, momentum, market direction, entry thesis, invalidation level, target progress, evidence freshness |
| Supervisor B — Risk / Position Guardian | Is the live position still inside the allowed risk envelope? | Exact position ID, side, size, average fill, mark price, net PnL after known costs, margin, liquidation distance where available, exposure, risk budget, exchange-side TP/SL and order state |

The supervisors receive the same versioned evidence snapshot but do not receive each other's answer before making their own decision. They must have distinct system prompts, evaluation checklists and failure tests. Two calls to the same model/provider under different names do not count as independent reviewers.

### 10.3 Allowed decision vocabulary

Each supervisor must return a schema-validated JSON decision with the exact position/experience identifiers, evidence hash, timestamp, reason codes and one action from this set:

- `APPROVE_ENTRY` — approve the proposed entry side and exact risk plan.
- `REJECT_ENTRY` — veto the proposed new entry.
- `DEFER_ENTRY` — evidence is incomplete, stale, or contradictory; do not enter.
- `HOLD_POSITION` — keep the existing position and protection unchanged.
- `TIGHTEN_STOP` — propose a safer stop; the new stop must never increase the maximum permitted loss.
- `ADJUST_TARGET` — propose a target adjustment only within the strategy's pre-approved target envelope.
- `REDUCE_POSITION` — request a partial reduction within the configured allowed size and precision.
- `CLOSE_POSITION` — request closure of the exact linked position.
- `URGENT_RISK_ALERT` — report an immediate risk condition requiring the deterministic risk engine to evaluate an emergency response.
- `REQUEST_DATA` — refuse to make a trade-management decision until specified missing evidence is recovered.

The response schema must reject free-form commands, unknown actions, missing IDs, non-finite prices, invalid side values, and instructions outside the allowed envelope. Every response must also include concise rationale, evidence references, material observations, detected risks, uncertainty/data gaps, and explicit reason codes. Record structured explanations rather than hidden chain-of-thought. A numerical confidence field is explanatory metadata only; it is not treated as a calibrated probability.

### 10.4 Decision and execution authority

The target authority model is:

- **New entries:** both supervisors must independently return `APPROVE_ENTRY` for the same side and the same evidence hash. Any `REJECT_ENTRY`, `DEFER_ENTRY`, missing response, timeout, model mismatch, or disagreement blocks that entry.
- **Ordinary position management:** the coordinator combines both decisions. `HOLD_POSITION` is not assumed when one result is missing. A missing or stale result becomes `SUPERVISOR_QUORUM_UNKNOWN`; no new exposure or risk increase is allowed.
- **Stop protection:** an AI may propose only a stop move that tightens protection. Widening a stop, removing a protective order, or increasing the maximum loss is prohibited. The executor re-reads the live position and live TP/SL state before an amendment.
- **Target changes:** target amendments require both supervisors to agree and must remain inside the original approved strategy envelope. A target change cannot weaken a stop or bypass risk limits.
- **Partial reduction:** requires an exact live-position match, valid exchange quantity/precision, and a risk check. The supervisor cannot select a different position by symbol alone.
- **Normal AI-directed exit:** both supervisors must agree on `CLOSE_POSITION`, or an explicitly pre-approved deterministic exit rule must independently authorize closure.
- **Emergency risk:** hard deterministic protection always has priority over AI consensus. A crossed exchange-side stop, invalid position state, or pre-defined hard loss/liquidation guard must not wait for an LLM response. One supervisor's `URGENT_RISK_ALERT` immediately blocks new entries and triggers a fresh deterministic risk evaluation; it does not by itself create an unrestricted market order.
- **No direct model execution:** the execution gateway alone may submit, amend, reduce, or close a Demo order. It validates the exact `positionId`, current side/size, order state, idempotency key, margin, quantity/price precision, daily risk and protective orders immediately before action. Ambiguous submission results are reconciled before any retry.

No supervisor may increase leverage, increase position size, add to a losing position, widen a stop, remove the final protective stop, change account mode, transfer funds, withdraw funds, or switch from Demo to Production. The hard risk engine can veto any AI proposal. AI agreement cannot override a failed hard gate.

### 10.5 Three-second active supervision cycle

The 3-second requirement applies to the **supervision coordinator**, not to a promise that a remote LLM will finish inference within three seconds.

Every three seconds, using a monotonic clock, the coordinator must:

1. Read the newest already-received, validated WebSocket snapshot from memory.
2. Verify freshness, exact position linkage, order/TP-SL state, market-data age, account/risk state and last supervisor-decision age.
3. Recompute deterministic risk flags and the current permitted action envelope.
4. Re-evaluate the two supervisors' latest accepted decisions against the new snapshot and evidence hash. A decision is invalidated when its evidence version is no longer compatible with the material current state.
5. Determine whether a new AI inference is due because of a material event, a new closed 5m candle, a price/volatility threshold, a stop/target proximity threshold, an unexpected order/position update, a risk-budget threshold, or an explicit recovery event.
6. Start a fresh independent review only if the provider is eligible, no same-position/same-evidence request is already in flight, and the per-provider budget allows it.
7. If no fresh AI result can be obtained, preserve the last known decision only within a configured short validity window. After expiry, move the supervisor state to `STALE`/`UNKNOWN`, block new entries and risk increases, and retain exchange-native protection. Never invent a fresh AI vote.
8. Persist the watchdog heartbeat, evidence hash, snapshot age, supervisor states, selected action, rejected actions, and reason codes.

**The two external LLM endpoints will not be called unconditionally every three seconds on the current free tiers.** The current observer code has per-reviewer application caps of 35 requests/day and 18 requests/minute. OpenRouter currently documents 20 requests/minute and 50 requests/day for free `:free` models; failed attempts count toward its daily limit. A three-second call cadence would generate 28,800 requests per reviewer per day and exceed the current application cap in roughly 105 seconds. The free-provider quotas therefore cannot support literal two-model inference every tick.

If the requirement is later interpreted as a fresh remote model response from both providers every three seconds, it requires a separately capacity-tested inference service and quota arrangement with measured sub-three-second response latency. It cannot be represented as a free-tier capability or assumed from a timer setting.

### 10.6 BloFin event transport

Use the documented private WebSocket channels for positions, orders, algo/TP-SL orders and account state, plus public channels for mark price/trades/candles as required. The initial position snapshot and event updates feed the local state cache. Order and algo-order channels are event-driven; position updates include snapshots and event/periodic updates.

The implementation must include authentication, subscription acknowledgement, ping/pong, reconnect, duplicate/out-of-order handling, event timestamps, stream health, gap detection and REST reconciliation after a disconnect or uncertain state. REST is a recovery/reconciliation mechanism, not a request issued on every 3-second heartbeat. The exact Demo endpoints and any Demo-specific channel behavior must be verified against BloFin's current API documentation before implementation.

### 10.7 Complete reporting and audit record for every supervisor cycle and intervention

For each position and each decision version, persist:

- `experience_id`, exact `positionId`, instrument, side, order/client-order IDs and environment `BLOFIN_DEMO`.
- Original entry snapshot hash and current evidence hash.
- Decision cycle ID, scheduled time, actual request time, response time and latency.
- Provider, configured model, returned model, prompt/schema version and request status.
- Each supervisor's action, verdict, reason codes, observations, risk flags, and data requested.
- The coordinator's quorum result, selected command, rejected proposal(s), and precise reason.
- Execution gateway pre-checks, command ID, idempotency key, BloFin response, actual order/position update and reconciliation result.
- Before/after SL, TP, position size, mark, unrealized PnL, fee/funding evidence when available, and realized exit PnL after closure.
- Whether an event was `REQUESTED`, `REVIEWED`, `APPROVED`, `VETOED`, `EXECUTED`, `REJECTED_BY_GUARD`, `STALE`, `UNKNOWN` or `NOT_CALLED`.

A scheduled heartbeat is not an AI review. A request accepted by an HTTP endpoint is not a response. A valid AI suggestion is not an exchange fill. These must remain separate audit states.

#### 10.7.1 Mandatory three-second cycle report

Every three-second coordinator cycle must append a durable heartbeat record, including when both models are not called. At minimum record: cycle ID; scheduled and actual timestamps; loop duration and drift; position/experience ID; snapshot age and evidence hash; WebSocket and reconciliation health; deterministic risk state; Supervisor A and B status (`NOT_DUE`, `PENDING`, `REVIEWED`, `STALE`, `UNKNOWN`, `RATE_LIMITED`, `TIMEOUT`, or `ERROR`); last-decision age; whether a new model request was due; the reason it was called or skipped; in-flight/ quota state; coordinator outcome; selected action or `NO_ACTION`; guard result; and any execution command/result reference.

#### 10.7.2 Required report categories

The reporting pipeline must preserve and expose, in chronological order: input/market observations; state changes; each supervisor's independent assessment; agreements and disagreements; approvals and vetoes; HOLD/NO_ACTION decisions and why; missing or stale evidence; requests and responses; timeout, quota, schema, provider and transport failures; retries and circuit-breaker transitions; coordinator decisions; rejected proposals and guard reasons; order submissions/amendments/cancellations; exchange acknowledgements; actual fills and reconciled position changes; protection/TP/SL changes; fees/funding and realized PnL when available; recovery/restart events; and post-exit defect findings.

Reports may be summarized in the dashboard for readability, but the underlying event ledger must retain every cycle and state transition. No heartbeat, failed attempt, disagreement, rejected action, or `NO_ACTION` event may be silently dropped. Use append-only durable storage with stable IDs and evidence hashes; Notion/dashboard remains a report projection, not the authoritative execution ledger.

### 10.8 Implementation sequence

- **P0 — Policy and schema:** extend the current open-position reporting path into an active-supervisor contract without removing or weakening reporting. Keep entry approval and open-position management as separate workflows. Define JSON schemas, action matrix, quorum logic, required report fields, per-cycle heartbeat events and prohibited actions.
- **P1 — Deterministic decision coordinator:** implement a pure function that consumes two validated supervisor outputs plus live state and returns one constrained action or `NO_ACTION`. Add exhaustive tests for every pair of votes and every missing/invalid state.
- **P2 — WebSocket state layer:** implement BloFin Demo private positions/orders/orders-algo/account streams and public market streams, backed by an initial REST snapshot and periodic/recovery reconciliation. Persist stream health and exact event IDs.
- **P3 — Three-second watchdog:** implement the non-blocking monotonic loop over cached snapshots. Add a test proving approximately 3-second scheduled ticks, bounded drift, no overlapping cycles, and no per-tick REST or LLM call.
- **P4 — Active AI orchestration:** call both independent supervisors on material event/evidence versions; add single-flight, evidence hashing, quotas, stale-result invalidation, retries with backoff/jitter, circuit breakers and structured result validation.
- **P5 — Guarded Demo interventions:** wire the coordinator to the Demo-only execution gateway for approved stop tightening, target adjustment, partial reduction and closure. Keep hard deterministic stops independent of AI availability. Do not permit any Live endpoint or credentials.
- **P6 — Evidence-based acceptance:** run deterministic replay, unit/integration tests, failure injection, then Demo shadow evaluation followed by bounded active Demo intervention. Compare every recommendation to actual position/order/fill results and realized PnL. Do not accept Challenge 1 until both intervention quality and execution reconciliation are demonstrated.

### 10.9 Mandatory acceptance tests

1. Supervisor A approves and B approves same side/evidence: entry can proceed only after all deterministic guards pass.
2. Supervisors disagree, one vetoes, one times out, or the evidence hashes differ: no entry.
3. Open position + both HOLD: retain exact current protection; record both fresh results.
4. Open position + one TIGHTEN_STOP and one HOLD: no amendment unless the approved policy explicitly defines a safe one-reviewer emergency tighten rule; default is no AI amendment.
5. Both TIGHTEN_STOP: proposed stop must reduce or preserve maximum loss; live TP/SL is re-read and matched by exact position ID before amendment.
6. Any request to widen SL, remove protection, add to a losing position, increase leverage/size or act on a different position: reject at schema or execution guard.
7. Both CLOSE_POSITION: re-read exact position, risk and exchange state; submit one idempotent Demo close request; reconcile actual fill.
8. One URGENT_RISK_ALERT: block new entries and invoke deterministic risk evaluation; do not blindly submit an order based on free-form model text.
9. WebSocket disconnect, stale mark, position ID mismatch, duplicate event or sequence gap: supervisor input becomes UNKNOWN; no new risk is allowed until REST reconciliation.
10. LLM 429, 401, timeout, invalid JSON, model mismatch or daily cap: record exact failure; do not run a tight retry loop; preserve exchange-native protection.
11. Restart during in-flight review or order submission: recover durable request/command state and reconcile before resending.
12. Every executed AI intervention must be traceable from evidence hash to both votes to coordinator command to BloFin exchange response and realized outcome.
13. Every three-second coordinator cycle must produce a durable report even when no model request is due; the report must distinguish `NOT_CALLED` from `REVIEWED` and preserve the exact reason.
14. Vetoes, disagreements, HOLD/NO_ACTION decisions, stale data, missing inputs, quota limits, provider failures, retries, guard rejections and recovery events must be queryable by position and experience ID, not just visible as aggregate counters.

### 10.10 External references for this active-supervision design

- BloFin API documentation, private WS Positions, WS Orders, WS Algo Orders, account channels and rate limits: https://docs.blofin.com/index.html
- OpenRouter Free Models and current free-tier rate limits: https://openrouter.ai/collections/free-models
- NIST AI RMF Playbook — Govern, roles, responsibility, human-AI configurations and documentation: https://airc.nist.gov/airmf-resources/playbook/govern/
- NIST AI RMF 1.0: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf
