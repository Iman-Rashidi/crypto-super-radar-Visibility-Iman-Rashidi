# Live Provider Acceptance / آزمون پذیرش زنده منابع داده

This is a bounded, read-only acceptance check for public data sources. It is
separate from the deterministic offline unit-test suite: mocked tests prove
normalization and fail-closed behavior, while this runner checks whether public
providers are reachable and returning current, mutually consistent observations
at the time of execution.

## Safety boundary

- The runner only performs public HTTP GET requests and read-only JSON-RPC methods. Binance Spot uses Binance's documented public-market-data-only host (`data-api.binance.vision`); Ethereum mainnet RPC uses the public endpoint advertised by PublicNode.
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
timestamp, latency breach, or cross-venue discrepancy produces FAIL. HTTP 403,
429, or 451 access/rate-limit responses are INCONCLUSIVE: the runner cannot use
them to validate payload correctness or market freshness, and the result must
be repeated from an authorized network/region. A missing/unavailable required
source also prevents an overall PASS.

Bybit's REST ticker response exposes a top-level `time` field. For Bybit, the
reported freshness timestamp therefore measures the provider response clock,
not the timestamp of an individual last trade. This limitation is made explicit
in the returned check details.

The Binance default funding interval is treated as 8 hours only when Binance's
adjusted `fundingInfo` endpoint has no entry for the selected symbol. Bybit
instrument metadata must provide an interval, and the two values must match.

## GitHub Actions

The dedicated `live-provider-acceptance` workflow runs for changes to this
acceptance tooling on the feature branch / its pull request, and also exposes a
`workflow_dispatch` input for a later operator-initiated run. It uploads the
JSON report even when an acceptance check fails. Treat a non-PASS report as a
provider-readiness blocker; do not weaken thresholds merely to make the workflow
green.

## Interpreting outcomes

- `PASS`: the provider returned valid, fresh evidence within configured thresholds.
- `FAIL`: concrete evidence violated a requirement, such as stale data, bad schema,
  provider error, excessive latency, or cross-venue disagreement.
- `INCONCLUSIVE`: the endpoint replied but did not provide the timestamp needed to
  assert freshness, or the evidence was insufficient for the check.

A PASS is a point-in-time availability snapshot, not proof of sustained uptime,
full historical coverage, economic validity, or trading readiness. Repeat the
acceptance run at multiple times and run a separate paper-trading observation
period before considering any release. Keep the project fail-closed throughout.
