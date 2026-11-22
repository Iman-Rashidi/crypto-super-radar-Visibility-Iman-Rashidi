from __future__ import annotations

import unittest
from unittest.mock import patch

from crypto_super_radar import web_app


class V2PublicDashboardSnapshotTests(unittest.TestCase):
    def test_public_snapshot_contains_only_latest_cycle_five_market_rows(self):
        rows = []
        for symbol in ("LINK-USDT", "XRP-USDT", "SOL-USDT", "ETH-USDT", "BTC-USDT"):
            rows.append({
                "cycle_id": "latest-cycle",
                "symbol": symbol,
                "started_at_utc": "2026-11-22T12:00:00+00:00",
                "finished_at_utc": "2026-11-22T12:01:00+00:00",
                "mode": "BLOFIN_DEMO",
                "status": "NO_TRADE",
                "signal": "HOLD",
                "reason": "No confirmed trend",
                "orders_submitted": False,
                "ai_votes": [
                    {
                        "reviewer_id": "nemotron", "provider": "OpenRouter",
                        "status": "UNKNOWN", "opinion": "UNKNOWN",
                        "reason_fa": "No reviewer call in this cycle",
                        "verified": False, "returned_model": "Nemotron",
                    },
                    {
                        "reviewer_id": "second_ai", "provider": "Groq",
                        "status": "NOT_CALLED", "opinion": "NOT_CALLED",
                        "reason_fa": "", "verified": False, "returned_model": "Groq",
                    },
                ],
                "ai_consensus": {"status": "UNKNOWN"},
            })
        rows.append({
            "cycle_id": "older-cycle", "symbol": "BTC-USDT",
            "started_at_utc": "older", "status": "OLD",
        })
        snapshot = {
            "status": "OK", "environment": "BLOFIN_DEMO",
            "cycles": rows,
            "run_state": {
                "status": "IDLE", "job_id": "job-1",
                "api_key": "must-not-leak", "token": "must-not-leak",
            },
            "open_exit_positions": [{"position_id": "private-position"}],
        }

        with patch.object(web_app, "_blofin_v2_cycle_snapshot", return_value=snapshot):
            result = web_app._blofin_v2_public_cycle_snapshot()

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["environment"], "BLOFIN_DEMO")
        self.assertEqual(result["latest_cycle_id"], "latest-cycle")
        self.assertEqual(len(result["cycles"]), 5)
        self.assertEqual(
            [row["symbol"] for row in result["cycles"]],
            ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT"],
        )
        self.assertEqual(result["cycles"][0]["ai_votes"][0]["status"], "UNKNOWN")
        self.assertEqual(result["run_state"]["job_id"], "job-1")
        self.assertNotIn("api_key", str(result))
        self.assertNotIn("token", str(result))
        self.assertNotIn("open_exit_positions", result)
        self.assertNotIn("position_id", str(result))


if __name__ == "__main__":
    unittest.main()
