from pathlib import Path


def test_scan_page_defines_and_calls_render_gates_with_persian_statuses():
    source = (Path(__file__).parents[1] / "crypto_super_radar" / "web_app.py").read_text(encoding="utf-8")

    assert "function renderGates(gates)" in source
    assert "renderGates(s.gates)" in source
    assert "trend:'روند بازار'" in source
    assert "market_regime_validation:'تأیید وضعیت کلی بازار'" in source
    assert "🟢 تأیید شد" in source
    assert "🔴 رد شد" in source
    assert "⚪ داده دریافت نشد" in source


def test_scan_page_explains_partial_data_and_groups_missing_sources_in_persian():
    source = (Path(__file__).parents[1] / "crypto_super_radar" / "web_app.py").read_text(encoding="utf-8")

    assert "تحلیل نیمه‌کامل است" in source
    assert "فعلاً معامله‌ای مجاز نیست" in source
    assert "📋 موارد دریافت‌نشده در این اسکن (کلیک برای مشاهده)" in source
    assert "منبع DEX" in source
    assert "مشتقات و CoinGlass" in source
    assert "فعالیت نهنگ‌ها" in source


def test_watchlist_explains_eur_100_qualification_and_german_timezone():
    source = (Path(__file__).parents[1] / "crypto_super_radar" / "web_app.py").read_text(encoding="utf-8")

    assert "فاز ۱ و ۲ با ریسک ≤ ۷۰" in source
    assert "€100 فرضی" in source
    assert "CET/CEST" in source
    assert "Europe/Berlin" in source
