"""Manual-wave Demo endpoint and dashboard regression tests; no exchange/network calls."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from crypto_super_radar import web_app


SOURCE = Path(__file__).resolve().parents[1] / "crypto_super_radar" / "web_app.py"


class ManualWaveTradeTests(unittest.TestCase):
    def setUp(self):
        self.valid = {
            "instrument_id": "BTC-USDT", "side": "LONG", "margin_usdt": 100,
            "leverage": 1, "tpTriggerPrice": "70000", "slTriggerPrice": "65000",
        }

    def test_panel_token_is_required_and_compared_safely(self):
        self.assertTrue(web_app._blofin_dashboard_token_valid("Bearer panel-secret", "panel-secret"))
        self.assertFalse(web_app._blofin_dashboard_token_valid("Bearer wrong", "panel-secret"))
        self.assertFalse(web_app._blofin_dashboard_token_valid("", "panel-secret"))
        self.assertFalse(web_app._blofin_dashboard_token_valid("Bearer panel-secret", ""))

    def test_demo_environment_lock_blocks_before_client_creation(self):
        with patch.dict(os.environ, {"BLOFIN_DEMO_TRADING_ENABLED": "false"}):
            with patch.object(web_app, "_blofin_manual_write_client") as client:
                with self.assertRaises(web_app.BloFinDemoExecutionBlocked):
                    web_app._blofin_manual_submit_wave_order(self.valid)
                client.assert_not_called()

    def test_leverage_is_limited_to_three(self):
        for leverage in (0, 4, 10, 1.5, True):
            payload = {**self.valid, "leverage": leverage}
            with self.subTest(leverage=leverage):
                with self.assertRaises(web_app.BloFinDemoExecutionBlocked):
                    web_app._blofin_manual_validate_wave_order_payload(payload)

    def test_both_exchange_protective_trigger_prices_are_mandatory(self):
        for missing in ("tpTriggerPrice", "slTriggerPrice"):
            payload = dict(self.valid)
            payload.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(web_app.BloFinDemoExecutionBlocked):
                    web_app._blofin_manual_validate_wave_order_payload(payload)

    def test_valid_payload_is_normalized_and_validated(self):
        result = web_app._blofin_manual_validate_wave_order_payload(self.valid)
        self.assertEqual(result["instrument_id"], "BTC-USDT")
        self.assertEqual(result["side"], "LONG")
        self.assertEqual(result["leverage"], 1)
        self.assertEqual(str(result["tp"]), "70000")
        self.assertEqual(str(result["sl"]), "65000")

    def test_dashboard_has_manual_trade_box_and_protected_routes(self):
        source = SOURCE.read_text(encoding="utf-8")
        for item in (
            'id="manual-wave-trade"', 'id="manual-wave-long"', 'id="manual-wave-short"',
            'id="manual-wave-close"', "/api/blofin-demo/manual-wave-order",
            "/api/blofin-demo/manual-wave-close", "tpTriggerPrice", "slTriggerPrice",
            "BLOFIN_DASHBOARD_TOKEN", "BLOFIN_DEMO_TRADING_ENABLED",
            "client.place_futures_order",
        ):
            self.assertIn(item, source)


if __name__ == "__main__":
    unittest.main()
