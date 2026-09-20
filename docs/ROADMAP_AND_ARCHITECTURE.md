# Proxgram Growth Engine — Roadmap & System Architecture

## ۱. نمای کلی پروژه (Executive Summary)
این پروژه یک سیستم متمرکز خودکارسازی رشد و تعامل (Growth Engine) است که وظیفه مدیریت نشست‌ها، پایش تعاملات، اجرای صف وظایف زمان‌بندی‌شده، تولید و انتشار هوشمند محتوا با هوش مصنوعی را بر عهده دارد.

---

## ۲. اصول مهندسی و معماری (Architectural Principles)
- **Single Source of Truth (SSOT):** پایگاه داده PostgreSQL مرجع نهایی تمامی داده‌های وضعیت، لاگ‌ها و پیکربندی‌ها است.
- **Fail-Safe & Idempotency:** تمامی تسک‌های صف باید قابلیت اجرای مجدد بدون اثر جانبی مخرب را داشته باشند.
- **Rate-Limit & Anti-Ban Architecture:** مدیریت فاصله زمانی ارسال درخواست‌ها و کنترل نشست‌ها جهت جلوگیری از اعمال محدودیت توسط پلتفرم‌های مقصد.
- **Stateless Execution Core:** ورکرها بدون وابستگی به حافظه محلی، وظایف را صرفاً از صف کارها پردازش می‌کنند.

---

## ۳. فازبندی اجرایی پروژه (Detailed Roadmap)

### فاز ۱: زیرساخت، محیط اجرا و پایگاه داده (Foundation & Persistence)
- [ ] استانداردسازی متغیرهای محیطی و پیکربندی قالب `.env.example`.
- [ ] پیاده‌سازی کانتینرسازی کامل با Docker و Docker Compose (سرویس‌های App، Postgres، Redis).
- [ ] طراحی اسکیماهای اصلی پایگاه داده (Accounts, Sessions, Tasks, Metrics, Logs).
- [ ] راه‌اندازی سیستم مهاجرت داده‌ها (Migrations Pipeline).
- [ ] اعتبارسنجی اتصال پایگاه داده و انجام اولین تست ذخیره‌سازی داده.

### فاز ۲: هسته موتور خودکارسازی و صف کارها (Core Engine & Task Queue)
- [ ] پیکربندی صف پیام غیرهمگام (Celery/Redis یا RQ).
- [ ] پیاده‌سازی سرویس زمان‌بندی وظایف دوره‌ای (Periodic Scheduler / Cron Beat).
- [ ] سیستم پایش سلامت نشست‌ها (Session Health Checks & Rotation).
- [ ] ایجاد لایه‌ی ثبت لاگ ساختاریافته (Structured Logging) و مکانیزم ارسال هشدارهای خطا.
- [ ] تست استرس صَف وظایف و کنترل رفتار در شرایط قطعی شبکه.

### فاز ۳: پایپ‌لاین هوش مصنوعی و تعاملات شبکه‌ها (AI Flow & Integration)
- [ ] ماژول اتصال به APIهای شبکه‌های اجتماعی جهت تعامل و انتشار پست‌ها.
- [ ] پیاده‌سازی پایپ‌لاین تولید محتوا با هوش مصنوعی (متن، بازنویسی، تحلیل هشتگ‌ها و مدیا).
- [ ] سیستم مدیریت کش و ریت‌لیمیت‌ها برای سرویس‌های خارجی.
- [ ] تست سناریوی کامل از دریافت فرمان تا تولید محتوا و زمان‌بندی انتشار.

### فاز ۴: استقرار پایدار، پایش و تحویل مداوم (Deployment & CI/CD)
- [ ] ایجاد پایپ‌لاین GitHub Actions برای اعتبارسنجی کدها و تست‌های خودکار در هر Push.
- [ ] ایجاد اسکریپت استقرار خودکار روی سرور (Zero-Downtime Deployment Script).
- [ ] تنظیمات مانیتورینگ سلامت سرور و کانتینرها (Auto-Restart & Resource Limits).
- [ ] تدوین راهنمای بهره‌برداری عملیاتی و مدیریت بحران (Runbook & Disaster Recovery).

---

## ۴. استک فنی (Tech Stack)
- **Runtime:** Python 3.11+
- **Database:** PostgreSQL (Relational SSOT)
- **Cache & Message Broker:** Redis
- **Queue Engine:** Celery / RQ
- **Containerization:** Docker & Docker Compose
- **Version Control & CI/CD:** GitHub Actions
