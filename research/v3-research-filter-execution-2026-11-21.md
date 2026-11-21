# V3 Research Filter execution audit — 2026-11-21

## Scope and safety

- Branch: `feature/unified-pattern-intelligence-v3`
- Base source HEAD audited: `8378ff4e75561e077edcd17692a53524ceba07c7`
- Dataset #109: verified immutable SQLite SHA-256 `98420da9742db289c31aef22a414554fb78c0d8285972c2c56509b159d47c9ca`
- All experiments ran in `OFFLINE_VIRTUAL_BLOFIN_DEMO_MODEL`; `network_order_writes=false`. No Demo or Live orders were sent.
- No Production service, BloFin adapter branch, default product risk settings, `bootstrap_web.py`, or Render deployment was changed.

## 1. Historical candle-gap investigation

The original Run #111 and #112 artifacts (#14710454107 and #14711593683) both contain the old unhelpful error:

```
historical candle series contains 1 missing 15m intervals
```

Those old artifacts do not contain a symbol, timestamp, or candle row around the gap. The exact timestamp of that particular single-candle event therefore cannot be recovered from those logs alone.

The current branch source has upgraded diagnostics to emit symbol, timeframe, `missing_from_utc`, `missing_to_utc`, interval count, and the candles on either side. Evidence-backed later gaps found in downloader artifacts include:

| Symbol | Timeframe | Missing from UTC | Missing through UTC | Count |
|---|---|---|---|---:|
| BTC-USDT | 15m | 2026-03-09 07:45 | 2026-03-09 12:30 | 20 |
| BTC-USDT | 1H | 2026-03-09 07:00 | 2026-03-09 11:00 | 5 |
| BTC-USDT | 4H | 2026-03-09 04:00 | 2026-03-09 08:00 | 2 |
| ETH-USDT | 15m | 2026-03-09 07:45 | 2026-03-09 12:30 | 20 |
| ETH-USDT | 1H | 2026-03-09 07:00 | 2026-03-09 11:00 | 5 |

The current implementation uses exact evidence-backed blackout windows, blocks new entries during the affected interval and indicator re-warm, and marks positions whose path crosses an unobservable price interval as Ambiguous without manufacturing price or PnL. Existing tests cover BTC/ETH window scoping, entry blocks, re-warm, ambiguous positions, and no synthetic candles. This change adds a regression test proving that one previously unseen 15-minute gap reports its exact symbol and UTC timestamp.

## 2. V3 model and isolation audit

The source already implements `ShadowPatternBrain` with six pattern-family features plus individual `pattern_name::...` features. It stores `model_version`, UTC training timestamp and SHA-256 model hash, then freezes the model before Validation/OOS. In the recorded scenarios B–E, the same TRAIN model identity was used:

- model version: `csr-v3-pattern-name-logistic-sgd-v2`
- training examples: 25
- total features: 32 (26 individual pattern names)
- model hash: `9fa8cd2e845259fb211209a608ec42057e91f6c31c19ee4da9130df64f8fa47e`

The default remains `SHADOW_TELEMETRY_ONLY_NO_ENTRY_VETO`. The `--v3-research-filter` path is opt-in and confined to offline replay. The aggregate brain score uses trained family and name features; the final gate also retains explicit fixed rules for selected toxic patterns, liquidity sweeps, priority-direction bonus, and a global score floor. Per-name coefficients are therefore learned, but the final veto policy is not purely learned per-name.

## 3. Replay #109 A–E results

All rows below use the immutable Replay #109 dataset; A is baseline, B and C are Scenario E with Shadow (C is the repeatability control), D is Scenario E with the opt-in V3 filter at 0.25% per-position risk, and E adds the 0.125% per-position risk cap (two nominal slots, 0.25% combined).

| Scenario | Closed trades | Win rate | Net PnL (USDT) | Profit Factor | Max DD |
|---|---:|---:|---:|---:|---:|
| A — Baseline | 46 | 63.04% | -7.33796 | 0.81008 | 2.5772% |
| B — Scenario E, Shadow | 48 | 68.75% | +2.75372 | 1.08729 | 1.7225% |
| C — Shadow repeatability control | 48 | 68.75% | +2.75372 | 1.08729 | 1.7225% |
| D — Research Filter | 34 | 64.71% | -1.63222 | 0.93270 | 1.5435% |
| E — Research Filter + lower risk | 34 | 64.71% | -1.00497 | 0.91629 | 0.7605% |

B and C match. D and E each record 32 blocked entry signals, 14 closed vetoed candidates in the counterfactual book, and 6 replacement entries. All five scenarios have a calculated overall PF below both 1.10 and 1.25.

Phase results:

| Scenario | TRAIN: trades / net / PF | VALIDATION: trades / net / PF | OOS: trades / net / PF |
|---|---|---|---|
| A | 23 / -16.02855 / 0.33799 | 11 / -0.99075 / 0.89669 | 12 / +9.68135 / 3.00242 |
| B | 25 / -7.10452 / 0.63414 | 11 / +1.54956 / 1.21402 | 12 / +8.30868 / 2.69995 |
| C | 25 / -7.10452 / 0.63414 | 11 / +1.54956 / 1.21402 | 12 / +8.30868 / 2.69995 |
| D | 25 / -7.10452 / 0.63414 | 4 / -2.66514 / 0.44863 | 5 / +8.13743 / not defined (zero losses) |
| E | 25 / -3.53700 / 0.63150 | 4 / -1.32820 / 0.44805 | 5 / +3.86023 / not defined (zero losses) |

D/E OOS has only five trades. Since all five won, an OOS PF is undefined, not infinite evidence of a robust edge. The smaller trade count materially reduces confidence.

## 4. Independent five-month dataset — blocked

The scheduled historical workflow requested six months of source candles (one technical warm-up month plus five scored months) from 2025-10-15 through 2026-04-21. Two retrieved artifacts contain partial SQLite files of 41,857 candle rows and only one instrument (BTC-USDT); downloader execution failed before the approved five-symbol basket was complete. The partial files have SHA-256 values:

- `6bf142578364dec6f3e0600dbd2f69b57d5010306fde6dd5a6a047784d7cd7dc`
- `2f8d35d3f9989af4f6d0fadc61ff54f37071dfcba40ddf95199a3419d27ae586`

Neither is accepted as a five-month independent dataset; neither was replayed. `independent_5m_status=NOT_RUN_INDEPENDENT_DOWNLOAD_UNAVAILABLE`. No synthetic candle, interpolation, or inferred funding data was substituted.

## 5. Validation decision

- Replay #109: research matrix completed successfully, but V3 Research Filter worsened net PnL and PF versus Scenario E Shadow.
- Independent five-month validation: not completed; incomplete one-symbol SQLite files are rejected.
- PF thresholds: neither 1.10 nor 1.25 reached by any B–E variant.
- Current decision: `RESEARCH_CONTINUES`. V3 remains Shadow in product defaults; no promotion to a trading candidate is justified.
- Required next gate: complete full five-symbol independent history with a verified hash, rerun A–E on that exact independent file, and re-check fold isolation, OOS sample size, counterfactual consistency, and blackout recovery.

## 6. Test additions in this commit

- Single missing 15m candle diagnostic test: asserts exact symbol, timeframe, UTC timestamp, missing count, and neighbor candle timestamps.
- Per-pattern-name learner test: proves a separately named feature receives a learned TRAIN-only coefficient.


## Supplemental finding from fresh download Run #148 — 21 November 2026

The fresh historical workflow reached the downloader but correctly refused the independent dataset because it found an additional unregistered price gap in the approved watchlist:

- Instrument: `ETH-USDT`
- Timeframe: `4H`
- Missing candles: `2026-03-09T04:00:00Z` and `2026-03-09T08:00:00Z` (2 intervals)
- Left candle: `2026-03-09T00:00:00Z`; right candle: `2026-03-09T12:00:00Z`
- Evidence: Run #148 artifact `14715766802`, `downloader.log`

The partial file `independent_5m.sqlite3` is only 8,151,040 bytes, has 41,857 price candles all for BTC-USDT, 453 funding rows, one instrument record, and passes SQLite quick_check. Its SHA-256 is `3608f3638844e6796f65e6e1ecaac97327719f09c7ef6f8b30d82fcb9f340638`. It is **not** accepted as an independent dataset because it does not contain the full five-symbol basket.

The workflow intentionally preserved the Replay #109 matrix after download failure: `download_exit_code=1`, `research_suite_exit_code=0`, `independent_5m_status=NOT_RUN_INDEPENDENT_DOWNLOAD_UNAVAILABLE`; the final enforcement gate then correctly failed the overall historical workflow. Replay #109 A-E ran and reproduced the previously reported metrics. No independent A-E result exists in this run.

The exact ETH-USDT 4H window has now been added as a separate evidence-backed Price-Candle Blackout. It remains isolated from other symbols and timeframes; no missing candle was synthesized. The corresponding tests assert exact bounds, count, left/right observations and blackout lookup. Re-run of the downloader is required before claiming an independent dataset.
