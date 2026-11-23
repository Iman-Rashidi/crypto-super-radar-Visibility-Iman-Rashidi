"""Manual-wave Demo endpoint and dashboard regression tests; no exchange/network calls."""
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

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


    def test_long_and_short_orders_use_demo_client_contract_and_attach_tp_sl(self):
        for side, expected_side, tp, sl in (
            ("LONG", "buy", "70000", "65000"),
            ("SHORT", "sell", "65000", "70000"),
        ):
            with self.subTest(side=side):
                client = MagicMock()
                client.base_url = web_app.DEMO_REST_URL
                client.get_position_mode.return_value = {"positionMode": "net_mode"}
                client.get_positions.return_value = []
                client.get_open_orders.return_value = []
                client.get_tradable_instrument.return_value = {
                    "tickSize": "0.1", "contractValue": "0.001",
                    "lotSize": "0.01", "minSize": "0.01", "maxMarketSize": "100",
                }
                client.get_mark_price.return_value = [{"markPrice": "68000"}]
                client.place_futures_order.return_value = {
                    "code": "0", "data": [{"orderId": "demo-order-123"}],
                }
                payload = {**self.valid, "side": side, "tpTriggerPrice": tp, "slTriggerPrice": sl}
                with patch.dict(os.environ, {"BLOFIN_DEMO_TRADING_ENABLED": "true"}):
                    with patch.object(web_app, "_blofin_manual_write_client", return_value=client):
                        with patch.object(web_app, "BloFinDemoEventStore") as store_cls:
                            store_cls.return_value = MagicMock()
                            result = web_app._blofin_manual_submit_wave_order(payload)
                self.assertEqual(result["status"], "ORDER_ACCEPTED")
                self.assertEqual(result["environment"], "BLOFIN_DEMO")
                self.assertEqual(result["exchange_order_id"], "demo-order-123")
                kwargs = client.place_futures_order.call_args.kwargs
                self.assertEqual(kwargs["side"], expected_side)
                self.assertEqual(kwargs["take_profit"], tp)
                self.assertEqual(kwargs["stop_loss"], sl)
                self.assertEqual(kwargs["position_side"], "net")
                self.assertEqual(kwargs["margin_mode"], "isolated")

    def test_manual_trade_ui_uses_top_panel_token_fallback_and_renders_error_details(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("blofin-dashboard-token", source)
        self.assertIn("error.payload", source)
        self.assertIn("جزئیات خطا", source)
        self.assertIn('"message_fa":message_fa', source)
        self.assertIn('"error_type":type(exc).__name__', source)


if __name__ == "__main__":
    unittest.main()
