# Replay #102 — Four controlled experiments (2026-11-21)

- Branch: `feature/unified-pattern-intelligence-v3`
- Parent: `7520f83805eca06b9908b32ded9283503a0f04ce`
- Dataset artifact: `14683812864`
- Dataset SHA-256: `906e98a9688b1845330d080abc833165d28efa835b95145aeadb8332c395f2e3`
- Window: 2026-04-21 04:30 UTC to 2026-10-21 04:15 UTC
- Funding/Spread: Funding retained from exact SQLite artifact; spread 0.0002 is a modeled assumption, not historical L2.
- V3 pattern intelligence: telemetry-only Shadow; no V3 entry veto.
- No order writes, no Render deployment, no Production changes.

## Results

| Scenario | Trades | Win rate | Gross PnL | Costs | Net PnL | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|
| Baseline, TIME_EXIT=12, TP1 1.2R/50% | 47 | 63.83% | +5.6970 | 13.0345 | -7.3375 | 0.8101 | 2.5771% |
| Exclude SOL, TIME_EXIT=12 | 31 | 64.52% | +9.4970 | 8.3330 | +1.1640 | 1.0482 | 1.9214% |
| ETH/SOL stop diagnostics | ETH: 6 SL / 14 trades; SOL: 7 SL / 16 trades | — | — | — | — | — | — |
| TP1 disabled (all symbols) | 47 | 63.83% | -1.8469 | 13.0336 | -14.8804 | 0.6126 | 2.6431% |
| TP1 moved to 1.5R (all symbols) | 47 | 63.83% | +6.9827 | 13.0512 | -6.0686 | 0.8430 | 2.5394% |

## Stop / Whipsaw

- ETH average stop distance across all trades: 0.7816% / 2.0540 ATR; SL-exit subset: 0.7262% / 1.5696 ATR. Four of six SL exits returned to entry within the next 24 closed 15m MARK candles (6h).
- SOL average stop distance across all trades: 0.8275% / 2.0979 ATR; SL-exit subset: 0.8620% / 2.0147 ATR. Four of seven SL exits returned to entry in the same diagnostic horizon.
- Recovery after the stop is a price-path diagnostic, not proof a wider stop would have profited or that the position should have been reopened.

## TP1 accounting

- Baseline TP1 triggered on 11/47 trades. Partial realized gross +16.7584 USDT; TP1-specific exit fee 0.2614 USDT (already included in total fees).
- Disabling TP1 reduced net PnL by 7.5429 USDT, but this is not a fee-only effect: it also disables the partial exit, break-even stop adjustment, and related trailing behavior.
- 1.5R improved full-period net by 1.2689 USDT versus 1.2R, but remained negative and PF below 1.

## Walk-forward / acceptance

- No-SOL full-window result is positive (+1.1640, PF 1.0482), but TRAIN PnL is -11.2520 and TRAIN+VALIDATION is still -10.9589; the OOS result is only 8 trades and the exclusion hypothesis was derived after reviewing this dataset.
- No configuration has positive combined TRAIN+VALIDATION. No strategy configuration is accepted or promoted to the default five-symbol watchlist.
- V3 remains SHADOW telemetry only. `bootstrap_web.py` is preserved.
