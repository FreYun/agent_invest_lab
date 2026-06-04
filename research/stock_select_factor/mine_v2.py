"""第二轮因子挖掘 — 更聪明的截面构造 + IC + 分regime Calmar.

目标: 找一个跨 regime (22-24红利 / 25-26创业板) 都能稳的选股因子,
不是裸动量/裸低波. 候选族:
  - 风险调整动量 (sharpe_mom): 累计收益/波动, 奖励平滑趋势
  - 趋势质量 (efficiency ratio): 路径效率, 直上直下的高分, 来回震荡的低分
  - 距高点 (52w high proximity): 接近1年高点的强者恒强
  - 加速度 (accel): 短动量-长动量
  - 下行调整动量 (sortino_mom)
  - 复合 z-score
判定: 截面 rank IC 均值/IR + top40 组合分 regime ann/Calmar + 换手.
"""
import sqlite3, numpy as np, pandas as pd

def spearman(a, b):
    """rank IC, 无 scipy 依赖."""
    return a.rank().corr(b.rank())

DB="/home/rooot/database/market.db"; MV=1_000_000.0; COST=5e-4; ANN=242.0
SEGS=[("14-21","20140701","20211231"),("22-24","20220101","20241231"),("25-26","20250101","20261231")]

c=sqlite3.connect(DB)
df=pd.read_sql("SELECT trade_date,ts_code,close,pct_chg FROM daily",c)
ts=pd.read_sql("SELECT ts_code,total_share FROM daily_basic WHERE total_share IS NOT NULL AND trade_date=(SELECT MAX(trade_date) FROM daily_basic)",c); c.close()
close=df.pivot(index="trade_date",columns="ts_code",values="close").sort_index().astype("float32")
ret=df.pivot(index="trade_date",columns="ts_code",values="pct_chg").sort_index().astype("float32")/100
share=ts.set_index("ts_code")["total_share"].reindex(close.columns)
mask=(close*share.values>=MV)&close.notna()
logc=np.log(close.clip(lower=0.01))

def zscore(d):
    return d.sub(d.mean(axis=1),axis=0).div(d.std(axis=1).replace(0,np.nan),axis=0)

# ---- 因子库 ----
def eff_ratio(n):  # 路径效率 (signed Kaufman ER), 趋势平滑度
    net=(close-close.shift(n))
    path=close.diff().abs().rolling(n).sum().replace(0,np.nan)
    return net/path  # 已带方向
def down_dev(n):
    neg=ret.where(ret<0,0.0); return neg.pow(2).rolling(n).mean().pow(0.5)

facs={}
for n in (60,120):
    facs[f"sharpe_mom_{n}"]=(close.shift(5)/close.shift(n)-1)/(ret.rolling(n).std().replace(0,np.nan))
    facs[f"sortino_mom_{n}"]=(close.shift(5)/close.shift(n)-1)/down_dev(n).replace(0,np.nan)
    facs[f"eff_ratio_{n}"]=eff_ratio(n)
facs["high_prox_250"]=close/close.rolling(250).max()
facs["high_prox_120"]=close/close.rolling(120).max()
facs["accel_20_60"]=(close.shift(5)/close.shift(20)-1)-(close.shift(20)/close.shift(60)-1)
# 复合: 风险调整动量 + 距高点 + 趋势质量
facs["combo_rqh"]=zscore(facs["sharpe_mom_60"])+zscore(facs["high_prox_250"])+zscore(facs["eff_ratio_60"])

def bt(factor,top_k=40,rb=10):
    fa=factor.where(mask); n=len(ret.index); cols=ret.columns
    W=np.zeros((n,len(cols)),dtype="float32")
    for i in range(250,n,rb):  # 250 warmup (52w)
        v=fa.iloc[i].dropna()
        if len(v)<top_k: continue
        ci=cols.get_indexer(v.nlargest(top_k).index); W[i:min(i+rb,n),:]=0; W[i:min(i+rb,n),ci]=1/top_k
    Wdf=pd.DataFrame(W,index=ret.index,columns=cols); pos=Wdf.shift(1).fillna(0)
    r=(pos*ret.fillna(0)).sum(axis=1)-pos.diff().abs().sum(axis=1).fillna(0)*COST
    turn=pos.diff().abs().sum(axis=1).fillna(0)
    return r, turn.sum()/(len(r)/ANN)

def ic(factor,rb=10):
    fa=factor.where(mask); fwd=close.shift(-rb)/close-1
    vals=[]
    idx=list(range(250,len(ret.index)-rb,rb))
    for i in idx:
        a=fa.iloc[i]; b=fwd.iloc[i]; m=a.notna()&b.notna()
        if m.sum()<30: continue
        rho=spearman(a[m],b[m])
        if not np.isnan(rho): vals.append(rho)
    v=pd.Series(vals); return v.mean(), (v.mean()/v.std() if v.std() else 0), (v>0).mean()

def met(r,s,e):
    x=r[(r.index>=s)&(r.index<=e)].dropna()
    if len(x)<20 or x.std()==0: return (0,0)
    eq=(1+x).cumprod(); a=eq.iloc[-1]**(ANN/len(x))-1; mdd=(eq/eq.cummax()-1).min()
    return a,(a/abs(mdd) if mdd<0 else 0)

print(f"{'因子':16s} {'IC':>6s} {'ICir':>6s} {'IC>0':>5s} | " + " ".join(f"{lab:>13s}" for lab,_,_ in SEGS) + f" {'换手/yr':>7s}")
rows=[]
for fn,fd in facs.items():
    r,tn=bt(fd); icm,icir,icp=ic(fd)
    cells=[]
    seg_cal={}
    for lab,s,e in SEGS:
        a,cal=met(r,s,e); cells.append(f"{a:+.0%}/{cal:+.2f}".rjust(13)); seg_cal[lab]=cal
    print(f"{fn:16s} {icm:+.3f} {icir:+.2f} {icp:.0%} | " + " ".join(cells) + f" {tn:6.1f}")
    rows.append(dict(factor=fn,ic=round(icm,3),ic_ir=round(icir,2),ic_pos=round(icp,2),
                     cal_22_24=round(seg_cal["22-24"],2),cal_25_26=round(seg_cal["25-26"],2),
                     min_cal=round(min(seg_cal["22-24"],seg_cal["25-26"]),2),turn_yr=round(tn,1)))
res=pd.DataFrame(rows).sort_values("min_cal",ascending=False)
res.to_csv("/home/rooot/agent_invest_lab/research/stock_select_factor/mine_v2_summary.csv",index=False)
print("\n按'两个regime里较差的那个Calmar'排序(要的是跨regime都不塌):")
print(res.to_string(index=False))
