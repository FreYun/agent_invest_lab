"""分段验证: 22-24 (红利regime) vs 25-26 (创业板regime), 看因子超额是否随风格切换翻转."""
import sqlite3, numpy as np, pandas as pd
DB="/home/rooot/database/market.db"; MV=1_000_000.0; COST=5e-4; ANN=242.0
SEGS=[("14-21","20140701","20211231"),("22-24","20220101","20241231"),("25-26","20250101","20261231")]

c=sqlite3.connect(DB)
df=pd.read_sql("SELECT trade_date,ts_code,close,pct_chg,vol FROM daily",c)
ts=pd.read_sql("SELECT ts_code,total_share FROM daily_basic WHERE total_share IS NOT NULL AND trade_date=(SELECT MAX(trade_date) FROM daily_basic)",c); c.close()
close=df.pivot(index="trade_date",columns="ts_code",values="close").sort_index().astype("float32")
ret=df.pivot(index="trade_date",columns="ts_code",values="pct_chg").sort_index().astype("float32")/100
vol=df.pivot(index="trade_date",columns="ts_code",values="vol").sort_index().astype("float32")
share=ts.set_index("ts_code")["total_share"].reindex(close.columns)
mask=(close*share.values>=MV)&close.notna()

facs={
 "lowvol_60": -(ret.rolling(60).std()),
 "lowvol_20": -(ret.rolling(20).std()),
 "mom_20_sk5": (close.shift(5)/close.shift(20)-1),
 "mom_60_sk5": (close.shift(5)/close.shift(60)-1),
 "rev_5": -(close/close.shift(5)-1),
 "volup_5": (vol/vol.rolling(20).mean()).rolling(5).mean(),
}

def bt(factor,top_k=40,rb=10):
    fa=factor.where(mask); n=len(ret.index); cols=ret.columns
    W=np.zeros((n,len(cols)),dtype="float32")
    for i in range(60,n,rb):
        v=fa.iloc[i].dropna()
        if len(v)<top_k: continue
        ci=cols.get_indexer(v.nlargest(top_k).index); W[i:min(i+rb,n),:]=0; W[i:min(i+rb,n),ci]=1/top_k
    Wdf=pd.DataFrame(W,index=ret.index,columns=cols); pos=Wdf.shift(1).fillna(0)
    return (pos*ret.fillna(0)).sum(axis=1)-pos.diff().abs().sum(axis=1).fillna(0)*COST

def w_ew():
    w=mask.astype("float32"); w=w.div(w.sum(axis=1).replace(0,np.nan),axis=0).fillna(0)
    return (w.shift(1).fillna(0)*ret.fillna(0)).sum(axis=1)

def met(r,s,e):
    x=r[(r.index>=s)&(r.index<=e)].dropna()
    if len(x)<20 or x.std()==0: return (0,0,0)
    eq=(1+x).cumprod(); a=eq.iloc[-1]**(ANN/len(x))-1; mdd=(eq/eq.cummax()-1).min()
    return a,(a/abs(mdd) if mdd<0 else 0),mdd

bench=w_ew()
print(f"{'因子/基准':14s} " + "  ".join(f"{lab+'(ann/cal)':>20s}" for lab,_,_ in SEGS))
def line(name,r):
    cells=[]
    for lab,s,e in SEGS:
        a,cal,_=met(r,s,e); cells.append(f"{a:+.1%}/{cal:+.2f}".rjust(20))
    print(f"{name:14s} " + "  ".join(cells))
line("基准等权",bench)
for fn,fd in facs.items(): line(fn,bt(fd))
