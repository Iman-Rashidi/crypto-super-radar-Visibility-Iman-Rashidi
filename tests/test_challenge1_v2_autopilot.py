from __future__ import annotations
import os
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


    def test_v2_policy_uses_half_percent_risk_and_5000_usdt_notional_cap(self):
        policy = build_v2_demo_risk_policy()
        self.assertEqual(policy.risk_per_trade_pct, 0.005)
        self.assertEqual(policy.max_position_notional_usdt, 5000.0)
        self.assertEqual(policy.max_aggregate_risk_pct, 0.005)
        self.assertEqual(policy.max_leverage, 1)
        self.assertEqual(policy.max_concurrent_positions, 4)


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

    def test_time_exit_remains_active_after_tp1_and_break_even(self):
        state=self.state()
        state.update(tp1_executed=True,remaining_size="1",status="TP1_EXECUTED",
                     break_even_stop_loss="100.15")
        store,exchange=FakeStore(state),FakeExchange(mark="100.2")
        out=manage_open_positions(exchange,store,now_ms=1_800_020_000_000)
        self.assertEqual(out["results"][0]["status"],"TIME_EXIT_SUBMITTED")
        self.assertEqual(exchange.calls[0][0],"time_exit")
        self.assertEqual(exchange.calls[0][1].get("position_id"),"pos-1")
        self.assertEqual(store.state["status"],"TIME_EXIT_PENDING")


class FakeEntryExecutor:
    def __init__(self,store): self.store=store; self.called=0
    def execute(self,signal,*,inst_id,allow_demo_orders,leverage):
        self.called+=1
        assert signal["ai_consensus"]["status"]=="APPROVED_BY_GROQ"
        assert allow_demo_orders is True
        return {"position_id":"pos-1","client_order_id":"entry123","instrument_id":inst_id,
          "side":"LONG","entry_price":"100","entry_reference_price":"100","stop_loss":"98",
          "quantity_contracts":"2","filled_size":"2","net_break_even_price":"100.15"}


class Challenge1V2EntryIntegrationTests(unittest.TestCase):
    def test_single_groq_approval_enters_executor_and_persists_exit_state(self):
        db=sqlite3.connect(":memory:")
        store=FakeStore({})
        executor=FakeEntryExecutor(store)
        exchange=FakeExchange(mark="100")
        signal={"execution_profile":"CHALLENGE_1_V2","instrument_id":"BTC-USDT","signal":"LONG",
          "status":"SIGNAL_READY","ai_consensus":{"status":"APPROVED_BY_GROQ","single_reviewer_approval":True,"reviewer_id":"groq"}}
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


from unittest.mock import patch
import pandas as pd


class Challenge1V2BalancedEntryTests(unittest.TestCase):
    def test_adx_trend_does_not_require_swing_structure_or_4h_alignment(self):
        from crypto_super_radar import challenge1_v2 as strategy
        i1 = pd.date_range("2026-11-01", periods=210, freq="1h", tz="UTC")
        i4 = pd.date_range("2026-10-01", periods=210, freq="4h", tz="UTC")
        c1 = pd.Series([100.0 + n * 0.2 for n in range(210)], index=i1)
        h1 = pd.DataFrame({"open": c1 - .05, "high": c1 + .1, "low": c1 - .1,
                           "close": c1, "volume": 100.0}, index=i1)
        c4 = pd.Series([200.0] * 210, index=i4)
        h4 = pd.DataFrame({"open": c4, "high": c4 + .1, "low": c4 - .1,
                           "close": c4, "volume": 100.0}, index=i4)
        with patch.object(strategy, "adx", return_value=pd.Series([25.0] * 210, index=i1)), patch.object(
            strategy, "_swing_structure", side_effect=[False, False]
        ):
            regime = strategy._regime(h1, h4, i1[-1] + pd.Timedelta(hours=1))
        self.assertEqual(regime["direction"], "LONG")
        self.assertEqual(regime["status"], "TREND")
        self.assertGreaterEqual(regime["adx_1h"], 22)
        self.assertFalse(regime["structure_aligned_1h"])
        self.assertFalse(regime["aligned_4h"])
        self.assertFalse(regime["opposed_4h"])

    def test_neutral_4h_and_ambiguous_elliott_reach_two_reviewer_supervisor(self):
        from crypto_super_radar import challenge1_v2_orchestration as flow
        asof_ms = 1_800_000_000_000
        asof = pd.Timestamp(asof_ms, unit="ms", tz="UTC")
        h1_open = (asof - pd.Timedelta(hours=1)).isoformat()
        h4_open = (asof - pd.Timedelta(hours=4)).isoformat()
        m15_open = (asof - pd.Timedelta(minutes=15)).isoformat()
        signal_id = f"challenge1-v2:BTC-USDT:15m:{m15_open}:LONG"
        wave = {"status": "AMBIGUOUS", "pattern": "AMBIGUOUS", "phase": "AMBIGUOUS",
                "wave_alignment": "UNCONFIRMED", "timeframe": "1H",
                "evidence_source": "CONFIRMED_CLOSED_1H_CANDLES",
                "data_asof_utc": asof.isoformat(), "latest_closed_candle_open_utc": h1_open}
        record = {"signal": "LONG", "signal_id": signal_id, "instrument_id": "BTC-USDT",
                  "status": "ANALYSIS_READY", "orders_submitted": False,
                  "regime": {"direction": "LONG", "status": "TREND", "aligned_4h": False,
                             "neutral_4h": True, "opposed_4h": False,
                             "candle_open_utc": h1_open, "candle_open_utc_4h": h4_open},
                  "challenge1_v2_evidence": {"elliott_wave": wave}}
        frames = [
            pd.DataFrame({"close": [1.0]}, index=[asof - pd.Timedelta(hours=1)]),
            pd.DataFrame({"close": [1.0]}, index=[asof - pd.Timedelta(hours=4)]),
            # The 5m input must satisfy the Elliott engine's full OHLCV contract.
            pd.DataFrame(
                {
                    "timestamp": pd.date_range(
                        end=asof - pd.Timedelta(minutes=5), periods=30,
                        freq="5min", tz="UTC",
                    ),
                    "open": [100.0] * 30,
                    "high": [101.0] * 30,
                    "low": [99.0] * 30,
                    "close": [100.0] * 30,
                    "volume": [1000.0] * 30,
                },
            ).set_index("timestamp"),
            pd.DataFrame({"close": [1.0]}, index=[asof - pd.Timedelta(minutes=15)]),
            pd.DataFrame({"close": [1.0]}, index=[asof - pd.Timedelta(minutes=15)]),
        ]
        reviewer_result = {"approved": False, "status": "PENDING", "ai_reviews": [],
                           "ai_consensus": {"status": "PENDING"}}
        with patch.object(flow, "_contract_specs", return_value={"instId": "BTC-USDT"}), patch.object(
            flow, "_market_quote", return_value={"asof_ts_ms": asof_ms, "last": 100.0, "spread_fraction": .0001}
        ), patch.object(flow, "_closed_frame", side_effect=frames), patch.object(
            flow, "_regular_funding", return_value={"funding_rate": 0.0, "funding_kind": "0",
                "funding_classification": "STANDARD_CRYPTO_FUNDING", "funding_event_time_ms": asof_ms - 60000,
                "funding_max_age_ms": 9 * 60 * 60_000, "funding_age_ms": 60000}
        ), patch.object(flow, "analyze_signal", return_value=record), patch.object(
            flow, "challenge1_v2_execution_evidence_ready", return_value=(True, "ok")
        ), patch.object(flow, "run_challenge1_v2_supervisor_reviews", return_value=reviewer_result) as review:
            class DummyClient:
                def get_candles(self, instrument_id, *, bar, limit):
                    # Supply realistic, closed synthetic candles for every requested
                    # interval, including the newly mandatory 5m feed. The primary
                    # orchestration frames remain isolated by the _closed_frame stub.
                    intervals = {"5m": 5 * 60_000, "15m": 15 * 60_000,
                                 "1H": 60 * 60_000, "4H": 4 * 60 * 60_000}
                    interval = intervals[bar]
                    count = min(limit, 300 if bar in {"1H", "4H"} else 120)
                    return [
                        [str(asof_ms - interval * (count - i)), "100", "101",
                         "99", "100", "1000", "100000", "0", "1"]
                        for i in range(count)
                    ]
            out = flow.analyze_challenge1_v2_market(DummyClient(), "BTC-USDT", local_now_ms=asof_ms)
        self.assertEqual(out["signal"], "LONG")
        self.assertEqual(out["status"], "AI_REVIEW_BLOCKED")
        self.assertFalse(out["orders_submitted"])
        self.assertEqual(review.call_count, 1)
        self.assertFalse(out["regime"]["aligned_4h"])
        self.assertEqual(out["challenge1_v2_evidence"]["elliott_wave"]["phase"], "AMBIGUOUS")

class Challenge1V2ElliottDiagnosticsRegressionTests(unittest.TestCase):
    def _record(self, wave):
        import pandas as pd
        asof = pd.Timestamp("2026-11-25T18:00:00Z")
        h1_open = asof - pd.Timedelta(hours=1)
        h4_open = asof - pd.Timedelta(hours=4)
        signal_open = asof - pd.Timedelta(minutes=15)
        symbol, side = "BTC-USDT", "LONG"
        signal_id = f"challenge1-v2:{symbol}:15m:{signal_open.isoformat()}:{side}"
        funding_time = int((asof - pd.Timedelta(minutes=1)).timestamp() * 1000)
        funding_max_age = 9 * 60 * 60_000
        return {
            "signal": side, "instrument_id": symbol, "orders_submitted": False,
            "signal_id": signal_id, "signal_candle_open_utc": signal_open.isoformat(),
            "entry": 100.0, "stop_loss": 98.0, "take_profit": 104.0,
            "confidence_score": 70.0, "score_parts": {"trend": 70.0},
            "gross_rr": 2.0, "funding_rate": 0.0, "spread_fraction": 0.0001,
            "funding_kind": "0", "funding_event_time_ms": funding_time,
            "funding_max_age_ms": funding_max_age,
            "regime": {"direction": side, "status": "TREND",
                       "candle_open_utc": h1_open.isoformat(),
                       "candle_open_utc_4h": h4_open.isoformat()},
            "challenge1_v2_evidence": {
                "decision_engine_version": "CHALLENGE_1_V2_1H_ADX_EMA21_15M_EXECUTION",
                "signal_id": signal_id, "instrument_id": symbol,
                "setup_timeframe": "1H", "entry_execution_timeframe": "15m",
                "context_timeframes": ["1H", "4H"], "higher_timeframes_used": True,
                "completed_candles_only": True, "signal_candle_open_utc": signal_open.isoformat(),
                "context_1h_candle_open_utc": h1_open.isoformat(),
                "context_4h_candle_open_utc": h4_open.isoformat(),
                "data_asof_utc": asof.isoformat(), "funding_rate": 0.0,
                "funding_kind": "0", "funding_event_time_ms": funding_time,
                "funding_max_age_ms": funding_max_age, "elliott_wave": wave,
            },
        }

    def test_confirmed_directional_elliott_evidence_passes_the_contract(self):
        from crypto_super_radar.challenge1_v2 import challenge1_v2_execution_evidence_ready
        wave = {
            "status": "CANDIDATE", "pattern": "BULLISH",
            "phase": "WAVES_1_AND_2_CONFIRMED", "wave_alignment": "ALIGNED",
            "timeframe": "1H", "evidence_source": "CONFIRMED_CLOSED_1H_CANDLES",
            "data_asof_utc": "2026-11-25T18:00:00+00:00",
            "latest_closed_candle_open_utc": "2026-11-25T17:00:00+00:00",
            "confirmed_pivot_count": 3, "invalidation_price": 95.0,
            "pivots": [
                {"timestamp": "2026-11-25T17:00:00+00:00", "confirmed_at": "2026-11-25T17:10:00+00:00", "kind": "LOW", "price": 96.0},
                {"timestamp": "2026-11-25T17:20:00+00:00", "confirmed_at": "2026-11-25T17:30:00+00:00", "kind": "HIGH", "price": 102.0},
                {"timestamp": "2026-11-25T17:40:00+00:00", "confirmed_at": "2026-11-25T17:50:00+00:00", "kind": "LOW", "price": 98.0},
            ],
        }
        ready, reason = challenge1_v2_execution_evidence_ready(self._record(wave))
        self.assertTrue(ready, reason)

    def test_no_rule_valid_impulse_remains_fail_closed_with_field_diagnostics(self):
        from crypto_super_radar.challenge1_v2 import challenge1_v2_execution_evidence_ready
        wave = {
            "status": "AMBIGUOUS", "pattern": "AMBIGUOUS",
            "phase": "NO_RULE_VALID_IMPULSE", "wave_alignment": "UNCONFIRMED",
            "timeframe": "1H", "evidence_source": "CONFIRMED_CLOSED_1H_CANDLES",
            "data_asof_utc": "2026-11-25T18:00:00+00:00",
            "latest_closed_candle_open_utc": "2026-11-25T17:00:00+00:00",
            "confirmed_pivot_count": 2, "pivots": [],
        }
        ready, reason = challenge1_v2_execution_evidence_ready(self._record(wave))
        self.assertFalse(ready)
        self.assertIn("no_rule_valid_impulse", reason)
        self.assertIn("NO_RULE_VALID_IMPULSE", reason)

    def test_opposed_elliott_still_fails_closed_with_field_diagnostics(self):
        from crypto_super_radar.challenge1_v2 import challenge1_v2_execution_evidence_ready
        wave = {
            "status": "CANDIDATE", "pattern": "BEARISH",
            "phase": "WAVE_3_CONFIRMED", "wave_alignment": "OPPOSED",
            "timeframe": "1H", "evidence_source": "CONFIRMED_CLOSED_1H_CANDLES",
            "data_asof_utc": "2026-11-25T18:00:00+00:00",
            "latest_closed_candle_open_utc": "2026-11-25T17:00:00+00:00",
        }
        ready, reason = challenge1_v2_execution_evidence_ready(self._record(wave))
        self.assertFalse(ready)
        self.assertIn("wave_alignment_opposed", reason)
        self.assertIn("BEARISH", reason)


class Challenge1V2ManagerOnlyModeTests(unittest.TestCase):
    def test_scheduled_manager_only_cycle_never_analyzes_or_opens_new_entries(self):
        db = sqlite3.connect(":memory:")
        store = FakeStore({})
        executor = FakeEntryExecutor(store)
        exchange = FakeExchange()
        class NoopNotifier:
            def notify_async(self, *_args, **_kwargs):
                pass
        def forbidden_analyzer(*_args):
            raise AssertionError("manager-only cycle must not analyze new candidates")
        env = {
            "CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "CHALLENGE_1_V2_MANAGE_ONLY": "true",
        }
        try:
            result = run_cycle(
                exchange, db, analyzer=forbidden_analyzer, symbols=("BTC-USDT", "ETH-USDT"),
                executor=executor, environ=env, notifier=NoopNotifier(),
            )
            self.assertEqual(result["status"], "CYCLE_COMPLETED")
            self.assertFalse(result["orders_submitted"])
            self.assertEqual(executor.called, 0)
            self.assertEqual([row["status"] for row in result["symbols"]],
                             ["MANAGEMENT_ONLY", "MANAGEMENT_ONLY"])
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0], 2)
        finally:
            db.close()




class Challenge1V2EasyDemoEntryModeTests(unittest.TestCase):
    def _wave(self, phase="NO_RULE_VALID_IMPULSE", alignment="UNCONFIRMED"):
        status = "CANDIDATE" if alignment == "OPPOSED" else "AMBIGUOUS"
        pattern = "BEARISH" if alignment == "OPPOSED" else "AMBIGUOUS"
        return {
            "status": status, "pattern": pattern, "phase": phase,
            "wave_alignment": alignment, "timeframe": "1H",
            "evidence_source": "CONFIRMED_CLOSED_1H_CANDLES",
            "data_asof_utc": "2026-11-25T18:00:00+00:00",
            "latest_closed_candle_open_utc": "2026-11-25T17:00:00+00:00",
            "confirmed_pivot_count": 2, "pivots": [],
        }

    def test_ambiguous_or_opposed_elliott_is_advisory_only_in_demo_mode(self):
        from crypto_super_radar.challenge1_v2 import challenge1_v2_execution_evidence_ready
        helper = Challenge1V2ElliottDiagnosticsRegressionTests()
        for wave in (
            self._wave("NO_RULE_VALID_IMPULSE", "UNCONFIRMED"),
            self._wave("WAVE_3_CONFIRMED", "OPPOSED"),
        ):
            record = helper._record(wave)
            with patch.dict(os.environ, {
                "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
                "BLOFIN_DEMO_TRADING_ENABLED": "true",
            }):
                ready, reason = challenge1_v2_execution_evidence_ready(record)
            self.assertTrue(ready, reason)

    def test_elliott_remains_blocking_outside_demo_mode(self):
        from crypto_super_radar.challenge1_v2 import challenge1_v2_execution_evidence_ready
        record = Challenge1V2ElliottDiagnosticsRegressionTests()._record(
            self._wave("NO_RULE_VALID_IMPULSE", "UNCONFIRMED")
        )
        with patch.dict(os.environ, {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "false",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
        }):
            ready, reason = challenge1_v2_execution_evidence_ready(record)
        self.assertFalse(ready)
        self.assertIn("no_rule_valid_impulse", reason)

    def test_low_adx_can_use_directional_ema_only_in_demo_mode(self):
        from crypto_super_radar import challenge1_v2 as strategy
        i1 = pd.date_range("2026-11-01", periods=210, freq="1h", tz="UTC")
        i4 = pd.date_range("2026-10-01", periods=210, freq="4h", tz="UTC")
        c1 = pd.Series([100.0 + n * 0.2 for n in range(210)], index=i1)
        h1 = pd.DataFrame({"open": c1 - .05, "high": c1 + .1, "low": c1 - .1,
                           "close": c1, "volume": 100.0}, index=i1)
        c4 = pd.Series([200.0] * 210, index=i4)
        h4 = pd.DataFrame({"open": c4, "high": c4 + .1, "low": c4 - .1,
                           "close": c4, "volume": 100.0}, index=i4)
        with patch.object(strategy, "adx", return_value=pd.Series([15.0] * 210, index=i1)), patch.dict(
            os.environ, {"CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
                         "BLOFIN_DEMO_TRADING_ENABLED": "true"}
        ):
            regime = strategy._regime(h1, h4, i1[-1] + pd.Timedelta(hours=1))
        self.assertEqual(regime["status"], "TREND")
        self.assertEqual(regime["direction"], "LONG")
        self.assertEqual(regime["adx_1h"], 15.0)

    def test_btc_opposition_is_advisory_only_in_demo_mode(self):
        from crypto_super_radar.challenge1_v2 import _btc_master_guard_allows_entry
        with patch.dict(os.environ, {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
        }):
            self.assertTrue(_btc_master_guard_allows_entry(False))
        with patch.dict(os.environ, {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "false",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
        }):
            self.assertFalse(_btc_master_guard_allows_entry(False))


class Challenge1V2EasyDemoIndicatorConfirmationTests(unittest.TestCase):
    def test_long_uses_h1_ema_direction_with_m15_rsi_and_green_candle(self):
        from crypto_super_radar.challenge1_v2 import _easy_demo_entry_confirmation
        result = _easy_demo_entry_confirmation(
            "LONG", {"open": 100.0, "close": 100.2}, 61.0
        )
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["candle_momentum"])
        self.assertTrue(result["rsi_aligned"])

    def test_short_uses_m15_rsi_and_red_candle(self):
        from crypto_super_radar.challenge1_v2 import _easy_demo_entry_confirmation
        result = _easy_demo_entry_confirmation(
            "SHORT", {"open": 100.2, "close": 100.0}, 44.0
        )
        self.assertTrue(result["confirmed"])

    def test_flat_candle_or_extreme_rsi_does_not_create_entry(self):
        from crypto_super_radar.challenge1_v2 import _easy_demo_entry_confirmation
        flat = _easy_demo_entry_confirmation(
            "LONG", {"open": 100.0, "close": 100.0}, 60.0
        )
        overbought = _easy_demo_entry_confirmation(
            "LONG", {"open": 100.0, "close": 100.2}, 82.0
        )
        self.assertFalse(flat["confirmed"])
        self.assertFalse(overbought["confirmed"])
