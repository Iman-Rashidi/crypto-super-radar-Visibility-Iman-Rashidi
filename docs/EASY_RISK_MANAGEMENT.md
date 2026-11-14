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
