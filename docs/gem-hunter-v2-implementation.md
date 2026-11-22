# Gem Hunter V2 — implementation notes

- Independent lightweight scoring engine; no dependency on the Spot/Futures 12-filter qualification pipeline.
- Discovery uses public DEX Screener token profiles and token-pair data. GeckoTerminal exact-pool lookup is spaced by 6.1 seconds, below 10 requests/minute.
- GoPlus and public Solana RPC checks are read-only. Missing security or holder capabilities are NOT_AVAILABLE; missing evidence cannot pass the security gate.
- Weighted score: contract security 25, liquidity 20, buy/sell momentum 20, holder distribution 15, age/activity 10, pool/data quality 10.
- REJECT on confirmed critical security flags or toxic liquidity; WATCH on incomplete critical evidence or insufficient liquidity; EARLY GEM at 70–84 after critical checks; STRONG GEM at 85+ only after at least two consecutive scans.
- CEX symbol matches are advisory only and do not prove contract identity, Germany account access, or MiCA authorization.
- Turso retention: top 10 rows only, 14-day expiry. Public endpoint is read-only and never accepts orders.
- GitHub scheduled workflows execute only from the repository default branch. Until this workflow is present on the default branch, schedule activation is not guaranteed; workflow_dispatch and Staging push triggers remain available. Do not move it to default without explicit scope approval.
