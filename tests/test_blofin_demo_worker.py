from __future__ import annotations

import unittest

from crypto_super_radar.blofin_demo_worker import validate_worker_environment


class Challenge1WorkerEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "BLOFIN_DEMO_AUTOPILOT_ROLE": "WORKER",
            "BLOFIN_DEMO_AUTOPILOT_ENABLED": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "TURSO_DATABASE_URL": "libsql://demo.example.turso.io",
            "TURSO_AUTH_TOKEN": "test-token",
            "BLOFIN_DASHBOARD_TOKEN": "test-dashboard-token",
            "BLOFIN_DEMO_TRADE_API_KEY": "demo-key",
            "BLOFIN_DEMO_TRADE_API_SECRET": "demo-secret",
            "BLOFIN_DEMO_TRADE_PASSPHRASE": "demo-passphrase",
        }

    def test_accepts_explicit_demo_worker_configuration(self):
        self.assertIsNone(validate_worker_environment(self.env))

    def test_rejects_web_role(self):
        self.env["BLOFIN_DEMO_AUTOPILOT_ROLE"] = "WEB"
        with self.assertRaisesRegex(RuntimeError, "must be WORKER"):
            validate_worker_environment(self.env)

    def test_rejects_disabled_autopilot(self):
        self.env["BLOFIN_DEMO_AUTOPILOT_ENABLED"] = "false"
        with self.assertRaisesRegex(RuntimeError, "AUTOPILOT_ENABLED"):
            validate_worker_environment(self.env)

    def test_rejects_disabled_demo_trading(self):
        self.env["BLOFIN_DEMO_TRADING_ENABLED"] = "false"
        with self.assertRaisesRegex(RuntimeError, "TRADING_ENABLED"):
            validate_worker_environment(self.env)

    def test_rejects_missing_durable_storage(self):
        self.env.pop("TURSO_AUTH_TOKEN")
        with self.assertRaisesRegex(RuntimeError, "Turso"):
            validate_worker_environment(self.env)

    def test_rejects_partial_credential_group(self):
        self.env.pop("BLOFIN_DEMO_TRADE_PASSPHRASE")
        with self.assertRaisesRegex(RuntimeError, "credential groups"):
            validate_worker_environment(self.env)

    def test_accepts_complete_existing_demo_key_group(self):
        for key in (
            "BLOFIN_DEMO_TRADE_API_KEY",
            "BLOFIN_DEMO_TRADE_API_SECRET",
            "BLOFIN_DEMO_TRADE_PASSPHRASE",
        ):
            self.env.pop(key)
        self.env.update({
            "BLOFIN_DEMO_API_KEY": "demo-read-write-key",
            "BLOFIN_DEMO_API_SECRET": "demo-read-write-secret",
            "BLOFIN_DEMO_PASSPHRASE": "demo-read-write-passphrase",
        })
        self.assertIsNone(validate_worker_environment(self.env))


if __name__ == "__main__":
    unittest.main()
