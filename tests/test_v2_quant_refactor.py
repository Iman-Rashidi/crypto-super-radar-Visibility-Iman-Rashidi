import json, sqlite3
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
from crypto_super_radar.challenge1_v2 import entry_score_meets_threshold, MIN_ENTRY_SCORE, MAX_STOP_DISTANCE_PCT
from crypto_super_radar.elliott_waves import detect_impulse_pattern
from crypto_super_radar.position_manager import PositionManager, ActionType
from crypto_super_radar.trade_journal import log_closed_trade
from crypto_super_radar.ai_supervisor import run_challenge1_v2_supervisor_reviews


def test_score_threshold_is_55_and_finite():
    assert MIN_ENTRY_SCORE == 55.0 and entry_score_meets_threshold(55)
    assert not entry_score_meets_threshold(float('nan'))
    assert MAX_STOP_DISTANCE_PCT == .035


def test_elliott_returns_none_for_insufficient_or_invalid_ratios():
    frame=pd.DataFrame({'high':[10,11,12,11,10,9,10,11,12,11,10], 'low':[9,10,11,10,9,8,9,10,11,10,9]})
    result=detect_impulse_pattern(frame)
    assert result['pattern']=='NONE' and 0<=result['confidence']<=100


def test_position_manager_break_even_and_trailing():
    pm=PositionManager(); now=datetime.now(timezone.utc).isoformat()
    pos={'side':'LONG','entry_price':100,'initial_stop_loss':98,'stop_loss':98,'entry_time':now}
    be=pm.on_tick(pos,{'close':102},{'atr14':1})
    assert be.action==ActionType.MOVE_STOP and be.stop_loss==100
    trail=pm.on_tick(pos,{'close':103},{'atr14':1})
    assert trail.action==ActionType.MOVE_STOP and trail.stop_loss==101.5


def test_position_manager_momentum_and_time_exits():
    pm=PositionManager(); old=(datetime.now(timezone.utc)-timedelta(hours=49)).isoformat()
    pos={'side':'LONG','entry_price':100,'initial_stop_loss':98,'stop_loss':98,'entry_time':old}
    a=pm.on_tick(pos,{'close':101.2},{'rsi14':49,'previous_rsi14':51,'atr14':.5})
    assert a.action==ActionType.CLOSE and a.reason=='MOMENTUM_EXIT'
    b=pm.on_tick(pos,{'close':100.2},{'rsi14':52,'previous_rsi14':49,'atr14':.5})
    assert b.action==ActionType.CLOSE and b.reason=='TIME_STOP'


def test_position_manager_structure_stop():
    pm=PositionManager(); pos={'side':'LONG','entry_price':100,'initial_stop_loss':98,'stop_loss':98,'entry_time':datetime.now(timezone.utc).isoformat()}
    a=pm.on_tick(pos,{'close':94},{'atr14':1,'last_confirmed_swing_low':95})
    assert a.action==ActionType.CLOSE and a.reason=='STRUCTURE_STOP'


def test_trade_journal_sqlite_roundtrip(tmp_path):
    p=tmp_path/'journal.sqlite3'; trade={'trade_id':'t1','symbol':'BTC-USDT','side':'LONG','entry_price':100,'exit_price':102,'entry_time':'2026-01-01T00:00:00Z','exit_time':'2026-01-01T01:00:00Z','exit_reason':'TP','r_multiple':1,'mfe_r':1.2,'mae_r':-.1,'entry_context_json':{'score':70},'exit_context_json':{'why':'tp'}}
    log_closed_trade(trade,p)
    with sqlite3.connect(p) as c: row=c.execute('select trade_id,entry_context_json from closed_trades').fetchone()
    assert row[0]=='t1' and json.loads(row[1])=={'score':70}


def test_supervisor_is_always_advisory(monkeypatch):
    class A: signal='LONG'; metrics={'execution_profile':'CHALLENGE_1_V2'}
    out=run_challenge1_v2_supervisor_reviews(A(),None,second_reviewer=lambda *a,**k:{'status':'VETO'})
    assert out['approved'] is True and out['advisory_only'] is True
    assert out['ai_consensus']['entry_gate_enforced'] is False
