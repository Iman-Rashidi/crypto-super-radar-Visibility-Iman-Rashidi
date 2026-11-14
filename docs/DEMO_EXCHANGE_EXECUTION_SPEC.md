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

## 4A. Native quantitative intelligence and machine-learning core

The bot must have its own quantitative decision engine. Nemotron is an independent adviser and reviewer, not the primary signal generator. This applies to both Aggressive and Strict engines.

### Unified market and feature pipeline

Before a candidate is scored, construct a timestamp-aligned, auditable feature set from validated data:

1. Candlestick structure and patterns: OHLCV geometry, candle bodies and wicks, bullish/bearish reversal and continuation patterns, pattern strength and invalidation. Include registered project patterns such as Shooting Star and Bearish Marubozu where the detection rules and required context are available. A candle pattern alone is never an automatic trade instruction.
2. Technical methods and indicators: trend, momentum, volatility, volume, support/resistance, market structure, breakouts, pullbacks, and leading/lagging indicators. Compute these from market data; educational lesson text is not a numerical signal.
3. Multiple timeframes: separate regime, setup and trigger timeframes; measure alignment and conflict. Use completed candles for final pattern confirmation unless a strategy explicitly models in-progress candles and their repaint risk.
4. Crypto-native fundamentals: supply/emissions, vesting/unlocks, token utility, network activity, fees/revenue where meaningful, active users, TVL where relevant, developer activity, security incidents, governance, concentration and credible capital flows.
5. Asset-aware broader fundamentals: industry/sector analysis, accounting principles, financial statements, earnings, ESG, KGV and PEG are usable only for assets/entities where those measures are relevant and comparable. Never force equity ratios onto tokens or invent missing fundamentals. Mark unavailable/inapplicable values N/A with a reason, not zero or a neutral score.
6. Market microstructure and derivatives: liquidity, spread, order-book depth, volume quality, open interest, funding, basis, liquidation data, whale/on-chain flows and sentiment where the instrument and trusted provider support them.
7. Data quality: every feature records source, observation time, instrument, timeframe, freshness and validity. Stale, misaligned, contradictory or critically missing data reduces eligibility or blocks the candidate.

Patterns, indicators and fundamental metrics must become tested, machine-readable feature definitions with inputs, formula/rules, timeframe, validity conditions and tests. A lesson description alone is not an operational signal.

### Native ML tasks

The internal ML layer may use an evaluated ensemble to perform distinct tasks:

- Market-regime classification: trend, range, volatility and liquidity conditions supported by evidence.
- Setup-quality estimation: estimate whether a combination of candle structure, technical features, fundamentals and context historically produced favourable outcomes.
- Outcome/risk estimation: calibrated estimates such as target-before-stop, expected return, expected adverse/favourable excursion and holding horizon.
- Candidate ranking: compare symbols and engines using expected net outcome, confidence, liquidity and portfolio correlation.

Each candidate must expose its symbol, market, side, signal/confirmation timeframes, feature snapshot, detected patterns, technical/fundamental scores, regime, model/version IDs, calibrated estimates, expected gross/net return, estimated fees/slippage/funding, risk/reward, stop/target rationale and uncertainty.

Rank by expected net expectancy after execution costs, not a bullish/bearish label or raw predicted price. A model probability is an estimate, never a profit promise.

### Training and validation

- Use chronological splits and walk-forward evaluation; prevent look-ahead, label leakage and survivorship bias.
- Use only information available at the historical decision time.
- Include fees, spread, slippage, funding, partial fills and instrument constraints in evaluation.
- Compare with simple baselines and retain out-of-sample results by regime, symbol, timeframe and strategy.
- Calibrate probabilities and monitor precision/recall, expected value, drawdown, turnover and model drift.
- Version datasets, features, models, thresholds and evaluation reports; make every order traceable to exact versions.
- A model cannot rewrite risk limits or promote itself. New models and parameters must pass offline and Demo validation.
- Demo outcomes enter training only after data-quality checks and controlled retraining/evaluation, not after blind reaction to one win or loss.

### Engine profiles

Both engines may share validated data, feature definitions, pattern detection and model infrastructure while retaining separate strategies, thresholds, state and risk budgets.

- Aggressive can evaluate earlier setups, shorter timeframes and alternative feature combinations; it may skip the 12-gate strategy screen but never universal data, account, risk, execution, protective-exit or STOP controls.
- Strict requires all 12 unique mandatory gates to PASS and consumes structured technical, candle, timeframe, fundamental, regime and model evidence relevant to its strategy. Missing, UNKNOWN or stale required evidence fails closed.
- Neither engine is forced to trade. NO_TRADE is a valid outcome.

### Nemotron advisory role

Nemotron receives the bot's structured evidence, feature/model versions, rationale, costs and risk summary. It independently challenges the thesis, identifies contradictions, missing context, event/news risks and reasoning errors, and returns a schema-validated result such as SUPPORT, CHALLENGE, VETO or INSUFFICIENT_EVIDENCE with reasons and timestamp.

- Nemotron does not originate the primary signal, fabricate data or select a trade unsupported by the internal engine.
- A valid veto blocks a candidate. Support cannot override a failed gate, stale data, risk cap, account restriction, exchange rejection or STOP BOT.
- Preserve the current Demo execution requirement for a fresh valid Nemotron review. The review is an independent check, not proof of profitability.
- Log disagreements and later evaluate whether the adviser improves or harms decisions; do not tune the system merely to force agreement.

## 4B. AI supervision, teaching, Notion memory and defect correction

The bot is experimental. It must be expected to encounter uncertainty, incomplete data, incorrect classifications and software defects. A separate supervisory AI workflow reviews those cases and turns verified findings into durable knowledge and tested improvements.

### Supervision triggers

Create a review case when the bot cannot analyse a symbol, timeframe, pattern, fundamental metric or regime; when data is unavailable/stale/contradictory; when the internal model and Nemotron materially disagree; when a forecast or classification materially differs from the observed path; when order/fill/protection/reconciliation differs from the expected lifecycle; or when a runtime exception, failed test, provider regression or model drift appears.

### Supervisory AI responsibilities

1. Reconstruct what the bot knew at decision time using immutable market snapshots, feature values, source timestamps, model versions, strategy profile and account/exchange state.
2. Classify the issue: data/provider problem, missing capability, faulty feature/pattern definition, model weakness, strategy/risk-policy issue, exchange behaviour or code defect.
3. Explain the lesson in machine-readable form: condition, evidence, root cause, corrected interpretation, applicable assets/timeframes/regimes, confidence, limitations and reproducible example.
4. Recommend provider repair, feature/pattern correction, model evaluation/retraining, strategy change, code fix or no change because the outcome was an ordinary probabilistic loss.
5. Create regression tests or labelled training examples where appropriate. A defect is not fixed until a test reproduces the old failure and passes with the correction.
6. Link the original decision, supervisory review, Notion learning record, GitHub issue/PR/commit, CI result and Demo validation evidence.

A losing trade alone is not proof of a programming error. Separate expected statistical losses from faulty logic, bad data, model drift and execution defects.

### Notion learning and defect log

Maintain a Crypto Super Radar learning/defect record in Notion. Each case includes case ID and time, engine/strategy/symbol/market/timeframe, original decision and evidence, model/feature versions, what the bot could not analyse or got wrong, Nemotron and supervisory diagnosis, root-cause category, corrected lesson, affected modules, reproduction steps, regression-test results, GitHub links, CI/Demo evidence, status and training approval/version.

Notion is the human-readable knowledge and audit layer, not the live trading database. Keep execution-critical state in the application database; queue and idempotently retry Notion sync if Notion is unavailable. Notion downtime must never block STOP BOT, protection management or emergency controls.

### Code and model correction

- Reproduce confirmed software defects with a failing test before patching.
- Apply the smallest targeted fix on a non-production development branch.
- Run unit, integration, regression, static/security checks and historical replay against the original and neighbouring cases.
- For model/strategy changes, run walk-forward and out-of-sample evaluation; for exchange changes, run Binance Demo integration tests.
- Attach evidence to GitHub and update the linked Notion case with commit, CI and Demo outcome.
- Promote only after required checks pass. Supervisory AI may analyse, document, propose code changes and prepare branches/PRs, but may not directly modify the running service, change credentials/hosts, weaken safety controls, alter hard risk limits or silently deploy.
- Keep unresolved features/strategies blocked or limited; never invent a lesson or force a trade.

### Continuous improvement loop

Observe -> preserve evidence -> diagnose -> teach -> record in Notion -> create test -> patch on a development branch -> CI and replay -> Demo validation -> controlled promotion -> monitor recurrence.

## 4C. Mandatory two-track training test for every learning case

Every learning scenario must be evaluated in two separate runs. The first measures the bot's unaided ability; the second measures the bot after receiving supervisory AI coaching. This is the required test protocol during the experimental phase.

### Test 1 — BOT_ONLY baseline

- Replay the immutable market/data snapshot at the original decision time with the same starting capital, instrument rules, fee/slippage/funding assumptions and engine configuration.
- The bot independently analyses candles, patterns, technical indicators, timeframes, applicable fundamentals, market regime and risk, then records BUY/LONG, SELL/SHORT or NO_TRADE with rationale, confidence and uncertainty.
- No supervisory AI hints, corrections or learned labels from the same case may enter this run. Freeze and timestamp the bot's complete decision before Test 2 begins.
- This is a diagnostic/shadow/paper test. It must not create an unprotected or unsupervised exchange order. The actual Binance Demo execution path retains its fresh AI review and all universal safety checks.

### Test 2 — BOT_WITH_AI_MENTOR

- Replay the exact same immutable snapshot, decision timestamp, starting capital, market assumptions and engine profile.
- The supervisory AI acts like an experienced trading mentor: it questions the bot's reading of candle patterns, trend, momentum, timeframe alignment, fundamental context, costs, risk/reward and missing evidence.
- The AI explains what is wrong or unknown, provides a corrected interpretation and teaches the bot why, with references to testable features/rules rather than vague instructions.
- The bot must independently re-evaluate the candidate after the coaching and record its revised decision, what changed, what it rejected and why. The mentor must not silently replace the bot's decision.
- Record where AI advice improved the decision, made it worse, was unsupported or was correctly rejected by the bot.

### Comparison and learning outcome

Compare Test 1 and Test 2 on pattern recognition, technical/fundamental correctness, timeframe consistency, calibrated confidence, false positives, missed opportunities, NO_TRADE quality, expected net outcome, drawdown, costs and explanation quality. Report bot-only capability, AI-added value, remaining weaknesses and unresolved unknowns separately for each engine and market regime.

Keep Test 1 immutable so Test 2 cannot contaminate the baseline. Store both outputs and the mentor lesson in Notion under one linked case ID. Only verified lessons may enter a later training dataset. Do not feed Test 2 answers into Test 1 or declare improvement from one isolated result; use repeated cases and out-of-sample evaluation.

After a lesson is approved, add it to the training curriculum and convert confirmed software defects into regression tests and GitHub code changes. A strategy lesson may change model/feature training; a programming defect requires a reproducible test and code fix. Neither is silently applied to the running bot.

## 5. Autonomous execution flow

For each engine independently:

1. Scan exchange-supported Demo instruments and retrieve fresh market, account, fundamental and microstructure evidence.
2. Validate timestamps and data quality; build a feature snapshot from candle patterns, technical methods, multiple timeframes, asset-appropriate fundamentals and supported derivatives/liquidity data.
3. Run internal regime, setup-quality and outcome/risk models; rank candidate signals by expected net outcome after execution costs.
4. Apply that engine's strategy profile. Strict requires all 12 gates; Aggressive uses its documented alternative signal rules and marks the 12-gate screen skipped.
5. Build a traceable decision with feature/model versions, evidence, symbol, market, side, timeframe, entry, order type, size, costs, stop, target, horizon, uncertainty and risk.
6. Request Nemotron's independent review for the exact candidate; require a fresh schema-valid response. A veto or insufficient evidence blocks the candidate.
7. Re-check shared portfolio risk, balances/margin, symbol filters, price drift, protective-order capacity and global STOP state.
8. Submit automatically to Binance Demo without a human per-order approval click.
9. Reconcile order state and partial/full fills; install and verify exchange-native protective exits.
10. Update the dashboard and preserve the decision/outcome evidence for later supervised review, Notion learning records and controlled model evaluation.

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

- Registered candle patterns such as Shooting Star and Bearish Marubozu have tests for detection, context, invalidation and completed-candle behaviour.
- Technical indicators, leading/lagging methods and multi-timeframe features are tested against fixtures and cannot read future candles.
- Asset-aware fundamental mapping uses crypto-native metrics for tokens and applies KGV/PEG/accounting/ESG only when relevant and comparable; missing data is never fabricated.
- ML training/evaluation uses chronological and walk-forward splits, leakage checks, cost-aware labels, calibrated outputs and versioned models.
- Every candidate exposes its feature snapshot, model version, calibrated estimates and expected net outcome; weak candidates can return NO_TRADE.
- Every learning case runs a frozen BOT_ONLY baseline first and a BOT_WITH_AI_MENTOR replay second against the same immutable snapshot.
- Test 1 receives no AI coaching; Test 2 records the mentor's teaching, the bot's own re-evaluation and any accepted/rejected advice.
- Supervisory AI diagnosis, Notion records, regression tests and GitHub changes are linked; a losing trade alone is not classified as a code defect.
- Supervisory AI cannot directly modify or deploy the running service or weaken safety/risk controls.
- Notion outage cannot block STOP BOT or protective-exit management; learning records sync idempotently later.
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

The requirements above do not prove that existing candle-pattern, technical-analysis, fundamental-analysis or educational material is connected to a tested feature pipeline, ML inference, AI-supervision workflow or order decision.
They also do not prove that the BOT_ONLY versus BOT_WITH_AI_MENTOR protocol, Notion learning capture, regression-test generation or controlled code-correction loop is implemented.
Those modules must be audited and wired into the system rather than assumed operational because lesson descriptions or dashboard cards exist.

No claim of implementation, live Demo order placement, test pass or Render deployment may be made until code, automated tests, model-validation tests, two-track learning tests, supervisory workflow tests and Binance Demo integration tests have actually completed.
