"""第五轮 — regime自适应选股因子.
开关: 中证1000/沪深300 相对强度 (小盘占优=进攻regime; 大盘占优=防守).
进攻腿=high_prox_250(吃成长beta+动量), 防守腿=idiovol_60(低特质波动).
多窗口 robustness; 跨 14-21/22-24/25-26 三段验证(含14-21内多次风格切换).
"""
import sqlite3, numpy as np, pandas as pd
DB="/home/rooot/database/market.db"; MV=1_000_000.0; COST=5e-4; ANN=242.0
SEGS=[("14-21","20140701","20211231"),("22-24","20220101","20241231"),("25-26","20250101","20261231"),("全样本","20140701","20261231")]
c=sqlite3.connect(DB)
df=pd.read_sql("SELECT trade_date,ts_code,close,pct_chg FROM daily",c)
ts=pd.read_sql("SELECT ts_code,total_share FROM daily_basic WHERE total_share IS NOT NULL AND trade_date=(SELECT MAX(trade_date) FROM daily_basic)",c)
idx=pd.read_sql("SELECT trade_date,ts_code,close FROM index_daily WHERE ts_code IN ('000852.SH','000300.SH')",c); c.close()
close=df.pivot(index="trade_date",columns="ts_code",values="close").sort_index().astype("float32")
ret=df.pivot(index="trade_date",columns="ts_code",values="pct_chg").sort_index().astype("float32")/100
share=ts.set_index("ts_code")["total_share"].reindex(close.columns)
mask=(close*share.values>=MV)&close.notna()
ipx=idx.pivot(index="trade_date",columns="ts_code",values="close").sort_index().reindex(close.index).ffill()
def demean(d): return d.sub(d.where(mask).mean(axis=1),axis=0)
DEF=-(demean(ret).rolling(60).std())            # 防守腿: 低特质波动
OFF=close/close.rolling(250).max()              # 进攻腿: 距52周高点

def rs_signal(n):  # 小盘-大盘 相对强度 (>0 = 小盘/成长占优 = 进攻)
    r1000=ipx["000852.SH"]/ipx["000852.SH"].shift(n)-1
    r300 =ipx["000300.SH"]/ipx["000300.SH"].shift(n)-1
    return (r1000-r300)

def trend_signal(n):  # 沪深300 高于 MA-n → risk-on=进攻
    hs=ipx["000300.SH"]; return (hs/hs.rolling(n).mean()-1)

def vol_signal(n):  # 沪深300 低波 → risk-on=进攻 (>0 表示当前波动低于中位)
    hr=ipx["000300.SH"].pct_change(); rv=hr.rolling(n).std()
    return (rv.rolling(250).median()-rv)  # >0 = 当前比中位平静 = 进攻

def bt_static(factor,top_k=40,rb=10):
    fa=factor.where(mask); n=len(ret.index); cols=ret.columns; W=np.zeros((n,len(cols)),dtype="float32")
    for i in range(250,n,rb):
        v=fa.iloc[i].dropna()
        if len(v)<top_k: continue
        ci=cols.get_indexer(v.nlargest(top_k).index); W[i:min(i+rb,n),:]=0; W[i:min(i+rb,n),ci]=1/top_k
    pos=pd.DataFrame(W,index=ret.index,columns=cols).shift(1).fillna(0)
    return (pos*ret.fillna(0)).sum(axis=1)-pos.diff().abs().sum(axis=1).fillna(0)*COST

def bt_adaptive(sig,top_k=40,rb=10):
    n=len(ret.index); cols=ret.columns; W=np.zeros((n,len(cols)),dtype="float32"); off_share=[]
    faO=OFF.where(mask); faD=DEF.where(mask)
    for i in range(250,n,rb):
        s=sig.iloc[i]; offensive = pd.notna(s) and s>0; off_share.append(1 if offensive else 0)
        use=faO if offensive else faD
        v=use.iloc[i].dropna()
        if len(v)<top_k: continue
        ci=cols.get_indexer(v.nlargest(top_k).index); W[i:min(i+rb,n),:]=0; W[i:min(i+rb,n),ci]=1/top_k
    pos=pd.DataFrame(W,index=ret.index,columns=cols).shift(1).fillna(0)
    r=(pos*ret.fillna(0)).sum(axis=1)-pos.diff().abs().sum(axis=1).fillna(0)*COST
    return r, np.mean(off_share)

def bench():
    w=mask.astype("float32"); w=w.div(w.sum(axis=1).replace(0,np.nan),axis=0).fillna(0)
    return (w.shift(1).fillna(0)*ret.fillna(0)).sum(axis=1)

def met(r,s,e):
    x=r[(r.index>=s)&(r.index<=e)].dropna()
    if len(x)<20 or x.std()==0: return (0,0,0)
    eq=(1+x).cumprod(); a=eq.iloc[-1]**(ANN/len(x))-1; mdd=(eq/eq.cummax()-1).min(); shp=x.mean()/x.std()*np.sqrt(ANN)
    return a,(a/abs(mdd) if mdd<0 else 0),shp

def line(name,r):
    print(f"{name:26s} " + " ".join(f"{met(r,s,e)[0]:+.0%}/{met(r,s,e)[1]:+.2f}".rjust(13) for _,s,e in SEGS))

print(f"{'策略 (ann/Calmar)':26s} " + " ".join(f"{lab:>13s}" for lab,_,_ in SEGS))
print("-"*95)
line("基准 同池等权",bench())
line("静态 防守(idiovol_60)",bt_static(DEF))
line("静态 进攻(high_prox_250)",bt_static(OFF))
print("--- 自适应: size RS 开关 ---")
for n in (60,120):
    r,osh=bt_adaptive(rs_signal(n)); line(f"自适应 RS{n}",r); print(f"    └ 进攻占比={osh:.0%}")
print("--- 自适应: 市场趋势 risk-on/off 开关 (沪深300>MA) ---")
for n in (60,120):
    r,osh=bt_adaptive(trend_signal(n)); line(f"自适应 trend{n}",r); print(f"    └ 进攻占比={osh:.0%}")
print("--- 自适应: 市场波动率开关 (低波=进攻) ---")
for n in (40,60):
    r,osh=bt_adaptive(vol_signal(n)); line(f"自适应 vol{n}",r); print(f"    └ 进攻占比={osh:.0%}")
print("--- 自适应: 因子动量 (跟最近60日在赢的那条腿) ---")
rOFF=bt_static(OFF); rDEF=bt_static(DEF)
fm=(1+rOFF).rolling(60).apply(np.prod,raw=True)-(1+rDEF).rolling(60).apply(np.prod,raw=True)
r,osh=bt_adaptive(fm); line("自适应 factor-mom60",r); print(f"    └ 进攻占比={osh:.0%}")
