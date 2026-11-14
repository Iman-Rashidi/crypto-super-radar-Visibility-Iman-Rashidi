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
