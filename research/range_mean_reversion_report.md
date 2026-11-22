# Range Mean Reversion — Offline Ablation Results

No live/demo order writes. Main, staging, Render and active CHALLENGE_1_V2 were not modified.

| Dataset / scored window | Engine | Trades | Win rate | Net PnL | PF | Max DD |
|---|---|---:|---:|---:|---:|---:|
| Replay #109 (6 months) | Trend only | 46 | 63.04% | -7.34 USDT | 0.810 | 2.58% |
| Replay #109 (6 months) | Range only | 48 | 56.25% | +19.54 USDT | 1.452 | 1.11% |
| Replay #109 (6 months) | Dual-Regime | 94 | 59.57% | +12.20 USDT | 1.149 | 3.12% |
| Independent OOS (3.25 months) | Trend only | 38 | 68.42% | +9.98 USDT | 1.341 | 1.22% |
| Independent OOS (3.25 months) | Range only | 26 | 34.62% | -13.80 USDT | 0.592 | 2.48% |
| Independent OOS (3.25 months) | Dual-Regime | 64 | 54.69% | -3.83 USDT | 0.939 | 2.89% |

## Net-cost breakdown

| Dataset | Engine | Gross PnL | Fees | Spread | Slippage | Funding | Net |
|---|---|---:|---:|---:|---:|---:|---:|
| Replay #109 | Range only | +41.12 | 13.48 | 3.87 | 4.30 | -0.07 | +19.54 |
| Independent OOS | Range only | -2.25 | 7.17 | 1.92 | 2.50 | -0.04 | -13.80 |

## Verdict

- PF > 1.15 appears only on Replay #109. It fails on the independent holdout.
- Independent gross Range PnL is already negative before costs. Fees and slippage worsen the loss but are not its only cause.
- Conclusion: not robust. Do not enable or connect this engine to any trading runtime.

## Method and limitations

- Regime: last fully closed 1H ADX(14) < 22. Signals use closed 15m candles; entry is simulated at the next 15m open.
- Entry: Bollinger(20,2) wick penetration with close reclaim, RSI(14)<32 / >68, plus engulfing or hammer/shooting-star confirmation.
- Guard: completed 4H true-range shock >2.5 x prior ATR(14) blocks entries. SL 1.5 ATR. Mid-band TP only when >=1.5R; otherwise skip. Time exit after 10 bars.
- Stops trigger on aligned MARK candles; TP/time exits use LAST candles. Same-bar stop/target ties resolve as stop first. Maximum four concurrent positions, no overlapping same-symbol positions.
- Costs are model assumptions, not a verified account-specific BloFin fee tier: maker 0.02%, taker 0.06%, modelled spread 0.02%, adverse slippage 0.03% on market exits. Historical funding rates are applied to entry notional (not re-marked to each funding-time mark price); FundingKind provenance is schedule-inferred, not explicit exchange metadata.
- Range risk uses fixed 0.25% of initial 1,000 USDT per trade; trend baseline retains original V2 sizing. Baseline trend entries were checked and were all in ADX>=22 regime.
- Replay #109 SHA-256: 98420da9742db289c31aef22a414554fb78c0d8285972c2c56509b159d47c9ca.
- Independent SHA-256: f2dc555dad89a5e1a9cbf1470547fe25f95946f1947e097395b3899c671887ba.
- Independent source spans 2025-10-31 09:00 UTC to 2026-03-09 00:00 UTC (~4.25 months); first month is warm-up. Scored holdout is 2025-11-30 to 2026-03-09 (~3.25 months), not a full 4.5-month scored sample.
- Only 26 independent Range-only trades: insufficient to establish durable profitability.
