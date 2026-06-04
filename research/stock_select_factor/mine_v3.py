"""第三轮 — 分regime IC + regime自适应选股因子.

验证: 选股能力(IC)是否随 regime 翻转? 自适应因子能否跨regime通吃?
开关: regime_classify_daily.total_score (>0 强市=进攻, <=0 弱市=防守).
"""
import sqlite3, numpy as np, pandas as pd

DB="/home/rooot/database/market.db"; MV=1_000_000.0; COST=5e-4; ANN=242.0
SEGS=[("14-21","20140701","20211231"),("22-24","20220101","20241231"),("25-26","20250101","20261231")]

c=sqlite3.connect(DB)
df=pd.read_sql("SELECT trade_date,ts_code,close,pct_chg FROM daily",c)
ts=pd.read_sql("SELECT ts_code,total_share FROM daily_basic WHERE total_share IS NOT NULL AND trade_date=(SELECT MAX(trade_date) FROM daily_basic)",c)
reg=pd.read_sql("SELECT trade_date,total_score,regime_code FROM regime_classify_daily",c); c.close()
close=df.pivot(index="trade_date",columns="ts_code",values="close").sort_index().astype("float32")
ret=df.pivot(index="trade_date",columns="ts_code",values="pct_chg").sort_index().astype("float32")/100
share=ts.set_index("ts_code")["total_share"].reindex(close.columns)
mask=(close*share.values>=MV)&close.notna()
# regime score 对齐到交易日 (regime 用 YYYY-MM-DD, daily 用 YYYYMMDD)
reg["d"]=reg["trade_date"].str.replace("-","")
reg=reg.drop_duplicates("d",keep="last")   # 同日 v1/v2 去重, 留最新
score=reg.set_index("d")["total_score"].reindex(close.index).ffill()
print(f"regime 覆盖: {score.notna().sum()}/{len(score)} 天; total_score 中位={score.median():.0f} 范围[{score.min():.0f},{score.max():.0f}]")

# 因子库
def demean(d): return d.sub(d.where(mask).mean(axis=1),axis=0)
facs={
 "lowvol_60": -(ret.rolling(60).std()),                     # 防守
 "max_20":    -(ret.rolling(20).max()),                     # 彩票(低MAX好), 防守系
 "idiovol_60":-(demean(ret).rolling(60).std()),             # 特质波动(剔市场)
 "high_prox_250": close/close.rolling(250).max(),           # 进攻
 "sharpe_mom_60": (close.shift(5)/close.shift(60)-1)/(ret.rolling(60).std().replace(0,np.nan)),  # 进攻
}

def ic_seg(factor,s,e,rb=10):
    fa=factor.where(mask); fwd=close.shift(-rb)/close-1; vals=[]
    sub=[d for d in ret.index if s<=d<=e]
    idx=[ret.index.get_loc(d) for d in sub[::rb] if ret.index.get_loc(d)>=250 and ret.index.get_loc(d)<len(ret)-rb]
    for i in idx:
        a=fa.iloc[i]; b=fwd.iloc[i]; m=a.notna()&b.notna()
        if m.sum()<30: continue
        rho=a[m].rank().corr(b[m].rank())
        if not np.isnan(rho): vals.append(rho)
    return np.mean(vals) if vals else 0.0

def bt(factor,top_k=40,rb=10):
    fa=factor.where(mask); n=len(ret.index); cols=ret.columns
    W=np.zeros((n,len(cols)),dtype="float32")
    for i in range(250,n,rb):
        v=fa.iloc[i].dropna()
        if len(v)<top_k: continue
        ci=cols.get_indexer(v.nlargest(top_k).index); W[i:min(i+rb,n),:]=0; W[i:min(i+rb,n),ci]=1/top_k
    Wdf=pd.DataFrame(W,index=ret.index,columns=cols); pos=Wdf.shift(1).fillna(0)
    r=(pos*ret.fillna(0)).sum(axis=1)-pos.diff().abs().sum(axis=1).fillna(0)*COST
    return r, pos.diff().abs().sum(axis=1).fillna(0).sum()/(len(r)/ANN)

def bt_adaptive(off,deff,top_k=40,rb=10,thr=0.0):
    """每个调仓日按 regime score 选用进攻或防守因子."""
    foff=off.where(mask); fdef=deff.where(mask); n=len(ret.index); cols=ret.columns
    W=np.zeros((n,len(cols)),dtype="float32"); pick=[]
    for i in range(250,n,rb):
        sc=score.iloc[i]; use=foff if (pd.notna(sc) and sc>thr) else fdef
        pick.append(1 if (pd.notna(sc) and sc>thr) else 0)
        v=use.iloc[i].dropna()
        if len(v)<top_k: continue
        ci=cols.get_indexer(v.nlargest(top_k).index); W[i:min(i+rb,n),:]=0; W[i:min(i+rb,n),ci]=1/top_k
    Wdf=pd.DataFrame(W,index=ret.index,columns=cols); pos=Wdf.shift(1).fillna(0)
    r=(pos*ret.fillna(0)).sum(axis=1)-pos.diff().abs().sum(axis=1).fillna(0)*COST
    return r, pos.diff().abs().sum(axis=1).fillna(0).sum()/(len(r)/ANN), np.mean(pick)

def met(r,s,e):
    x=r[(r.index>=s)&(r.index<=e)].dropna()
    if len(x)<20 or x.std()==0: return (0,0)
    eq=(1+x).cumprod(); a=eq.iloc[-1]**(ANN/len(x))-1; mdd=(eq/eq.cummax()-1).min()
    return a,(a/abs(mdd) if mdd<0 else 0)

print("\n===== 分 regime 的截面 rank IC (验证选股能力是否翻转) =====")
print(f"{'因子':16s} " + " ".join(f"{lab:>9s}" for lab,_,_ in SEGS))
for fn,fd in facs.items():
    print(f"{fn:16s} " + " ".join(f"{ic_seg(fd,s,e):>+9.3f}" for _,s,e in SEGS))

print("\n===== 组合分 regime 表现 (ann/Calmar) =====")
print(f"{'策略':22s} " + " ".join(f"{lab:>13s}" for lab,_,_ in SEGS) + f" {'换手/yr':>7s}")
def line(name,r,tn):
    print(f"{name:22s} " + " ".join(f"{met(r,s,e)[0]:+.0%}/{met(r,s,e)[1]:+.2f}".rjust(13) for _,s,e in SEGS) + f" {tn:6.1f}")
for fn in ("lowvol_60","high_prox_250","sharpe_mom_60"):
    r,tn=bt(facs[fn]); line(f"静态 {fn}",r,tn)
for offname in ("high_prox_250","sharpe_mom_60"):
    r,tn,pk=bt_adaptive(facs[offname],facs["lowvol_60"])
    line(f"自适应 {offname[:10]}|lowvol",r,tn)
    print(f"    └ 进攻仓位占比={pk:.0%}")
