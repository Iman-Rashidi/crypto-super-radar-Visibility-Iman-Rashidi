from __future__ import annotations

import unittest
import numpy as np
import pandas as pd

from crypto_super_radar.easy_trade_management import (
    evaluate_breakout_retest, stop_execution_guidance, EasyPositionManager,
)


class EasyTradeManagementTests(unittest.TestCase):
    def breakout_frame(self, scenario="confirm"):
        n = 30
        close = np.full(n, 100.0)
        high = np.full(n, 101.0)
        low = np.full(n, 99.0)
        open_ = np.full(n, 100.0)
        volume = np.full(n, 100.0)
        close[23], high[23], low[23], open_[23], volume[23] = 102.0, 103.0, 100.5, 100.0, 220.0
        if scenario == "confirm":
            close[24], high[24], low[24], open_[24] = 101.0, 102.0, 100.4, 102.0
            close[25], high[25], low[25], open_[25] = 103.0, 104.0, 100.8, 101.0
        elif scenario == "false":
            close[24], high[24], low[24], open_[24] = 99.5, 102.0, 99.0, 102.0
        index = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
        return pd.DataFrame({"open": open_, "high": high, "low": low,
                             "close": close, "volume": volume}, index=index)

    def test_breakout_retest_and_later_confirmation(self):
        result = evaluate_breakout_retest(self.breakout_frame(), 100, "LONG")
        self.assertEqual(result["state"], "RETEST_CONFIRMED")
        self.assertTrue(result["volume_confirmed"])
        self.assertFalse(result["used_as_hard_gate"])

    def test_false_breakout_is_reported(self):
        result = evaluate_breakout_retest(self.breakout_frame("false"), 100, "LONG")
        self.assertEqual(result["state"], "FALSE_BREAKOUT")
        self.assertEqual(result["status"], "WARNING")

    def test_open_last_candle_is_excluded(self):
        frame = self.breakout_frame()
        frame.iloc[-1, frame.columns.get_loc("close")] = 105.0
        result = evaluate_breakout_retest(frame, 100, "LONG", last_candle_closed=False)
        self.assertNotEqual(result.get("confirmation_time"), frame.index[-1].isoformat())

    def test_incomplete_history_is_unknown(self):
        self.assertEqual(evaluate_breakout_retest(self.breakout_frame().head(10), 100, "LONG")["status"], "UNKNOWN")

    def test_hard_and_mental_stop_semantics_are_distinct(self):
        self.assertTrue(stop_execution_guidance("HARD")["automatic_trigger_expected"])
        self.assertFalse(stop_execution_guidance("MENTAL")["automatic_trigger_expected"])

    def test_position_manager_ignores_open_candle(self):
        manager = EasyPositionManager(db=connect_database(":memory:"))
        opened = manager.open_position("cex:BTCUSDT", "LONG", 2, 100, 95, 110)
        result = manager.update_position(opened["position_id"], {"open":100,"high":101,"low":90,"close":99},
                                         candle_closed=False)
        self.assertEqual(result["status"], "PROVISIONAL")
        self.assertEqual(manager.get_position(opened["position_id"])["bars_held"], 0)

    def test_hard_stop_closes_and_journals_position(self):
        manager = EasyPositionManager(db=__import__("sqlite3").connect(":memory:", check_same_thread=False))
        opened = manager.open_position("cex:BTCUSDT", "LONG", 2, 100, 95, 110)
        result = manager.update_position(opened["position_id"], {"open":100,"high":101,"low":94,"close":96})
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["reason"], "STOP_LOSS")
        self.assertEqual(manager.journal()[0]["exit_reason"], "STOP_LOSS")

    def test_ambiguous_stop_and_target_bar_uses_stop_first(self):
        manager = EasyPositionManager(db=__import__("sqlite3").connect(":memory:", check_same_thread=False))
        opened = manager.open_position("cex:BTCUSDT", "LONG", 1, 100, 95, 105)
        result = manager.update_position(opened["position_id"], {"open":100,"high":106,"low":94,"close":101})
        self.assertEqual(result["reason"], "STOP_LOSS")
        self.assertEqual(result["conservative_intrabar_rule"], "STOP_FIRST")

    def test_trailing_stop_only_ratchets_toward_profit(self):
        manager = EasyPositionManager(db=__import__("sqlite3").connect(":memory:", check_same_thread=False))
        opened = manager.open_position("cex:BTCUSDT", "LONG", 1, 100, 90, 130)
        first = manager.update_position(opened["position_id"], {"open":100,"high":110,"low":99,"close":108}, atr_value=3)
        self.assertGreater(first["current_stop"], 90)
        second = manager.update_position(opened["position_id"], {"open":108,"high":109,"low":100,"close":102}, atr_value=10)
        self.assertGreaterEqual(second["current_stop"], first["current_stop"])

    def test_mental_stop_only_alerts_until_manual_close(self):
        manager = EasyPositionManager(db=__import__("sqlite3").connect(":memory:", check_same_thread=False))
        opened = manager.open_position("cex:BTCUSDT", "LONG", 1, 100, 95, 110, stop_mode="MENTAL")
        result = manager.update_position(opened["position_id"], {"open":100,"high":101,"low":94,"close":96})
        self.assertEqual(result["status"], "OPEN")
        self.assertTrue(result["mental_stop_alert"])
        closed = manager.manual_close(opened["position_id"], 96, "MANUAL_STOP")
        self.assertEqual(closed["status"], "CLOSED")
        self.assertEqual(manager.journal()[0]["exit_reason"], "MANUAL_STOP")


if __name__ == "__main__":
    unittest.main()
