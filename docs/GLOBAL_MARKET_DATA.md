# Global Market Data — US, Europe, ETFs, FX and Metals

## Purpose and status

This module adds a curated traditional-markets data board beside the existing crypto radar. It is a **research and feed-observation layer only**. It does not create equity, ETF, FX or commodity trading signals, does not create Paper positions, and cannot submit live orders.

The first watchlist contains 25 instruments:
- US equities: AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, JPM.
- US ETFs: SPY, QQQ, DIA, IWM.
- European equities: ASML, SAP, SIE, AZN, MC, NESN.
- FX: EUR/USD, GBP/USD, USD/JPY, USD/CHF, EUR/GBP.
- Precious metals: XAU/USD and XAG/USD.

The watchlist is a deliberate starting subset, not coverage of every listed security or exchange.

## Provider and configuration

Provider: Twelve Data.

Set the API key only in the deployment secret store:
- Primary variable: `TWELVEDATA_API_KEY`
- Compatibility alias: `TWELVE_DATA_API_KEY`

The adapter uses the `Authorization: apikey ...` request header. It does not expose the key in dashboard responses or store it in Git, logs, or Notion. The Twelve Data key is separate from `OPENROUTER_API_KEY`.

Official plan scope changes over time; confirm the current entitlement in the provider account:
- Basic: 8 API credits per minute and 800 per day; real-time US equities/ETFs, FX, crypto and reference data; international securities are trial-symbol scoped.
- Grow: 20+ markets and EOD global equities/ETFs, including European coverage; no daily request cap on the published individual plan.
- Pro: 70+ markets and real-time EU market data.
- Commodity access begins on Grow in the current individual-plan matrix.

Sources:
- https://twelvedata.com/pricing
- https://twelvedata.com/exchanges
- https://twelvedata.com/docs/mcp
- https://support.twelvedata.com/en/articles/9935903-us-equities-market-data

The provider describes its Basic US real-time equity source as using venues that collectively represent approximately 5% of total US trading volume. Treat it as a useful research feed, not a consolidated SIP quote or proof of best execution.

## API

- `GET /api/global-markets`: returns the cached board or starts an asynchronous refresh.
- `GET /api/global-markets?refresh=1`: requests a refresh if one is not already running.
- Cache TTL: 3600 seconds.
- Minimum refresh interval: 3600 seconds, including manual refresh attempts.
- In-process daily reservation ceiling: 700 symbol requests (below the published Basic 800/day cap); failed attempts are conservatively reserved until UTC reset. Process restarts reset this in-memory counter, so this is not a substitute for a provider-side quota monitor.
- Requests are paced at 7.6 seconds per Twelve Data host, below the published Basic 8-credits/minute limit. A full 25-symbol pass can take several minutes; the first response is normally `LOADING`.

Each item reports:
- asset class, region, symbol, MIC code, display name and plan floor;
- native-currency price, quote timestamp, age and market-open flag where supplied;
- provider, freshness, and data status.

## Fail-closed statuses

- `CONFIGURATION_REQUIRED`: API key is absent.
- `LOADING`: first collection or refresh is still running.
- `DAILY_BUDGET_OR_REFRESH_COOLDOWN`: the hourly refresh cooldown or conservative daily reservation ceiling prevents another call.
- `OBSERVED_SINGLE_SOURCE_NOT_VALIDATED`: all requested symbols returned one-provider observations.
- `PARTIAL_SINGLE_SOURCE`: some symbols returned observations, others did not.
- `ENTITLEMENT_REQUIRED`: the configured plan does not include that market or instrument.
- `RATE_LIMITED`: provider credit limit was reached.
- `UNKNOWN` / `STALE`: quote or timestamp was missing, invalid or too old.

No global-board status sets `trade_eligible=true`; the board always emits `trade_eligible=false` and `orders_submitted=false`. A one-provider quote is not cross-validation.

## Existing Paper Trading boundary

The existing Paper Bot remains CEX Spot LONG-only. Its allowlist now includes 12 major crypto symbols that already have explicit conservative CEX security handling:
BTC, ETH, SOL, XRP, BNB, ADA, DOGE, AVAX, DOT, LTC, TRX and GRAM (USDT pairs).

The current SQLite Paper ledger allows only one open position at a time. The new US/EU/ETF/FX/metal board is not connected to this ledger or to the existing crypto 12-gate engine. Do not represent global-market observations as paper-tradeable signals.

## Required next engineering gates before Paper Trading in traditional markets

1. Independent price-source validation and licensed feed entitlement for every exchange and instrument.
2. Exchange calendar, timezone, session, auction, halt, holiday and delayed/EOD handling.
3. Currency conversion with timestamped FX rates; corporate actions, split/dividend adjustments and symbol changes.
4. Market-specific signal contracts and 12-stage risk gates. Crypto-only on-chain/whale filters must not be copied blindly to equities or FX.
5. Chronological walk-forward/OOS testing with fees, spread, slippage, borrow/financing and currency costs.
6. Multi-position, multi-currency Paper portfolio ledger with asset-level and portfolio-level risk limits, reconciliation and restart recovery.
7. AI second-opinion schema for listed securities, FX and commodities, with deterministic risk controls remaining authoritative.
8. Deployment acceptance tests against the exact release SHA.

Until these gates pass, the traditional-market panel is research-only and the bot does not place or simulate trades in those markets.
