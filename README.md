# CRYPTO SUPER RADAR

**Quantitative Crypto Intelligence Platform — Persian-first, zero-cost public-data prototype**

CRYPTO SUPER RADAR is a modular Python research engine for swing-trading (days to weeks). It does not search for a single “best coin”. It screens measurable market conditions, validates independent data sources, evaluates a fixed strategy out of sample, and issues a paper-only `LONG`, `SHORT`, or `NO TRADE` decision.

> **Safety rule / قانون اصلی:** all twelve mandatory gates must be `PASS`. Any `FAIL` or `UNKNOWN` means `NO TRADE`. A high score can never override a failed or unknown gate.

> **Zero-cost scope:** the project has no paid API keys, paid data subscriptions, paid database, hosted service, or paid Python dependency. It runs locally using free public endpoints and open-source packages. Public services can impose rate limits, restrict geographic access, change schemas, or become unavailable; €0 does not mean unlimited or guaranteed availability.

## 1. What is implemented

| Stage | English module | توضیح فارسی |
|---|---|---|
| 1 | Data Integrity & Cross-validation | قیمت و حجم از دو منبع مستقل مقایسه می‌شود؛ اختلاف بیش از ۵٪ یا Timestamp نامعتبر، دارایی را از مسیر معامله خارج می‌کند. |
| 2 | Market Regime | روند کلی بازار با کندل‌های روزانه BTC و EMA50/EMA200 و بازده ۳۰ روزه ارزیابی می‌شود. |
| 3 | Trend & Structure | EMA20/EMA50/EMA200 و جهت ساختار قیمت برای Swing بررسی می‌شوند. |
| 4 | Momentum & Relative Strength | RSI14 و قدرت نسبی ۳۰ روزه در برابر BTC سنجیده می‌شوند. |
| 5 | Capital Flow & Liquidity | حجم دلاری، عمق نقدینگی DEX، گردش حجم و در بازار Perpetual، Funding و Open Interest بررسی می‌شود. |
| 6 | On-chain / Contract Risk | در Solana اختیار Mint/Freeze و تمرکز حساب‌ها بررسی می‌شود؛ در Ethereum/Base غربالگری GoPlus و تطبیق دقیق Sourcify اجرا می‌شود. این بررسی ممیزی کامل نیست. |
| 7 | News & Sentiment | تیترهای انگلیسی/فارسی، خبر RSS و متن اجتماعی با واژه‌نامه شفاف تحلیل می‌شوند؛ منبع نامعتبر `UNKNOWN` است. |
| 8 | Initial Out-of-sample Validation | استراتژی EMA20/EMA50 با جداسازی زمانی ۷۰/۳۰، هزینه معاملات، Sharpe، Profit Factor و Drawdown ارزیابی می‌شود. |
| 9 | Portfolio & Trade Risk | اندازه موقعیت، بودجه زیان، محدودیت نقدینگی، نسبت سود به زیان و ریسک باز پرتفوی کنترل می‌شوند. |
| 10 | Whale Intelligence | انتقال‌های بزرگ، جریان ورود/خروج صرافی، کیف‌پول‌های بزرگ و معاملات بزرگ CEX رصد می‌شوند؛ پوشش ناقص سیگنال را مسدود می‌کند. |
| 11 | Full Market Sentiment Coverage | خبر RSS، منبع اجتماعی RSS/متن و شاخص Fear & Greed باید پوشش معتبر داشته باشند. |
| 12 | Multi-fold Walk-forward Stability | چند پنجره آزمون زمانی بدون هم‌پوشانی، هزینه‌ها، Drawdown هر Fold و Funding را اعتبارسنجی می‌کند. |

## 2. Supported markets

Use one of these identifiers:

```text
solana:MINT_ADDRESS     DEX token on Solana
base:0xTOKEN_ADDRESS    DEX token on Base
eth:0xTOKEN_ADDRESS     DEX token on Ethereum
cex:BTCUSDT             Spot instrument; LONG only
perp:BTCUSDT            Perpetual instrument; LONG or SHORT if every gate passes
```

**DEX adapters:** GeckoTerminal V2 + DexScreener public API.

**CEX spot adapters:** Binance Spot + Bybit Spot.

**CEX perpetual adapters:** Binance USD-M Futures + Bybit Linear. Funding and open interest are cross-checked; historical funding is required for the perpetual backtest.

**Market benchmark:** Binance Spot BTCUSDT daily candles.

**Solana security RPC:** public Solana mainnet JSON-RPC.

Base and Ethereum market data can be collected, but the generic EVM security adapter intentionally returns `UNKNOWN`. This avoids pretending that a basic `owner()` or bytecode check proves that a contract is free of mint, proxy, blacklist, honeypot, or privileged-role risks. A verified EVM contract-risk adapter is a prerequisite before those assets can receive trade signals.



> **Source package note / راهنمای دریافت سورس:** برای جلوگیری از ناقص‌بودن ساختار فایل‌ها در مرورگر GitHub، نسخه کامل قابل اجرا در آرشیو `dist/crypto-super-radar-source.tar.xz` قرار دارد. ابتدا آرشیو را در یک مسیر جداگانه استخراج کنید و فرمان‌های نصب و اجرا را از داخل پوشه `crypto-super-radar` اجرا کنید:
>
> ```bash
> mkdir -p /tmp/crypto-super-radar
> tar -xJf dist/crypto-super-radar-source.tar.xz -C /tmp
> cd /tmp/crypto-super-radar
> python -m pip install -r requirements.txt
> python -m crypto_super_radar --test
> ```
>
> **English:** The complete executable package is bundled in the source archive. Extract it first; run commands from the extracted project directory.

## 3. Installation

Requires Python 3.10 or later.

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS / Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

No API key or `.env` file is required for the default public endpoints.

## 4. Run automated tests

```bash
python -m crypto_super_radar --test
```

or:

```bash
python -m unittest discover -s tests -v
```

The offline test suite validates accepted and rejected data, timestamp handling, RSI, local sentiment, the 3-state safety layout, twelve-gate behavior, and preservation of all rows in the Pandas master DataFrame. Tests do not call public APIs and do not incur network costs.

## 5. Run a scan

Spot example:

```bash
python -m crypto_super_radar \
  --asset cex:BTCUSDT \
  --headline "Bitcoin institutional adoption increases"
```

Perpetual example:

```bash
python -m crypto_super_radar \
  --asset perp:BTCUSDT \
  --headline "Bitcoin institutional adoption increases"
```

Multi-market example:

```bash
python -m crypto_super_radar \
  --asset cex:BTCUSDT \
  --asset perp:ETHUSDT \
  --asset solana:YOUR_SOLANA_MINT \
  --asset base:YOUR_BASE_TOKEN_ADDRESS \
  --headline "Institutional adoption increases" \
  --headline "No exploit or withdrawal warning reported"
```

Custom public feeds can be supplied without an API key:

```bash
python -m crypto_super_radar \
  --asset cex:BTCUSDT \
  --news-rss "https://www.coindesk.com/arc/outboundfeeds/rss/" \
  --social-rss "https://www.reddit.com/r/CryptoCurrency/.rss" \
  --social-post "Community reports a major adoption upgrade"
```

Save a CSV report:

```bash
python -m crypto_super_radar \
  --asset cex:BTCUSDT \
  --headline "Bitcoin adoption upgrade" \
  --output radar_report.csv
```

`--headline` and `--social-post` can be repeated. By default the integrated CLI also fetches public news RSS, public social/community RSS and the Alternative.me Fear & Greed Index. Override feeds with `--news-rss` and `--social-rss`. If any of the three required sentiment sources is missing, Gate 11 is `UNKNOWN` and no trade is allowed. The text scorer is a transparent keyword baseline, not a trained NLP model.

### Optional Persian Streamlit dashboard (local installation)

Streamlit is an optional UI dependency; it is not installed by the base CLI or the default Render service.

```bash
python -m pip install -e ".[dashboard]"
streamlit run crypto_super_radar/streamlit_app.py
```

The dashboard reads the same `AnalysisResult` as the CLI and builds Pandas DataFrames for all 12 gates, FAIL/UNKNOWN data errors, RSI/EMA/ATR and backtest evidence, whale-flow observations, sentiment and Fear & Greed. It does not create synthetic values for missing evidence.

The default `render.yaml` continues to launch the lightweight HTTP dashboard in `web_app.py`; it has not been switched to Streamlit or deployed. Its build command extracts the bundled legacy engine modules, overlays the current tracked Feature-branch package files, and then installs only `requirements.txt`. This is necessary because the complete source distribution includes compatibility modules retained in the archive. Auto-deploy remains disabled, and this blueprint has not created or changed a Render service. The optional Streamlit runtime is not a mandatory dependency on the constrained Render Free service. Run Streamlit locally first and measure memory use before considering a hosted Streamlit service.

## 6. Global throttling and cross-validation

A shared process-wide, thread-safe singleton limiter spaces requests by host. GeckoTerminal uses a conservative **one request every six seconds** (approximately 10 requests/minute), based on its public API documentation. DexScreener and exchange endpoints also have conservative per-host spacing. HTTP 429 and transient 5xx responses are retried with bounded delays.

Each DEX asset is checked against GeckoTerminal and DexScreener. CEX spot assets are checked against Binance and Bybit; perpetual instruments are checked against Binance USD-M and Bybit Linear, including a funding-rate discrepancy check. A 5% price mismatch rejects the asset. For DEX data, price **and** volume mismatches above 5% reject the asset. CEX trading volumes are not compared across venues because each exchange reports its own traded volume; each venue's turnover is screened against the minimum independently.

A provider returning no data, an unreadable candle timestamp, a missing pool creation timestamp, or insufficient history does **not** become zero, fresh, or safe. The corresponding gate is `UNKNOWN`.

## 7. Signal and risk policy

- The initial confirmation threshold is 75/100; it is a tunable research parameter, not a probability of profit.
- **Risk Score** is the model's estimated risk on a 0–100 scale (higher means riskier); **Opportunity Rank** is a separate 0–100 ranking output; **Confidence Level** is raw mandatory-gate coverage adjusted for whale/sentiment disagreement. Confidence is not a probability of profit. Their separate calculation paths do not prove statistical independence or zero correlation; that requires historical validation.
- The default heuristic modifiers are centralized in `IntelligenceScoringConfig`: confirmation uses whale +5/-10 and sentiment +3/-3 for aligned/opposed evidence; risk adds +10 for opposed whale flow, +3 for neutral whale flow, +5 for opposed sentiment, and +1 for neutral sentiment; opportunity uses whale +4/-6 and sentiment +2/-3. Confidence deducts 15 for opposed whale flow, 5 for neutral whale flow, 10 for opposed sentiment, and another 5 when whale and sentiment point in opposite directions. These are transparent starting weights, not learned coefficients; they must be calibrated and re-tested out of sample before being treated as an edge.
- Estimated risk is separate from confirmation score; lower is better.
- Entry/Stop/Target use daily ATR14. Default SL distance is 2×ATR and TP distance is 4×ATR (2:1 nominal reward/risk before gaps and execution effects).
- Paper sizing starts from a configurable simulated equity of $10,000 and a 0.50% risk budget per trade. It does not read a real account balance.
- Position notional is capped by simulated equity and a conservative liquidity/volume participation proxy.
- `LOW_RISK` is only a relative model label. It is not a guarantee of capital safety.
- Spot and DEX markets cannot issue a short-sale signal. `SHORT` is considered only for a verified perpetual market with funding and open-interest evidence.
- The scanner never submits exchange orders, connects to wallets, signs transactions, or stores private keys.

## 8. Important limitations before real capital

1. **EVM contract security is a minimum screen, not an audit.** GoPlus risk fields and exact Sourcify source matching are required; missing fields remain `UNKNOWN`. Proxy contracts, privileged controls, honeypot flags, high taxes or holder concentration can fail the screen. Liquidity locks, upgrade governance and every possible attack path are not certified.
2. **Solana security is a minimum screen, not an audit.** Mint and Freeze authority status plus top-10 token-account concentration do not prove LP locking or eliminate hidden program risks. Token accounts are not the same as beneficial wallet owners; LP/exchange accounts can distort concentration.
3. **Order-book depth is not available in the public DEX adapters.** DEX liquidity is a pool-reserve proxy. CEX size caps use quote-volume as a conservative proxy, not a measured order-book slippage curve.
4. **Sentiment is a transparent baseline.** The integrated CLI fetches public RSS and Alternative.me Fear & Greed, and can consume public social RSS or supplied social text. It does not use paid X/Twitter APIs, does not infer missing sources, and is not a trained NLP model. It cannot claim a statistically measured *sudden* attention spike without a historical baseline.
5. **Backtesting is not proof of a persistent edge.** The original 70/30 OOS gate is supplemented by a fixed EMA20/EMA50 multi-fold validation gate. Perpetual multi-fold validation remains `UNKNOWN` if funding history does not cover every required day. Survivorship-bias controls, token delisting history, regime-specific analysis, realistic spread/impact, liquidation mechanics, and portfolio-level correlation/tail-risk stress tests remain future work.
6. **Free APIs are not SLAs.** Providers can change rate limits, schemas, availability, and geographic access. The rate limiter reduces risk but cannot guarantee the IP will never be throttled.

Do not deploy this prototype as an unattended live trading bot. First complete live-provider verification, historical market-universe reconstruction, realistic execution simulation, and a sustained paper-trading observation period.

## 9. Professional references

- GeckoTerminal V2 public API: <https://api.geckoterminal.com/docs/index.html>
- DexScreener API reference: <https://docs.dexscreener.com/api/reference>
- Binance Spot API: <https://github.com/binance/binance-spot-api-docs>
- Binance USD-M Futures API: <https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api>
- Bybit V5 API: <https://bybit-exchange.github.io/docs/v5/intro>
- Solana JSON-RPC: <https://solana.com/docs/rpc/http>
- GoPlus Token Security API: <https://docs.gopluslabs.io/>
- Sourcify verification API: <https://docs.sourcify.dev/>
- Alternative.me Crypto Fear & Greed API: <https://alternative.me/crypto/api/>


## 10. Whale intelligence, sentiment and multi-fold validation (new research modules)

The feature branch adds the following explicit research APIs:

| Module | Capability | Important boundary |
|---|---|---|
| `whale_intelligence.py` | Classifies large transfers, exchange inflows/outflows and labelled-entity movements | A transfer is not proof of a buy or sell; unknown labels remain unattributed |
| `evm_transfers.py` | Bounded ERC-20 Transfer-log scanning on Ethereum and Base public JSON-RPC | Requires a valid USD price to size whales; block-range and log-count caps prevent uncontrolled RPC use |
| `solana_whales.py` | Samples recent transactions for the largest SPL token accounts | Explicitly partial coverage; not a full historical Solana indexer or proof of beneficial ownership |
| `sentiment.py` | Persian/English news/social/positioning keyword scoring, Fear & Greed normalization and divergence flags | Transparent baseline; not a trained NLP model |
| `sentiment_feeds.py` | Public RSS headlines and Alternative.me Fear & Greed API | Free public sources with caching/rate limiting; no paid news feed |
| `market_sentiment.py` | Combined news RSS + social RSS/text + Fear & Greed | Missing social coverage keeps the sentiment gate PARTIAL/blocked |
| `evm_security.py` | GoPlus token-security checks plus Sourcify source verification | Requires exact Sourcify match; proxy/admin and missing fields are not assumed safe |
| `walk_forward.py` | Non-overlapping chronological out-of-sample folds, costs and per-fold drawdown | Requires a causally generated return series; historical funding is mandatory for perpetuals |
| `intelligence_engine.py` | Fail-closed combination of whale, sentiment, market, derivatives, security and validation states | Any mandatory `FAIL` or `UNKNOWN` blocks the research signal |
| `integrated_engine.py` | Connects whale, sentiment and 12-gate walk-forward checks to the legacy engine | اتصال ماژول‌ها به موتور اصلی و ۱۲ فیلتر اجباری |
| `cex_whales.py` | Cross-venue large-trade flow from Binance and Bybit | تشخیص جریان معاملات بزرگ در دو صرافی |
| `enhanced_cli.py` | Integrated CLI entry point with RSS configuration | رابط خط فرمان یکپارچه |
| `whale_store.py` | Local SQLite history for repeated whale-flow observations | ذخیره محلی تاریخچه حرکت نهنگ‌ها |
| `web_app.py` | Persian on-demand dashboard, job polling and history endpoint | داشبورد وب فارسی برای اسکن و تاریخچه |

### Whale-flow interpretation

- Net exchange inflow is a **potential increase in sell-side supply**, not evidence that the coins were sold.
- Net exchange outflow can indicate custody or accumulation, but does not prove spot buying.
- EVM top-holder watchlists can be derived from GoPlus holder percentages and token supply; Solana token-account owners are resolved through public RPC. Both are partial samples, not a complete wallet universe.
- A wallet is only identified as an exchange, fund, market maker or smart-money entity when an upstream label is explicitly supplied. The engine does not invent wallet identities.
- Solana sampling only inspects a bounded recent subset of the largest token accounts. Its coverage status stays `PARTIAL`; the unified engine will not treat that as a complete whale confirmation.

### Example: offline sentiment and transfer analysis

```python
from crypto_super_radar.whale_intelligence import analyze_whale_activity
from crypto_super_radar.sentiment import analyze_market_sentiment, detect_sentiment_divergence

whales = analyze_whale_activity(normalized_transfer_rows)
sentiment = analyze_market_sentiment({
    "news": ["Institutional adoption increases"],
    "social": ["Community reports a major exploit"],
})
divergence = detect_sentiment_divergence(
    sentiment.get("score"),
    whales.get("net_exchange_flow_usd"),
)
print(whales)
print(sentiment)
print(divergence)
```

### Release boundary

These modules are committed on the feature branch and covered by offline tests. They are **research components**, not yet evidence of a live end-to-end production scan. No live market scan, sustained wallet monitoring, or unattended trading has been run. EVM and Solana wallet watchlists are bounded top-holder samples; CEX whale flow is a recent large-trade sample, not wallet attribution. The integrated CLI/report path is wired to these modules. It still requires live-provider verification and sustained Paper Trading before the separate Render service is created. The CI workflow rebuilds the bundled source archive and publishes it as an artifact; the original archive in the repository is not treated as updated until that CI job succeeds.




## 14. Market overview and scenario calculator

The Streamlit dashboard includes an OHLC candlestick view, per-asset and benchmark regime labels (bullish / bearish / neutral / unknown), and observed high/low extrema for 7, 30, 90 and 365 calendar-day windows. A range is shown as `KNOWN` only when timestamps, OHLC consistency, freshness and minimum window coverage pass validation; insufficient history, stale candles, duplicate timestamps or malformed bars remain `UNKNOWN`. These extrema are observed historical ranges, not guaranteed support/resistance levels.

The Spot scenario calculator is an unleveraged long-only arithmetic tool. For investment (I), assumed price move (r), buy/sell fee fractions (f_b,f_s), and buy/sell slippage fractions (s_b,s_s):

`V_net = I × (1-f_b) × (1-s_b) × (1+r) × (1-s_s) × (1-f_s)`

`Net P/L = V_net − I`

Break-even price appreciation is `1 / ((1-f_b)(1-s_b)(1-s_s)(1-f_s)) − 1`. The user-selected horizon is a scenario label only; the calculator does not estimate the time or probability of reaching a target. Taxes, EUR/USD FX changes, custody/network charges and market impact beyond the supplied slippage assumptions are excluded. The calculator never submits orders and does not relax any of the twelve mandatory research gates.

## 13. RSS provider coverage semantics

A configured RSS set is reported as `KNOWN` only when every configured feed returns at least one titled item and none of the feeds fails. If some feeds return usable headlines while another fails or returns no titled items, the source is `PARTIAL`; the full-market-sentiment gate must remain blocked. Whitespace-only manually supplied social text is not accepted as social coverage. This is a coverage control, not a claim that RSS feeds represent the entire news or social universe.

---

**CRYPTO SUPER RADAR** — Discover Early. Analyze Smarter. Manage Risk.


## 12. Optional AI headline enrichment (Gemini)

AI is an **optional explanatory layer**, not part of the deterministic scoring or trade-decision path. The engine continues to calculate technical indicators, whale evidence, sentiment baseline, Risk Score, Opportunity Rank, Confidence Level and all 12 mandatory gates when AI is disabled or unavailable.

To enable it on Render without changing the Blueprint or committing a secret:

1. Create a Gemini API key in [Google AI Studio](https://aistudio.google.com/apikey).
2. In the Render Dashboard, open the existing CRYPTO SUPER RADAR service and add these variables under **Environment**:
   - `GEMINI_API_KEY`: the private key from AI Studio
   - `CSR_AI_PROVIDER`: `gemini`
   - `CSR_GEMINI_MODEL`: `gemini-2.5-flash`
3. Save the environment changes when you are ready to apply them. This repository change does not create, edit, or deploy a Render service.

For local use, copy the optional examples from `.env.example` into an untracked `.env` file and load them through your own process environment. Never commit keys or expose them in browser code.

The Gemini Developer API currently lists a free tier for Gemini 2.5 Flash, but limits and model availability can change. Google's free-tier terms state that submitted content may be used to improve its products; this integration therefore sends only public headline text, never private user or trading data. Do not enable it unless those terms are acceptable.

```text
GEMINI_API_KEY=<server-side secret>
CSR_AI_PROVIDER=gemini
CSR_GEMINI_MODEL=gemini-2.5-flash
```

`GEMINI_API_KEY` is intentionally optional. Without it, AI enrichment reports `DISABLED`; the regular sentiment pipeline continues normally. The gateway sends only a bounded set of public news headline titles/summaries to Gemini. It does not send user-supplied social posts, wallet addresses, portfolio holdings, private keys, or trade plans.

The response is constrained to a small JSON schema (summary, themes, risk flags, conflicting evidence). Invalid JSON, schema violations, authentication failures, quota errors, timeouts, and provider outages become `UNKNOWN` enrichment; none can change numeric sentiment, open a gate, or override `NO TRADE`. Results are cached in-process for 15 minutes. Render Free may clear this cache when the service sleeps or restarts.

**Privacy and quota note:** Gemini API free-tier eligibility, model availability, rate limits and data-use terms can change. Check the current Google AI Studio/API terms before enabling it. Do not send confidential or proprietary material to a free-tier provider unless its current data-use terms are acceptable. No provider key is included in this repository and no Render environment variable or service has been changed.
