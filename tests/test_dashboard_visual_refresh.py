from pathlib import Path


def test_modern_radar_visual_theme_and_mobile_navigation_are_present():
    source = Path("crypto_super_radar/web_app.py").read_text(encoding="utf-8")
    assert "backdrop-filter:blur(12px)" in source
    assert "Vazirmatn" in source
    assert "radar-theme-toggle" in source
    assert "light-theme" in source
    assert "CRYPTO SUPER RADAR" in source
    assert "LIVE PULSE" in source
    assert "radar-quick-jump" in source
    for anchor in ("#gem-hunter-panel", "#free-airdrop-radar", "#futures-paper-bot", "#live-market-radar"):
        assert anchor in source


def test_scan_radar_motion_and_reduced_motion_accessibility_are_present():
    source = Path("crypto_super_radar/web_app.py").read_text(encoding="utf-8")
    assert "radar-sweep" in source
    assert "radar-wave" in source
    assert "radar-progress" in source
    assert "is-active" in source
    assert "prefers-reduced-motion:reduce" in source
    assert "gem-hunter-run" in source
    assert "اجرای اسکن زنده Gem Hunter V3" in source


def test_gem_candidate_visual_scores_and_copy_feedback_are_present():
    source = Path("crypto_super_radar/web_app.py").read_text(encoding="utf-8")
    assert "gem-score-panel" in source
    assert "gem-score-fill opportunity" in source
    assert "gem-score-fill risk" in source
    assert "gem-visual-badges" in source
    assert "net-solana" in source
    assert "net-base" in source
    assert "net-ethereum" in source
    assert "gem-copy-success" in source
    assert "کپی آدرس قرارداد" in source


def test_existing_dashboard_controls_and_safety_contracts_remain_unchanged():
    source = Path("crypto_super_radar/web_app.py").read_text(encoding="utf-8")
    for existing_id in (
        "gem-hunter-run",
        "gem-hunter-results",
        "free-airdrop-radar",
        "futures-paper-bot",
        "live-market-radar",
        "live-radar-refresh",
        "crypto-market-refresh",
        "scan-asset",
    ):
        assert f'id="{existing_id}"' in source
    assert '"NO_TRADE"' in source
    assert "CHALLENGE_1_V2" in source
