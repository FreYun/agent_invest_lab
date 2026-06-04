"""第八轮 — 无偏池重跑 + 2026 YTD + 当前 regime 状态 + 最新持仓.
真实 PIT 市值 (hist_mv.db 2018+) + ST 过滤, trend150 自适应, 分段+全样本+2026YTD.
"""
import sqlite3, numpy as np, pandas as pd
DB="/home/rooot/database/market.db"; HMV="/home/rooot/agent_invest_lab/research/stock_select_factor/data/hist_mv.db"
MV_YUAN=1e10; COST=5e-4; ANN=242.0
MA,DEFW,OFFW,TK,RB=150,40,120,40,10
SEGS=[("18-21","20180101","20211231"),("22-24","20220101","20241231"),("25-26","20250101","20261231"),
      ("2026YTD","20260101","20261231"),("全(18+)","20180101","20261231")]

c=sqlite3.connect(DB)
df=pd.read_sql("SELECT trade_date,ts_code,close,pct_chg FROM daily WHERE trade_date>='20180101'",c)
idx=pd.read_sql("SELECT trade_date,close FROM index_daily WHERE ts_code='000300.SH' AND trade_date>='20180101'",c); c.close()
h=sqlite3.connect(HMV); mv=pd.read_sql("SELECT trade_date,ts_code,total_mv FROM hist_mv",h); h.close()
close=df.pivot(index="trade_date",columns="ts_code",values="close").sort_index().astype("float32")
ret=df.pivot(index="trade_date",columns="ts_code",values="pct_chg").sort_index().astype("float32")/100
mvp=mv.pivot(index="trade_date",columns="ts_code",values="total_mv").sort_index()
mvp=mvp.reindex(index=close.index,columns=close.columns).ffill()
try:
    import akshare as ak
    nm=ak.stock_info_a_code_name(); nm["ts6"]=nm["code"].astype(str).str.zfill(6)
    name_map=dict(zip(nm["ts6"],nm["name"]))
    st6=set(nm.loc[nm["name"].str.contains("ST",na=False),"ts6"])
    st_codes=[c for c in close.columns if c.split(".")[0] in st6]
except Exception as e:
    print("akshare 名字获取失败:",str(e)[:80]); name_map={}; st_codes=[]
mask=(mvp>=MV_YUAN)&close.notna()
if st_codes: mask=mask & (~close.columns.isin(st_codes))
print(f"无偏池: 排除ST {len(st_codes)} 只; 日均池规模 ≈ {mask.sum(axis=1).mean():.0f} 只 (近似池 ~1528)")
print(f"  vs 第一轮(当前股本近似池): 早期被高估(漂移)、退市票被剔(survivorship)")

hs=idx.set_index("trade_date")["close"].reindex(close.index).ffill()
dm=ret.sub(ret.where(mask).mean(axis=1),axis=0)
faD=(-(dm.rolling(DEFW).std())).where(mask)
faO=(close/close.rolling(OFFW).max()).where(mask)
sig=hs/hs.rolling(MA).mean()-1

n=len(ret.index); cols=ret.columns; W=np.zeros((n,len(cols)),dtype="float32"); regime_log=[]
for i in range(250,n,RB):
    s=sig.iloc[i]; offensive = pd.notna(s) and s>0
    regime_log.append((ret.index[i], "进攻" if offensive else "防守", float(s) if pd.notna(s) else None))
    use=faO if offensive else faD
    v=use.iloc[i].dropna()
    if len(v)<TK: continue
    ci=cols.get_indexer(v.nlargest(TK).index); W[i:min(i+RB,n),:]=0; W[i:min(i+RB,n),ci]=1/TK
pos=pd.DataFrame(W,index=ret.index,columns=cols).shift(1).fillna(0)
ra=(pos*ret.fillna(0)).sum(axis=1)-pos.diff().abs().sum(axis=1).fillna(0)*COST
w=mask.astype("float32"); w=w.div(w.sum(axis=1).replace(0,np.nan),axis=0).fillna(0)
rb=(w.shift(1).fillna(0)*ret.fillna(0)).sum(axis=1)

def met(r,s,e):
    x=r[(r.index>=s)&(r.index<=e)].dropna()
    if len(x)<5 or x.std()==0: return (0,0,0,0)
    eq=(1+x).cumprod(); a=eq.iloc[-1]**(ANN/len(x))-1; mdd=(eq/eq.cummax()-1).min()
    cum=eq.iloc[-1]-1
    return a, (a/abs(mdd) if mdd<0 else 0), x.mean()/x.std()*np.sqrt(ANN), cum

print(f"\n{'策略 (ann/Cal/Sharpe/累计)':28s} " + " ".join(f"{lab:>22s}" for lab,_,_ in SEGS))
for name,r in [("无偏 基准等权",rb),("无偏 自适应 trend150",ra)]:
    print(f"{name:28s} " + " ".join(f"{met(r,s,e)[0]:+.0%}/{met(r,s,e)[1]:+.2f}/{met(r,s,e)[2]:+.2f}/{met(r,s,e)[3]:+.0%}".rjust(22) for _,s,e in SEGS))

# 2026 表现细节 + regime 序列
print("\n===== 2026 YTD 细节 =====")
y26=ra[ra.index>="20260101"].dropna()
b26=rb[rb.index>="20260101"].dropna()
print(f"自适应  累计 {(1+y26).prod()-1:+.1%}, 最大回撤 {((1+y26).cumprod()/(1+y26).cumprod().cummax()-1).min():+.1%}, 交易日 {len(y26)}")
print(f"基准    累计 {(1+b26).prod()-1:+.1%}, 最大回撤 {((1+b26).cumprod()/(1+b26).cumprod().cummax()-1).min():+.1%}")
print(f"超额    {(1+y26).prod()/(1+b26).prod()-1:+.1%}")

print("\n===== 2026 内 regime 切换序列 =====")
r26=[(d,r,s) for d,r,s in regime_log if d>="20260101"]
print(f"  共 {len(r26)} 个调仓点, 进攻={sum(1 for _,r,_ in r26 if r=='进攻')} 次, 防守={sum(1 for _,r,_ in r26 if r=='防守')} 次")
for d,r,s in r26: print(f"  {d}: {r} (300/MA150 偏离 {s*100:+.1f}%)" if s is not None else f"  {d}: {r}")

# 当前持仓
print("\n===== 最新调仓日持仓 =====")
last_i=max(i for i in range(250,n,RB))
last_d=ret.index[last_i]; last_s=sig.iloc[last_i]
offensive = pd.notna(last_s) and last_s>0
print(f"最新调仓 {last_d}: {'进攻(距高点)' if offensive else '防守(低特质波动)'} 腿  (signal={last_s:+.2%})")
use=faO if offensive else faD; v=use.iloc[last_i].dropna()
top=v.nlargest(TK).index
names=[name_map.get(t.split(".")[0],"?") for t in top]
print("  " + " | ".join(names))
