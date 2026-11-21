from __future__ import annotations
import unittest
import numpy as np
import pandas as pd
from crypto_super_radar.adaptive_brain import ShadowPatternBrain
from crypto_super_radar.ai_sandbox import _actual_entry_veto, _v3_pattern_entry_gate, _v3_pattern_shadow_observation, _v3_pattern_research_observation
from crypto_super_radar.pattern_intelligence_v3 import PATTERN_FAMILIES,advance_pattern_lifecycle,analyze_unified_patterns_v3,closed_frame,normalized_candle_metrics,per_pattern_name_features

def frame(rows=100,scale=1.0):
    idx=pd.date_range("2026-01-01T00:00:00Z",periods=rows,freq="15min"); base=100+np.linspace(0,8,rows)+np.sin(np.arange(rows)/3)
    o=base-np.cos(np.arange(rows))*.08; c=base+np.sin(np.arange(rows)/2)*.05
    return pd.DataFrame({"open":o*scale,"high":(np.maximum(o,c)+.25)*scale,"low":(np.minimum(o,c)-.25)*scale,"close":c*scale,"volume":np.full(rows,1000.)},index=idx)

class CausalPatternTests(unittest.TestCase):
    def test_only_closed_bars(self):
        f=frame(80); t=f.index[49]+pd.Timedelta(minutes=15); visible=closed_frame(f,"15m",t)
        self.assertEqual(len(visible),50); self.assertTrue(all(x+pd.Timedelta(minutes=15)<=t for x in visible.index))
    def test_future_mutation_does_not_change_snapshot(self):
        f=frame(); t=f.index[70]+pd.Timedelta(minutes=15)
        a=analyze_unified_patterns_v3(instrument_id="X",frames={"15m":f,"1H":f,"4H":f},asof=t,expected_direction="LONG",lifecycle_state={})
        g=f.copy(); mask=g.index+pd.Timedelta(minutes=15)>t; g.loc[mask,["open","high","low","close","volume"]]*=9
        b=analyze_unified_patterns_v3(instrument_id="X",frames={"15m":g,"1H":g,"4H":g},asof=t,expected_direction="LONG",lifecycle_state={})
        self.assertEqual(a["features"],b["features"]); self.assertEqual(a["pattern_name_features"],b["pattern_name_features"])
        self.assertEqual(a["timeframes"]["15m"]["atr_normalized"],b["timeframes"]["15m"]["atr_normalized"])
    def test_atr_ratios_are_price_scale_invariant(self):
        a=normalized_candle_metrics(frame(scale=1)); b=normalized_candle_metrics(frame(scale=250))
        for k in ("body_atr","upper_wick_atr","lower_wick_atr","range_atr"): self.assertAlmostEqual(a[k],b[k],places=8)

class LifecycleTests(unittest.TestCase):
    def test_forming_confirmed_invalidated(self):
        f=frame(60); state={"X|15m|B":{"instrument_id":"X","timeframe":"15m","direction":"BULLISH","status":"FORMING","trigger_price":1.,"invalidation_price":.5,"created_at":(f.index[-3]+pd.Timedelta(minutes=15)).isoformat(),"invalidated_after_confirmation":False,"max_age_bars":12,"last_updated_asof":None}}
        t=f.index[-2]+pd.Timedelta(minutes=15); f.loc[f.index[-2],"close"]=2.; f.loc[f.index[-2],"high"]=2.1
        advance_pattern_lifecycle(state,instrument_id="X",frames={"15m":f},asof=t); self.assertEqual(state["X|15m|B"]["status"],"CONFIRMED")
        t2=f.index[-1]+pd.Timedelta(minutes=15); f.loc[f.index[-1],"low"]=.4; advance_pattern_lifecycle(state,instrument_id="X",frames={"15m":f},asof=t2)
        self.assertEqual(state["X|15m|B"]["status"],"INVALIDATED"); self.assertTrue(state["X|15m|B"]["invalidated_after_confirmation"])

class PatternBrainTests(unittest.TestCase):
    def test_twenty_balanced_matured_labels_train(self):
        b=ShadowPatternBrain()
        for i in range(20): b.add_finalized_outcome(signal_id=str(i),instrument_id="X",candle_time_utc=f"2026-01-{i+1:02d}T00:00:00Z",features={k:i%2 for k in PATTERN_FAMILIES},net_pnl_usdt=1 if i<10 else -1,fees_complete=True,funding_complete=True)
        r=b.train(); self.assertEqual(r["status"],"TRAINED"); self.assertTrue(b.trained); self.assertEqual(r["samples"],20)
    def test_unbalanced_or_unreconciled_labels_do_not_train(self):
        b=ShadowPatternBrain()
        for i in range(20): b.add_finalized_outcome(signal_id=f"u{i}",instrument_id="X",candle_time_utc=f"2026-02-{i+1:02d}T00:00:00Z",features={k:1 for k in PATTERN_FAMILIES},net_pnl_usdt=1,fees_complete=True,funding_complete=True)
        p=b.add_finalized_outcome(signal_id="p",instrument_id="X",candle_time_utc="2026-03-01T00:00:00Z",features={},net_pnl_usdt=1,fees_complete=False,funding_complete=True)
        self.assertFalse(p["inserted"]); self.assertEqual(b.train()["status"],"MODEL_NOT_READY"); self.assertFalse(b.trained)


class PerPatternNameFeatureTests(unittest.TestCase):
    def test_pattern_name_features_are_normalized_binary_and_ignore_invalidated_patterns(self):
        features = per_pattern_name_features([
            {"name_en": "  ATR   Hammer ", "lifecycle_status": "CONFIRMED"},
            {"name_en": "Bullish Engulfing", "lifecycle_status": "FORMING"},
            {"name_en": "Rising Wedge", "lifecycle_status": "INVALIDATED"},
            {"name_en": "Expired Pattern", "lifecycle_status": "EXPIRED"},
        ])
        self.assertEqual(features, {
            "pattern_name::atr_hammer": 1,
            "pattern_name::bullish_engulfing": 1,
        })


    def test_named_pattern_features_are_learned_from_train_examples_and_frozen(self):
        b = ShadowPatternBrain()
        for i in range(20):
            features = {key: i % 2 for key in PATTERN_FAMILIES}
            features["pattern_name::atr_hammer"] = 1 if i % 2 == 0 else 0
            features["pattern_name::bullish_engulfing"] = 1 if i % 2 == 1 else 0
            b.add_finalized_outcome(
                signal_id=f"named-{i}", instrument_id="X",
                candle_time_utc=f"2026-03-{i+1:02d}T00:00:00Z",
                features=features, net_pnl_usdt=1 if i < 10 else -1,
                fees_complete=True, funding_complete=True,
            )
        trained = b.train()
        self.assertEqual(trained["status"], "TRAINED")
        self.assertEqual(trained["pattern_name_feature_count"], 2)
        self.assertIn("pattern_name::atr_hammer", b.feature_names)
        before = list(b.feature_names)
        frozen = b.freeze()
        self.assertEqual(frozen["status"], "FROZEN")
        self.assertEqual(frozen["training_scope"], "TRAIN_ONLY")
        self.assertTrue(frozen["model_hash"] and len(frozen["model_hash"]) == 64)
        self.assertTrue(frozen["training_timestamp"])
        pending = b.add_finalized_outcome(
            signal_id="validation-unseen-name", instrument_id="X",
            candle_time_utc="2026-04-01T00:00:00Z",
            features={"pattern_name::future_validation_only": 1},
            net_pnl_usdt=1, fees_complete=True, funding_complete=True,
        )
        self.assertEqual(pending["status"], "MODEL_FROZEN")
        self.assertEqual(b.feature_names, before)
        with self.assertRaises(ValueError):
            b.train()

    def test_individual_pattern_name_weight_is_learned_from_train_labels(self):
        brain = ShadowPatternBrain()
        feature_name = "pattern_name::bullish_engulfing"
        for index in range(20):
            is_bullish_engulfing = index % 2 == 0
            features = {key: 0 for key in PATTERN_FAMILIES}
            features[feature_name] = int(is_bullish_engulfing)
            brain.add_finalized_outcome(
                signal_id=f"weight-{index}",
                instrument_id="X",
                candle_time_utc=f"2026-04-{index + 1:02d}T00:00:00Z",
                features=features,
                net_pnl_usdt=1 if is_bullish_engulfing else -1,
                fees_complete=True,
                funding_complete=True,
            )

        trained = brain.train()
        self.assertEqual(trained["status"], "TRAINED")
        weights = {row["feature"]: row["weight"] for row in brain.feature_weights()}
        self.assertGreater(weights[feature_name], 0.0)
        self.assertEqual(trained["pattern_name_feature_count"], 1)
        self.assertTrue(all(row["training_scope"] == "TRAIN" for row in brain.examples))


class ActivePatternGateTests(unittest.TestCase):
    def _gate(self, detections, side="LONG", score=0.50):
        return _v3_pattern_entry_gate(
            detections=detections, expected_direction=side,
            brain_evaluation={"status": "READY", "score_0_1": score},
        )

    def test_toxic_patterns_are_vetoed(self):
        for name in ("Symmetrical Triangle", "Tweezer Bottom", "Rising Wedge"):
            with self.subTest(name=name):
                result = self._gate([{"name_en": name, "family": "structure_reversal",
                                      "direction": "NEUTRAL", "lifecycle_status": "FORMING"}])
                self.assertEqual(result["decision"], "VETO")
                self.assertIn("TOXIC_PATTERN", result["reason"])

    def test_liquidity_sweep_is_vetoed(self):
        result = self._gate([{"name_en": "False Break Liquidity Sweep Low",
                              "family": "liquidity_sweep", "direction": "BULLISH",
                              "lifecycle_status": "CONFIRMED"}])
        self.assertEqual(result["decision"], "VETO")
        self.assertIn("LIQUIDITY_SWEEP_HIGH_FALSE_BREAK_RATE", result["reason"])

    def test_aligned_priority_pattern_adds_bounded_bonus(self):
        result = self._gate([{"name_en": "Bullish Engulfing", "family": "candle_reversal",
                              "direction": "BULLISH", "lifecycle_status": "FORMING"}],
                            side="LONG", score=0.42)
        self.assertEqual(result["decision"], "ALLOW")
        self.assertAlmostEqual(result["priority_bonus"], 0.05)
        self.assertAlmostEqual(result["adjusted_brain_score"], 0.47)

    def test_opposing_priority_pattern_does_not_add_bonus(self):
        result = self._gate([{"name_en": "Hanging Man", "family": "candle_reversal",
                              "direction": "BEARISH", "lifecycle_status": "FORMING"}],
                            side="LONG", score=0.42)
        self.assertEqual(result["decision"], "VETO")
        self.assertEqual(result["priority_bonus"], 0.0)

    def test_unaligned_weak_brain_score_is_vetoed(self):
        result = self._gate([], side="SHORT", score=0.44)
        self.assertEqual(result["decision"], "VETO")
        self.assertEqual(result["reason"], "V3_PATTERN_BRAIN_LOW_SCORE")

    def test_invalidated_toxic_pattern_is_ignored(self):
        result = self._gate([{"name_en": "Rising Wedge", "family": "structure_reversal",
                              "direction": "BEARISH", "lifecycle_status": "INVALIDATED"}],
                            side="SHORT", score=0.50)
        self.assertEqual(result["decision"], "ALLOW")

class ResearchFilterOptInTests(unittest.TestCase):
    def test_research_filter_can_veto_only_when_explicitly_enabled(self):
        observation = _v3_pattern_research_observation(
            detections=[{"name_en": "Rising Wedge", "family": "structure_reversal",
                         "direction": "BEARISH", "lifecycle_status": "FORMING"}],
            expected_direction="LONG",
            brain_evaluation={"status": "READY", "score_0_1": 0.60},
        )
        self.assertEqual(observation["decision"], "RESEARCH_FILTER")
        self.assertTrue(observation["would_veto"])
        self.assertTrue(_actual_entry_veto(
            model_veto=False, pattern_observation=observation, v3_research_filter=True
        ))
        with self.assertRaises(ValueError):
            _actual_entry_veto(model_veto=False, pattern_observation=observation)

    def test_default_shadow_observation_never_vetoes_from_v3(self):
        observation = _v3_pattern_shadow_observation(
            detections=[{"name_en": "Rising Wedge", "family": "structure_reversal",
                         "direction": "BEARISH", "lifecycle_status": "FORMING"}],
            expected_direction="LONG",
            brain_evaluation={"status": "READY", "score_0_1": 0.60},
        )
        self.assertTrue(observation["would_veto"])
        self.assertFalse(_actual_entry_veto(model_veto=False, pattern_observation=observation))


class ShadowOnlyEntryIntegrationTests(unittest.TestCase):
    def test_v3_toxic_pattern_veto_is_only_a_suggestion(self):
        observation = _v3_pattern_shadow_observation(
            detections=[{
                "name_en": "Rising Wedge",
                "family": "structure_reversal",
                "direction": "BEARISH",
                "lifecycle_status": "FORMING",
            }],
            expected_direction="LONG",
            brain_evaluation={"status": "READY", "score_0_1": 0.50},
        )
        self.assertEqual(observation["decision"], "SHADOW_ONLY")
        self.assertTrue(observation["would_veto"])
        self.assertEqual(observation["suggested_decision"], "VETO")
        self.assertFalse(_actual_entry_veto(model_veto=False, pattern_observation=observation))

    def test_v3_priority_bonus_never_changes_actual_entry_authorization(self):
        observation = _v3_pattern_shadow_observation(
            detections=[{
                "name_en": "Bullish Engulfing",
                "family": "candle_reversal",
                "direction": "BULLISH",
                "lifecycle_status": "CONFIRMED",
            }],
            expected_direction="LONG",
            brain_evaluation={"status": "READY", "score_0_1": 0.42},
        )
        self.assertEqual(observation["decision"], "SHADOW_ONLY")
        self.assertGreater(observation["priority_bonus"], 0)
        self.assertFalse(_actual_entry_veto(model_veto=False, pattern_observation=observation))

    def test_existing_v2_veto_remains_respected(self):
        observation = {"decision": "SHADOW_ONLY", "would_veto": True}
        self.assertTrue(_actual_entry_veto(model_veto=True, pattern_observation=observation))


if __name__=="__main__": unittest.main()
