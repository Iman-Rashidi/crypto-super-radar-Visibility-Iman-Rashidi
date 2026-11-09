# CRYPTO SUPER RADAR

**Quantitative Crypto Intelligence Platform — Persian-first, zero-cost public-data prototype**

CRYPTO SUPER RADAR is a modular Python research engine for swing-trading (days to weeks). It does not search for a single “best coin”. It screens measurable market conditions, validates independent data sources, evaluates a fixed strategy out of sample, and issues a paper-only `LONG`, `SHORT`, or `NO TRADE` decision.

> **Safety rule / قانون اصلی:** all nine mandatory gates must be `PASS`. Any `FAIL` or `UNKNOWN` means `NO TRADE`. A high score can never override a failed or unknown gate.

> **Zero-cost scope:** the project has no paid API keys, paid data subscriptions, paid database, hosted service, or paid Python dependency. It runs locally using free public endpoints and open-source packages. Public services can impose rate limits, restrict geographic access, change schemas, or become unavailable; €0 does not mean unlimited or guaranteed availability.

## 1. What is implemented

| Stage | English module | توضیح فارسی |
|---|---|---|
| 1 | Data Integrity & Cross-validation | قیمت و حجم از دو منبع مستقل مقایسه می‌شود؛ اختلاف بیش از ۵٪ یا Timestamp نامعتبر، دارایی را از مسیر معامله خارج می‌کند. |
| 2 | Market Regime | روند کلی بازار با کندل‌های روزانه BTC و EMA50/EMA200 و بازده ۳۰ روزه ارزیابی می‌شود. |
| 3 | Trend & Structure | EMA20/EMA50/EMA200 و جهت ساختار قیمت برای Swing بررسی می‌شوند. |
| 4 | Momentum & Relative Strength | RSI14 و قدرت نسبی ۳۰ روزه در برابر BTC سنجیده می‌شوند. |
| 5 | Capital Flow & Liquidity | حجم دلاری، عمق نقدینگی DEX، گردش حجم و در بازار Perpetual، Funding و Open Interest بررسی می‌شود. |
| 6 | On-chain / Contract Risk | در Solana اختیار Mint/Freeze و تمرکز ۱۰ حساب بزرگ توکن بررسی می‌شود؛ برای EVM فعلاً وضعیت امنیت `UNKNOWN` است و معامله مسدود می‌شود. |
| 7 | News & Sentiment | تیترهای ورودی با واژه‌نامه محلی امتیازدهی می‌شوند؛ نبود تیتر، `UNKNOWN` است و خبر زنده جعل نمی‌شود. |
| 8 | Walk-forward Strategy Validation | یک استراتژی ثابت EMA20/EMA50 با جداسازی زمانی ۷۰/۳۰، هزینه معاملات، Sharpe، Profit Factor و Drawdown ارزیابی می‌شود. برای Perpetual هزینه Funding تاریخی نیز وارد می‌شود. |
| 9 | Portfolio & Trade Risk | اندازه موقعیت، بودجه زیان، محدودیت نقدینگی، نسبت سود به زیان و ریسک باز پرتفوی کنترل می‌شوند. |

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

The offline test suite validates accepted and rejected data, timestamp handling, RSI, local sentiment, the 3-state safety layout, nine-gate behavior, and preservation of all rows in the Pandas master DataFrame. Tests do not call public APIs and do not incur network costs.

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

Save a CSV report:

```bash
python -m crypto_super_radar \
  --asset cex:BTCUSDT \
  --headline "Bitcoin adoption upgrade" \
  --output radar_report.csv
```

`--headline` can be repeated. In this release, headlines are supplied by the operator and analyzed locally. There is no automatic news crawler or paid NLP model. If no headlines are supplied, Gate 7 is `UNKNOWN`, so the engine does not trade.

## 6. Global throttling and cross-validation

A shared process-wide, thread-safe singleton limiter spaces requests by host. GeckoTerminal uses a conservative **one request every six seconds** (approximately 10 requests/minute), based on its public API documentation. DexScreener and exchange endpoints also have conservative per-host spacing. HTTP 429 and transient 5xx responses are retried with bounded delays.

Each DEX asset is checked against GeckoTerminal and DexScreener. CEX spot assets are checked against Binance and Bybit; perpetual instruments are checked against Binance USD-M and Bybit Linear, including a funding-rate discrepancy check. A 5% price mismatch rejects the asset. For DEX data, price **and** volume mismatches above 5% reject the asset. CEX trading volumes are not compared across venues because each exchange reports its own traded volume; each venue's turnover is screened against the minimum independently.

A provider returning no data, an unreadable candle timestamp, a missing pool creation timestamp, or insufficient history does **not** become zero, fresh, or safe. The corresponding gate is `UNKNOWN`.

## 7. Signal and risk policy

- The initial confirmation threshold is 75/100; it is a tunable research parameter, not a probability of profit.
- Estimated risk is separate from confirmation score; lower is better.
- Entry/Stop/Target use daily ATR14. Default SL distance is 2×ATR and TP distance is 4×ATR (2:1 nominal reward/risk before gaps and execution effects).
- Paper sizing starts from a configurable simulated equity of $10,000 and a 0.50% risk budget per trade. It does not read a real account balance.
- Position notional is capped by simulated equity and a conservative liquidity/volume participation proxy.
- `LOW_RISK` is only a relative model label. It is not a guarantee of capital safety.
- Spot and DEX markets cannot issue a short-sale signal. `SHORT` is considered only for a verified perpetual market with funding and open-interest evidence.
- The scanner never submits exchange orders, connects to wallets, signs transactions, or stores private keys.

## 8. Important limitations before real capital

1. **EVM contract security is deliberately incomplete.** Base and Ethereum token contracts remain `UNKNOWN` until a robust free-data adapter can inspect verified source, proxy/admin roles, mint/freeze/blacklist controls, honeypot behavior, holder concentration, and liquidity lock evidence.
2. **Solana security is a minimum screen, not an audit.** Mint and Freeze authority status plus top-10 token-account concentration do not prove LP locking or eliminate hidden program risks. Token accounts are not the same as beneficial wallet owners; LP/exchange accounts can distort concentration.
3. **Order-book depth is not available in the public DEX adapters.** DEX liquidity is a pool-reserve proxy. CEX size caps use quote-volume as a conservative proxy, not a measured order-book slippage curve.
4. **News coverage is operator-supplied.** The local keyword scorer is transparent and intentionally simple; it is not a trained sentiment model and does not crawl live news or social media. It returns a batch hype-intensity score and counts mentions of selected market leaders (for example, Elon Musk). It cannot claim a statistically measured *sudden* spike without a historical attention baseline.
5. **Backtesting is an initial validation gate, not proof of an edge.** The fixed EMA20/EMA50 rule uses a single chronological split. Further work is required for multiple walk-forward folds, survivorship-bias controls, token delisting history, regime-specific analysis, realistic spread/impact, liquidation mechanics, and portfolio-level correlation/tail-risk stress tests.
6. **Free APIs are not SLAs.** Providers can change rate limits, schemas, availability, and geographic access. The rate limiter reduces risk but cannot guarantee the IP will never be throttled.

Do not deploy this prototype as an unattended live trading bot. First complete the EVM risk adapter, multi-fold walk-forward tests, historical market-universe reconstruction, realistic execution simulation, and paper-trading observation.

## 9. Professional references

- GeckoTerminal V2 public API: <https://api.geckoterminal.com/docs/index.html>
- DexScreener API reference: <https://docs.dexscreener.com/api/reference>
- Binance Spot API: <https://github.com/binance/binance-spot-api-docs>
- Binance USD-M Futures API: <https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api>
- Bybit V5 API: <https://bybit-exchange.github.io/docs/v5/intro>
- Solana JSON-RPC: <https://solana.com/docs/rpc/http>

---

**CRYPTO SUPER RADAR** — Discover Early. Analyze Smarter. Manage Risk.
