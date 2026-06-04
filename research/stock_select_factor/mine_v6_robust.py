"""第六轮 — trend120 自适应因子的 robustness 扫描.
变 MA窗口 × 防守腿窗口 × 进攻腿窗口 × top_k × rb, 看邻域是否普遍超基准(全样本Calmar>0.24).
SOP步骤7: ≥40%配置超基准 + 最优区在网格中央 + median dCalmar>0 才算稳.
"""
import sqlite3, itertools, numpy as np, pandas as pd
DB="/home/rooot/database/market.db"; MV=1_000_000.0; COST=5e-4; ANN=242.0
SEGS=[("14-21","20140701","20211231"),("22-24","20220101","20241231"),("25-26","20250101","20261231"),("全","20140701","20261231")]
BENCH_FULL_CAL=0.24
c=sqlite3.connect(DB)
df=pd.read_sql("SELECT trade_date,ts_code,close,pct_chg FROM daily",c)
ts=pd.read_sql("SELECT ts_code,total_share FROM daily_basic WHERE total_share IS NOT NULL AND trade_date=(SELECT MAX(trade_date) FROM daily_basic)",c)
idx=pd.read_sql("SELECT trade_date,ts_code,close FROM index_daily WHERE ts_code='000300.SH'",c); c.close()
close=df.pivot(index="trade_date",columns="ts_code",values="close").sort_index().astype("float32")
ret=df.pivot(index="trade_date",columns="ts_code",values="pct_chg").sort_index().astype("float32")/100
share=ts.set_index("ts_code")["total_share"].reindex(close.columns)
mask=(close*share.values>=MV)&close.notna()
hs=idx.set_index("trade_date")["close"].reindex(close.index).ffill()
dm=ret.sub(ret.where(mask).mean(axis=1),axis=0)   # demeaned once

# 预算各窗口的腿因子
def def_leg(w): return (-(dm.rolling(w).std())).where(mask)
def off_leg(w): return (close/close.rolling(w).max()).where(mask)
DEF={w:def_leg(w) for w in (40,60,120)}
OFF={w:off_leg(w) for w in (120,250)}
TREND={n:(hs/hs.rolling(n).mean()-1) for n in (60,90,120,150,200)}

def bt_adaptive(faO,faD,sig,top_k,rb):
    n=len(ret.index); cols=ret.columns; W=np.zeros((n,len(cols)),dtype="float32")
    for i in range(250,n,rb):
        s=sig.iloc[i]; use=faO if (pd.notna(s) and s>0) else faD
        v=use.iloc[i].dropna()
        if len(v)<top_k: continue
        ci=cols.get_indexer(v.nlargest(top_k).index); W[i:min(i+rb,n),:]=0; W[i:min(i+rb,n),ci]=1/top_k
    pos=pd.DataFrame(W,index=ret.index,columns=cols).shift(1).fillna(0)
    return (pos*ret.fillna(0)).sum(axis=1)-pos.diff().abs().sum(axis=1).fillna(0)*COST

def cal(r,s,e):
    x=r[(r.index>=s)&(r.index<=e)].dropna()
    if len(x)<20 or x.std()==0: return 0
    eq=(1+x).cumprod(); a=eq.iloc[-1]**(ANN/len(x))-1; mdd=(eq/eq.cummax()-1).min()
    return a/abs(mdd) if mdd<0 else 0

rows=[]
for ma,dw,ow,tk,rb in itertools.product((60,90,120,150,200),(40,60,120),(120,250),(20,40),(10,)):
    r=bt_adaptive(OFF[ow],DEF[dw],TREND[ma],tk,rb)
    cals={lab:cal(r,s,e) for lab,s,e in SEGS}
    rows.append(dict(ma=ma,defw=dw,offw=ow,tk=tk,rb=rb,
        full=round(cals["全"],2),s14=round(cals["14-21"],2),s22=round(cals["22-24"],2),s25=round(cals["25-26"],2),
        all_pos=int(min(cals["14-21"],cals["22-24"],cals["25-26"])>0),
        beat=int(cals["全"]>BENCH_FULL_CAL)))
res=pd.DataFrame(rows).sort_values("full",ascending=False)
res.to_csv("/home/rooot/agent_invest_lab/research/stock_select_factor/v6_robust.csv",index=False)
n=len(res)
print(f"配置数={n}")
print(f"全样本 Calmar 超基准(>{BENCH_FULL_CAL})占比: {res['beat'].mean():.0%}")
print(f"三段全正占比: {res['all_pos'].mean():.0%}")
print(f"全样本 Calmar  median={res['full'].median():.2f}  mean={res['full'].mean():.2f}  min={res['full'].min():.2f}  max={res['full'].max():.2f}")
print(f"\n按MA窗口分组的全样本Calmar均值 (看最优区是否连续):")
print(res.groupby('ma')['full'].mean().round(2).to_string())
print(f"\nTop 8 配置:")
print(res.head(8).to_string(index=False))
print(f"\nBottom 5 配置:")
print(res.tail(5).to_string(index=False))
