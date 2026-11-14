# Owner-Oriented Autonomous Dual-Engine Binance Demo Bot

**Status: Specification only. Not implemented, tested against Binance Demo, or deployed.**  
**Branch:** `feature/unlimited-human-demo-cycles`  
**Environment:** Binance Demo Spot + USDⓈ-M Futures only

This specification supersedes all earlier drafts in this file. Individual trades do not require human approval. The bot and its AI components autonomously select markets and execute Demo orders. Human control is through STOP BOT.

## 1. Primary objective

The system's objective is to improve the account owner's **net, risk-adjusted Demo account outcome** after fees, slippage, funding and other execution costs. It is not designed to benefit the exchange, maximize exchange volume, or generate trades merely to stay active.

The system must:
- Prefer expected net benefit to trade frequency.
- Stay out of a market when evidence is poor or expected net reward is inadequate.
- Adapt strategy selection to market regime and observed outcomes.
- Compare both engines' results independently and at portfolio level.
- Attribute every decision and outcome to the engine and strategy that produced it.
- Never claim that profits are guaranteed or that Demo outcomes predict Live outcomes.

"Free and intelligent" means freedom to research, compare, switch and optimize strategies within the supported Demo environment. It does not mean bypassing exchange security, account ownership/access controls, exchange order filters, rate limits, regional/legal restrictions, or the account's actual balance and margin.

## 2. Two autonomous engines operating simultaneously

Both engines run concurrently, with distinct strategy state, configuration, risk budgets, order-ID namespaces and performance attribution. They share only the exchange connection, authoritative account reconciliation and the portfolio-wide hard risk guard.

### Engine A — Aggressive / High Risk

- Designed for higher-frequency, earlier and higher-variance Demo experiments.
- The full mandatory 12-gate *strategy confirmation* is not required.
- It may use alternative signals, shorter timeframes, volatility/momentum/flow setups and experimental strategy combinations.
- It must disclose the bypass explicitly as `12_GATE_SCREEN=SKIPPED_BY_AGGRESSIVE_PROFILE`; never label skipped gates PASS.
- It must still obey all universal safety and execution checks in Section 3.
- Initial Demo risk budget: 1.0% of allocated strategy equity per trade, subordinate to the shared portfolio risk ceiling. This is an experimental setting, not a recommendation for Live trading.
- Every event and position is tagged `AGGRESSIVE_HIGH_RISK`.

### Engine B — Strict / 12-Gate

- Every new entry requires exactly one PASS for each mandatory gate ID 1–12.
- Any FAIL, UNKNOWN, missing, duplicate, stale or malformed mandatory gate blocks the entry.
- Requires fresh Nemotron approval and all shared account, risk, price and execution checks.
- Initial Demo risk budget: 0.5% of allocated strategy equity per trade, subordinate to the shared portfolio risk ceiling.
- Every event and position is tagged `STRICT_12_GATE`.

### Parallel operation and conflict handling

- Both engines scan independently and may submit separate Demo orders at the same time.
- Either engine can hold multiple positions simultaneously, including across many symbols.
- Neither engine may overwrite or silently net the other's local strategy lots.
- If both engines signal the same symbol and side, use unique exchange client order IDs and track separate strategy lots while reconciling the exchange's aggregate position.
- If engines signal opposite directions on the same symbol, respect Binance position mode. In One-way mode, block a conflicting order unless a separately tested policy proves the resulting aggregate position is intended. Hedge mode can separate LONG and SHORT only when explicitly configured and validated.
- No artificial application-level lifetime order quota, session quota, service-start quota or fixed position-count cap is allowed.
- Real exchange balance/margin, order/conditional-order capacity, symbol filters, request weights/rate limits and shared portfolio risk remain binding.

## 3. Universal safety checks — mandatory for BOTH engines

The Aggressive engine skips only the strict 12-gate confirmation framework. It cannot skip these controls:

1. **Demo-only routing:** hard-coded Binance Demo host allowlist. Production and legacy testnet hosts are rejected.
2. **Account integrity:** verify the correct Demo account, balances, margin, account mode and permissions. Never use credentials belonging to another environment.
3. **Fresh market state:** valid timestamps, tradable symbol status, current quotes, spread, liquidity/order-book checks and required market metadata. Stale or contradictory data means no order.
4. **Supported instrument:** trade only symbols/products supported by a tested adapter and explicit risk configuration.
5. **Nemotron review:** require a fresh, schema-valid approval for the exact proposed symbol, market type and direction. Missing, stale, malformed, rate-limited, mismatched or vetoed responses block new entries. AI may veto but cannot override code-level safety checks.
6. **Sizing and affordability:** validate quantity steps, minimum notional, price ticks, available Spot balance or Futures margin, leverage and position-mode semantics.
7. **Per-trade and portfolio risk:** enforce engine-specific budgets and shared hard limits for aggregate open risk, exposure, drawdown, daily loss and correlated exposure where evidence exists.
8. **Protective orders:** every filled position must have exchange-confirmed stop-loss and take-profit protection for the actual filled quantity. Partial fills require protection to be sized/adjusted to actual fills.
9. **Idempotency:** same engine + strategy + symbol + side + completed signal candle cannot create duplicate entry orders. Unknown submissions must be reconciled by client order ID before any further action.
10. **Global STOP:** if STOP BOT is active, neither engine may submit new entries.
11. **Audit:** record engine, strategy, selected market, rationale, AI response, risk calculation, exchange IDs, protection IDs, fills and realized result.

The bot must not invent missing evidence or reinterpret UNKNOWN as PASS. The Aggressive engine's distinct strategy is the alternative to the 12-gate model, not an excuse to bypass account or execution safety.

## 4. Owner-oriented intelligence and adaptation

The decision system should optimize for the account owner's net result, not for trade count:

- Discover and rank supported Demo Spot and Futures opportunities.
- Select market, symbol, direction, timeframe, entry type, size, leverage (Futures only), stop and target automatically.
- Compare expected net reward against fees, spread, slippage and (for Futures) funding.
- Evaluate whether an additional position improves the portfolio or merely adds correlated exposure.
- Reduce activity or remain flat when no valid positive-expectancy candidate exists.
- Track results by engine, strategy, symbol, market regime and timeframe.
- Use walk-forward/out-of-sample evaluation before promoting a changed strategy or model configuration.
- Permit adaptive parameter/strategy selection only inside preconfigured risk and safety bounds. Changes to hard risk limits, exchange routing, credential permissions or STOP behaviour are never self-authorized.
- Do not use martingale, unbounded position doubling, loss-chasing, fake volume, wash trading, spoofing, self-trading or attempts to manipulate exchange systems.

The system may work around its own *strategy* weaknesses by choosing another validated method or market. It must not evade exchange controls, authentication, rate limits, legal restrictions or account protection.

## 5. Autonomous execution flow

For each engine independently:

1. Scan exchange-supported Demo instruments and retrieve fresh market/account evidence.
2. Apply that engine's strategy. Strict requires all 12 gates; Aggressive uses its separately documented signal rules and visibly marks the 12-gate framework skipped.
3. Build a decision with symbol, market, side, timeframe, entry, order type, size, estimated costs, stop, target, holding horizon and risk.
4. Request Nemotron review for that exact decision; require a fresh valid approval.
5. Re-check shared portfolio risk, available balances/margin, symbol filters, price drift, protective-order capacity and global STOP state.
6. Submit automatically to Binance Demo. No human order-approval click or queue.
7. Reconcile the exchange order state and partial/full fills.
8. Install and verify exchange-native protective exits.
9. Update the human supervision dashboard and continue managing all open lots independently.

A failed condition returns a structured no-trade/block status and submits no order. A score or AI response cannot override a failed universal check.

## 6. Human supervision: STOP BOT only

Provide one prominent, authenticated, CSRF-protected global **STOP BOT** action. No individual order approval or reject workflow.

When activated:
- Persist the global stop flag atomically.
- Immediately prevent both engines from submitting new entries.
- Cancel queued decisions and pending/unfilled bot ENTRY orders after reconciling their current exchange state.
- Do not cancel or delete exchange-native protective SL/TP orders for open positions.
- Do not automatically flatten open positions merely because the bot is stopped.
- Continue read-only reconciliation and display of existing positions; exchange-native protection must remain active if Render sleeps or disconnects.
- Preserve STOP state across restarts. Resume requires an explicit human Start Bot action.
- A stop request must win over queued/in-flight work that has not submitted an exchange order. Recheck the stop flag in the final order-submission critical section.

If a filled position has missing/rejected/unknown protection, halt new entries from both engines, raise a critical alert and execute only a tested recovery procedure based on reconciled exchange state. Never call a position protected until the exchange confirms the exit protection.

## 7. Binance Demo environment and credentials

Use Binance Demo Trading, not the old Spot Testnet:

- Spot Demo REST base: `https://demo-api.binance.com`.
- USDⓈ-M Futures Demo REST base: `https://demo-fapi.binance.com`.
- Use separate Spot Demo and Futures Demo credentials as server-side Render secrets.
- Hard-pin these allowed hosts in code. Reject `api.binance.com`, `fapi.binance.com`, `testnet.binance.vision` and all non-Demo hosts.
- No production switch, production fallback or production confirmation path may exist in this execution service.
- Use read/trade permissions only, no withdrawals; IP restrictions should be enabled where supported.
- Never expose keys to browser code, logs, GitHub, Notion or chat.

## 8. Spot and Futures behaviour

### Spot Demo

- Spot has no leverage; UI must display `N/A (Spot)`.
- Entries and exits must appear in Binance Demo Spot order/trade history.
- After confirmed fills, create exchange-native protective stop-loss/take-profit using supported Spot OCO/order-list functionality where valid.
- Reconcile list IDs, child IDs, actual filled quantity, partial fills, cancellations, balances and open orders.
- Partial fills must receive protection for the actual executed quantity. Adjust protection only after confirmed additional fills.
- Never label the position protected until Binance confirms the relevant protective orders.

### USDⓈ-M Futures Demo

- Support LONG and SHORT according to the engine decision and configured position mode.
- Use isolated margin and 1x leverage initially; no automatic leverage increase.
- Show leverage, margin mode, side, margin used, mark/entry/liquidation price (when supplied), PnL, funding, stop, target and exchange status.
- Use supported Futures conditional/algo order endpoints for stop-loss and take-profit; reconcile their IDs separately from entry orders.
- Support many open positions across symbols, respecting One-way/Hedge aggregate semantics.
- If opposite engine positions cannot be represented safely by the configured account mode, block the conflicting entry rather than misstate exposure.

## 9. Multiple-position accounting and shared risk

The current Spot and Futures ledgers use singleton position records (`id=1`). Replace them with stable unique position/lot IDs and migrations preserving existing trade/account history.

- Every decision, order, fill, position and trade stores `engine_id` and `strategy_id`.
- Allocate shared exchange balances/margin across both engines without double-counting.
- Track each engine's equity, PnL, exposure and open risk, plus a combined portfolio view.
- Demo target risk budgets: Aggressive 1.0%, Strict 0.5% of allocated engine equity per trade.
- Shared Demo baseline maximum aggregate open risk: 2.0% of strategy equity across both engines. This is a portfolio hard cap, not a position-count cap.
- Before every entry and immediately before order submission, recompute open risk, free balance, used margin, exposure, daily loss, drawdown, rate-limit state and protective-order capacity.
- Manage each lot independently: stop, target, trailing protection where supported, time exit, funding, partial fills and recovery.
- Closing/updating one lot must never mutate or delete another lot, including another engine's lot on the same symbol.
- Use DB transactions, unique constraints and idempotent client order IDs.
- On startup, restore both engines, STOP state, open lots, orders and protective exits, then fully reconcile with Binance before enabling new entries.

## 10. Order lifecycle and failure handling

Persist the lifecycle, for example:

`SCANNING -> STRATEGY_ELIGIBLE -> AI_APPROVED -> PORTFOLIO_RISK_APPROVED -> SUBMITTING -> NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/UNKNOWN -> PROTECTION_PENDING -> PROTECTED -> CLOSED`

- Never retry an order POST after a timeout or ambiguous response.
- On UNKNOWN, query Binance by unique client order ID before further submit/cancel operations.
- Reconcile partial fills and protect only executed quantity.
- Entry acceptance is not proof of protection.
- Halt new entries from both engines if data is stale, account reconciliation fails, protection is missing/unknown, STOP is active or the exchange is unavailable.
- Render disconnect/sleep must not remove exchange-native protective exits.
- Record all decision and execution evidence per engine and strategy.

## 11. Dashboard

- Prominent `BINANCE DEMO` indicator; no Production selection.
- Two side-by-side engine panels: `AGGRESSIVE · HIGH RISK` and `STRICT · 12 GATES`.
- Show scanning state, selected market, symbol, direction, rationale, AI status and blocked candidate reasons.
- Show multiple open Spot/Futures positions with engine ID, exchange order IDs, fill quantity, stop, target, protection status, leverage/margin (Futures only), fees, funding, PnL and last reconciliation time.
- Show per-engine and combined equity, free balance, used margin, aggregate exposure/open risk and historical performance.
- One global STOP BOT action; optional engine-specific pause/resume; no per-order approve/reject.
- Show the Aggressive 12-gate bypass as SKIPPED, never PASS.

## 12. Acceptance tests

- Both engines scan and operate concurrently with isolated strategy state.
- Aggressive may trade a candidate that does not pass the full 12-gate strategy framework, but cannot bypass any universal safety check.
- Strict submits zero orders unless all 12 unique mandatory gates PASS.
- Both engines require a fresh valid Nemotron approval for the same proposed symbol/market/side.
- Valid entries are automatically submitted to Binance Demo without human approval.
- STOP BOT prevents new entries from both engines, cancels pending entries, leaves protective exits active and persists across restart.
- Multiple simultaneous positions per engine and across engines work correctly.
- No artificial lifetime/session/service-start order quota or application position-count cap blocks a valid Demo trade.
- Same signal key is idempotent; new valid signals can open additional positions.
- Shared portfolio exposure/risk cap blocks over-allocation even when each engine is individually within budget.
- Stale quotes, failed AI review, FAIL/UNKNOWN Strict gate, bad symbol filters, insufficient balance/margin, unknown submission or missing protective exits prevent unsafe new entries.
- Spot always reports leverage N/A; Futures reports its actual leverage/margin mode.
- Integration tests use mocks or Binance Demo credentials only and assert production hosts cannot be reached.

## 13. Current implementation gap

This document is a requirements specification only. At capture time the repository includes internal simulated paper ledgers, a Binance Spot adapter that defaults to Spot Testnet, single-position Spot/Futures state, and no complete autonomous dual-engine Binance Demo execution flow with verified exchange-side protective exits.

No claim of implementation, live Demo order placement, test pass or Render deployment may be made until code, automated tests and Binance Demo integration tests have actually completed.
