# Challenge 1 — Forward Validation Design

**Status:** Engineering design proposal, not yet implemented or approved as a mastery rule.  
**Updated:** 2026-11-15  
**Scope:** BloFin Demo Spot public candles only; no orders, no Live trading.

## 1. Current verified state

- Challenge 1 BOT_ONLY currently classifies the same completed candle using its final Open and Close.
- This is a descriptive candle-label consistency task. It is not a forecast of a later candle and its 100% score must never be described as predictive accuracy or trading edge.
- The Mentor performs an append-only audit of each locked baseline and trains a pure-Python multinomial logistic-regression candidate from the oldest 80 feature/target pairs, with the newest 20 as a temporal validation split.
- The candidate artifact stores the feature schema, normalization statistics, weights, biases, training/validation dataset hashes, validation metrics and artifact SHA-256 in a LESSON_PROPOSED event. The hash is independently recalculated on read-back.
- A protected dashboard endpoint invokes the batch Mentor only when exactly 100 valid, final-labelled forward samples exist and the configured memory backend is Turso Cloud SQLite.
- Dashboard status distinguishes completed Mentor feedback from a verified candidate artifact. Missing or invalid artifact keeps the stage at AI_MENTOR_MODEL_BUILD_REQUIRED; a valid artifact advances only to FORWARD_HOLDOUT_VALIDATION_REQUIRED. All assisted rounds remain blocked.
- GitHub Actions run 1573 passed: focused Challenge 1 tests 20/20, full repository suite 673/673, and Streamlit smoke. This proves CI only, not Render execution or persistent runtime memory.

## 2. Why the next rounds must change

The same-candle Open/Close comparison contains the target inside the input. Repeating it on new candles would only repeat a tautology. It cannot establish generalization, learning, or future-market performance.

The next implementation should convert the skill to:

**At the close of candle t, use only information available through candle t to classify the direction of candle t+1.**

The target label must be derived from the next candle only after the prediction snapshot is frozen.

## 3. Proposed data protocol

For every sample:

1. Select one fully completed feature candle t.
2. Freeze the feature snapshot and decision timestamp at or after t closes.
3. Predict the next candle's direction using no fields from candle t+1.
4. Once t+1 is complete, append the final outcome and its evidence.
5. Keep the target candle's OHLC out of the decision input snapshot. It may appear only in the later outcome evidence.

The first round needs 101 consecutive completed candles to produce 100 feature/target pairs. Each later round uses the last completed target candle of the prior round as the first feature candle, plus 100 strictly newer completed target candles.

Required data guards:
- strict 1H interval adjacency for the first version;
- no duplicated timestamps;
- newest-first provider ordering verified before chronological ordering;
- no open candle (confirm=0) in a target;
- no overlap of target timestamps between rounds;
- no missing hourly bars inside a sample block;
- fail closed as UNKNOWN/BLOCKED when the provider does not supply enough newer completed candles.

BloFin's documented candle pagination defines after as records earlier than the requested timestamp and before as records newer than the requested timestamp. The adapter now supports before and rejects simultaneous after and before. See https://docs.blofin.com/spot.html.

## 4. Proposed learning sequence

- **BOT_ONLY baseline:** a deliberately simple, frozen rule predicts the next candle from the prior completed candle. Record its prediction before its future label is appended.
- **Mentor audit/training:** inspect all 100 baseline outcomes, reject data leakage, and train a small supervised model using only the baseline feature/target pairs. Persist its exact feature schema, normalization statistics, weights, training dataset hash, and model version in append-only durable memory.
- **Assisted rounds 2–5:** evaluate the frozen candidate model on four chronologically later, non-overlapping 100-sample blocks. Pair the BOT_ONLY and BOT_WITH_AI_MENTOR predictions on the same targets to measure whether the assisted model adds value.
- **Incremental updates:** after rounds 1–4 are final-labelled and locked, the Mentor may produce a new candidate model for the next round. Round 5 consumes the candidate trained after round 4; no unused round-5 model is trained because there is no round 6. A candidate may not alter predictions or labels already recorded.
- **Holdout discipline:** a round's target outcomes may update a model only after that round is closed; the updated model is used on a later block, never on the same block.

The exact model family and numeric pass thresholds remain open until the dataset and baseline distribution are measured. Accuracy, balanced accuracy, macro-F1, per-class support, confusion matrix, and uncertainty must be reported. A single score or profitable-looking sample must not award mastery.

## 5. Memory contract change required

The current memory API requires a BOT_WITH_AI_MENTOR decision to reference a BOT_ONLY baseline from the same cycle. That is too restrictive for genuine learning transfer from a previous, already-labelled round.

Proposed safe replacement:
- the referenced baseline must be the same challenge and engine;
- it must be BOT_ONLY, integrity-verified, final-labelled, and have Mentor feedback;
- its event timestamp and round number must precede the assisted decision;
- the assisted decision must carry the Mentor/model version and immutable model-artifact hash;
- it must never reference a baseline from a future round;
- every assisted prediction and outcome remains a separate append-only experience.

Do not weaken the memory check to merely accept any baseline ID. Add positive and negative tests for cross-round references, future references, wrong challenge/engine, missing final outcome, and missing Mentor feedback.

## 6. Pass and safety rules

- No Live order capability is added.
- No Demo order is needed for this classification challenge.
- Missing, stale, contradictory, or insufficient market data returns UNKNOWN/BLOCKED, not a guessed label.
- Five consecutive PASS results remain mandatory; the numeric thresholds must be justified and locked before the first scored run.
- The Mentor cannot rewrite BOT_ONLY decisions, outcomes, previous model artifacts, or test labels.
- XP, badge, and Challenge 2 remain locked until all evidence is verified.

## 7. Implementation order

1. Add deterministic tests for the BloFin before pagination boundary and chronological non-overlap.
2. Refactor the baseline runner to use prior-candle features and next-candle labels; update all existing tests that currently expect same-candle classification.
3. Add a training artifact format and hash-chained persistence in the existing learning memory.
4. Amend the assisted-decision memory contract with prior-round and timestamp checks.
5. Add paired BOT_ONLY/BOT_WITH_AI_MENTOR holdout evaluation for rounds 2–5.
6. Add UI/status controls only after the new runner and its tests are complete.
7. Verify on a separate approved staging environment with Turso; do not change the active Render Demo service until separately authorized.

**Do not treat this design document as implementation evidence or permission to deploy.**
