import sys
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
from backtest.loaders.akshare_loader import DataLoader
DATA=Path("/home/rooot/agent_invest_lab/research/index_timing/data");SR=Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
ld=DataLoader()
if not (DATA/"588000.SH.csv").exists(): ld.fetch(["588000.SH"],"2020-01-01","2026-06-06")["588000.SH"].to_csv(DATA/"588000.SH.csv")
def load(p):df=pd.read_csv(p,parse_dates=[0],index_col=0).sort_index();df.index.name="date";return df
def bt(w,sig,s):
    sig=sig.clip(0,1).fillna(0).reindex(w.index).fillna(0);ret=w["close"].pct_change().fillna(0)
    pos=sig.shift(1).fillna(0);cost=pos.diff().abs().fillna(pos.iloc[0])*0.0005
    sr=(pos*ret-cost);m=w.index>=pd.Timestamp(s);return (1+sr.loc[m]).cumprod(),pos.loc[m].mean()
def stats(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return ann,dd,(ann/abs(dd) if dd<0 else 0),(r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0)
def full(df,fn,kw,s,e,label):
    w=L.slice_range(df,s,e,260);eq,exp=bt(w,fn(w,**kw),s);eqb,_=bt(w,L.s_buyhold(w),s)
    a,d,c,sh=stats(eq);ab,db,cb,_=stats(eqb)
    ci=bootstrap_sharpe_ci(eq,n_bootstrap=1000,confidence=0.95)
    wf=walk_forward_analysis(eq,[],n_windows=5);shs=[round(x.get("sharpe"),2) for x in (wf.get("windows") or wf.get("per_window") or [])]
    print("  %-22s Cal=%.2f(BH%.2f) DD=%.2f(BH%.2f) Shp=%.2f exp=%.2f | bootCI=[%.2f,%.2f]p+=%.0f%% wf=%s"%(
        label,c,cb,d,db,sh,exp,ci.get("ci_lower",0),ci.get("ci_upper",0),ci.get("prob_positive",0)*100,shs))

semi=load(SR/"512480.SH.csv");cyb=load(DATA/"159915.SZ.csv")
kc50=load(DATA/"588000.SH.csv");sc50=load(SR/"588800.SH.csv")

print("== 半导体 donchian robustness (win) 全周期2019-2026 ==")
w=L.slice_range(semi,"2024-01-01","2026-06-06",260);bh=L.backtest(w,L.s_buyhold(w),"2024-01-01")
for win in [40,60,80,100,120,160]:
    m=L.backtest(w,L.s_donchian(w,win=win),"2024-01-01")
    print("  donchian_%d OOS dCal=%+.2f exp=%.2f flips=%d"%(win,m["calmar"]-bh["calmar"],m["avg_exposure"],m["trade_flips"]))
print("  --validation 半导体 donchian_60 (2019-2026)--")
full(semi,L.s_donchian,dict(win=60),"2019-06-01","2026-06-06","semi donchian_60")
print("  --跨标的: 科创50 / 双创50 donchian_60--")
full(kc50,L.s_donchian,dict(win=60),"2021-01-01","2026-06-06","科创50 donchian_60")
full(sc50,L.s_donchian,dict(win=60),"2023-06-01","2026-06-06","双创50 donchian_60")

print("\n== 创业板 trail_dd robustness 全周期2014-2026 ==")
w=L.slice_range(cyb,"2024-01-01","2026-06-06",260);bh=L.backtest(w,L.s_buyhold(w),"2024-01-01")
for dd in [-0.08,-0.10,-0.12,-0.15,-0.20]:
    m=L.backtest(w,L.s_trail_dd(w,peak_win=120,dd=dd,re_ma=60),"2024-01-01")
    print("  trail_dd%d OOS dCal=%+.2f exp=%.2f flips=%d"%(int(dd*100),m["calmar"]-bh["calmar"],m["avg_exposure"],m["trade_flips"]))
print("  --validation 创业板 trail_dd15 (2014-2026)--")
full(cyb,L.s_trail_dd,dict(peak_win=120,dd=-0.15,re_ma=60),"2014-06-01","2026-06-06","创业板 trail_dd15")
print("  --跨标的: 科创50 trail_dd15 / 半导体 trail_dd15--")
full(kc50,L.s_trail_dd,dict(peak_win=120,dd=-0.15,re_ma=60),"2021-01-01","2026-06-06","科创50 trail_dd15")
