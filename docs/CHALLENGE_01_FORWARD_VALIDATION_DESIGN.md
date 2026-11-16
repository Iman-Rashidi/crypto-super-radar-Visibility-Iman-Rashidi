# Challenge 1 — Forward Validation Design

**Status:** Partially implemented on the Challenge 1 feature branch. Mastery rules remain unapproved; no Challenge 1 pass is claimed.  
**Updated:** 2026-11-16  
**Scope:** BloFin Demo Spot public candles only; no orders, no Live trading.

## 1. Current verified state

- The active BOT_ONLY path predicts the next completed candle from information available through the final feature candle. Its frozen decision event is appended before the target OHLC is read and labelled. A regression test observes this event order.
- Each sample stores 50 completed, contiguous context candles. The final five candles form the pattern window. The next candle is the target and appears only in later outcome evidence, not in the frozen input snapshot.
- The BloFin adapter is a GET-only public Demo Spot candle reader. Challenge 1 does not submit Demo or Live orders; outcomes are recorded as NOT_EXECUTED with no PnL.
- The Mentor deterministically audits locked BOT_ONLY experiences, appends feedback, and trains a pure-Python multinomial logistic-regression candidate. Training uses the oldest 80% of reviewed samples and the newest 20% for chronological validation.
- The candidate artifact contains the feature schema, pattern catalogue/mapping, normalization, weights, biases, chronological dataset hashes, validation metrics, and SHA-256. It remains CANDIDATE_NOT_PROMOTED. The 50-bar context is currently audit/regime-stratification evidence, not model input; the Candidate uses five-bar candle features and candlestick-pattern flags.
- The implemented lifecycle supports a BOT_ONLY baseline followed by paired BOT_ONLY and Candidate predictions on later, non-overlapping 100-sample rounds. A candidate for the next round is trained only after the previous round's outcomes and Mentor feedback are final. Round 5 does not create an unused round-6 model.
- The memory API is append-only and validates event sequence, payload hashes, event-hash chain, duplicate event IDs, immutable outcomes, and Mentor/lesson lifecycle. It supports a prior-round BOT_ONLY reference only when that baseline is integrity-verified, final-labelled, has Mentor feedback, belongs to the same challenge/engine, and is chronologically available before the assisted feature time.
- Runtime Turso durability is enforced by the production runner but remains unverified in the active Render service. CI uses test SQLite and synthetic/mock market providers; it does not prove that the deployed service connected to Turso or completed a real market-data challenge.
- GitHub Actions run 1726 on commit d439189 passed: 27 focused Challenge 1 tests, 685 full-repository tests, Streamlit smoke, Render overlay build/startup check, and extracted source-bundle verification. This is code/CI evidence only, not proof of learning or market edge.

## 2. Original defect and current correction

The initial same-candle Open/Close classification was not a forecast: it included the candle being labelled and could not prove generalization. The active forward-validation path has replaced that task.

At the close of candle t, BOT_ONLY predicts the direction of t+1 using only the completed feature/context data through t. The BOT_ONLY decision is appended first. Only after that immutable event is written does the runner inspect the target candle's OHLC and append its final label.

For an assisted round, both the BOT_ONLY and frozen Candidate decisions are appended before the target label is read. The target outcome is then appended for both experiences, and Mentor review is appended only afterward. That feedback can influence a later candidate, never the prediction on the sample that generated it.

The code-level order is now covered by a regression test. This does not yet constitute a real-market execution or a completed five-round evaluation.

## 3. Implemented data protocol (code-level)

For every sample:

1. Select 50 fully completed, contiguous context candles ending at time t.
2. Use only the last five candles of that context as the candlestick-pattern feature window.
3. Derive and freeze the 50-bar Price Action evidence and five-bar pattern window using information available through t.
4. Compute and append the BOT_ONLY prediction before reading the target OHLC.
5. In paired rounds, append both BOT_ONLY and Candidate decisions before reading the target OHLC.
6. After both decisions are immutable, inspect the completed t+1 candle, append the final labels and evidence, then append Mentor feedback for the BOT_ONLY baseline.
7. Do not put target OHLC or later candles in the input snapshot or derived context. Target OHLC belongs only in final outcome evidence.

The first baseline round requires 150 consecutive completed hourly candles for 100 samples: a 50-candle context plus one later target for each sample. Each assisted round seeds its first sample with the prior round's final 50 completed target candles, then consumes 100 strictly newer target candles. Context and pattern windows must be contiguous and the five-bar window must equal the final five bars of the 50-bar context.

Required data guards:
- strict 1H interval adjacency for the first version;
- no duplicate timestamps and newest-first provider ordering;
- exclude open candles (confirm=0) from both features and targets;
- no target timestamp overlap between rounds;
- no missing hourly bars in a sample block;
- return UNKNOWN/BLOCKED rather than guess when completed data is insufficient.

The adapter supports BloFin's before pagination mode and rejects simultaneous after and before arguments. Its integration test checks request construction with a mock session; that test is not a live connectivity test. See https://docs.blofin.com/spot.html.

## 4. Implemented learning sequence (not yet proven on live market history)

- **Round 1, BOT_ONLY:** record 100 next-candle forecasts using the frozen prior-candle baseline. The target candle is read only after the decision event is appended.
- **Mentor audit and Candidate build:** audit the 100 final-labelled baseline experiences, verify the 50-bar context and five-bar pattern data, append Mentor feedback, and train a candidate from the oldest 80 samples with the newest 20 as chronological validation.
- **Rounds 2–5, paired forward holdouts:** run the unchanged BOT_ONLY baseline and the Candidate on the same 100 newer, non-overlapping target candles. Neither model sees the target before both decisions are stored.
- **Incremental training:** after rounds 1–4 are final-labelled and reviewed, train a candidate for the next round from eligible past BOT_ONLY experiences. Round 5 consumes the round-4 candidate; no round-6 model is needed.
- **No same-round feedback:** a round's outcomes and Mentor feedback can update only a later candidate. Already-recorded predictions, labels, and model artifacts remain immutable.

This is a chronological staged evaluation, not an EASY-to-HARD curriculum. The execution profiles named EASY/STANDARD/STRICT elsewhere in the repository are trade-gate profiles, not stages of Challenge 1 teaching. The candidate is still only a candidate, and no round-level market-performance improvement is claimed until actual forward results are produced.

## 5. Current cross-round memory contract

The memory API supports both same-cycle paired decisions and strictly earlier-round transfer. For cross-round transfer, the referenced BOT_ONLY baseline must:
- belong to the same challenge and engine;
- pass integrity verification and have a final outcome;
- already contain Mentor feedback;
- have a lower round number than the assisted decision;
- have a target timestamp no later than the assisted sample's feature timestamp.

The assisted prediction stores the referenced baseline ID, the Candidate version, and the immutable model-artifact SHA-256. The current assisted runner references the reviewed prior-round anchor that produced the frozen Candidate, not the paired current sample's baseline outcome. The paired current-round BOT_ONLY experience is separately recorded and reviewed after both decisions and outcomes are locked.

Future-round references, missing outcomes, missing Mentor feedback, mismatched challenge/engine, and invalid chronology must remain blocked.

## 6. Pass and safety rules

- No Live order capability is added.
- No Demo order is needed for this classification challenge.
- Missing, stale, contradictory, or insufficient market data returns UNKNOWN/BLOCKED, not a guessed label.
- Five consecutive PASS results remain mandatory; the numeric thresholds must be justified and locked before the first scored run.
- The Mentor cannot rewrite BOT_ONLY decisions, outcomes, previous model artifacts, or test labels.
- XP, badge, and Challenge 2 remain locked until all evidence is verified.

## 7. Remaining acceptance work

1. Prove the active execution environment reports TURSO_CLOUD_SQLITE from its actual configured credentials without exposing secrets.
2. Run a permitted read-only connectivity check against the BloFin Demo public Spot candles endpoint from the approved runtime, validating current completed OHLCV, ordering, timestamp adjacency, and freshness. No order endpoint is involved.
3. Run BOT_ONLY round 1 on actual, non-overlapping completed market candles and verify persistent Turso writes, close/reopen recovery, append-only integrity, and exact decision-before-target ordering.
4. Lock and SHA-256 the statistical acceptance protocol before the first scored forward run. Define the eligible sample policy, UNKNOWN handling, Balanced Accuracy/Macro-F1 thresholds, uncertainty method, regime reporting, and baseline comparison.
5. Only after round 1 and Mentor candidate are verified, execute paired forward rounds 2–5 on fresh future market candles. Record all predictions before outcomes, then retrain only for the next round.
6. Report sample counts, UNKNOWN, Balanced Accuracy, Macro-F1, per-class precision/recall, confidence intervals, regime-stratified performance, errors, baseline deltas, and stability. Do not promote a Candidate based on synthetic tests or in-sample fit.
7. Keep Render deployment/configuration changes, Demo orders, Live orders, Candidate promotion, Challenge 2, XP, and mastery locked unless separately authorized and all acceptance evidence is complete.

The active Render Demo service has not been changed by this work. This document is a code/design record, not evidence of a completed runtime challenge or permission to deploy.

## 8. Five-candle pattern catalogue extension

The Challenge 1 feature contract now uses five contiguous completed candles before the next-candle target. This enables Rising Three Methods and Falling Three Methods to enter the Mentor feature vector without exposing the future target. The catalogue adds Dragonfly Doji, Gravestone Doji, Long-Legged Doji, Bullish/Bearish Kicker, Three Inside Up/Down, Three Outside Up/Down, and both five-bar continuation formations.

Kicker labels use an explicitly documented continuous-market body-displacement heuristic; they must not be described as proof of a traditional overnight gap. All candle-pattern definitions remain deterministic heuristics. Their predictive value, context sensitivity, and regime-specific reliability remain unproven until independent market-data validation. The five-candle schema and catalogue version are included in candidate-artifact integrity checks.


## 9. Fifty-bar Price Action context and evidence boundaries

The 50-candle context is recomputed from completed OHLCV bars using existing repository engines: non-repainting confirmed pivots, market regime and trend (EMA20/50/200, RSI14, ADX14, MACD and ATR), relative volume, divergences, Fibonacci retracement setup, geometric chart patterns, Elliott-wave hypothesis, and candlestick/geometric pattern evidence. A separate conservative structure summary records HH/HL and LH/LL, candidate BOS/CHoCH, local liquidity sweeps, retest candidates, and three-bar FVG candidates. Pivot confirmation delay is preserved: a pivot is not known until its right-side confirmation bars have closed.

Local five-bar highs/lows are not called structural support/resistance. The 50-bar context uses confirmed swing pivots and is explicitly timestamped at the final feature candle. Order Blocks are recorded as UNKNOWN because this branch has no independently validated Order Block detector; no zone is fabricated. FVG, BOS/CHoCH, retest, and Elliott outputs are descriptive hypotheses, not trade entries or probabilities.

The Mentor recomputes the complete context before accepting or training from an experience. The 50-bar OHLCV context and exact timestamps are retained in the append-only decision snapshot and included in the candidate dataset hash. The candidate model still uses only its five-bar continuous features and 31 candlestick-pattern flags. The richer 50-bar context remains audit and regime-stratification evidence, not added model features: 100 samples provide only 80 training rows, too few to justify enlarging the model feature set and claiming stable learned effects. The candidate schema is CSR_CH01_CANDLE_PATTERN_PA_SOFTMAX_V4 and remains CANDIDATE_NOT_PROMOTED. Existing BOT_ONLY decisions and outcomes are immutable.
