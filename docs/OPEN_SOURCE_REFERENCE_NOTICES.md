# Open-source reference acknowledgements

This integration uses compatible design patterns from the following public projects. No full application or third-party source file has been copied verbatim; the added Python modules are original adaptations designed for Crypto Super Radar's existing architecture.

- **HyperData Terminal** — Apache-2.0, Copyright (c) 2026 Co-Messi. Reference: https://github.com/Co-Messi/HyperData-Terminal. Adapted ideas: explicit freshness, partial coverage, stale-feed and cross-source drift states; a health report must state its coverage boundary.
- **million** — MIT, Copyright (c) 2026 Kelows. Reference: https://github.com/Kelows/million. Reference ideas: event-driven wallet/token discovery, wallet-quality filtering and explicit experimental boundaries. The Node.js application and Helius-dependent runtime were not copied or embedded.
- **TradeClaw** — MIT, Copyright (c) 2024 TradeClaw Contributors. Reference: https://github.com/naimkatiman/tradeclaw. Adapted ideas: cost-aware realized performance, explicit sample size, expectancy, profit factor, drawdown and a public distinction between modeled evidence and profitability claims.
- **Solbeam** — Reference reviewed: https://github.com/CodeMuscle/solbeam. No source code was copied because a repository license grant could not be verified at review time.

The additions in this repository do not reproduce the full functionality of those projects. In particular, the current Gem Hunter does not yet have a complete token-lifetime swap index, so million-style smart-wallet discovery is not represented as active functionality.
