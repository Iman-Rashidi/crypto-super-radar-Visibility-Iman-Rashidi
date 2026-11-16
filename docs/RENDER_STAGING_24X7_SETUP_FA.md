# راه‌اندازی ۲۴/۷ سرویس BloFin Demo روی Render Staging

> **وضعیت جدید در ۱۶ نوامبر ۲۰۲۶:** این راهنما فقط گزینهٔ قدیمیِ اجرای دائمی روی Compute پولی Render را توضیح می‌دهد و برای معماری رایگان فعلی دستور اجرایی نیست. مسیر منتخب رایگان، زمان‌بندی مستقل GitHub Actions در فایل `docs/GITHUB_ACTIONS_FREE_DEMO_RUNNER_FA.md` است. برای هدف هزینهٔ صفر، پلن Render را ارتقا نده و مراحل خرید Starter این سند را اجرا نکن. سرویس Render می‌تواند صرفاً داشبورد اختیاری باشد؛ اجرای چرخهٔ ربات از GitHub Actions انجام می‌شود. زمان‌بندی رایگان، SLA یا تضمین اجرای دقیق هر پنج دقیقه ندارد.

این راهنما مخصوص سرویس زیر است و نباید روی Production یا سرویس‌های دیگر اعمال شود:

- Render service: `crypto-super-radar-unlimited-demo-staging`
- Service ID: `srv-dbsidmbbc2fs73blhblg`
- Branch: `staging/challenge1-integration-2026-11-16`
- Region: Frankfurt
- Dashboard: https://dashboard.render.com/web/srv-dbsidmbbc2fs73blhblg
- App: https://crypto-super-radar-unlimited-demo-staging.onrender.com

## 1) همیشه‌روشن کردن Compute

سرویس فعلاً روی Free است. Free بعد از ۱۵ دقیقه بدون ترافیک ورودی به خواب می‌رود و اجرای threadهای داخل پردازش را متوقف می‌کند. Turso این مشکل را حل نمی‌کند.

در Render:
1. لینک Dashboard بالا را باز کن.
2. وارد Settings شو.
3. در بخش Instance Type / Compute، پلن Free را به Starter (کمترین پلن پولی) ارتقا بده.
4. تعداد Instance را روی 1 نگه دار؛ اجرای چند نسخه می‌تواند چند Autopilot هم‌زمان ایجاد کند.
5. Health Check Path را روی `/health` قرار بده.
6. تغییر را ذخیره کن.

قیمت Compute را قبل از ذخیره در صفحه Render بررسی کن؛ قیمت منتشرشده ممکن است تغییر کند. با ارتقای پلن، سرویس دیگر به‌خاطر ۱۵ دقیقه بی‌ترافیکی Free نمی‌خوابد، ولی این به‌معنی SLA یا تضمین عدم Restart در Deploy و نگهداری پلتفرم نیست.

## 2) ثبت Secretها در Environment

در همان سرویس Staging به Environment برو. مقادیر را فقط داخل Render وارد کن. هیچ Secret یا Token را در GitHub، فایل عمومی یا چت قرار نده.

### Turso

هر دو متغیر لازم‌اند:
- `TURSO_DATABASE_URL`: آدرس دقیق دیتابیس از Turso
- `TURSO_AUTH_TOKEN`: توکن دسترسی همان دیتابیس

فقط یکی را وارد نکن؛ کد وقتی یکی از این دو ناقص باشد، اتصال را رد می‌کند. پس از راه‌اندازی، readiness باید `durable_storage=true` نشان دهد.

### BloFin Demo READ

این سه متغیر را با کلید جداگانهٔ READ پر کن:
- `BLOFIN_DEMO_API_KEY`
- `BLOFIN_DEMO_API_SECRET`
- `BLOFIN_DEMO_PASSPHRASE`

این کلید باید فقط مجوز Read داشته باشد.

### BloFin Demo TRADE

این سه متغیر را با کلید جدید و مستقل Demo پر کن:
- `BLOFIN_DEMO_TRADE_API_KEY`
- `BLOFIN_DEMO_TRADE_API_SECRET`
- `BLOFIN_DEMO_TRADE_PASSPHRASE`

کلید TRADE باید Read و Trade/Read-Write داشته باشد. Transfer و Withdrawal باید خاموش بمانند. کلید Production را در این سرویس وارد نکن. کد، مجوز واقعی همین کلید را از API صرافی بررسی می‌کند و اگر `readOnly=1` یا مجوز Trade تأیید نشود، سفارش را مسدود می‌کند.

### توکن پنل

- `BLOFIN_DASHBOARD_TOKEN`: یک مقدار تصادفی و طولانی بساز و فقط در Render ثبت کن.

### کلیدهای فعال‌سازی

ابتدا این دو را `false` نگه دار:
- `BLOFIN_DEMO_TRADING_ENABLED`
- `BLOFIN_DEMO_AUTOPILOT_ENABLED`

بعد از ثبت همهٔ Secretها و تأیید موفق تست‌های READ، TRADE، Turso و Token:
- `BLOFIN_DEMO_TRADING_ENABLED=true`
- `BLOFIN_DEMO_AUTOPILOT_ENABLED=true`

تنظیم‌های اختیاری:
- `BLOFIN_DEMO_AUTOPILOT_INTERVAL_SECONDS=300` (برنامه مقدار را بین ۳۰۰ تا ۹۰۰ ثانیه محدود می‌کند)
- `BLOFIN_DEMO_AUTOPILOT_INSTRUMENTS` (در صورت خالی‌ماندن، فهرست پیش‌فرض خود برنامه استفاده می‌شود)

این مورد را روشن نکن:
- `BLOFIN_DEMO_SMOKE_TEST_ENABLED=false`

تست مستقل سفارش، مسیر جداگانه‌ای است و نباید به‌عنوان شرط شروع Autopilot روشن شود.

## 3) راه‌اندازی مجدد

پس از ذخیره Environment، Render سرویس را Restart/Deploy می‌کند. مخزن و سرویس فعلی `autoDeploy=no` دارند؛ بنابراین برای تغییرات کد، از:
Manual Deploy → Deploy latest commit
استفاده کن. Branch باید همان `staging/challenge1-integration-2026-11-16` باشد. Production را تغییر نده.

## 4) تست بعد از راه‌اندازی

این مسیرها را با GET باز کن:
- سلامت سرویس: https://crypto-super-radar-unlimited-demo-staging.onrender.com/health
- اتصال READ به BloFin Demo: https://crypto-super-radar-unlimited-demo-staging.onrender.com/api/blofin/demo/status
- پیش‌نیازهای معامله: https://crypto-super-radar-unlimited-demo-staging.onrender.com/api/blofin/demo/execution-readiness
- وضعیت Autopilot: https://crypto-super-radar-unlimited-demo-staging.onrender.com/api/blofin/demo/autopilot/status

شرایط قبولی:
- `/health` پاسخ HTTP 200 بدهد.
- اتصال BloFin: `CONNECTED_READ_ONLY`
- execution readiness: `READY`
- `credential_source=DEDICATED_TRADE_KEY`
- `trade_permission_status=READ_WRITE_CONFIRMED`
- `durable_storage=true`
- `dashboard_auth_configured=true`
- `trading_enabled=true`
- `autopilot_enabled=true`
- وضعیت Autopilot پس از چند چرخه، زمان آخرین چرخه و شمارندهٔ چرخه‌ها را به‌روز کند.

HTTP 200 به‌تنهایی اثبات معامله یا حتی اجرای موفق چرخه نیست؛ خروجی JSON و لاگ‌های اپلیکیشن را هم بررسی کن. هیچ‌گاه صرفاً به‌دلیل نمایش `READY` ادعا نکن که سفارشی پر شده است.

## 5) محدودیت‌های باقی‌مانده

- Turso فقط ماندگاری داده را فراهم می‌کند؛ Compute باید همیشه‌روشن باشد.
- یک Render Web Service با یک process، یک thread Autopilot داخلی راه می‌اندازد؛ به همین دلیل تعداد instance باید 1 بماند.
- Deploy، Restart یا نگهداری پلتفرم ممکن است فرایند را موقتاً متوقف کند؛ وضعیت باید از Turso بازیابی و حلقه پس از Startup از نو آغاز شود.
- هیچ Ping خارجی برای دورزدن Free Sleep به‌عنوان راه‌حل ۲۴/۷ استفاده نشود.
- تمام سفارش‌ها فقط در BloFin Demo مجازند؛ Live trading در این تنظیمات مجاز نیست.
