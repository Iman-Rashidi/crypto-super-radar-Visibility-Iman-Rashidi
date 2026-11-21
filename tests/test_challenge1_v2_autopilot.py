from __future__ import annotations
import sqlite3
import unittest
from crypto_super_radar.challenge1_v2_autopilot import SYMBOLS,TABLE,preflight_errors,run_cycle,manage_open_positions,build_v2_demo_risk_policy
from crypto_super_radar.challenge1_v2_trade_controls import v2_entry_capacity
from crypto_super_radar.blofin_demo_executor import BloFinDemoExecutor

class Challenge1V2FourSlotPolicyTests(unittest.TestCase):
    def test_runtime_policy_is_four_slots_and_blocks_the_fifth(self):
        policy = build_v2_demo_risk_policy()
        self.assertEqual(policy.max_concurrent_positions, 4)
        self.assertEqual(policy.max_leverage, 1)
        executor = BloFinDemoExecutor(client=object(), store=object(), policy=policy, trading_enabled=True)
        self.assertEqual(executor.policy.max_concurrent_positions, 4)
        for active_count in range(4):
            positions = [{"instId": f"ASSET{i + 1}-USDT"} for i in range(active_count)]
            allowed, reason = v2_entry_capacity(
                positions, [], candidate_symbol="NEWCOIN-USDT",
                max_concurrent_positions=policy.max_concurrent_positions,
            )
            self.assertTrue(allowed, reason)
        four_positions = [{"instId": f"ASSET{i + 1}-USDT"} for i in range(4)]
        allowed, reason = v2_entry_capacity(
            four_positions, [], candidate_symbol="FIFTH-USDT",
            max_concurrent_positions=policy.max_concurrent_positions,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "MAX_CONCURRENT_POSITIONS_REACHED")


class Challenge1V2AutopilotDryRunTests(unittest.TestCase):
    def test_requires_explicit_enable_and_turso(self):
        self.assertIn("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED must be true",preflight_errors({}))
    def test_both_opt_in_flags_are_allowed_but_not_required_for_dry_run(self):
        env={"CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED":"true","TURSO_DATABASE_URL":"libsql://unit.test",
             "TURSO_AUTH_TOKEN":"fake","BLOFIN_DEMO_TRADING_ENABLED":"true",
             "BLOFIN_DEMO_AUTOPILOT_ENABLED":"false",
             "BLOFIN_DEMO_TRADE_API_KEY":"demo-key","BLOFIN_DEMO_TRADE_API_SECRET":"demo-secret",
             "BLOFIN_DEMO_TRADE_PASSPHRASE":"demo-pass"}
        self.assertEqual(preflight_errors(env),[])
    def test_scans_five_symbols_and_persists_without_orders(self):
        db=sqlite3.connect(":memory:"); seen=[]
        def analyzer(client,symbol):
            seen.append(symbol)
            return {"execution_profile":"CHALLENGE_1_V2","signal":"NO_TRADE","status":"NO_TRADE","reason":"unit"}
        payload=run_cycle(object(),db,analyzer=analyzer)
        self.assertEqual(tuple(seen),SYMBOLS); self.assertEqual(payload["status"],"CYCLE_COMPLETED")
        self.assertFalse(payload["orders_submitted"]); self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0],5)
        db.close()
    def test_data_failure_is_blocked(self):
        db=sqlite3.connect(":memory:")
        payload=run_cycle(object(),db,analyzer=lambda *_: (_ for _ in ()).throw(ValueError()),symbols=("BTC-USDT",))
        self.assertEqual(payload["status"],"CYCLE_PARTIAL"); self.assertFalse(payload["orders_submitted"]); db.close()

class FakeStore:
    def __init__(self,state): self.state=dict(state)
    def list_v2_position_exit_states(self,active_only=True,limit=100):
        return [dict(self.state,payload=dict(self.state))] if self.state else []
    def save_v2_position_exit_state(self,state): self.state=dict(state); return self.state

class FakeExchange:
    def __init__(self,mark="102.4"): self.mark=mark; self.calls=[]
    def get_positions(self,inst_id=None):
        return [{"instId":"BTC-USDT","positionId":"pos-1","positionSide":"net","positions":"2"}]
    def get_mark_price(self,*,inst_id): return [{"markPrice":self.mark}]
    def get_open_tpsl_orders(self,*,inst_id,client_order_id,limit):
        return [{"positionId":"pos-1","state":"live","slTriggerPrice":"98","tpslId":"tpsl-1"}]
    def partial_close_reduce_only(self,**kw): self.calls.append(("partial",kw)); return {"data":[{"orderId":"tp1"}]}
    def get_order_detail(self,inst_id,*,client_order_id):
        return [{"clientOrderId":client_order_id,"state":"filled","filledSize":"1"}]
    def amend_or_replace_sl(self,**kw): self.calls.append(("amend_sl",kw)); return {"sl_verified":True}
    def get_candles(self,inst_id,*,bar,limit):
        base=1_800_000_000_000
        return [[base+i*900000,"1","2","1","2","1","1","1","1"] for i in range(12)]
    def place_limit_exit_reduce_only(self,**kw): self.calls.append(("time_exit",kw)); return {"data":[{"orderId":"time-exit"}]}

class Challenge1V2ExitManagerTests(unittest.TestCase):
    def state(self):
        return {"position_id":"pos-1","client_order_id":"entry-1","instrument_id":"BTC-USDT","side":"LONG",
          "entry_price":"100","initial_stop_loss":"98","original_size":"2","remaining_size":"2",
          "tp1_executed":False,"candles_elapsed":0,"entry_time_ms":1_799_999_999_999,
          "net_break_even_price":"100.15","sl_tpsl_id":"tpsl-1","status":"OPEN"}
    def test_tp1_half_fill_then_net_be_sl_verified(self):
        store,exchange=FakeStore(self.state()),FakeExchange()
        out=manage_open_positions(exchange,store,now_ms=1_800_010_000_000)
        self.assertEqual(out["results"][0]["status"],"TP1_EXECUTED_SL_VERIFIED")
        self.assertTrue(store.state["tp1_executed"]); self.assertEqual(store.state["remaining_size"],"1")
        self.assertEqual([c[0] for c in exchange.calls],["partial","amend_sl"])
    def test_12_closed_candles_submit_reduce_only_limit_exit(self):
        store,exchange=FakeStore(self.state()),FakeExchange(mark="100")
        out=manage_open_positions(exchange,store,now_ms=1_800_020_000_000)
        self.assertEqual(out["results"][0]["status"],"TIME_EXIT_SUBMITTED")
        self.assertEqual(exchange.calls[0][0],"time_exit"); self.assertEqual(store.state["status"],"TIME_EXIT_PENDING")


class FakeEntryExecutor:
    def __init__(self,store): self.store=store; self.called=0
    def execute(self,signal,*,inst_id,allow_demo_orders,leverage):
        self.called+=1
        assert signal["ai_consensus"]["status"]=="APPROVED_BY_BOTH"
        assert allow_demo_orders is True
        return {"position_id":"pos-1","client_order_id":"entry123","instrument_id":inst_id,
          "side":"LONG","entry_price":"100","entry_reference_price":"100","stop_loss":"98",
          "quantity_contracts":"2","filled_size":"2","net_break_even_price":"100.15"}


class Challenge1V2EntryIntegrationTests(unittest.TestCase):
    def test_two_reviewer_approval_enters_executor_and_persists_exit_state(self):
        db=sqlite3.connect(":memory:")
        store=FakeStore({})
        executor=FakeEntryExecutor(store)
        exchange=FakeExchange(mark="100")
        signal={"execution_profile":"CHALLENGE_1_V2","instrument_id":"BTC-USDT","signal":"LONG",
          "status":"SIGNAL_READY","ai_consensus":{"status":"APPROVED_BY_BOTH"}}
        env={"CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED":"true","BLOFIN_DEMO_TRADING_ENABLED":"true"}
        out=run_cycle(exchange,db,analyzer=lambda *_:signal,symbols=("BTC-USDT",),
                      executor=executor,environ=env)
        self.assertEqual(executor.called,1)
        self.assertTrue(out["orders_submitted"])
        self.assertEqual(store.state["position_id"],"pos-1")
        self.assertEqual(store.state["net_break_even_price"],"100.15")
        self.assertEqual(store.state["sl_tpsl_id"],"tpsl-1")
        db.close()

if __name__=="__main__": unittest.main()
