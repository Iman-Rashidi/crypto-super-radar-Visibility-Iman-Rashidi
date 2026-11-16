# اجرای رایگان Challenge 1 با GitHub Actions

## هدف و مرز معماری

این مسیر، Compute سرویس Render را از حلقهٔ اجرای ربات حذف می‌کند. GitHub Actions روی مخزن عمومی یک Runner موقت می‌سازد، نسخهٔ Python شاخهٔ Staging را آماده می‌کند، یک Batch کامل Challenge 1 را اجرا می‌کند، وضعیت را در Turso ذخیره می‌کند و Process را خاتمه می‌دهد.

- زمان‌بندی: هر ۵ دقیقه، UTC
- کد معاملاتی: همان موتور فعلی Challenge 1، بدون بازنویسی استراتژی
- نمادهای پیش‌فرض: BTC-USDT، ETH-USDT، SOL-USDT، XRP-USDT، LINK-USDT
- صرافی: BloFin Demo فقط
- حافظه: Turso Cloud SQLite در طرح Free
- تعداد Runner هم‌زمان: یک
- سفارش Live: ممنوع
- تست یک‌بارهٔ مستقل: غیرفعال

این راهکار یک Process دائماً روشن نیست؛ یک اجرای زمان‌بندی‌شده و مستقل از Render است. GitHub حداقل فاصلهٔ Cron را پنج دقیقه اعلام می‌کند، اما اجرای Scheduled ممکن است در بار زیاد تأخیر بخورد یا حذف شود. برای معاملات باز، ربات هنگام ثبت سفارش TP/SL را همراه سفارش بازار به BloFin می‌فرستد؛ بااین‌حال باید وجود سفارش محافظ در حساب Demo پس از هر Fill در Acceptance واقعی بررسی شود.

## چرا این راهکار رایگان انتخاب شد؟

مخزن فعلی Public است. GitHub استاندارد Runnerهای GitHub-hosted را برای مخازن عمومی رایگان و نامحدود اعلام می‌کند. Turso در طرح Free فعلی $0 است و برای شروع به کارت بانکی نیاز ندارد. هیچ Compute پولی، VPS پولی یا سرویس جانبی به این معماری اضافه نمی‌شود.

محدودیت‌های رایگان:
- GitHub زمان‌بندی دقیق با SLA تضمین نمی‌کند.
- در مخزن عمومی اگر ۶۰ روز فعالیتی رخ ندهد، Schedule ممکن است خودکار غیرفعال شود.
- Turso سهمیهٔ Free دارد؛ Overages را فعال نکن.
- قطع GitHub، BloFin یا Turso باعث توقف چرخه می‌شود. سیستم در نبود Storage یا مجوز Trade باید Fail Closed بماند.

## نقش‌های مستقل

- `BLOFIN_DEMO_AUTOPILOT_ROLE=WEB`: رفتار قبلی برنامهٔ وب؛ مقدار پیش‌فرض برای سازگاری.
- `BLOFIN_DEMO_AUTOPILOT_ROLE=API`: برنامهٔ وب داشبورد را ارائه می‌کند اما Thread محلی Autopilot را شروع نمی‌کند.
- `BLOFIN_DEMO_AUTOPILOT_ROLE=WORKER`: نقش Runner مستقل؛ سرویس وب Thread دائمی را شروع نمی‌کند و CLI فقط یک Batch اجرا می‌کند.

Workflow زمان‌بندی‌شده فایل‌های کد را از `staging/challenge1-integration-2026-11-16` Checkout می‌کند. خود Workflow باید در شاخهٔ پیش‌فرض مخزن یعنی `main` هم وجود داشته باشد؛ چون GitHub فقط Schedule شاخهٔ پیش‌فرض را اجرا می‌کند. این فایل Workflow به‌تنهایی هیچ Render Service را Deploy نمی‌کند.

## تنظیم GitHub Secrets و Variables

در مخزن GitHub برو به:

Settings → Secrets and variables → Actions

### Repository Secrets

هر مورد را جداگانه با مقدار واقعی حساب خود ثبت کن. مقدارها نباید در Commit، فایل یا چت وارد شوند.

```text
TURSO_DATABASE_URL
TURSO_AUTH_TOKEN

BLOFIN_DEMO_API_KEY
BLOFIN_DEMO_API_SECRET
BLOFIN_DEMO_PASSPHRASE

BLOFIN_DEMO_TRADE_API_KEY
BLOFIN_DEMO_TRADE_API_SECRET
BLOFIN_DEMO_TRADE_PASSPHRASE

BLOFIN_DASHBOARD_TOKEN
```

کلید READ باید فقط Read باشد. کلید TRADE باید کلید مستقل Demo با Read و Trade باشد. Transfer و Withdrawal خاموش بمانند. هیچ کلید Production را وارد نکن.

### Repository Variables

تا وقتی همهٔ Secretها وارد نشده و پیش‌نیازها در BloFin Demo و Turso تأیید نشده‌اند، هر دو را `false` نگه دار:

```text
BLOFIN_DEMO_TRADING_ENABLED=false
BLOFIN_DEMO_AUTOPILOT_ENABLED=false
```

پس از آماده‌شدن Secretها، برای فعال‌کردن چرخه‌های Demo هر دو را به `true` تغییر بده. این دو سوئیچ در هر اجرای Runner دوباره کنترل می‌شوند.

## اجرای دستی و بررسی وضعیت

در GitHub → Actions → BloFin Demo scheduled runner می‌توان یک اجرای دستی را با ورودی تأیید روشن کرد. Workflow قبل از ورود به حلقهٔ معاملاتی این موارد را کنترل می‌کند:

- نقش Worker صریحاً WORKER است.
- هر دو متغیر فعال‌سازی true هستند.
- همهٔ Secretهای لازم وجود دارند.
- Storage واقعاً TURSO_CLOUD_SQLITE است.
- وضعیت از دیتابیس بازیابی می‌شود.
- Readiness برابر READY است.
- کلید معامله DEDICATED_TRADE_KEY است.
- مجوز واقعی Read/Write تأیید شده است.
- هیچ سفارش UNKNOWN حل‌نشده‌ای در دفتر سفارش وجود ندارد.
- تست Smoke مستقل خاموش است.

نتیجهٔ هر چرخه در GitHub Actions Summary چاپ می‌شود. هیچ Secret یا مقدار Credential در Summary نوشته نمی‌شود.

## معیار پذیرش

- [ ] Workflow هر ۵ دقیقه در Actions شروع شود.
- [ ] هر Job بعد از یک Batch کامل خارج شود؛ Thread بی‌نهایت در Runner باقی نماند.
- [ ] دو Job هم‌زمان اجرا نشوند.
- [ ] پس از پایان Job، شمارنده و زمان چرخه در Turso افزایش یابد.
- [ ] پس از اجرای بعدی، وضعیت قبلی از Turso بازیابی شود.
- [ ] هیچ سفارش تکراری برای سیگنال/کندل ثبت نشود.
- [ ] سفارش‌ها فقط به BloFin Demo ارسال شوند.
- [ ] هر سفارش Fill شده دارای TP/SL سمت صرافی باشد و این موضوع از API حساب Demo تأیید شود.
- [ ] نتیجهٔ هر تحلیل، حتی NO_TRADE یا UNKNOWN، در حافظهٔ پایدار ثبت شود.
- [ ] چند چرخهٔ متوالی و تست Restart/Recovery موفق شود.

تا زمانی که این موارد با لاگ و خروجی API ثابت نشده‌اند، Challenge 1 قبول‌شده محسوب نمی‌شود.
