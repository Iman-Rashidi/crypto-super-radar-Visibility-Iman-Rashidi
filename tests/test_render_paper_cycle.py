"""Offline tests for the scheduled Render paper-cycle entry point."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from scripts import render_paper_cycle


class RenderPaperCycleTests(unittest.TestCase):
    def test_local_sqlite_is_rejected_before_engine_creation(self):
        with (
            patch.object(render_paper_cycle, "storage_backend", return_value="LOCAL_SQLITE"),
            patch.object(render_paper_cycle, "WhaleAwareConfirmationEngine") as engine,
            self.assertRaises(SystemExit) as raised,
        ):
            render_paper_cycle.main(["--asset", "cex:BTCUSDT"])
        self.assertEqual(raised.exception.code, 2)
        engine.assert_not_called()

    def test_partial_turso_configuration_fails_closed(self):
        with (
            patch.object(
                render_paper_cycle,
                "storage_backend",
                side_effect=RuntimeError("partial Turso configuration"),
            ),
            patch.object(render_paper_cycle, "WhaleAwareConfirmationEngine") as engine,
            self.assertRaises(SystemExit) as raised,
        ):
            render_paper_cycle.main([])
        self.assertEqual(raised.exception.code, 2)
        engine.assert_not_called()

    def test_durable_storage_runs_exactly_one_paper_cycle(self):
        fake_engine = object()
        fake_bot = MagicMock()
        fake_bot.run_once.return_value = {"status": "NO_TRADE", "reason": "UNKNOWN evidence"}
        fake_bot.ledger = MagicMock()
        output = io.StringIO()

        with (
            patch.object(render_paper_cycle, "storage_backend", return_value="TURSO_CLOUD_SQLITE"),
            patch.object(
                render_paper_cycle,
                "WhaleAwareConfirmationEngine",
                return_value=fake_engine,
            ),
            patch.object(render_paper_cycle, "PaperTradingBot", return_value=fake_bot) as bot_factory,
            redirect_stdout(output),
        ):
            exit_code = render_paper_cycle.main(["--asset", "cex:ETHUSDT"])

        self.assertEqual(exit_code, 0)
        bot_factory.assert_called_once_with(fake_engine)
        fake_bot.run_once.assert_called_once_with("cex:ETHUSDT")
        fake_bot.ledger.close.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "PAPER_ONLY")
        self.assertEqual(payload["storage"], "TURSO_CLOUD_SQLITE")
        self.assertFalse(payload["orders_submitted"])
        self.assertFalse(payload["transactions_signed"])
        self.assertEqual(payload["result"]["status"], "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
