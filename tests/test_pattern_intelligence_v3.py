from __future__ import annotations
import unittest
import numpy as np
import pandas as pd
from crypto_super_radar.adaptive_brain import ShadowPatternBrain
from crypto_super_radar.pattern_intelligence_v3 import PATTERN_FAMILIES,advance_pattern_lifecycle,analyze_unified_patterns_v3,closed_frame,normalized_candle_metrics

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
        self.assertEqual(a["features"],b["features"]); self.assertEqual(a["timeframes"]["15m"]["atr_normalized"],b["timeframes"]["15m"]["atr_normalized"])
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

if __name__=="__main__": unittest.main()
