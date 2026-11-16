from __future__ import annotations

import unittest

from crypto_super_radar.demo_autopilot_once import preflight_errors


class DemoAutopilotOneShotPreflightTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "BLOFIN_DEMO_AUTOPILOT_ROLE": "WORKER",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_DEMO_AUTOPILOT_ENABLED": "true",
            "BLOFIN_DEMO_SMOKE_TEST_ENABLED": "false",
            "TURSO_DATABASE_URL": "libsql://example.turso.io",
            "TURSO_AUTH_TOKEN": "test-token",
            "BLOFIN_DEMO_API_KEY": "read-key",
            "BLOFIN_DEMO_API_SECRET": "read-secret",
            "BLOFIN_DEMO_PASSPHRASE": "read-passphrase",
            "BLOFIN_DEMO_TRADE_API_KEY": "trade-key",
            "BLOFIN_DEMO_TRADE_API_SECRET": "trade-secret",
            "BLOFIN_DEMO_TRADE_PASSPHRASE": "trade-passphrase",
            "BLOFIN_DASHBOARD_TOKEN": "dashboard-token",
        }

    def test_accepts_complete_demo_worker_configuration(self):
        self.assertEqual(preflight_errors(self.env), [])

    def test_rejects_missing_required_secret_without_echoing_value(self):
        self.env.pop("TURSO_AUTH_TOKEN")
        errors = preflight_errors(self.env)
        self.assertIn("missing TURSO_AUTH_TOKEN", errors)
        self.assertNotIn("test-token", " ".join(errors))

    def test_requires_explicit_worker_role(self):
        self.env["BLOFIN_DEMO_AUTOPILOT_ROLE"] = "WEB"
        self.assertTrue(any("ROLE must be WORKER" in error for error in preflight_errors(self.env)))

    def test_requires_both_activation_switches(self):
        self.env["BLOFIN_DEMO_TRADING_ENABLED"] = "false"
        self.env["BLOFIN_DEMO_AUTOPILOT_ENABLED"] = "false"
        errors = preflight_errors(self.env)
        self.assertTrue(any("BLOFIN_DEMO_TRADING_ENABLED" in error for error in errors))
        self.assertTrue(any("BLOFIN_DEMO_AUTOPILOT_ENABLED" in error for error in errors))

    def test_keeps_one_off_smoke_test_disabled(self):
        self.env["BLOFIN_DEMO_SMOKE_TEST_ENABLED"] = "true"
        self.assertIn(
            "BLOFIN_DEMO_SMOKE_TEST_ENABLED must remain false",
            preflight_errors(self.env),
        )


if __name__ == "__main__":
    unittest.main()
