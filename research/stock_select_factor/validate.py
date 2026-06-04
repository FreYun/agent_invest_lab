"""第七轮 — vibe-trading validation 三件套 (用 vibe venv python 3.11).
配置: trend MA150 自适应, 防守腿idiovol40, 进攻腿距高点120, top40, 周调仓.
全样本 + OOS(22-26) 分别给 bootstrap Sharpe CI / walk-forward / monte-carlo.
"""
import sys
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import sqlite3, numpy as np, pandas as pd
from backtest.validation import monte_carlo_test, bootstrap_sharpe_ci, walk_forward_analysis
from backtest.models import TradeRecord

DB="/home/rooot/database/market.db"; MV=1_000_000.0; COST=5e-4; CAP=1_000_000.0
MA,DEFW,OFFW,TK,RB=150,40,120,40,10
c=sqlite3.connect(DB)
df=pd.read_sql("SELECT trade_date,ts_code,close,pct_chg FROM daily",c)
ts=pd.read_sql("SELECT ts_code,total_share FROM daily_basic WHERE total_share IS NOT NULL AND trade_date=(SELECT MAX(trade_date) FROM daily_basic)",c)
idx=pd.read_sql("SELECT trade_date,close FROM index_daily WHERE ts_code='000300.SH'",c); c.close()
close=df.pivot(index="trade_date",columns="ts_code",values="close").sort_index().astype("float32")
ret=df.pivot(index="trade_date",columns="ts_code",values="pct_chg").sort_index().astype("float32")/100
share=ts.set_index("ts_code")["total_share"].reindex(close.columns)
mask=(close*share.values>=MV)&close.notna()
hs=idx.set_index("trade_date")["close"].reindex(close.index).ffill()
dm=ret.sub(ret.where(mask).mean(axis=1),axis=0)
faD=(-(dm.rolling(DEFW).std())).where(mask)
faO=(close/close.rolling(OFFW).max()).where(mask)
sig=hs/hs.rolling(MA).mean()-1

n=len(ret.index); cols=ret.columns; W=np.zeros((n,len(cols)),dtype="float32"); periods=[]
for i in range(250,n,RB):
    s=sig.iloc[i]; use=faO if (pd.notna(s) and s>0) else faD
    v=use.iloc[i].dropna()
    if len(v)<TK: continue
    ci=cols.get_indexer(v.nlargest(TK).index); e=min(i+RB,n); W[i:e,:]=0; W[i:e,ci]=1/TK
    periods.append((i,e))
pos=pd.DataFrame(W,index=ret.index,columns=cols).shift(1).fillna(0)
strat=(pos*ret.fillna(0)).sum(axis=1)-pos.diff().abs().sum(axis=1).fillna(0)*COST

def build(seg_start):
    r=strat[strat.index>=seg_start].dropna()
    eq=CAP*(1+r).cumprod(); eq.index=pd.to_datetime(eq.index,format="%Y%m%d")
    # 每个调仓周期一笔 trade
    trades=[]
    for (i,e) in periods:
        d0=ret.index[i]
        if d0<seg_start: continue
        pr=strat.iloc[i+1:e+1].sum()  # 该周期(T+1后)累计收益近似
        trades.append(TradeRecord(symbol="ADAPT",direction=1,entry_price=100.0,exit_price=100.0*(1+pr),
            entry_time=pd.to_datetime(d0,format="%Y%m%d"),exit_time=pd.to_datetime(ret.index[min(e,n-1)],format="%Y%m%d"),
            size=1.0,leverage=1.0,pnl=pr*CAP,pnl_pct=pr,exit_reason="signal",holding_bars=e-i,commission=0.0))
    return r,eq,trades

for label,start in [("全样本","20140701"),("OOS 22-26","20220101")]:
    r,eq,trades=build(start)
    print(f"\n========== {label}  (n_days={len(r)}, n_trades={len(trades)}) ==========")
    ci=bootstrap_sharpe_ci(eq,n_bootstrap=2000,bars_per_year=242,seed=42)
    print(f"[bootstrap] Sharpe obs={ci['observed_sharpe']:.2f} CI95=[{ci['ci_lower']:.2f},{ci['ci_upper']:.2f}] prob>0={ci.get('prob_positive',float('nan')):.0%}")
    try:
        mc=monte_carlo_test(trades,initial_capital=CAP,n_simulations=2000,seed=42)
        print(f"[monte_carlo] p(sharpe)={mc.get('p_value_sharpe',float('nan')):.3f} p(maxdd)={mc.get('p_value_max_dd',float('nan')):.3f}")
    except Exception as e: print("[monte_carlo] 失败:",str(e)[:90])
    try:
        wf=walk_forward_analysis(eq,trades,n_windows=5,bars_per_year=242)
        ws=wf.get("windows",wf) if isinstance(wf,dict) else wf
        shps=[w.get("sharpe") for w in ws] if isinstance(ws,list) else None
        print(f"[walk_forward] 各窗Sharpe={[round(s,2) for s in shps] if shps else wf}")
    except Exception as e: print("[walk_forward] 失败:",str(e)[:90])
