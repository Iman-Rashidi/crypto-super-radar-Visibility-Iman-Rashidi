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
