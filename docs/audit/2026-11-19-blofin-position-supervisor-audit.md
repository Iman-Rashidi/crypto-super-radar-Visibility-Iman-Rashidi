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
