from types import SimpleNamespace

from crypto_super_radar.dex_radar_payload import build_dex_radar_payload


def test_dex_radar_payload_has_three_panels_and_partial_when_side_source_missing():
    asset = SimpleNamespace(raw="dex:solana:token", symbol="TEST")
    report = {
        "price_usd": 0.125,
        "token_name": "Test Token",
        "symbol": "TEST",
        "liquidity_usd": 12000,
        "volume_24h_usd": 8000,
        "price_change_pct": {"m5": 1.2, "h1": -0.4, "h6": None, "h24": 3.1},
        "whale_flow": {"status": "NOT_AVAILABLE"},
        "red_flag_status": "UNKNOWN",
    }

    payload = build_dex_radar_payload(asset, report)

    assert payload["status"] == "PARTIAL"
    assert payload["signal"] == "NO_TRADE"
    assert payload["diagnosis"]["final_recommendation"]
    assert set(payload["timeframe_ranges"]) == {"5m", "1h", "6h", "24h"}
    assert payload["market_overview"]["current_price"] == 0.125
    assert payload["market_overview"]["candles"] == []
    assert payload["timeframe_ranges"]["6h"]["low"] == "NOT_AVAILABLE"
    assert payload["timeframe_ranges"]["6h"]["high"] == "NOT_AVAILABLE"
    assert payload["safety"]["orders_submitted"] is False
