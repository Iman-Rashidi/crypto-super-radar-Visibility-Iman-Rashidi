# V3 training and pattern audit — 2026-11-21

## Executive verdict

**RESEARCH_CONTINUES** — no strategy promotion, no live/demo order writes, no Render deployment, no production/default changes.

This audit separates the two learning systems that were previously both described as "the AI":

1. The V2 `OnlineMetaBrain` was **not trained** on Replay #109. It had 25 matured TRAIN labels, but requires at least 100 total labels and at least 20 samples in each class. It completed zero training rounds.
2. The V3 `ShadowPatternBrain` **was trained**. It used 25 matured TRAIN outcomes (15 wins, 10 losses) and completed two training rounds. It remained telemetry-only and did not veto any entry.

The V3 filter effect was **not** tested as an executable research filter. The current sandbox deliberately forces every V3 observation to `SHADOW_ONLY`; the actual entry-veto integration rejects any V3 observation that is not Shadow. We did not bypass this guard.

## Scope and provenance

- Repository: `Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi`
- Branch: `feature/unified-pattern-intelligence-v3`
- Branch HEAD at audit: `9bb7a33492781111ecee45d36bcb3cb4b75a5f56`
- Strategy code commit used for the replay logic: `ec0d094f10a8bbd0b92cf9d12e27723adef8aaa8`; the relevant `ai_sandbox.py`, `adaptive_brain.py`, and `pattern_intelligence_v3.py` blobs match the audited branch code.
- Replay #109 artifact: `14707189176`
- Replay #109 SQLite SHA-256: `98420da9742db289c31aef22a414554fb78c0d8285972c2c56509b159d47c9ca`
- SQLite size: 45,268,992 bytes; 237,520 candles, 3,200 funding rows, 5 instrument metadata rows.
- Market window: 2026-04-21 16:00 UTC to 2026-10-21 15:45 UTC.
- Cost model: 0.02% modeled spread (not historical order-book data); actual fee/funding/slippage fields from the fixed replay artifact.
- No future-candle leakage was reported by the Replay #109 look-ahead audit; the model freeze and purge audit passed.

## Scenario A versus Scenario E rerun

Scenario E was rerun locally against the exact Replay #109 SQLite above, with:
- SOL stop = 2.25 ATR
- ETH stop = 1.625 ATR
- TP1 = 1.2R, 50% partial close
- TIME_EXIT = 12 bars
- 0.02% modeled spread
- Existing risk cap and two-position simulator limit unchanged

| Metric | A — Baseline | E — Scenario E, V3 Shadow |
|---|---:|---:|
| Closed trades | 46 | 48 |
| Wins / losses | 29 / 17 | 33 / 15 |
| Win rate | 63.04% | 68.75% |
| Gross PnL before costs | +5.4779 | +15.5677 |
| Fees | 8.7385 | 8.8149 |
| Funding | 0.1543 | 0.2026 |
| Modeled spread | 2.3826 | 2.4266 |
| Slippage | 1.5404 | 1.3699 |
| Total costs | 12.8158 | 12.8140 |
| Net PnL | -7.3380 | +2.7537 |
| Profit Factor | 0.8101 | 1.0873 |
| Maximum drawdown | 2.5772% | 1.7225% |
| TP1 fills | 11 | 13 |
| SL / TIME_EXIT / TP exits | 22 / 19 / 5 | 21 / 22 / 5 |

Average net winning trade in Scenario E was +1.0394 USDT; average net losing trade was -2.1031 USDT. The average loss was therefore about twice the average win.

### Scenario E walk-forward

| Phase | Trades | Wins / losses | Net PnL | PF | Max DD |
|---|---:|---:|---:|---:|---:|
| TRAIN | 25 | 15 / 10 | -7.1045 | 0.6341 | 1.0621% |
| VALIDATION | 11 | 8 / 3 | +1.5496 | 1.2140 | 0.6630% |
| OOS | 12 | 10 / 2 | +8.3087 | 2.6999 | 0.4843% |

The OOS result contains only 12 trades. It is encouraging but too small to establish repeatability. The positive later phases do not erase the negative TRAIN result.

### Scenario definitions requested for the V3 experiment

| ID | Requested experiment | Execution status |
|---|---|---|
| A | Baseline | Available from the fixed Replay #109 artifact |
| B | Scenario E with V3 disabled | Not run separately; the sandbox has no V3-off switch |
| C | Scenario E with V3 trained in Shadow | Replayed; V3 Shadow learner trained, but V2 OnlineMetaBrain did not |
| D | Scenario E with V3 Research Filter | Not run; no separate research-only entry-veto path currently exists |
| E | Scenario E with trained filters and separate risk controls | Not run; the same research-filter path is missing |

B and C are expected to have identical actual trade outcomes under the current architecture because V3 output is forced to Shadow and is excluded from entry authorization. This is an architectural expectation, **not** a separately measured B result. D/E were not fabricated using post-hoc subtraction because suppressing a trade can free a portfolio slot and alter later fills; subtracting tagged trades would not be a valid replay.

## Training audit

### V2 OnlineMetaBrain

- Model: `csr-online-meta-sgd-v1`
- `trained=false`
- Matured TRAIN labels: 25 (15 wins, 10 losses)
- Required total warm-up: 100
- Required minimum per class: 20
- Training rounds: 0
- V2 model weights remained at zero and Validation/OOS model probabilities were unavailable.

The proposed `warmup_trades=15` is not a valid setting for this implementation: the constructor rejects values below 100. Lowering it to force `trained=true` would bypass the configured evidence requirement and is not recommended.

### V3 ShadowPatternBrain

- `trained=true`
- Matured TRAIN labels: 25 (15 wins, 10 losses)
- Required warm-up: 20
- Required minimum per class: 5
- Training rounds: 2
- Frozen before Validation/OOS: yes
- Score is explicitly uncalibrated; it is not a probability.

Important limitation: this brain learns six binary **pattern-family** features, not individual pattern names. It cannot independently learn a dedicated coefficient for "Symmetrical Triangle" or "Tweezer Bottom". The separate V3 gate currently has a hard-coded toxic-name list (`symmetrical triangle`, `tweezer bottom`, `rising wedge`) and a fixed score floor. That is not the same as those specific pattern names having been statistically learned as harmful.

The V3 learner currently does not persist a dedicated model version, training timestamp, or model hash. This metadata gap should be fixed before future model promotion.

## Pattern evidence in Scenario E TRAIN

These are **trade-level associations**: a trade is tagged when that pattern name appeared in the entry-time pattern detections. A trade may carry several pattern tags and may therefore appear in multiple rows. Counts are small and do not establish causality.

| Pattern | Tagged trades | Wins / losses | Win rate | Tagged-trade net PnL | PF |
|---|---:|---:|---:|---:|---:|
| Symmetrical Triangle | 4 | 2 / 2 | 50.00% | -4.8754 | 0.0003 |
| Tweezer Bottom | 7 | 5 / 2 | 71.43% | -2.4257 | 0.0145 |
| Rising Wedge | 12 | 7 / 5 | 58.33% | +0.6226 | 1.0641 |

The three labels overlap. Their combined unique set contains 16 trades, 10 wins and 6 losses, net -1.8004 USDT, PF 0.8517. The Rising Wedge result is not evidence of toxicity in aggregate; its outcomes also differ by regime. These findings do not justify hard-blocking any of the three patterns yet.

Regime split for the same TRAIN tags:

| Pattern | Regime | Trades | Wins / losses | Net PnL |
|---|---|---:|---:|---:|
| Symmetrical Triangle | Trending | 1 | 0 / 1 | -2.4435 |
| Symmetrical Triangle | Ranging | 3 | 2 / 1 | -2.4318 |
| Tweezer Bottom | Trending | 4 | 3 / 1 | -2.4169 |
| Tweezer Bottom | Ranging | 1 | 1 / 0 | +0.0003 |
| Tweezer Bottom | High volatility | 2 | 1 / 1 | -0.0091 |
| Rising Wedge | Trending | 8 | 4 / 4 | -5.8385 |
| Rising Wedge | Ranging | 3 | 3 / 0 | +6.4790 |
| Rising Wedge | High volatility | 1 | 0 / 1 | -0.0180 |

At family level in TRAIN, the liquidity-sweep family appeared in 11 tagged trades (7 wins, 4 losses, expectancy -0.2510 USDT). The lifecycle tracker recorded 10 invalidations after confirmation across those 11 evaluated family observations (90.91%). This is a lifecycle diagnostic, not a per-name false-positive rate and not proof that all associated trades should have been blocked.

The V3 shadow gate recorded 32 hypothetical veto suggestions and **zero actual V3 entry vetoes**. It did not produce a closed counterfactual trade book, so losses avoided and winners missed remain unknown.

## Independent historical data attempt

- Historical workflow Run: `44914587555` (#111), at strategy code commit `2f15d0049fd507af408b44aa257805bebda8c848`.
- Download request: 13 calendar months of public BloFin Demo market data.
- Downloader result: failed closed with `historical candle series contains 1 missing 15m intervals`.
- Sandbox result: skipped because download exit code was non-zero.
- Artifact: `14710454107`.
- Artifact SQLite contains zero candle rows, zero funding rows and zero instrument rows; its SHA-256 is `73fc9bc4415309a155b837c5f6c15a5f3298542dd29615f0f6bbc15cf1ffa390`. This is an empty failed-run artifact, **not** a replay dataset.
- The current downloader reports the number of gaps but not the exact missing candle timestamp. The current sandbox loader rejects any missing price-candle interval. Existing blackout handling applies to a known **funding-data** gap, not to missing price candles.

Therefore no clean older five-month window or price-candle Blackout was validated in this run. We must not mark the missing candle as an accepted Blackout until its exact timestamp, common-symbol coverage, open-position handling, and indicator re-warm-up have explicit implementation and tests.

## CI validation

On the audited branch:
- Full offline suite Run `44914587548` (#2511): **930 tests passed**; Streamlit smoke test succeeded.
- Focused walk-forward/time-exit suite: 13 tests passed.
- Risk, funding, replay and look-ahead suite: 68 tests passed.
- V3 pattern suite: 15 tests passed.
- Historical replay job's compile and focused regressions succeeded, but its data download failed and the Sandbox was skipped.

These tests validate existing code contracts; they do not validate profitability or the missing independent dataset.

## Safety and change log

- Research branch only.
- No strategy code changed in this audit report commit.
- No strategy defaults changed.
- No `bootstrap_web.py` changes.
- No Render deploy, Production changes, environment or secret changes.
- No Demo or Live order writes.
- No change to `feature/blofin-demo-adapter`.
- V3 continues to report `SHADOW_TELEMETRY_ONLY_NO_ENTRY_VETO`.
- Challenge 1 remains OPEN / NOT PASSED.

## Required next research work

1. Add exact timestamps to candle-gap diagnostics, then obtain a date-bounded older window that does not overlap Replay #109; validate all five symbols and all required timeframes before writing an artifact.
2. Do not lower V2's 100-sample/20-per-class threshold just to make the model appear trained. Accumulate sufficient real, matured, cost-reconciled TRAIN outcomes.
3. Give V3 pattern learning a version, timestamp, hash, and per-pattern-name evidence contract; retain family-level features as a separate layer.
4. Implement a separately named, sandbox-only V3 counterfactual filter mode with causal TRAIN-only fitting, frozen Validation/OOS weights, and full re-simulation of fills and portfolio-slot reuse. Do not weaken `_actual_entry_veto` or change the production Shadow default.
5. Repeat on a truly independent historical window. PF > 1.25 remains a target, never an optimization constraint or a pass condition by itself.

**Final status: `RESEARCH_CONTINUES`.**


## Post-commit verification — 2026-11-21

The audit commit itself triggered a fresh CI cycle:
- Full offline suite Run `44916421481` (#2512): **930 tests passed**, Streamlit smoke succeeded.
- Focused TIME_EXIT/look-ahead workflow Run `44916421476` (#55): success.
- Historical Replay Run `44916421490` (#112), at audit commit `ff041a2cf0fa5343666c68fdc345e1e2416be2c4`: the same one-missing-15m-interval error occurred; sandbox remained skipped.
- Latest failed-run artifact: `14711593683`; SQLite contained zero candles, zero funding rows and zero instrument rows; SHA-256 remained `73fc9bc4415309a155b837c5f6c15a5f3298542dd29615f0f6bbc15cf1ffa390`.

This repeat confirms the data-acquisition blocker remains present on the committed branch. The workflow still does not disclose the exact missing timestamp, so a price-candle Blackout cannot be registered or validated from the current evidence.
