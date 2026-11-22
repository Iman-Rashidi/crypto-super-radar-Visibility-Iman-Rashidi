# Gem Hunter V2 — implementation notes

- Independent lightweight scoring engine; no dependency on the Spot/Futures 12-filter qualification pipeline.
- Discovery uses public DEX Screener token profiles and token-pair data. GeckoTerminal exact-pool lookup is spaced by 6.1 seconds, below 10 requests/minute.
- GoPlus and public Solana RPC checks are read-only. Missing security or holder capabilities are NOT_AVAILABLE; missing evidence cannot pass the security gate.
- Weighted score: contract security 25, liquidity 20, buy/sell momentum 20, holder distribution 15, age/activity 10, pool/data quality 10.
- REJECT on confirmed critical security flags or toxic liquidity; WATCH on incomplete critical evidence or insufficient liquidity; EARLY GEM at 70–84 after critical checks; STRONG GEM at 85+ only after at least two consecutive scans.
- CEX symbol matches are advisory only and do not prove contract identity, Germany account access, or MiCA authorization.
- Turso retention: top 10 rows only, 14-day expiry. Public endpoint is read-only and never accepts orders.
- The default-branch dispatcher is now installed on main and always checks out staging/challenge1-integration-2026-11-16. Its schedule is 17 */2 * * *; the Staging copy retains the push/manual triggers. The existing Challenge 1 15-minute workflow is separate and unchanged.


## Deployment verification note — 22 November 2026

- PR #73 merged to the Staging branch; PR CI and Staging offline regression suite passed. The source archive refresh workflow publishes the updated package bundle after a successful Staging run.
- The first scanner run stored up to 10 WATCH candidates in the GitHub Actions Turso context; no EARLY GEM or STRONG GEM was promoted without complete critical security evidence.
- The Render Staging service was deployed, and the existing read-only Challenge 1 public snapshot health check returned HTTP 200. The new Gem Hunter public endpoint returned a database OperationalError during its first smoke test; the latest code adds a non-sensitive error classification so the exact cause can be identified on the next deployment. Do not mark the public Turso read path complete until the endpoint reports status OK.
- Gem Hunter remains read-only and has no order execution, BloFin Demo, Telegram, or Challenge 1 strategy imports.
