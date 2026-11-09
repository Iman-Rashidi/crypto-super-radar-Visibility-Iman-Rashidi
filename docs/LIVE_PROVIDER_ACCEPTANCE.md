# Live Provider Acceptance / آزمون پذیرش زنده منابع داده

This is a bounded, read-only acceptance check for public data sources. It is
separate from the deterministic offline unit-test suite: mocked tests prove
normalization and fail-closed behavior, while this runner checks whether public
providers are reachable and returning current, mutually consistent observations
at the time of execution.

## Safety boundary

- The runner only performs public HTTP GET requests and read-only JSON-RPC methods.
- Binance Spot uses Binance's documented public-market-data-only host (`data-api.binance.vision`); Ethereum mainnet RPC uses PublicNode's public endpoint.
- It does not sign transactions, connect wallets, read exchange accounts, create
  orders, or need private/API credentials.
- It does not run the trading/research signal pipeline and cannot authorize a trade.
- It checks BTCUSDT by default. Change the symbol only to another public market
  instrument supported by both exchanges.

## Run locally

From the repository root, install the existing project dependencies and run:

```bash
python -m pip install -r requirements.txt
python -m scripts.live_provider_acceptance --symbol BTCUSDT \
  --output reports/live-provider-acceptance.json
```

Exit codes are `0 = PASS`, `1 = FAIL`, and `2 = INCONCLUSIVE`. The JSON report
is printed to stdout and optionally written to the supplied output path.

## Checks

| Area | Acceptance evidence |
|---|---|
| Binance Spot | BTCUSDT ticker schema, positive last price/turnover, recent exchange close timestamp |
| Bybit Spot | V5 success envelope, exactly one symbol result, positive last price, recent server timestamp |
| Spot cross-check | Symmetric Binance/Bybit price difference no greater than 5% |
| Binance USD-M | Positive mark price, finite signed funding rate, source timestamp and funding interval metadata |
| Bybit Linear | V5 success envelope, positive mark price, finite signed funding rate, source timestamp and instrument funding interval |
| Perpetual cross-checks | Mark-price difference no greater than 5%; funding-rate difference no greater than 0.0002; matching settlement intervals |
| Ethereum and Base RPC | Expected chain ID, valid latest block number and recent block timestamp |
| Solana RPC | `getHealth = ok`, positive confirmed slot and recent block time |
| Alternative.me | Fear & Greed value in 0–100 and a timestamp no older than 72 hours |
| News and social RSS | Valid RSS/Atom XML, at least one headline and a publication timestamp no older than 72 hours |

Default thresholds are 5 seconds for a check's total latency, 180 seconds for
market/RPC data freshness, and 72 hours for daily sentiment/feed observations.
The latest source timestamp is compared with the UTC receive time recorded by
the runner. A missing trustworthy timestamp produces `INCONCLUSIVE`, not PASS.
An invalid schema, non-access-related provider error, stale data, future
timestamp, latency breach, or cross-venue discrepancy produces FAIL. HTTP 403
(access/WAF denial), 429 (rate limit), and 451 (legal access restriction) are
INCONCLUSIVE: the runner cannot use them to validate payload correctness or
market freshness. Retry a 429 only after the provider's backoff requirement;
repeat 403/451 checks from an authorized network/region. A missing/unavailable
required source also prevents an overall PASS.

The observed GitHub-hosted run was placed in Azure `East US 2`. Bybit's current
V5 integration guidance states that US and Mainland China IPs are restricted
and can receive HTTP 403. This explains the Bybit result in that run; it does
not establish whether the same APIs are reachable from the intended production
region. Do not route around a provider's regional controls. Run the same
read-only command from the authorized deployment region instead.

Bybit's REST ticker response exposes a top-level `time` field. For Bybit, the
reported freshness timestamp therefore measures the provider response clock,
not the timestamp of an individual last trade. This limitation is made explicit
in the returned check details. See the [Bybit V5 ticker response reference](https://bybit-exchange.github.io/docs/v5/market/tickers).

The [Binance Spot REST documentation](https://developers.binance.com/en/docs/products/spot/rest-api)
specifically directs public-market-data-only requests to
`https://data-api.binance.vision`. The Ethereum endpoint
`https://ethereum-rpc.publicnode.com` is the public mainnet RPC published by
[PublicNode](https://ethereum.publicnode.com/).

The Binance default funding interval is treated as 8 hours only when Binance's
adjusted `fundingInfo` endpoint has no entry for the selected symbol. Bybit
instrument metadata must provide an interval, and the two values must match.

## Integrated 12-gate live research scan

Run the full read-only research engine after installing the project dependencies:

```bash
python -m scripts.live_research_scan --asset cex:BTCUSDT \
  --output reports/live-research-scan-spot.json
python -m scripts.live_research_scan --asset perp:BTCUSDT \
  --output reports/live-research-scan-perpetual.json
```

The runner is deliberately limited to a single CEX spot/perpetual symbol. Its
JSON output for both spot and perpetual markets includes all twelve gate results,
decision blockers, source evidence, the final `LONG`/`SHORT`/`NO TRADE` research result,
and explicit safety fields
confirming that no order was submitted and no transaction was signed. A scan
that cannot establish all twelve mandatory gates must remain `NO TRADE`.
A `PASS` gate summary is only evidence that the required gates passed at that
point in time; it is not a guarantee of profitability and does not authorize
execution.

## GitHub Actions

The dedicated `live-provider-acceptance` workflow runs for changes to the
acceptance tooling and the relevant provider adapters on the feature branch /
its pull request. It rebuilds and extracts the complete source bundle, runs
focused tests for both runners, runs the live provider checks, executes the integrated 12-gate spot and perpetual
research scans, and uploads all three JSON reports as one artifact.
It exposes a `workflow_dispatch` input for a later operator-initiated symbol
selection. The workflow intentionally remains blocked unless provider
acceptance is fully `PASS` and the integrated scan completes. An
`INCONCLUSIVE` provider result is not promoted to `PASS`; repeat from an
authorized network/region rather than weakening a threshold.

## Interpreting outcomes

- `PASS`: the provider returned valid, fresh evidence within configured thresholds.
- `FAIL`: concrete evidence violated a requirement, such as stale data, bad schema,
  provider error, excessive latency, or cross-venue disagreement.
- `INCONCLUSIVE`: the endpoint did not provide enough evidence to decide, its
  timestamp was missing, or access was denied/throttled in the test environment.

A PASS is a point-in-time availability snapshot, not proof of sustained uptime,
full historical coverage, economic validity, or trading readiness. Repeat the
acceptance run at multiple times and run a separate paper-trading observation
period before considering any release. Keep the project fail-closed throughout.
