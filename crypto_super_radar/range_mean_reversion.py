"""Offline-only RangeMeanReversionEngine. No exchange clients or order writes."""
from __future__ import annotations
import argparse, hashlib, json, math, sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
import numpy as np
import pandas as pd

SYMBOLS=("BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","LINK-USDT")
@dataclass(frozen=True)
class RangeConfig:
    adx_threshold:float=22.0; bb_period:int=20; bb_std:float=2.0
    rsi_period:int=14; rsi_long:float=32.0; rsi_short:float=68.0
    atr_period:int=14; stop_atr:float=1.5; min_target_r:float=1.5
    max_holding_bars:int=10; crash_atr_multiple:float=2.5
    initial_equity:float=1000.0; risk_fraction:float=0.0025
    maker_fee:float=0.0002; taker_fee:float=0.0006
    spread_fraction:float=0.0002; stop_slippage:float=0.0003

def wilder(x,n): return x.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
def indicators(f,c):
    z=f.copy(); prev=z.close.shift(1)
    tr=pd.concat([z.high-z.low,(z.high-prev).abs(),(z.low-prev).abs()],axis=1).max(axis=1)
    z['atr']=wilder(tr,c.atr_period)
    d=z.close.diff(); rs=wilder(d.clip(lower=0),c.rsi_period)/wilder(-d.clip(upper=0),c.rsi_period).replace(0,np.nan)
    z['rsi']=100-100/(1+rs)
    mid=z.close.rolling(c.bb_period).mean(); sd=z.close.rolling(c.bb_period).std(ddof=0)
    z['mid']=mid; z['upper']=mid+c.bb_std*sd; z['lower']=mid-c.bb_std*sd
    up=z.high.diff(); dn=-z.low.diff(); pdm=up.where((up>dn)&(up>0),0.0); mdm=dn.where((dn>up)&(dn>0),0.0)
    atr=wilder(tr,c.atr_period); pdi=100*wilder(pdm,c.atr_period)/atr.replace(0,np.nan); mdi=100*wilder(mdm,c.atr_period)/atr.replace(0,np.nan)
    z['adx']=wilder(100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan),c.atr_period)
    po=z.open.shift(1); pc=z.close.shift(1); body=(z.close-z.open).abs(); pb=(pc-po).abs()
    bull=(z.close>z.open)&(pc<po)&(z.open<=pc)&(z.close>=po)&(body>=pb)
    lo=z[['open','close']].min(axis=1)-z.low; hi=z.high-z[['open','close']].max(axis=1)
    hammer=(z.close>z.open)&(lo>=2*body)&(hi<=body)&(body>0)
    bear=(z.close<z.open)&(pc>po)&(z.open>=pc)&(z.close<=po)&(body>=pb)
    star=(z.close<z.open)&(hi>=2*body)&(lo<=body)&(body>0)
    z['bull_reversal']=bull|hammer; z['bear_reversal']=bear|star
    return z

def load(conn,s,b,src='LAST'):
    d=pd.read_sql_query('SELECT ts,open,high,low,close,volume FROM replay_candles WHERE instrument_id=? AND bar=? AND source=? ORDER BY ts',conn,params=(s,b,src))
    if d.empty: raise ValueError(f'missing {s} {b} {src}')
    d.index=pd.to_datetime(d.pop('ts'),unit='ms',utc=True); return d

def manifest(conn):
    req={(s,b,x) for s in SYMBOLS for b,x in [('15m','LAST'),('15m','MARK'),('1H','LAST'),('4H','LAST')]}
    rows=conn.execute('SELECT instrument_id,bar,source,count(*),min(ts),max(ts) FROM replay_candles GROUP BY 1,2,3').fetchall()
    found={(r[0],r[1],r[2]) for r in rows}
    if req-found: raise ValueError(f'missing series: {sorted(req-found)}')
    step={'15m':900000,'1H':3600000,'4H':14400000}
    for s,b,x in req:
        ts=[r[0] for r in conn.execute('SELECT ts FROM replay_candles WHERE instrument_id=? AND bar=? AND source=? ORDER BY ts',(s,b,x))]
        if len(ts)<2 or any(y-x!=step[b] for x,y in zip(ts,ts[1:])): raise ValueError(f'candle gap/duplicate: {s} {b} {x}')
    return rows

def run_symbol(conn,s,c,score_start=None,score_end=None):
    m=indicators(load(conn,s,'15m'),c); mark=load(conn,s,'15m','MARK'); h=indicators(load(conn,s,'1H'),c); q=load(conn,s,'4H')
    if not m.index.equals(mark.index): raise ValueError(f'MARK/LAST timestamps differ: {s}')
    m['mark_high']=mark.high; m['mark_low']=mark.low
    tr4=pd.concat([q.high-q.low,(q.high-q.close.shift()).abs(),(q.low-q.close.shift()).abs()],axis=1).max(axis=1)
    a4=wilder(tr4,c.atr_period); shock=tr4>c.crash_atr_multiple*a4.shift(1)
    funding=conn.execute('SELECT funding_time,funding_rate,funding_kind,funding_kind_source FROM replay_funding WHERE instrument_id=? ORDER BY funding_time',(s,)).fetchall()
    for ts,rate,kind,source in funding:
        day=int(ts)%(24*3600000); bounds=(0,8*3600000,16*3600000)
        dist=min(min((day-b)%(24*3600000),(b-day)%(24*3600000)) for b in bounds)
        if kind!='0' or source not in ('EXPLICIT_API','SCHEDULE_INFERRED_UTC_8H_BOUNDARY') or (source.startswith('SCHEDULE_') and dist>10000): raise ValueError(f'funding provenance invalid: {s}')
    trades=[]; blocks={'not_range_regime':0,'anti_crash':0,'signal_filters':0,'target_below_1_5R':0,'funding_blackout':0}
    for i in range(max(c.bb_period,c.rsi_period,c.atr_period)+2,len(m)-c.max_holding_bars-1):
        sig=m.iloc[i]; close_ts=m.index[i]+pd.Timedelta(minutes=15)
        if score_start and close_ts<pd.Timestamp(score_start): continue
        if score_end and close_ts>=pd.Timestamp(score_end): continue
        hp=h.index.searchsorted(close_ts-pd.Timedelta(hours=1),side='right')-1
        if hp<0 or pd.isna(h.adx.iloc[hp]): continue
        if h.adx.iloc[hp]>=c.adx_threshold: blocks['not_range_regime']+=1; continue
        qp=q.index.searchsorted(close_ts-pd.Timedelta(hours=4),side='right')-1
        if qp<c.atr_period+1 or pd.isna(a4.iloc[qp]): continue
        if shock.iloc[qp]: blocks['anti_crash']+=1; continue
        if pd.Timestamp('2026-06-25T16:00Z')<=close_ts<pd.Timestamp('2026-06-26T16:00Z'):
            blocks['funding_blackout']+=1; continue
        side=None
        if sig.low<sig.lower<sig.close and sig.rsi<c.rsi_long and sig.bull_reversal: side='LONG'
        elif sig.high>sig.upper>sig.close and sig.rsi>c.rsi_short and sig.bear_reversal: side='SHORT'
        if side is None: blocks['signal_filters']+=1; continue
        raw=float(m.open.iloc[i+1]); atr=float(sig.atr); mid=float(sig.mid)
        if not np.isfinite(atr) or atr<=0 or not np.isfinite(mid): continue
        stop=raw-c.stop_atr*atr if side=='LONG' else raw+c.stop_atr*atr
        reward=mid-raw if side=='LONG' else raw-mid; risk=abs(raw-stop)
        if reward<=0 or reward/risk<c.min_target_r: blocks['target_below_1_5R']+=1; continue
        spec=json.loads(conn.execute('SELECT raw_json FROM replay_instruments WHERE instrument_id=?',(s,)).fetchone()[0])
        cv=float(spec['contractValue']); lot=float(spec['lotSize']); tick=float(spec['tickSize'])
        if side=='LONG': entry=math.floor(raw/tick)*tick; stop=math.floor(stop/tick)*tick; target=math.ceil(mid/tick)*tick
        else: entry=math.ceil(raw/tick)*tick; stop=math.ceil(stop/tick)*tick; target=math.floor(mid/tick)*tick
        if not ((stop<entry<target) if side=='LONG' else (target<entry<stop)): continue
        actual_r=(target-entry)/abs(entry-stop) if side=='LONG' else (entry-target)/abs(entry-stop)
        if actual_r<c.min_target_r: blocks['target_below_1_5R']+=1; continue
        slip= c.stop_slippage; stop_fill=stop*(1-slip if side=='LONG' else 1+slip)
        loss_per=(abs(entry-stop_fill)*cv+entry*cv*c.maker_fee+stop*cv*c.taker_fee+entry*cv*(c.spread_fraction+0.0002))
        qty=math.floor((c.initial_equity*c.risk_fraction/loss_per)/lot)*lot if loss_per>0 else 0
        if spec.get('maxMarketSize') is not None: qty=min(qty,math.floor(float(spec['maxMarketSize'])/lot)*lot)
        if qty<float(spec['minSize']): continue
        exit_ts=exit_price=exit_fill=None; reason=None; fee_rate=None; slip_cost=0.0
        for j in range(i+1,min(i+1+c.max_holding_bars,len(m))):
            bar=m.iloc[j]; t=m.index[j]+pd.Timedelta(minutes=15)
            hit_sl=bar.mark_low<=stop if side=='LONG' else bar.mark_high>=stop
            hit_tp=bar.high>=target if side=='LONG' else bar.low<=target
            if hit_sl:
                exit_ts=t; exit_price=stop; exit_fill=stop_fill; reason='SL'; fee_rate=c.taker_fee; slip_cost=abs(exit_fill-exit_price)*qty*cv; break
            if hit_tp:
                exit_ts=t; exit_price=target; exit_fill=target; reason='TP_MID_BAND'; fee_rate=c.maker_fee; break
        if exit_ts is None:
            j=min(i+c.max_holding_bars,len(m)-1); bar=m.iloc[j]; exit_ts=m.index[j]+pd.Timedelta(minutes=15)
            exit_price=float(bar.close); exit_fill=exit_price*(1-slip if side=='LONG' else 1+slip); reason='TIME_10_BARS'; fee_rate=c.taker_fee
            slip_cost=abs(exit_fill-exit_price)*qty*cv
        if pd.Timestamp('2026-06-25T16:00Z')<exit_ts and close_ts<pd.Timestamp('2026-06-26T16:00Z') or (close_ts<pd.Timestamp('2026-06-25T16:00Z') and exit_ts>pd.Timestamp('2026-06-25T16:00Z') and exit_ts<pd.Timestamp('2026-06-26T16:00Z')):
            blocks['funding_blackout']+=1; continue
        gross=(exit_price-entry)*qty*cv*(1 if side=='LONG' else -1)
        entry_n=entry*qty*cv; exit_n=exit_fill*qty*cv; fees=entry_n*c.maker_fee+exit_n*fee_rate; spread=entry_n*c.spread_fraction
        fund=0.0
        for ft,rate,kind,source in funding:
            ftime=pd.to_datetime(int(ft),unit='ms',utc=True)
            if pd.Timestamp(close_ts)<ftime<=exit_ts: fund+=(1 if side=='LONG' else -1)*entry_n*float(rate)
        net=gross-fees-spread-slip_cost-fund
        trades.append({'symbol':s,'direction':side,'entry_ts':m.index[i+1].isoformat(),'exit_ts':exit_ts.isoformat(),'entry_price':entry,'exit_price':exit_price,'exit_fill_price':exit_fill,'stop_price':stop,'target_mid_price':target,'net_pnl_usdt':net,'gross_pnl_usdt':gross,'fees_usdt':fees,'spread_usdt':spread,'slippage_usdt':slip_cost,'funding_cost_usdt':fund,'exit_reason':reason,'adx_1h':float(h.adx.iloc[hp]),'rsi_15m':float(sig.rsi),'target_r':reward/risk,'bars_held':j-i})
    return trades,blocks

class RangeMeanReversionEngine:
    def __init__(self,config=None): self.config=config or RangeConfig()
    def run_symbol(self,conn,symbol,score_start=None,score_end=None):
        if symbol not in SYMBOLS: raise ValueError('symbol outside approved five-market basket')
        return run_symbol(conn,symbol,self.config,score_start,score_end)

def select_portfolio(candidates,fixed=(),cap=4):
    chosen=[]
    for t in sorted(candidates,key=lambda x:x['entry_ts']):
        a=pd.Timestamp(t['entry_ts']); b=pd.Timestamp(t['exit_ts']); existing=list(fixed)+chosen
        if any(x['symbol']==t['symbol'] and pd.Timestamp(x['entry_ts'])<b and pd.Timestamp(x['exit_ts'])>a for x in existing): continue
        ev=[]
        for x in existing:
            x1=pd.Timestamp(x['entry_ts']); x2=pd.Timestamp(x['exit_ts'])
            if x1<b and x2>a: ev.extend([(max(x1,a),1),(min(x2,b),-1)])
        ev.extend([(a,1),(b,-1)]); active=peak=0
        for _,d in sorted(ev,key=lambda z:(z[0],z[1])): active+=d; peak=max(peak,active)
        if peak<=cap: chosen.append(t)
    return chosen

def metrics(trades):
    if not trades: return {'trades':0,'wins':0,'losses':0,'win_rate_pct':None,'gross_pnl_usdt':0,'net_pnl_usdt':0,'profit_factor':None,'max_drawdown_pct':0,'fees_usdt':0,'spread_usdt':0,'slippage_usdt':0,'funding_cost_usdt':0}
    w=[x for x in trades if x['net_pnl_usdt']>0]; l=[x for x in trades if x['net_pnl_usdt']<0]
    by={}
    for x in trades: by[x['exit_ts']]=by.get(x['exit_ts'],0)+x['net_pnl_usdt']
    eq=peak=1000.0; dd=0.0
    for _,p in sorted(by.items()): eq+=p; peak=max(peak,eq); dd=max(dd,(peak-eq)/peak*100)
    fee=sum(x['fees_usdt'] for x in trades); spread=sum(x['spread_usdt'] for x in trades); slip=sum(x['slippage_usdt'] for x in trades); fund=sum(x['funding_cost_usdt'] for x in trades); net=sum(x['net_pnl_usdt'] for x in trades)
    gross=net+fee+spread+slip+fund; loss=-sum(x['net_pnl_usdt'] for x in l)
    return {'trades':len(trades),'wins':len(w),'losses':len(l),'win_rate_pct':len(w)/len(trades)*100,'gross_pnl_usdt':gross,'fees_usdt':fee,'spread_usdt':spread,'slippage_usdt':slip,'funding_cost_usdt':fund,'net_pnl_usdt':net,'profit_factor':sum(x['net_pnl_usdt'] for x in w)/loss if loss else None,'max_drawdown_pct':dd}

def run_dataset(db,baseline_path,output_path,name,score_start=None,score_end=None):
    conn=sqlite3.connect(db); rows=manifest(conn); eng=RangeMeanReversionEngine(); candidates=[]; blocks={}
    for s in SYMBOLS:
        ts,b=eng.run_symbol(conn,s,score_start,score_end); candidates+=ts
        for k,v in b.items(): blocks[k]=blocks.get(k,0)+v
    conn.close(); base=json.load(open(baseline_path,encoding='utf-8'))['trade_ledger']
    if score_start: base=[t for t in base if pd.to_datetime(t['entry_ts_ms'],unit='ms',utc=True)>=pd.Timestamp(score_start)]
    if score_end: base=[t for t in base if pd.to_datetime(t['entry_ts_ms'],unit='ms',utc=True)<pd.Timestamp(score_end)]
    trend=[{'symbol':t['instrument_id'],'entry_ts':pd.to_datetime(t['entry_ts_ms'],unit='ms',utc=True).isoformat(),'exit_ts':pd.to_datetime(t['exit_ts_ms'],unit='ms',utc=True).isoformat(),'net_pnl_usdt':float(t['net_pnl_usdt']),'fees_usdt':float(t['fees_usdt']),'spread_usdt':float(t['spread_cost_usdt']),'slippage_usdt':float(t['slippage_cost_usdt']),'funding_cost_usdt':float(t['funding_usdt'])} for t in base]
    ranges=select_portfolio(candidates,cap=4); dual_ranges=select_portfolio(candidates,fixed=trend,cap=4)
    dual=trend+dual_ranges
    def norm(x): return {'entry_ts':x['entry_ts'],'exit_ts':x['exit_ts'],'net_pnl_usdt':x['net_pnl_usdt'],'fees_usdt':x['fees_usdt'],'spread_usdt':x['spread_usdt'],'slippage_usdt':x['slippage_usdt'],'funding_cost_usdt':x['funding_cost_usdt']}
    def met(xs):
        a=[]
        for x in xs:
            gross=x['net_pnl_usdt']+x['fees_usdt']+x['spread_usdt']+x['slippage_usdt']+x['funding_cost_usdt']
            a.append({**x,'gross_pnl_usdt':gross})
        return metrics(a)
    result={'dataset':name,'database_sha256':hashlib.sha256(Path(db).read_bytes()).hexdigest(),'score_window_utc':f'{score_start} to {score_end}' if score_start else 'full Replay #109 window','config':asdict(eng.config),'series_manifest':[{'symbol':r[0],'bar':r[1],'source':r[2],'rows':r[3],'first_ms':r[4],'last_ms':r[5]} for r in rows],'blocked_counts':blocks,'trend_only':met([norm(x) for x in trend]),'range_only':metrics(ranges),'dual_regime':met([norm(x) for x in dual]),'range_candidates':len(candidates),'range_only_selected':len(ranges),'dual_range_added':len(dual_ranges),'range_trades':ranges}
    Path(output_path).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8'); return result

def main():
    p=argparse.ArgumentParser(); p.add_argument('--replay109-db',required=True); p.add_argument('--replay109-baseline',required=True); p.add_argument('--independent-db',required=True); p.add_argument('--independent-baseline',required=True); p.add_argument('--output-dir',required=True); a=p.parse_args()
    out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)
    six=run_dataset(a.replay109_db,a.replay109_baseline,out/'replay109.json','Replay #109 — 6 months')
    ind=run_dataset(a.independent_db,a.independent_baseline,out/'independent.json','Independent — 4.25-month source / 3.25-month OOS','2025-11-30T09:00:00Z','2026-03-09T00:00:00Z')
    print(json.dumps({'replay109':{k:six[k] for k in ('trend_only','range_only','dual_regime')},'independent':{k:ind[k] for k in ('trend_only','range_only','dual_regime')}},indent=2))
    return 0
if __name__=='__main__': raise SystemExit(main())
