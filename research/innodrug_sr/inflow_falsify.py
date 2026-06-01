"""Prove the 15mo cum120-inflow +0.6 corr is a path artifact: split 7yr into
non-overlapping ~1yr segments, show cum120_rank vs fwd20 corr is unstable / sign-flipping.
"""
from pathlib import Path
import numpy as np, pandas as pd
DATA = Path("/home/rooot/agent_invest_lab/research/innodrug_sr/data")
m = pd.read_csv(DATA/"sector_BK000208_market.csv", parse_dates=["date"]).set_index("date").sort_index()
m["ret"]=m["ret_pct"]/100; m["close"]=(1+m["ret"]).cumprod()
inflow=m["main_inflow"]; close=m["close"]
def sig(N): return inflow.rolling(N,min_periods=N//2).sum().rolling(252,min_periods=60).rank(pct=True)
fwd20=close.shift(-20)/close-1
for N in [60,120]:
    s=sig(N)
    print(f"\ncum{N}_rank vs fwd20 — 不重叠年度子段 spearman:")
    d=pd.concat([s.rename("s"),fwd20.rename("f")],axis=1).dropna()
    bnds=np.linspace(0,len(d),7).astype(int)
    seg=[d.iloc[bnds[i]:bnds[i+1]] for i in range(6)]
    for i,g in enumerate(seg):
        c=g["s"].corr(g["f"],method="spearman")
        print(f"  段{i+1} {g.index[0].date()}..{g.index[-1].date()} n={len(g):3d}  corr={c:+.3f}")
    print(f"  全7年 corr={d['s'].corr(d['f'],method='spearman'):+.3f}  (子段忽正忽负⇒不稳定⇒15月+0.6是路径运气)")
