"""第四轮 — long-short 十分位价差 (剥离beta的纯选股alpha) + idiovol 窗口优化.
判定: top十分位 - bottom十分位 的多空价差, 每个regime是否都正.
"""
import sqlite3, numpy as np, pandas as pd
DB="/home/rooot/database/market.db"; MV=1_000_000.0; COST=5e-4; ANN=242.0
SEGS=[("14-21","20140701","20211231"),("22-24","20220101","20241231"),("25-26","20250101","20261231")]
c=sqlite3.connect(DB)
df=pd.read_sql("SELECT trade_date,ts_code,close,pct_chg FROM daily",c)
ts=pd.read_sql("SELECT ts_code,total_share FROM daily_basic WHERE total_share IS NOT NULL AND trade_date=(SELECT MAX(trade_date) FROM daily_basic)",c); c.close()
close=df.pivot(index="trade_date",columns="ts_code",values="close").sort_index().astype("float32")
ret=df.pivot(index="trade_date",columns="ts_code",values="pct_chg").sort_index().astype("float32")/100
share=ts.set_index("ts_code")["total_share"].reindex(close.columns)
mask=(close*share.values>=MV)&close.notna()
def demean(d): return d.sub(d.where(mask).mean(axis=1),axis=0)

facs={
 "idiovol_40": -(demean(ret).rolling(40).std()),
 "idiovol_60": -(demean(ret).rolling(60).std()),
 "idiovol_120":-(demean(ret).rolling(120).std()),
 "max_20":     -(ret.rolling(20).max()),
 "lowvol_60":  -(ret.rolling(60).std()),
}

def decile_ls(factor,rb=10,q=0.1):
    """每rb日: top q分位等权 - bottom q分位等权, T+1. 返回多空日收益序列 + 多头腿."""
    fa=factor.where(mask); n=len(ret.index); cols=ret.columns
    WL=np.zeros((n,len(cols)),dtype="float32"); WS=np.zeros((n,len(cols)),dtype="float32")
    for i in range(120,n,rb):
        v=fa.iloc[i].dropna()
        if len(v)<50: continue
        k=max(int(len(v)*q),10)
        hi=v.nlargest(k).index; lo=v.nsmallest(k).index
        e=min(i+rb,n)
        ci_h=cols.get_indexer(hi); ci_l=cols.get_indexer(lo)
        WL[i:e,:]=0; WL[i:e,ci_h]=1/k; WS[i:e,:]=0; WS[i:e,ci_l]=1/k
    L=pd.DataFrame(WL,index=ret.index,columns=cols).shift(1).fillna(0)
    S=pd.DataFrame(WS,index=ret.index,columns=cols).shift(1).fillna(0)
    long_r=(L*ret.fillna(0)).sum(axis=1)-L.diff().abs().sum(axis=1).fillna(0)*COST
    short_r=(S*ret.fillna(0)).sum(axis=1)
    ls=long_r-short_r
    return ls, long_r

def stat(r,s,e):
    x=r[(r.index>=s)&(r.index<=e)].dropna()
    if len(x)<20 or x.std()==0: return (0,0)
    return x.mean()*ANN, x.mean()/x.std()*np.sqrt(ANN)   # 年化, Sharpe

print(f"{'因子':14s} | " + " ".join(f"{lab+' LS(ann/Shp)':>20s}" for lab,_,_ in SEGS))
print("-"*92)
for fn,fd in facs.items():
    ls,lg=decile_ls(fd)
    cells=[f"{stat(ls,s,e)[0]:+.1%}/{stat(ls,s,e)[1]:+.2f}".rjust(20) for _,s,e in SEGS]
    print(f"{fn:14s} | " + " ".join(cells))
print("\n(LS=多空价差, 正且各段一致=真选股alpha且与regime无关; Sharpe>0.5算可观)")
print("\n同因子的纯多头腿 (long-only top十分位) 年化, 看beta拖累:")
print(f"{'因子':14s} | " + " ".join(f"{lab:>13s}" for lab,_,_ in SEGS))
for fn,fd in facs.items():
    ls,lg=decile_ls(fd)
    print(f"{fn:14s} | " + " ".join(f"{stat(lg,s,e)[0]:+.1%}".rjust(13) for _,s,e in SEGS))
