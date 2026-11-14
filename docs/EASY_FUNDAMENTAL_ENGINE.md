# Easy Fundamental Engine

## Scope and safety
This module belongs to the Easy trading engine only. It is an evidence-aware research layer; it does not place orders and its score cannot override the existing mandatory trading gates. Standard and Strict engines are unchanged.

## Seven modules
| Module | Weight | Required evidence |
|---|---:|---|
| Macro | 15% | Explicitly sourced macro score |
| Tokenomics | 15% | Supply, unlocks, distribution and utility analysis |
| On-chain | 15% | Network activity and exchange/whale flows |
| Project fundamentals | 15% | Treasury, protocol revenue/fees, costs and developer/adoption evidence |
| Industry | 15% | Sector growth, share, competition, entry barriers, technology, regulation and adoption |
| DCF | 10% | Forecast net protocol cashflows, discount/growth assumptions, treasury and explicit token value-capture ratio |
| Comparable projects | 15% | Same-sector peers and valid market-cap/FDV-to-revenue/TVL multiples |

Weights are initial product defaults, not empirically calibrated predictive coefficients. They must be validated before being used as a trading filter.

## Implemented API
- `classify_industry(project)`: normalized sector classification; missing sector is UNKNOWN.
- `industry_analysis(evidence)`: averages only explicit 0-100 scores from seven industry criteria; missing criteria remain unknown.
- `discounted_protocol_cashflows(...)`: DCF scenario based on token-attributable net cashflows. Requires explicit token value capture; protocol revenue is not automatically assigned to token holders.
- `comparable_projects(project, peers)`: computes four valuation multiples and peer medians using same-sector peers only; non-positive/missing denominators are excluded.
- `build_fundamental_report(evidence)`: combines the seven modules, applies weights, reports coverage and withholds the total score below 70% coverage.

## Integration status
The Easy integrated engine now attaches `metrics.fundamental_analysis` to each analysis result. It reads optional `data.fundamental_evidence(asset)` if a provider implements it. No such live fundamentals provider is currently configured, so the production-shaped default is UNKNOWN with no score. This is intentional: the dashboard must not display fabricated positive/negative fundamentals.

Fundamental output is descriptive only and is not a thirteenth gate, a trade authorization, or a modification to the existing twelve-gate decision logic. Live data adapters and empirical validation of the provisional weights remain follow-up work. A Persian Fundamental tab is present in the optional Streamlit dashboard, and the default Render HTTP dashboard exposes the same report in its scan result.

## Test coverage
Unit tests cover sector classification, partial/missing evidence, DCF assumptions and token capture, same-sector comparable filtering, coverage threshold, weighted score and the no-trade-authorization invariant.


## Supporting accounting and market-analysis diagnostics

The Easy Fundamental report now exposes two additional supporting modules:

- `accounting_principles`: revenue recognition, inventory valuation and depreciation.
  - Revenue checks distinguish protocol fees, reported revenue and value actually accruing to token holders.
  - Inventory valuation is `NOT_APPLICABLE` only when the project's disclosed operating model supports that conclusion. It is never awarded a positive score.
  - Depreciation checks apply only to material depreciable assets disclosed by the project, such as equipment or capitalized infrastructure/development where relevant.
  - Missing accounting disclosures remain `UNKNOWN`; explicit failed checks remain `FAIL`.
- `market_analysis`: GDP growth, inflation, interest rates, employment, industry trends, regulatory developments, market risks and market outlook.
  - Each known observation must include a value, source and observation timestamp.
  - The panel reports coverage and status but does not invent a market forecast.

These are supporting diagnostics, not extra weighted score modules. The original seven-module weights and 70% minimum coverage rule are unchanged until empirical calibration. Their evidence does not alter the 12 mandatory trade gates, the Standard/Strict engines, or execution behavior. No live macro/accounting data provider is implied by this schema; absent observations remain `UNKNOWN`.


## Event analysis / event study

The Easy Fundamental report exposes `event_analysis` as a supporting diagnostic, separate from the seven weighted modules. It does not change the seven-module weights or 70% coverage threshold and cannot authorize or block a trade by itself.

- Input events require an event identifier/type, a verified source URL, an event timestamp, event-window metadata, and timestamped asset/benchmark return observations.
- At least 20 valid estimation-window observations and one event-window observation are required. Missing or malformed evidence remains `UNKNOWN`.
- A market-model OLS regression estimates normal return: `asset_return = alpha + beta * benchmark_return`.
- Abnormal return is `actual_return - normal_return`; the event-window abnormal returns are summed into CAR.
- CAR significance is evaluated with a two-sided Student-t test at the 5% level, using estimation residual degrees of freedom.
- Limitations are surfaced: window/benchmark sensitivity, possible residual autocorrelation, and confounding simultaneous events. Statistical significance does not prove causality or forecast profit.
- The default DefiLlama provider does not supply event news or benchmark return series. Until a timestamped event/news provider and historical return feed are connected, live reports correctly show `UNKNOWN` rather than fabricate event data.

The HTTP/Render and Streamlit Persian dashboards expose event status, event type, CAR, t-statistic, p-value, 5% significance and source when present.

Tests cover absent event data, a controlled market-model event study, and missing source/timestamp handling.


## Dividend analysis / dividend quality

The Easy Fundamental report exposes `dividend_analysis` as a supporting diagnostic, excluded from the seven weighted score modules. It applies only to explicitly classified equities/stocks/shares/REITs. Crypto/token assets return `NOT_APPLICABLE`, not a negative score. Unknown asset classes remain `UNKNOWN`.

- Computes dividend yield from annual dividend per share / current price, or accepts a sourced yield observation.
- Computes payout ratio (cash dividends / net income), earnings dividend coverage (net income / dividends), and optional free-cash-flow coverage.
- Evaluates consecutive annual per-share dividend history and CAGR only when at least two consecutive positive years are supplied.
- Sustainability flags use transparent diagnostics: payout above 100% is a risk, above 80% is caution; free-cash-flow coverage below 1.0 is a risk and below 1.2 is caution.
- Dividend-trap warning requires sourced yield of at least 8% and a sourced 12-month price decline of at least 20%; it is a warning, not a sell signal.
- Every metric requires a source URL and observation timestamp before its check is marked known. Missing data remains `UNKNOWN`; no synthetic score is generated.
- The Persian Streamlit Fundamental panel displays the module, applicability, key ratios, sustainability, growth, trap warning, evidence checks and source.
- This module does not alter the seven-module weights, 70% coverage rule, twelve mandatory trade gates, or trade authorization.


## M&A analysis / mergers and acquisitions

The Easy Fundamental report exposes `mergers_acquisitions` as an evidence-only corporate-action diagnostic, excluded from the seven weighted modules. For crypto/token assets this company-level module returns `NOT_APPLICABLE`, not a negative score.

- Each deal requires a source URL and announcement timestamp before it can be marked known.
- Captures acquirer, target, deal status, transaction value, financing type, expected synergies, integration risk, regulatory status and indirect competitor impact when supplied.
- Offer premium is calculated as `(offer price per share / unaffected share price - 1) * 100`; it is not a guaranteed target return or completion probability.
- Debt financing, high integration risk and regulatory review/challenge/block are exposed as review flags.
- Rumored, announced, pending, completed, terminated and withdrawn statuses are distinguished. Missing evidence remains `UNKNOWN`.
- The module does not infer deal probability, fair value, causality or a trading signal.

## Sector rotation analysis

The Easy Fundamental report exposes `sector_rotation` as a supporting market diagnostic, excluded from the seven weighted modules.

- Requires timestamped sector returns, benchmark returns and source URLs. Capital-flow change is optional and remains `UNKNOWN` when not supplied.
- Relative strength is calculated as `sector return - benchmark return`; sectors are ranked by this observed relative performance, with leaders and laggards displayed.
- Economic phase accepts early expansion, mid expansion, late expansion or contraction only when accompanied by a source and observation timestamp.
- The cycle-phase/sector examples are contextual, not hard-coded forecasts. Missing sector evidence remains `UNKNOWN`; partial sector coverage is labeled `PARTIAL`.
- No automatic portfolio overweight, allocation change or trade authorization is produced.

Both modules are displayed in the Persian Streamlit Fundamental panel and the default HTTP/Render dashboard. They do not change the seven-module weights, 70% coverage threshold, mandatory trade gates, Standard/Strict engines or order execution.


## Share buyback analysis

The Easy Fundamental report exposes `share_buybacks` as an evidence-only equity diagnostic, outside the seven weighted modules.

- Only completed repurchases are counted. An authorization or announced maximum is not treated as executed activity.
- Inputs can include completed shares repurchased, shares outstanding before/after, average repurchase price, current price reference, cash spent and debt-funded amount.
- Buyback yield is completed shares repurchased divided by pre-buyback shares outstanding. Share-count reduction is calculated from the before/after share counts.
- The dashboard reports debt funding share, repurchase price premium/discount versus the supplied current-price reference and mechanical EPS accretion assuming unchanged net income.
- Debt funding and repurchases above the supplied current-price reference are review flags, not automatic sell signals.
- Mechanical EPS accretion is explicitly not treated as operating earnings growth. Missing source URL or observation time keeps evidence UNKNOWN.

## EPS analysis

The Easy Fundamental report exposes `eps_analysis` as an evidence-only equity diagnostic, outside the seven weighted modules.

- Basic EPS = (net income - preferred dividends) / weighted-average common shares.
- Diluted EPS uses diluted weighted-average shares when supplied.
- Adjusted EPS is calculated from adjusted net income when supplied; it is not substituted for reported EPS.
- P/E = current share price / positive basic EPS. P/E is omitted for zero or negative EPS.
- Historical EPS CAGR and forecast-vs-actual EPS change are displayed only when the required sourced inputs exist.
- EPS increases can result from repurchases or accounting effects; the module does not infer better operating performance from EPS alone.
- Missing source URL or observation timestamp keeps the checks UNKNOWN. Crypto/token assets return NOT_APPLICABLE, not a negative score.

Both diagnostics are shown in Persian Streamlit and HTTP/Render dashboards. They do not change the seven weighted modules, 70% coverage threshold, mandatory trade gates, Standard/Strict engines or order execution.


## ESG Analysis (Easy Fundamental diagnostic)

- Added `esg_analysis(evidence)` with three separate pillars: environmental, social and governance (0–100).
- Requires a source URL and observation timestamp before pillar evidence is considered known.
- The displayed ESG score is an unweighted arithmetic diagnostic average of sourced pillar scores, not a provider rating or calibrated investment score.
- Crypto assets can use the same three pillar framework, while network-specific metrics remain separate: energy per transaction, renewable-energy share, validator/miner concentration, client diversity, uptime and security incidents.
- Controversies are surfaced as human-review flags; they are not automatically converted into a fabricated penalty.
- Missing evidence remains UNKNOWN. ESG is not added to the seven weighted modules and cannot authorize trades.
- UI added to Streamlit and HTTP dashboard. Tests cover sourced company data, missing source, crypto network metrics, unknown asset class and architectural isolation.
