# BloFin Demo Position and Supervisor Audit
**Date:** 2026-11-19  
**Scope:** User-reported BloFin Demo snapshot at 02:09 Europe/Berlin, candidate journal reconciliation, and AI-supervisor audit trace.  
**Safety:** Read-only evidence review only. No order, position, TP/SL, environment, deploy, or account setting was changed.

## 1. User-provided exchange snapshot
The screenshot shows three open Buy positions, each Isolated at 1x:
| Contract | Displayed size | Displayed margin |
|---|---:|---:|
| LINKUSDT | 1 LINK | 9.06 USDT |
| XRPUSDT | 10 XRP | 9.66 USDT |
| SOLUSDT | 0.01 SOL | 0.73 USDT |

The screenshot does not expose exchange order IDs, position IDs, exact fill timestamps, or the state of protective TP/SL orders.

## 2. Candidate matches in the Notion activity journal
The following filled Demo Futures records are a strong match to the three screenshot rows by symbol, direction, contract size economics, and displayed margin. They are candidate matches, not a completed exchange API reconciliation.

| Screenshot row | Journal entry (UTC) | Side | Fill price | Predicted SL | Predicted TP1 | BloFin order ID | Experience ID |
|---|---|---|---:|---:|---:|---|---|
| LINKUSDT, 1 LINK | 2026-11-18 23:26 | LONG | 9.064 | 8.9672357143 | 9.1427642857 | 1000145612600 | blofin-ch1-90b8c5dec1d270cddd6ddfee8c3079f1 |
| XRPUSDT, 10 XRP | 2026-11-18 23:25 | LONG | 0.9661 | 0.9568164286 | 0.9751835714 | 1000145612580 | blofin-ch1-c49d018c4f9eb75b74fb6264e50093e2 |
| SOLUSDT, 0.01 SOL | 2026-11-18 23:13 | LONG | 73.15 | 72.2688571429 | 74.2511428571 | 1000145611947 | blofin-ch1-93af637b0383eb296d810d7dcee5470d |

The implied entry notionals are approximately 9.064, 9.661, and 0.7315 USDT respectively, which closely match the screenshot's displayed isolated margins of 9.06, 9.66, and 0.73 USDT at 1x.

Journal records:
- LINK: https://app.notion.com/p/41d6c893021881ff9476daf3241696fd
- XRP: https://app.notion.com/p/41c6c8930218811f8fcfc504c3612eda
- SOL: https://app.notion.com/p/41c6c893021881f3b234cd9f19985b8d

## 3. Supervisor audit finding
For each of the three candidate entry records, the Notion activity journal currently shows:
- `ai_status = NOT_CALLED`
- `ai_model` and `ai_reason` blank in the evidence payload
- independent review status = `در انتظار`
- the weakness field identifies `بازبینی AI: NOT_CALLED`

Therefore, the currently available Notion evidence does **not** show that Nemotron or the second supervisor actually reviewed these entries. No approval, rejection, disagreement, validation result, or reviewer response may be inferred.

The recorded SL and TP1 values above are strategy predictions. They are not evidence that BloFin accepted and activated protective trigger orders for the matching position IDs.

## 4. Nearby journal events, kept separate from the three screenshot rows
- BTC-USDT LONG was recorded Filled at 2026-11-19 01:09 UTC, entry 63080, order ID 1000145618879, experience ID blofin-ch1-9af9f515e2bd534f40ed20a56dbfc556; its journal evidence also says `NOT_CALLED`. BTC is not visible in the supplied screenshot, so it is not included in the three-row match.
- An ETH-USDT LONG attempt at 01:09 UTC was rejected with `Maximum concurrent-position limit has been reached`.
- A SOL-USDT SHORT attempt at 01:10 UTC was rejected with `A position is already open for this contract`.
These are separate execution/audit events and should not be merged into the three screenshot positions.

## 5. Repository and policy status
Relevant repository work is not yet deployed as a proven runtime capability:
- PR #42 and PR #46 describe two independent verified reviewers for new automatic entries, but both remain open and unmerged.
- PR #61 describes Staging-only, post-decision, report-only supervisor auditing and read-only position monitoring; it remains Draft and unmerged.
- PR #63 describes one combined entry/exit report per experience; it remains Draft and unmerged.
- PR #55 records a prior Staging diagnostic where Nemotron returned HTTP 200 but failed response/schema validation, while the second reviewer provider returned HTTP 401. This is historical Staging evidence, not proof of the current active runtime's exact error.

There is a policy wording mismatch across open PRs: older #42/#46 describe a dual-approval entry gate, while #61 and the 2026-11-18 approved hand-off describe supervisors as post-decision AUDIT/REPORT-only with no order authority. This mismatch must be resolved in documentation/code before claiming either policy is active. Under either interpretation, `NOT_CALLED` is not a supervisor audit.

## 6. Required read-only reconciliation
1. Read BloFin Demo open positions and match each exact `positionId`, `instId`, side, size, average price, leverage, margin mode, and create time to the candidate order IDs and experience IDs.
2. Read open TP/SL trigger orders and verify each against the exact position ID, quantity, side, trigger price, and exchange status.
3. Retrieve the two independent supervisor REQUESTED/RESULT records for each experience ID, including actual provider/model, request/response times, validation, recommendation, and reason. If there was no call, retain `NOT_CALLED`; do not synthesize an approval.
4. Confirm the Notion outbox/event-store delivery state and any Render worker errors around each entry time.
5. Keep the three positions unchanged during this audit. Do not close, resize, amend protection, retry orders, or enable Live trading as part of this report.

## 7. Audit status
**Finding:** Three likely screenshot-to-journal matches have filled entries, but their supervisor review is recorded as `NOT_CALLED` and pending. Exact position-ID, exchange-side TP/SL, and current Render log reconciliation remain open.  
**Severity:** High-priority audit gap (P0 for evidence/observability).  
**Conclusion:** The three entries must not be described as reviewed or approved by the two AI supervisors until actual per-reviewer evidence is recovered and read back.


## 8. Follow-up engineering and deployment reconciliation — 2026-11-19 17:00 Europe/Berlin

### 8.1 PR branch/base verification
| PR | State | Base | Head | Audit conclusion |
|---|---|---|---|---|
| #57 | Merged | `feature/blofin-demo-adapter` | `fix/challenge1-observer-only-2026-11-18` | Merged into Feature only; not proof of Staging inclusion |
| #60 | Open | `feature/blofin-demo-adapter` | `fix/observer-outbox-status-and-position-gaps-2026-11-18` | Not part of Staging merely by being open |
| #63 | Open, Draft | `feature/blofin-demo-adapter` | `fix/observer-recovery-profit-exit-audit-2026-11-18` | Post-decision reporting does not establish continuous live-position supervision |
| #64 | Open | `staging/challenge1-integration-2026-11-16` | `fix/staging-position-closed-notion-audit-2026-11-19` | Targets Staging but remains unmerged; no merge performed |

### 8.2 Staging-only changes made
Changes were committed directly to `staging/challenge1-integration-2026-11-16`; no PR was merged and no Render deploy was triggered.

- Connected the BloFin Demo Position Manager's open-position cycle to the independent observer scheduler.
- Each manager cycle now captures a millisecond-unique cycle key, UTC heartbeat, mark price, unrealized PnL, holding duration, robot HOLD/EXIT_ELIGIBLE decision, market-direction evidence, and protective-order evidence.
- The observer scheduler is invoked only when a persisted entry journal snapshot contains non-empty Challenge 1 evidence. Missing experience IDs, missing snapshots, snapshot-read failures, and scheduler exceptions are represented as explicit per-reviewer NOT_CALLED events.
- The two reviewers' output remains observation-only and cannot alter an order, position, TP/SL, or exit decision.
- Market direction is derived only from recent, confirmed, closed BloFin 5-minute candles; stale, incomplete, invalid, or discontinuous candle evidence yields UNKNOWN.
- Protective TP/SL visibility is reported only when the returned order is linked by exact position ID or exact originating client order ID. No exact link means NOT_VERIFIED.
- Stop-loss threshold crossing is exposed as auditable state. This change does not send a manual close order or alter any open position.
- Position cycles lacking a linked experience ID are retained in the append-only audit chain under an explicit unlinked-position reference; they are not projected into the Notion activity outbox as if they had a real experience ID.
- Added regression tests for unique cycle events, hash-chain integrity, explicit per-reviewer NOT_CALLED records, restart/cadence recovery, confirmed candle direction, exact protective-order matching, and long/short SL threshold crossing.

### 8.3 CI evidence
- Commit `b4c3ab85949c9a3ba912e14dccecce85f8555cb5`: GitHub Actions run [#44442310919](https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi/actions/runs/44442310919) completed **success** on 2026-11-19 02:11 UTC.
- Both workflow jobs passed: `test` and `streamlit-smoke`.
- The test job ran compile checks, the repeated Challenge 1 regression suite, extracted Render source-bundle verification, full unittest discovery, and CLI smoke checks before publishing the refreshed source archive.
- GitHub Actions then created archive-refresh commit `e6218889a0398b5a04f6d4ca7349701a37055e35` on the Staging branch. This is a source-bundle refresh, not a Render deployment.

### 8.4 Render Staging read-only reconciliation
- Service: `srv-dbsidmbbc2fs73blhblg`, `crypto-super-radar-unlimited-demo-staging`.
- Configured branch: `staging/challenge1-integration-2026-11-16`.
- `autoDeploy=no`; no deployment was triggered during this work.
- Latest Render deployment remains `dep-dbv3vlutlv9c73fo3shg`, status `live`, running commit `153b1fc50eddff2e8414199f09ba8fa95ff9ec30`.
- Staging branch has advanced beyond that deployed commit. The code changes in this follow-up are therefore **not deployed and have no live-runtime evidence yet**.
- No order, open position, protective order, API key, Secret, or production service was changed.

### 8.5 HTTP 502 investigation
Read-only Render application logs show POST `/api/ai/dry-run` responses with HTTP 502 at:
- 2026-11-18 01:54:03 UTC
- 2026-11-18 02:44:35 UTC
- 2026-11-18 03:49:35 UTC
- 2026-11-18 03:58:56 UTC
- 2026-11-19 00:12:20 UTC
- 2026-11-19 00:18:29 UTC

The same route maps any completed dry-run where `test_passed` is false (unless it is a configured 429 or missing-key 503) to HTTP 502. Consequently, the access-log status alone does **not** prove an uncaught server exception. The route does not log the response payload or per-reviewer failure detail, and no traceback/error-level application log was returned for the selected window. Exact cause remains UNKNOWN until the corresponding response payload or provider request/response evidence is captured. Earlier 429 responses are consistent with the route's explicit rate-limit/cooldown statuses, but the access log alone does not identify which one.

### 8.6 Current requirement matrix
| Requirement | Code exists | Tests pass | On Staging branch | Deployed | Runtime evidence | Missing |
|---|---|---|---|---|---|---|
| Per-cycle open-position audit event | Yes | CI green; targeted regression included | Yes | No | No | Verify live event stream after separately approved deployment |
| Independent reviewer scheduling while position open | Yes, scheduled cadence | CI green; scheduler/restart tests included | Yes | No | No | Confirm both real provider calls and saved results on an open Demo position |
| Explicit NOT_CALLED per reviewer | Yes | CI green | Yes | No | No | Verify runtime skip reasons are durably stored |
| Mark/PnL/holding/heartbeat evidence | Yes | Compile/full CI green | Yes | No | No | Live BloFin values and exchange reconciliation |
| Market-direction evidence | Yes, confirmed closed 5m candles only | Regression tests included | Yes | No | No | Live candle response and timestamp freshness |
| TP/SL protective-order identity | Exact position/client-order match only | Regression tests included | Yes | No | No | Confirm actual open protective orders on BloFin Demo |
| Stop-loss threshold visibility | Yes, observation only | Long/short threshold tests included | Yes | No | No | Verify actual exchange SL behavior; no manual close tested |
| Notion projection of linked minute summaries | Yes; unlinked summaries are not queued | CI green | Yes | No | No | Verify outbox delivery and Notion page sync in live runtime |
| Exit fill and realized PnL reconciliation | Existing close reconciliation path; not re-proven here | Not established by this follow-up alone | PR #64 remains unmerged; inspect current Staging implementation separately | No new deployment | No | Full BloFin history-to-journal match for each closed position |
| HTTP 502 root cause | Route mapping identified; no exact provider cause | Not applicable | Existing route inspected | Deployed old route | Access-log only | Capture response body and structured per-reviewer diagnostics |

### 8.7 Acceptance decision
**Challenge 1 remains NOT ACCEPTED.** Offline CI success proves the tested code paths on the Staging branch, not successful live BloFin Demo supervision. The Staging changes are not deployed because Render auto-deploy is disabled and no deployment authorization was given. Open positions remain untouched. Next acceptance work must use a separately authorized deployment, then observe real open Demo positions without changing or closing them, recover both reviewers' request/response records, and reconcile eventual exchange close history and realized PnL.
