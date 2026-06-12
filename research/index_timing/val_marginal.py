import sys
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
DATA=Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing/data");SR=Path("/home/ubuntu/rooot/agent_invest_lab/research/sr_factor/data")
def load(p):df=pd.read_csv(p,parse_dates=[0],index_col=0).sort_index();df.index.name="date";return df
def bt(w,sig,s):
    sig=sig.clip(0,1).fillna(0).reindex(w.index).fillna(0);ret=w["close"].pct_change().fillna(0)
    pos=sig.shift(1).fillna(0);cost=pos.diff().abs().fillna(pos.iloc[0])*0.0005
    sr=(pos*ret-cost);m=w.index>=pd.Timestamp(s);return (1+sr.loc[m]).cumprod(),pos.loc[m].mean()
def stats(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return ann,dd,(ann/abs(dd) if dd<0 else 0),(r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0)
def V(name,p,s):
    df=load(p);w=L.slice_range(df,s,"2026-06-06",260)
    eq,exp=bt(w,L.s_donchian(w,win=60),s);eqb,_=bt(w,L.s_buyhold(w),s)
    a,d,c,sh=stats(eq);ab,db,cb,_=stats(eqb)
    ci=bootstrap_sharpe_ci(eq,n_bootstrap=1000,confidence=0.95)
    wf=walk_forward_analysis(eq,[],n_windows=5);shs=[round(x.get("sharpe"),2) for x in (wf.get("windows") or wf.get("per_window") or [])]
    pos_wf=sum(1 for x in shs if x and x>0)
    print("%-8s Cal=%.2f(BH%.2f) DD=%.2f(BH%.2f) Shp=%.2f exp=%.2f | bootCI=[%.2f,%.2f]p+=%.0f%% wf=%s(%d/5)"%(
        name,c,cb,d,db,sh,exp,ci.get("ci_lower",0),ci.get("ci_upper",0),ci.get("prob_positive",0)*100,shs,pos_wf))
print("donchian_60 完整 validation:")
V("半导体",SR/"512480.SH.csv","2019-06-01")
V("稀土",DATA/"159713.SZ.csv","2021-06-01")
V("光伏",DATA/"515790.SH.csv","2021-01-01")
