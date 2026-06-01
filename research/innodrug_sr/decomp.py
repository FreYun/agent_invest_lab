import sys; sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
from pathlib import Path
import numpy as np, pandas as pd
BASE=Path("/home/rooot/agent_invest_lab/research/innodrug_sr"); DATA=BASE/"data"; COM=0.0005; WU=110
def lc(f):
    d=pd.read_csv(DATA/f,parse_dates=[0]); d.columns=[c.lower() for c in d.columns]
    d=d.set_index(d.columns[0]).sort_index(); s=d["close"].astype(float); s.index=pd.to_datetime(s.index).normalize(); return s
sr=pd.read_csv(BASE/"innodrug_sr_daily.csv",parse_dates=["date"]); sr=sr[sr.persona=="个人"].set_index("date").sort_index()
sr.index=pd.to_datetime(sr.index).normalize()
sr["imbalance"]=(sr.applied-sr.redeemed)/(sr.applied+sr.redeemed).replace(0,np.nan)
def z(s,n,w): c=s.rolling(n,min_periods=max(3,n//2)).sum(); return (c-c.rolling(w,min_periods=20).mean())/c.rolling(w,min_periods=20).std()
def sig(close,src,n,w,thr):
    zz=z(sr[src],n,w).reindex(close.index,method="ffill"); st=pd.Series(np.nan,index=close.index)
    st=st.where(~(zz>thr),0.0); st=st.where(~(zz<-thr),1.0); return st.ffill().fillna(0.5)
c=lc("159992.SZ.csv").loc[sr.index.min():sr.index.max()]
ret=c.pct_change().fillna(0)
for src,n,w,thr in [("net",10,60,1.5),("imbalance",15,90,1.0)]:
    s=sig(c,src,n,w,thr); pos=s.shift(1).fillna(0); strat=pos*ret-pos.diff().abs().fillna(0)*COM
    exc=(strat-ret).iloc[WU:]   # factor minus buyhold daily excess
    m=exc.resample("ME").apply(lambda x:(1+x).prod()-1)
    print(f"\n=== {src}_{n}_{w}_thr{thr}  月度超额(因子-buyhold) ===")
    print((m*100).round(1).to_string())
    tot=(1+exc).prod()-1; top=m.abs().nlargest(3)
    print(f"  全期累计超额={tot*100:.1f}%   贡献最大3个月={[str(d.date())[:7] for d in top.index]}")
    posm=(m>0).sum(); print(f"  超额为正的月份: {posm}/{len(m)} = {posm/len(m):.0%}")
