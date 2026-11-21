"""Causal multi-timeframe pattern research. Shadow-only: never changes trade decisions."""
from __future__ import annotations
from typing import Any, Mapping
import numpy as np
import pandas as pd
from .elliott_waves import monitor_elliott_waves
from .market_patterns import analyze_market_patterns
DURATIONS={"15m":pd.Timedelta(minutes=15),"1H":pd.Timedelta(hours=1),"4H":pd.Timedelta(hours=4)}
PATTERN_FAMILIES=("candle_reversal","candle_continuation","structure_reversal","structure_continuation","elliott_alignment","liquidity_sweep")
REGIMES=("TRENDING","RANGING","HIGH_VOLATILITY")

def closed_frame(frame:pd.DataFrame,timeframe:str,asof:Any)->pd.DataFrame:
    if timeframe not in DURATIONS: raise ValueError("unsupported timeframe")
    if not isinstance(frame,pd.DataFrame) or not isinstance(frame.index,pd.DatetimeIndex): raise ValueError("DatetimeIndex required")
    if frame.index.tz is None or frame.index.has_duplicates or not frame.index.is_monotonic_increasing: raise ValueError("timestamps must be timezone-aware, unique and increasing")
    t=pd.Timestamp(asof)
    if t.tzinfo is None: raise ValueError("asof must be timezone-aware")
    return frame.loc[(frame.index.tz_convert("UTC")+DURATIONS[timeframe])<=t.tz_convert("UTC")].copy()

def _atr(f:pd.DataFrame,period:int=14)->pd.Series:
    h=f.high.astype(float); l=f.low.astype(float); c=f.close.astype(float); p=c.shift(1)
    tr=pd.concat([h-l,(h-p).abs(),(l-p).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/period,adjust=False,min_periods=period).mean()

def normalized_candle_metrics(f:pd.DataFrame)->dict[str,float|None]:
    empty={"atr14":None,"body_atr":None,"upper_wick_atr":None,"lower_wick_atr":None,"range_atr":None}
    if len(f)<15: return empty
    a=_atr(f).iloc[-1]; r=f.iloc[-1]; v=np.asarray([r[k] for k in ("open","high","low","close")],float)
    if not np.isfinite(float(a)) or float(a)<=0 or not np.isfinite(v).all(): return empty
    o,h,l,c=v; a=float(a)
    return {"atr14":a,"body_atr":abs(c-o)/a,"upper_wick_atr":max(0,h-max(o,c))/a,"lower_wick_atr":max(0,min(o,c)-l)/a,"range_atr":max(0,h-l)/a}

def _det(name:str,family:str,side:str,state:str,trigger:float|None,inval:float|None,reason:str)->dict[str,Any]:
    return {"name_en":name,"family":family,"direction":side,"state":state,"trigger_price":float(trigger) if trigger is not None else None,"invalidation_price":float(inval) if inval is not None else None,"reason":reason}

def _atr_patterns(f:pd.DataFrame)->list[dict[str,Any]]:
    if len(f)<35: return []
    a=float(_atr(f).iloc[-1])
    if not np.isfinite(a) or a<=0: return []
    d=f.tail(35); o=d.open.to_numpy(float); h=d.high.to_numpy(float); l=d.low.to_numpy(float); c=d.close.to_numpy(float)
    if not np.isfinite(np.r_[o,h,l,c]).all(): return []
    out=[]; body=abs(c[-1]-o[-1])/a; up=max(0,h[-1]-max(o[-1],c[-1]))/a; lw=max(0,min(o[-1],c[-1])-l[-1])/a
    drift=c[-2]/c[-9]-1 if c[-9]>0 else 0
    if body<=.55 and lw>=.9 and up<=.25 and drift<=-.003: out.append(_det("ATR Hammer","candle_reversal","BULLISH","FORMING",h[-1],l[-1],"Lower wick >=0.9 ATR after decline."))
    if body<=.55 and up>=.9 and lw<=.25 and drift>=.003: out.append(_det("ATR Shooting Star","candle_reversal","BEARISH","FORMING",l[-1],h[-1],"Upper wick >=0.9 ATR after rise."))
    ao,ac,bo,bc=o[-3],c[-3],o[-2],c[-2]
    if ac<ao and bc>bo and min(bo,bc)>=ac and max(bo,bc)<=ao and c[-1]>h[-3]: out.append(_det("Three Inside Up","candle_reversal","BULLISH","CONFIRMED",h[-3],l[-3],"Harami confirmed above first candle high."))
    if ac>ao and bc<bo and min(bo,bc)>=ao and max(bo,bc)<=ac and c[-1]<l[-3]: out.append(_det("Three Inside Down","candle_reversal","BEARISH","CONFIRMED",l[-3],h[-3],"Harami confirmed below first candle low."))
    if abs(h[-1]-h[-2])<=.15*a and c[-2]>o[-2] and c[-1]<o[-1]: out.append(_det("Tweezer Top","candle_reversal","BEARISH","FORMING",min(l[-1],l[-2]),max(h[-1],h[-2]),"Repeated high within 0.15 ATR rejected."))
    if abs(l[-1]-l[-2])<=.15*a and c[-2]<o[-2] and c[-1]>o[-1]: out.append(_det("Tweezer Bottom","candle_reversal","BULLISH","FORMING",max(h[-1],h[-2]),min(l[-1],l[-2]),"Repeated low within 0.15 ATR rejected."))
    impulse=(c[-6]-c[-11])/a; ph=float(np.max(h[-5:-1])); pl=float(np.min(l[-5:-1]))
    if impulse>=2.5 and (ph-pl)/a<=3 and c[-1]>=pl: out.append(_det("Bull Flag","structure_continuation","BULLISH","CONFIRMED" if c[-1]>ph else "FORMING",ph,pl,"Impulse up and compact pause."))
    if impulse<=-2.5 and (ph-pl)/a<=3 and c[-1]<=ph: out.append(_det("Bear Flag","structure_continuation","BEARISH","CONFIRMED" if c[-1]<pl else "FORMING",pl,ph,"Impulse down and compact pause."))
    ceiling=float(np.max(h[-25:-4])); floor=float(np.min(l[-25:-4])); prior=c[-4:-1]
    if np.any(prior>ceiling) and l[-1]<=ceiling+.15*a and c[-1]>=ceiling: out.append(_det("Breakout Retest Up","structure_continuation","BULLISH","CONFIRMED",ceiling,floor,"Closed breakout then retest of resistance."))
    if np.any(prior<floor) and h[-1]>=floor-.15*a and c[-1]<=floor: out.append(_det("Breakout Retest Down","structure_continuation","BEARISH","CONFIRMED",floor,ceiling,"Closed breakdown then retest of support."))
    prior_hi=float(np.max(h[-21:-1])); prior_lo=float(np.min(l[-21:-1]))
    if h[-1]>prior_hi+.1*a and c[-1]<prior_hi: out.append(_det("False Break Liquidity Sweep High","liquidity_sweep","BEARISH","CONFIRMED",prior_hi,h[-1],"High swept then close returned below resistance."))
    if l[-1]<prior_lo-.1*a and c[-1]>prior_lo: out.append(_det("False Break Liquidity Sweep Low","liquidity_sweep","BULLISH","CONFIRMED",prior_lo,l[-1],"Low swept then close returned above support."))
    return out

def _pivots(f:pd.DataFrame,radius:int=2)->list[dict[str,Any]]:
    if len(f)<radius*2+5: return []
    hi=f.high.to_numpy(float); lo=f.low.to_numpy(float); dt=f.index[1]-f.index[0]; out=[]
    for i in range(radius,len(f)-radius):
        left=slice(i-radius,i); right=slice(i+1,i+radius+1); j=i+radius
        if hi[i]>np.max(hi[left]) and hi[i]>=np.max(hi[right]): out.append({"index":i,"kind":"HIGH","price":float(hi[i]),"timestamp":f.index[i].isoformat(),"confirmed_at":(f.index[j]+dt).isoformat()})
        if lo[i]<np.min(lo[left]) and lo[i]<=np.min(lo[right]): out.append({"index":i,"kind":"LOW","price":float(lo[i]),"timestamp":f.index[i].isoformat(),"confirmed_at":(f.index[j]+dt).isoformat()})
    return sorted(out,key=lambda x:x["index"])[-30:]

def _family(p:Mapping[str,Any])->str|None:
    n=str(p.get("name_en") or "").lower(); cat=str(p.get("category") or "").upper()
    if "liquidity sweep" in n or "false break" in n: return "liquidity_sweep"
    if cat=="CANDLE":
        continuation=("inside bar","three white soldiers","three black crows","three inside up","three inside down")
        return "candle_continuation" if any(x in n for x in continuation) else "candle_reversal"
    if cat:
        reversal=("double top","double bottom","head and shoulders","inverse head and shoulders","wedge")
        return "structure_reversal" if any(x in n for x in reversal) else "structure_continuation"
    return None

def _market_regime(frame:pd.DataFrame,analysis:Mapping[str,Any])->str:
    """Three causal regimes using 4H ADX context and trailing ATR expansion."""
    atr_values=_atr(frame,14).dropna()
    if len(atr_values)>=20:
        current=float(atr_values.iloc[-1])
        baseline=atr_values.iloc[-97:-1]
        if len(baseline)>=10 and current>0:
            percentile=float((baseline<=current).mean())
            median=float(baseline.median())
            ratio=current/median if median>0 else 1.0
            if percentile>=0.80 or ratio>=1.75:
                return "HIGH_VOLATILITY"
    direction=analysis.get("direction") or {}
    base=str(direction.get("regime") or "").upper()
    if base=="TRENDING":
        return "TRENDING"
    if base=="RANGING":
        return "RANGING"
    adx=direction.get("adx14")
    trend=str(direction.get("trend") or "").upper()
    if adx is not None and float(adx)>=20 and trend in {"BULLISH","BEARISH"}:
        return "TRENDING"
    return "RANGING"

def _advance(r:dict[str,Any],close:float,high:float,low:float,t:pd.Timestamp,duration:pd.Timedelta)->None:
    if r.get("last_updated_asof")==t.isoformat(): return
    status=r.get("status","FORMING")
    if status in {"INVALIDATED","EXPIRED"}: r["last_updated_asof"]=t.isoformat(); return
    age=max(0,int((t-pd.Timestamp(r["created_at"])).total_seconds()//duration.total_seconds())); r["age_bars"]=age
    confirmed=status=="CONFIRMED"; side=r.get("direction"); trigger=r.get("trigger_price"); inv=r.get("invalidation_price")
    if status=="FORMING" and trigger is not None and ((side=="BULLISH" and close>float(trigger)) or (side=="BEARISH" and close<float(trigger))):
        r["status"]="CONFIRMED"; r["confirmed_at"]=t.isoformat(); r["breakout_price"]=close
    if confirmed and inv is not None and ((side=="BULLISH" and low<=float(inv)) or (side=="BEARISH" and high>=float(inv))):
        r["status"]="INVALIDATED"; r["invalidated_at"]=t.isoformat(); r["invalidated_after_confirmation"]=True; r["invalidation_reason"]="POST_CONFIRMATION_FAILURE"
    if age>int(r.get("max_age_bars",12)) and r.get("status")!="INVALIDATED": r["status"]="EXPIRED"; r["expired_at"]=t.isoformat()
    r["last_updated_asof"]=t.isoformat()

def advance_pattern_lifecycle(state:dict[str,dict[str,Any]],*,instrument_id:str,frames:Mapping[str,pd.DataFrame],asof:Any)->None:
    t=pd.Timestamp(asof)
    if t.tzinfo is None: raise ValueError("asof must be timezone-aware")
    t=t.tz_convert("UTC")
    for tf,duration in DURATIONS.items():
        if tf not in frames: continue
        f=closed_frame(frames[tf],tf,t)
        if f.empty: continue
        row=f.iloc[-1]; c,h,l=float(row.close),float(row.high),float(row.low)
        for r in state.values():
            if r.get("instrument_id")==instrument_id and r.get("timeframe")==tf: _advance(r,c,h,l,t,duration)

def _upsert(state:dict[str,dict[str,Any]],symbol:str,tf:str,p:Mapping[str,Any],t:pd.Timestamp,close:float)->str:
    name=str(p.get("name_en") or p.get("name") or "UNKNOWN"); fam=str(p.get("family") or _family(p) or "structure_continuation")
    key=f"{symbol}|{tf}|{name}"; r=state.get(key)
    if r and r.get("status") in {"INVALIDATED","EXPIRED"}: key+="|"+t.isoformat(); r=None
    incoming=str(p.get("state") or "FORMING").upper(); status="CONFIRMED" if incoming=="CONFIRMED" else "FORMING"
    if r is None:
        state[key]={"instrument_id":symbol,"timeframe":tf,"name":name,"family":fam,"direction":str(p.get("direction") if p.get("direction") in {"BULLISH","BEARISH"} else (p.get("potential_direction") or "NEUTRAL")),"status":status,
          "trigger_price":p.get("trigger_price"),"invalidation_price":p.get("invalidation_price"),"created_at":t.isoformat(),
          "confirmed_at":t.isoformat() if status=="CONFIRMED" else None,"breakout_price":close if status=="CONFIRMED" else None,
          "invalidated_after_confirmation":False,"max_age_bars":12 if tf=="15m" else 8 if tf=="1H" else 6,"last_updated_asof":t.isoformat()}
    else:
        r["family"]=fam; r["trigger_price"]=p.get("trigger_price",r.get("trigger_price")); r["invalidation_price"]=p.get("invalidation_price",r.get("invalidation_price"))
        if incoming=="CONFIRMED" and r.get("status")=="FORMING": r["status"]="CONFIRMED"; r["confirmed_at"]=t.isoformat(); r["breakout_price"]=close
        r["last_updated_asof"]=t.isoformat()
    return key

def analyze_unified_patterns_v3(*,instrument_id:str,frames:Mapping[str,pd.DataFrame],asof:Any,expected_direction:str,lifecycle_state:dict[str,dict[str,Any]])->dict[str,Any]:
    t=pd.Timestamp(asof)
    if t.tzinfo is None: raise ValueError("asof must be timezone-aware")
    t=t.tz_convert("UTC"); direction=str(expected_direction).upper()
    if direction not in {"LONG","SHORT"}: raise ValueError("expected_direction must be LONG or SHORT")
    advance_pattern_lifecycle(lifecycle_state,instrument_id=instrument_id,frames=frames,asof=t)
    features={x:0 for x in PATTERN_FAMILIES}; keys={x:[] for x in PATTERN_FAMILIES}; tf_result={}; all_patterns=[]
    for tf,duration in DURATIONS.items():
        if tf not in frames: continue
        f=closed_frame(frames[tf],tf,t)
        if f.empty: tf_result[tf]={"closed_candles":0,"patterns":[],"atr_normalized":normalized_candle_metrics(f)}; continue
        close=float(f.close.iloc[-1]); pivots=_pivots(f); floor={"15m":.003,"1H":.006,"4H":.012}[tf]
        try: wave=monitor_elliott_waves(f,min_reversal_pct=floor,max_reversal_pct=min(.2,floor*5))
        except (ValueError,TypeError,IndexError): wave={"pattern":"AMBIGUOUS","status":"UNKNOWN","phase":"UNKNOWN"}
        try: analysis=analyze_market_patterns(f,pivots,wave)
        except (ValueError,TypeError,KeyError,IndexError): analysis={"candlestick_patterns":[],"geometric_patterns":[],"direction":{"divergence":{"type":"NONE"}}}
        detections=[]
        for raw in analysis.get("candlestick_patterns",[])+analysis.get("geometric_patterns",[]):
            p=dict(raw); p["family"]=_family(p) or "structure_continuation"; detections.append(p)
        div=((analysis.get("direction") or {}).get("divergence") or {}).get("type","NONE")
        if str(div).upper() in {"BULLISH","BEARISH"}: detections.append(_det("RSI Divergence","structure_reversal",str(div).upper(),"FORMING",None,None,"Confirmed swing-price and RSI pivots diverge."))
        detections.extend(_atr_patterns(f)); local=[]
        for p in detections:
            fam=str(p.get("family") or "")
            if fam not in PATTERN_FAMILIES: continue
            key=_upsert(lifecycle_state,instrument_id,tf,p,t,close); keys[fam].append(key); local.append(key); features[fam]=1
            rec=lifecycle_state[key]; item=dict(p); item.update({"lifecycle_key":key,"lifecycle_status":rec.get("status"),"confirmed_at":rec.get("confirmed_at"),"breakout_price":rec.get("breakout_price")}); all_patterns.append(item)
        wp=str((wave or {}).get("pattern") or "AMBIGUOUS").upper(); ws=str((wave or {}).get("status") or "UNKNOWN").upper()
        aligned=wp==("BULLISH" if direction=="LONG" else "BEARISH") and ws in {"CANDIDATE","CONFIRMED"}
        if aligned: features["elliott_alignment"]=1
        regime=str(((analysis.get("direction") or {}).get("regime") or "UNKNOWN")).upper().replace(" ","_")
        tf_result[tf]={"closed_candles":len(f),"latest_closed_at":(f.index[-1]+duration).isoformat(),"atr_normalized":normalized_candle_metrics(f),"market_regime":_market_regime(f,analysis) if tf=="4H" else str(((analysis.get("direction") or {}).get("regime") or "UNKNOWN")).upper(),
          "patterns":[{"name_en":x.get("name_en") or x.get("name"),"family":x.get("family"),"direction":x.get("direction"),"status":x.get("lifecycle_status") or x.get("state"),"trigger_price":x.get("trigger_price"),"invalidation_price":x.get("invalidation_price"),"lifecycle_key":x.get("lifecycle_key")} for x in all_patterns if x.get("lifecycle_key") in local],
          "wave_structure":{"pattern":wp,"status":ws,"phase":str((wave or {}).get("phase") or "UNKNOWN"),"aligned_with_v2":bool(aligned)}}
    keys={k:list(dict.fromkeys(v)) for k,v in keys.items()}
    raw_regime=str((tf_result.get("4H") or {}).get("market_regime") or "UNKNOWN").upper()
    if "HIGH_VOL" in raw_regime or "VOLATILE" in raw_regime: market_regime="HIGH_VOLATILITY"
    elif "RANG" in raw_regime or "SIDEWAYS" in raw_regime: market_regime="RANGING"
    elif "TREND" in raw_regime: market_regime="TRENDING"
    else: market_regime="UNKNOWN"
    return {"version":"UNIFIED_MULTI_TIMEFRAME_PATTERN_INTELLIGENCE_V3","mode":"SHADOW_ONLY","instrument_id":instrument_id,"asof_utc":t.isoformat(),"expected_direction":direction,
      "completed_candles_only":True,"future_candles_used":False,"timeframes":tf_result,"market_regime":market_regime,"features":features,"family_lifecycle_keys":keys,"detections":all_patterns,
      "score_semantics":"Binary evidence flags; not a trade score or success probability."}

def lifecycle_outcomes(state:Mapping[str,Mapping[str,Any]],keys:Mapping[str,list[str]])->dict[str,dict[str,Any]]:
    out={}
    for fam in PATTERN_FAMILIES:
        rows=[state.get(k) for k in keys.get(fam,[])]; rows=[r for r in rows if r]; mature=[r for r in rows if r.get("status") in {"CONFIRMED","INVALIDATED"}]
        out[fam]={"evaluated":bool(mature),"false_break":any(r.get("status")=="INVALIDATED" and r.get("invalidated_after_confirmation") is True for r in rows),"terminal_states":[str(r.get("status")) for r in rows]}
    return out

def pattern_family_performance(trades:list[Mapping[str,Any]],phase:str|None=None,regime:str|None=None)->dict[str,Any]:
    chosen=[x for x in trades if (phase is None or x.get("phase")==phase) and (regime is None or x.get("market_regime")==regime)]; out={}
    for fam in PATTERN_FAMILIES:
        tagged=[x for x in chosen if float((x.get("pattern_features") or {}).get(fam,0) or 0)>=1 and x.get("net_pnl_usdt") is not None]
        pnl=[float(x["net_pnl_usdt"]) for x in tagged]; wins=sum(v>0 for v in pnl)
        life=[(x.get("pattern_lifecycle_outcomes") or {}).get(fam,{}) for x in tagged]; life=[x for x in life if x.get("evaluated")]
        false=sum(bool(x.get("false_break")) for x in life); mfe=[float(x["mfe_pct"]) for x in tagged if x.get("mfe_pct") is not None]; mae=[float(x["mae_pct"]) for x in tagged if x.get("mae_pct") is not None]
        out[fam]={"trade_count":len(tagged),"wins":wins,"losses":len(pnl)-wins,"win_rate_pct":round(wins/len(pnl)*100,4) if pnl else None,
          "expectancy_usdt":round(float(np.mean(pnl)),8) if pnl else None,"mfe_pct_mean":round(float(np.mean(mfe)),6) if mfe else None,"mae_pct_mean":round(float(np.mean(mae)),6) if mae else None,
          "false_break_rate_pct":round(false/len(life)*100,4) if life else None,"false_break_evaluated_patterns":len(life),"false_break_events":false}
    return out
