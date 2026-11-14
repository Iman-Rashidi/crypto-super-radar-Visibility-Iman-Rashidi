# Easy Risk Management and Strategy Diagnostics

This module is informational. It does not submit orders and does not add mandatory gates.

## Implemented

- Position sizing by fixed equity percentage or fixed cash risk amount.
- Stop-distance sizing, optional ATR floor, notional cap, and estimated round-trip fees/slippage.
- Initial-margin estimate from notional and leverage.
- Market-cycle heuristic: accumulation candidate, mark-up, distribution/range, mark-down, transition, or unknown.
- Strategy-style reference metadata for scalping, day trading, swing, and position trading.
- Dashboard payload exposes the cycle and sizing diagnostics.

## Important limitations

- A stop order is not a guaranteed maximum loss. Gaps, thin liquidity, latency and slippage can exceed the estimate.
- The ATR method uses ATR as a minimum stop-distance floor. It does not confuse total position notional with risk budget.
- Leverage does not reduce the underlying loss at a given position size. It changes required margin and liquidation exposure.
- Liquidation price is intentionally not calculated without exchange-specific maintenance tiers, fee schedule, funding, position mode and margin rules.
- Kelly sizing is not implemented. It should not be added without a statistically defensible, out-of-sample win-rate and payoff estimate; even then fractional Kelly is preferable.
- Market-cycle labels are heuristic candidates. Price/volume alone cannot prove institutional accumulation or distribution.
- Trade journal persistence, VaR/stress scenarios, hedge sizing, diversification/correlation controls and automated strategy backtesting by style remain future work.
- No dashboard order controls or real execution were added.

## Safety boundary

All outputs are diagnostics only. They do not bypass, weaken, or replace the existing decision logic. The existing gate architecture remains unchanged in this change.



## Independent Easy profile and portfolio diagnostics

The independent engine is implemented in `crypto_super_radar/easy_engine.py`. The default dashboard scan and the Spot paper bot use this profile. The legacy `WhaleAwareConfirmationEngine` remains in place for Standard/Strict-style workflows and Futures/BloFin Demo paths; this change does not rewrite its 12-gate decision function.

Easy hard requirements:
- Valid positive cross-provider snapshot price.
- Chronological unique OHLCV candle history, sufficient warm-up and a fresh completed daily candle.
- Valid EMA20/EMA50, RSI and ATR so a directional proposal has a computable stop/target.
- Risk-budget sizing and account-level paper controls before a paper entry.

Easy advisory diagnostics:
- Market-cycle phase, support/resistance, trendlines, volume/OBV, Elliott and chart patterns.
- News/sentiment, whale/security, fundamental and market-intelligence results.
- Breakout/retest state, relative volume and portfolio context.
- A missing advisory source must not by itself veto an otherwise valid Easy opportunity.

Additional risk diagnostics:
- Fractional Kelly requires a minimum sample; the calculated amount is capped and is not used as default sizing.
- Historical VaR / Expected Shortfall requires at least 60 observations; its horizon is one input-return period.
- Parallel-shock stress tests, aggregate stop-risk/gross-exposure summary, common-sample return correlations and simple beta hedge sizing.
- These portfolio outputs are diagnostics. They do not automatically rebalance or submit hedge orders.

## Breakout, retest and false-breakout states

`crypto_super_radar/easy_trade_management.py` provides deterministic completed-candle states:
- BREAKOUT_CANDIDATE / BREAKOUT_CONFIRMED
- RETEST_PENDING / RETEST_TOUCHED / RETEST_CONFIRMED
- FALSE_BREAKOUT
- RETEST_EXPIRED
- NO_SETUP / UNKNOWN

Volume confirmation is relative to the prior 20 completed candles. A retest is not considered confirmed on the same candle that created it; a later completed close is required. A false breakout is a warning, not an automatic short signal or a hard gate.

## Easy paper-position manager

SQLite-backed records are exposed through:
- GET `/api/easy/positions` — open positions and recent journal
- GET `/api/easy/positions/events?position_id=...` — append-only lifecycle events
- POST `/api/easy/positions/open` — open a paper-tracked position
- POST `/api/easy/positions/update` — update from a completed OHLC candle and optional ATR
- POST `/api/easy/positions/close` — manual paper close

The manager checks stop-side validity, a default 0.50% per-trade modeled loss budget and 10% gross-notional cap, including estimated round-trip fees/slippage. These limits are read from the shared Easy risk configuration at runtime. Open positions are also evaluated against the newest completed daily candle during a later Easy scan.

Stop behavior:
- HARD: a simulated stop event closes the paper record when a completed candle crosses the stop.
- MENTAL: crossing the stop raises an alert but leaves the position open until manually closed.
- Trailing ATR stops can only tighten toward profit.
- If one OHLC candle touches both the stop and target, the manager applies STOP_FIRST as the conservative intrabar assumption.
- If a completed candle opens through a hard stop, the simulated stop-market fill uses the worse opening price, not the stop trigger. Favorable gaps through a take-profit use the opening price as a better limit-style fill. Real fills can still differ.
- The journal PnL is gross and excludes fees/slippage; it is not an exchange-fill ledger.

## Dashboard / safety boundaries

- The dashboard scan is routed through `EasyModeEngine`; it no longer calls the Legacy 12-gate engine for the Easy scan.
- The Easy Spot paper bot accepts the Easy policy (zero Legacy gates) but still retains stop/target validity, stop-risk sizing, notional cap, duplicate-candle checks, paper account circuit breakers, cost-adjusted reward/risk and its configured AI reviewer.
- The Futures paper bot and BloFin Demo execution path remain on the existing Legacy engine.
- No live exchange order route was added by this work. The position-manager endpoints update only the paper journal. They use same-origin checks; no wallet credentials or private keys are used.
- Paper-position persistence depends on the configured database backend. Without a persistent Render disk or configured Turso backend, local SQLite state can be lost on service replacement.
