# Architecture / معماری موتور

## Decision contract / قرارداد تصمیم

A signal is emitted only when all twelve mandatory gate states are `PASS`:

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
  -> Whale Intelligence
  -> Full Market Sentiment Coverage
  -> Multi-fold Walk-forward Stability
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
| `sentiment.py` | Persian/English keyword sentiment, Fear & Greed normalization and divergence | تحلیل متن فارسی/انگلیسی، شاخص ترس و طمع و تضاد جریان نهنگ |
| `sentiment_feeds.py` | Public RSS + Alternative.me Fear & Greed adapters | دریافت خبر عمومی و شاخص ترس و طمع رایگان |
| `market_sentiment.py` | Combined news, Fear & Greed and optional social input | تجمیع منابع احساسات و اعلام پوشش داده |
| `evm_security.py` | GoPlus + exact Sourcify contract screening | غربالگری امنیت قرارداد EVM با تطبیق دقیق سورس |
| `walk_forward.py` | Non-overlapping chronological OOS folds | اعتبارسنجی چندبخشی بدون هم‌پوشانی داده آزمون |
| `intelligence_engine.py` | Fail-closed evidence combiner | ترکیب شواهد با مسدودسازی FAIL/UNKNOWN |
| `providers.py` | GeckoTerminal, DexScreener, Binance, Bybit and Solana RPC | دریافت داده رایگان و عمومی |
| `indicators.py` | RSI, ATR, EMA regime and relative strength | اندیکاتورهای تکنیکال و رژیم بازار |
| `backtesting.py` | Fixed-rule, chronological out-of-sample testing | آزمون تاریخی با هزینه و بدون نگاه به آینده |
| `filters.py` | Gates 1–7 | هفت فیلتر داده، بازار، روند، مومنتوم، جریان پول، امنیت و خبر |
| `engine.py` | Gates 8–9, score, TP/SL and sizing | تصمیم پایه، بک‌تست، اندازه موقعیت و حد سود/ضرر |
| `cex_whales.py` | Binance/Bybit large-trade flow sample | رصد معاملات بزرگ در بازار متمرکز |\n| `integrated_engine.py` | Adds mandatory gates 10–12 and combines whale/sentiment/walk-forward evidence | اتصال سه فیلتر جدید به موتور تصمیم |\n| `enhanced_cli.py` | Integrated command-line entry point | رابط خط فرمان یکپارچه |\n| `web_app.py` | Persian on-demand web dashboard and job polling | داشبورد وب فارسی و اجرای اسکن غیرهمزمان |\n| `whale_store.py` | Local SQLite whale-flow history | ذخیره محلی تاریخچه جریان نهنگ‌ها |
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


## Integrated release contract / قرارداد نسخه یکپارچه

`enhanced_cli.py` is the active entry point for both `python -m crypto_super_radar` and the installed command. It calls the legacy nine-gate engine, then appends three mandatory gates:

- Gate 10 — Whale Intelligence: Ethereum/Base use bounded ERC-20 transfer logs and GoPlus top-holder labels; Solana samples recent largest token-account activity and stays PARTIAL; CEX spot/perpetuals compare large aggressive trades from Binance and Bybit.
- Gate 11 — Full Market Sentiment Coverage: public news RSS, a selected social/community RSS feed or explicit social posts, and Alternative.me Fear & Greed must all be available. Missing coverage blocks.
- Gate 12 — Multi-fold Walk-forward Stability: fixed EMA20/EMA50 strategy, non-overlapping chronological test folds, fees/slippage, per-fold drawdown, and signed historical funding costs for perpetuals. Missing funding remains UNKNOWN.

The decision contract remains fail-closed: all 12 gates must PASS. Solana's bounded recent sample is not treated as full-history coverage, and EVM/CEX samples do not establish beneficial ownership. No orders are executed; output remains paper/research only. Render Free sleeps after 15 minutes of inactivity and loses local SQLite data on spin-down/restart, so the hosted dashboard is on-demand and history is not durable there.
