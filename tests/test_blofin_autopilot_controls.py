from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from crypto_super_radar import web_app


class FakeTradeClient:
    def __init__(self, **kwargs):
        self.api_key = "demo-key"
        self.api_secret = "demo-secret"
        self.passphrase = "demo-pass"

    def verify_trade_permission(self):
        return {"readOnly": 0, "trade_permission": True}


class FakeStore:
    def trade_budget_status(self):
        return {"orders_today": 0, "orders_total": 0, "unresolved_orders": 0}

    def close(self):
        pass


class BloFinAutopilotControlTests(unittest.TestCase):
    def setUp(self):
        self.previous_override = web_app.BLOFIN_AUTOPILOT_RUNTIME_OVERRIDE
        web_app._set_blofin_demo_autopilot_runtime_override(None)

    def tearDown(self):
        web_app._set_blofin_demo_autopilot_runtime_override(self.previous_override)

    def test_environment_value_is_used_when_dashboard_override_is_not_set(self):
        with patch.dict(os.environ, {"BLOFIN_DEMO_AUTOPILOT_ENABLED": "true"}):
            self.assertTrue(web_app._blofin_demo_autopilot_enabled())
        with patch.dict(os.environ, {"BLOFIN_DEMO_AUTOPILOT_ENABLED": "false"}):
            self.assertFalse(web_app._blofin_demo_autopilot_enabled())

    def test_dashboard_override_takes_precedence_over_render_environment(self):
        with patch.dict(os.environ, {"BLOFIN_DEMO_AUTOPILOT_ENABLED": "false"}):
            web_app._set_blofin_demo_autopilot_runtime_override(True)
            self.assertTrue(web_app._blofin_demo_autopilot_enabled())
            web_app._set_blofin_demo_autopilot_runtime_override(False)
            self.assertFalse(web_app._blofin_demo_autopilot_enabled())

    def test_readiness_returns_each_missing_demo_prerequisite(self):
        values = {
            "BLOFIN_DEMO_TRADE_API_KEY": "demo-key",
            "BLOFIN_DEMO_TRADE_API_SECRET": "demo-secret",
            "BLOFIN_DEMO_TRADE_PASSPHRASE": "demo-pass",
            "BLOFIN_DEMO_TRADING_ENABLED": "false",
            "BLOFIN_DASHBOARD_TOKEN": "",
        }
        with patch.dict(os.environ, values, clear=False), \
             patch.object(web_app, "BloFinDemoTradeClient", FakeTradeClient), \
             patch.object(web_app, "storage_backend", return_value="LOCAL_SQLITE"), \
             patch.object(web_app, "BloFinDemoEventStore", return_value=FakeStore()):
            result = web_app._blofin_demo_execution_readiness()

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("DEMO_TRADING_DISABLED", result["blockers"])
        self.assertIn("DASHBOARD_TOKEN_NOT_CONFIGURED", result["blockers"])
        self.assertIn("DURABLE_TURSO_NOT_CONFIGURED", result["blockers"])
        self.assertNotIn("TRADE_PERMISSION_NOT_VERIFIED", result["blockers"])
        self.assertEqual(len(result["blockers"]), len(result["blockers_fa"]))


if __name__ == "__main__":
    unittest.main()
