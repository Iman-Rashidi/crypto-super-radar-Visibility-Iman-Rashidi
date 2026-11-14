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
