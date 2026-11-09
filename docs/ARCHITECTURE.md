# Architecture / معماری موتور

## Decision contract / قرارداد تصمیم

A signal is emitted only when all nine mandatory gate states are `PASS`:

```text
Data Integrity
  -> Market Regime
  -> Trend & Structure
  -> Momentum & Relative Strength
  -> Capital Flow & Liquidity
  -> On-chain / Contract Risk
  -> News & Sentiment
  -> Out-of-sample Strategy Validation
  -> Portfolio & Trade Risk
  -> LONG | SHORT | NO TRADE
```

Any `FAIL` or `UNKNOWN` blocks entry. Scores are descriptive evidence and never override a failed or unknown gate.

## Package map / ساختار کد

| Module | Responsibility | توضیح فارسی |
|---|---|---|
| `config.py` | Thresholds, rate limits, paper risk parameters | تنظیمات مرکزی، محدودیت API و بودجه ریسک آزمایشی |
| `models.py` | Typed assets, gates, snapshots and results | مدل داده و وضعیت هر فیلتر |
| `rate_limiter.py` | Process-wide per-host scheduling | کنترل یکپارچه فاصله درخواست‌ها |
| `whale_intelligence.py` | Large transfer classification and exchange-flow aggregation | تفکیک انتقال بزرگ از جریان ورود/خروج صرافی |
| `evm_transfers.py` | Bounded ERC-20 Transfer-log scanner on Ethereum/Base | رصد انتقال‌های EVM با محدودیت بازه بلاک |
| `solana_whales.py` | Recent SPL largest-account transaction sampling | نمونه‌برداری محدود تراکنش حساب‌های بزرگ سولانا |
| `sentiment.py` | Persian/English keyword sentiment, Fear & Greed normalization and divergence | تحلیل متن فارسی/انگلیسی، شاخص ترس و طمع و تضاد جریان نهنگ |\n| `sentiment_feeds.py` | Public RSS + Alternative.me Fear & Greed adapters | دریافت خبر عمومی و شاخص ترس و طمع رایگان |\n| `market_sentiment.py` | Combined news, Fear & Greed and optional social input | تجمیع منابع احساسات و اعلام پوشش داده |
| `evm_security.py` | GoPlus + exact Sourcify contract screening | غربالگری امنیت قرارداد EVM با تطبیق دقیق سورس |
| `walk_forward.py` | Non-overlapping chronological OOS folds | اعتبارسنجی چندبخشی بدون هم‌پوشانی داده آزمون |
| `intelligence_engine.py` | Fail-closed evidence combiner | ترکیب شواهد با مسدودسازی FAIL/UNKNOWN |
| `providers.py` | GeckoTerminal, DexScreener, Binance, Bybit and Solana RPC | دریافت داده رایگان و عمومی |
| `indicators.py` | RSI, ATR, EMA regime and relative strength | اندیکاتورهای تکنیکال و رژیم بازار |
| `sentiment.py` | Local keyword sentiment | تحلیل محلی تیترها بدون سرویس پولی |
| `backtesting.py` | Fixed-rule, chronological out-of-sample testing | آزمون تاریخی با هزینه و بدون نگاه به آینده |
| `filters.py` | Gates 1–7 | هفت فیلتر داده، بازار، روند، مومنتوم، جریان پول، امنیت و خبر |
| `engine.py` | Gates 8–9, score, TP/SL and sizing | تصمیم نهایی، بک‌تست، اندازه موقعیت و حد سود/ضرر |
| `reporting.py` | Persian explanations and Pandas DataFrame | گزارش قابل فهم برای کاربر فارسی‌زبان |
| `cli.py` | Command-line interface | اجرای اسکن و ذخیره گزارش |
| `selftest.py` | Offline smoke/unit tests | تست خودکار بدون تماس با API |

## Gate semantics / معنی وضعیت‌ها

- `PASS`: required evidence is present and meets the configured rule.
- `FAIL`: evidence exists but violates a hard threshold.
- `UNKNOWN`: evidence is missing, stale, unreadable, or cannot be verified.

Persian: `UNKNOWN` به معنی «احتمالاً خوب» نیست؛ یعنی اطلاعات کافی برای تأیید وجود ندارد و معامله مسدود می‌شود.

## Separation of concerns / جداسازی مسئولیت‌ها

- Provider adapters fetch and normalize; they do not decide trade direction.
- Filters evaluate one rule each and emit a structured `GateResult`.
- The confirmation engine applies an AND policy to hard gates.
- The opportunity score ranks evidence quality; it cannot override a failed gate.
- Portfolio sizing is paper-only and never sends execution requests.


## New research-module integration contract / قرارداد اتصال ماژول‌های جدید

The modules above expose normalized research APIs. They must be wired into the scan/CLI/report path before a hosted end-to-end release. EVM transfer scanning can cover bounded public RPC ranges; Solana whale sampling is intentionally partial and cannot satisfy a full-history whale gate. Sentiment currently scores supplied text and does not claim live social coverage. The decision combiner requires all six mandatory evidence groups to be PASS and emits only paper/research labels.
