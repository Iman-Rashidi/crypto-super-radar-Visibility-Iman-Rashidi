from __future__ import annotations
import sqlite3
import unittest
from crypto_super_radar.challenge1_v2_autopilot import SYMBOLS, TABLE, preflight_errors, run_cycle

class Challenge1V2AutopilotDryRunTests(unittest.TestCase):
    def test_requires_explicit_enable_and_turso(self):
        self.assertIn("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED must be true", preflight_errors({}))
    def test_rejects_trade_write_configuration(self):
        env={"CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED":"true",
             "TURSO_DATABASE_URL":"libsql://unit.test","TURSO_AUTH_TOKEN":"fake",
             "BLOFIN_DEMO_TRADING_ENABLED":"true"}
        self.assertIn("BLOFIN_DEMO_TRADING_ENABLED must remain false for dry-run", preflight_errors(env))
    def test_scans_five_symbols_and_persists_without_orders(self):
        db=sqlite3.connect(":memory:")
        seen=[]
        def analyzer(client, symbol):
            seen.append(symbol)
            return {"execution_profile":"CHALLENGE_1_V2","signal":"NO_TRADE",
                    "status":"NO_TRADE","reason":"unit test","market_data_proof":{"common_asof":True}}
        payload=run_cycle(object(),db,analyzer=analyzer)
        self.assertEqual(tuple(seen),SYMBOLS)
        self.assertEqual(payload["status"],"CYCLE_COMPLETED")
        self.assertFalse(payload["orders_submitted"])
        self.assertFalse(payload["trade_executed"])
        self.assertFalse(payload["position_limit_enforced"])
        self.assertFalse(payload["tpsl_verified"])
        self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0],5)
        self.assertEqual(db.execute(f"SELECT SUM(orders_submitted) FROM {TABLE}").fetchone()[0],0)
        db.close()
    def test_data_failure_is_blocked_and_never_converted_to_signal(self):
        db=sqlite3.connect(":memory:")
        def analyzer(client,symbol): raise ValueError("fixture")
        payload=run_cycle(object(),db,analyzer=analyzer,symbols=("BTC-USDT",))
        self.assertEqual(payload["status"],"CYCLE_PARTIAL")
        self.assertEqual(payload["symbols"][0]["status"],"DATA_BLOCKED")
        self.assertFalse(payload["orders_submitted"])
        db.close()

if __name__ == "__main__": unittest.main()
