import unittest
import pandas as pd
import numpy as np
from crypto_super_radar.range_mean_reversion import (
    RangeConfig, RangeMeanReversionEngine, indicators,
    select_portfolio, metrics,
)

class RangeEngineTests(unittest.TestCase):
    def test_config_matches_research_hypothesis(self):
        c=RangeConfig()
        self.assertEqual((c.adx_threshold,c.bb_period,c.bb_std),(22.0,20,2.0))
        self.assertEqual((c.rsi_period,c.rsi_long,c.rsi_short),(14,32.0,68.0))
        self.assertEqual((c.stop_atr,c.min_target_r,c.max_holding_bars),(1.5,1.5,10))
        self.assertEqual(c.crash_atr_multiple,2.5)

    def test_future_bars_do_not_rewrite_past_indicators(self):
        ix=pd.date_range('2026-01-01',periods=80,freq='15min',tz='UTC')
        base=100+np.sin(np.arange(80)/4)+np.arange(80)*0.01
        f=pd.DataFrame({'open':base-0.1,'high':base+0.4,'low':base-0.4,'close':base+0.1,'volume':1000.0},index=ix)
        a=indicators(f,RangeConfig())
        g=f.copy(); g.iloc[60:,g.columns.get_indexer(['open','high','low','close'])]*=7
        b=indicators(g,RangeConfig())
        for col in ('atr','rsi','mid','upper','lower','adx'):
            self.assertAlmostEqual(float(a[col].iloc[59]),float(b[col].iloc[59]),places=10)

    def test_engine_rejects_unapproved_symbol(self):
        engine=RangeMeanReversionEngine()
        with self.assertRaises(ValueError):
            engine.run_symbol(None,'DOGE-USDT')

    def test_portfolio_selection_caps_concurrency_and_symbol_overlap(self):
        rows=[]
        for i,symbol in enumerate(['BTC-USDT','ETH-USDT','SOL-USDT','XRP-USDT','LINK-USDT']):
            rows.append({'symbol':symbol,'entry_ts':f'2026-01-01T00:0{i}:00+00:00','exit_ts':'2026-01-01T02:00:00+00:00'})
        rows.append({'symbol':'BTC-USDT','entry_ts':'2026-01-01T00:30:00+00:00','exit_ts':'2026-01-01T01:30:00+00:00'})
        selected=select_portfolio(rows,cap=4)
        self.assertEqual(len(selected),4)
        self.assertEqual(len({r['symbol'] for r in selected}),4)

    def test_metrics_use_net_pnl_and_group_simultaneous_exits(self):
        trades=[
            {'exit_ts':'2026-01-01T01:00:00Z','net_pnl_usdt':2.0,'fees_usdt':0.1,'spread_usdt':0.1,'slippage_usdt':0.0,'funding_cost_usdt':0.0},
            {'exit_ts':'2026-01-01T01:00:00Z','net_pnl_usdt':-1.0,'fees_usdt':0.1,'spread_usdt':0.1,'slippage_usdt':0.0,'funding_cost_usdt':0.0},
        ]
        m=metrics(trades)
        self.assertEqual(m['trades'],2)
        self.assertAlmostEqual(m['net_pnl_usdt'],1.0)
        self.assertAlmostEqual(m['gross_pnl_usdt'],1.4)
        self.assertAlmostEqual(m['profit_factor'],2.0)

if __name__=='__main__':
    unittest.main()
