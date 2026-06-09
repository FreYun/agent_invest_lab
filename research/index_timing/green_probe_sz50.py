import sys
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
DATA=Path("/home/rooot/agent_invest_lab/research/index_timing/data")

def stats(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1
    dd=(eq/eq.cummax()-1).min();shp=r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0
    return ann,dd,(ann/abs(dd) if dd<0 else 0),shp
def bt(w,sig,s):
    sig=sig.clip(0,1).fillna(0).reindex(w.index).fillna(0);ret=w["close"].pct_change().fillna(0)
    pos=sig.shift(1).fillna(0);cost=pos.diff().abs().fillna(pos.iloc[0])*0.0005
    sr=(pos*ret-cost);m=w.index>=pd.Timestamp(s);return (1+sr.loc[m]).cumprod(),pos.loc[m].mean()

# ---------- A) 官方 erp_vix 配方套上证50 ----------
px=pd.read_csv(DATA/"510050.SH.csv",parse_dates=[0],index_col=0).sort_index();px.index.name="date"
erp=pd.read_csv(DATA/"000016_erp.csv",parse_dates=["交易日期"]).set_index("交易日期").sort_index()
vix=pd.read_csv(DATA/"50etf_vix.csv",parse_dates=["交易日期"]).set_index("交易日期").sort_index()
m=px.copy()
m["p3"]=erp["近3年百分位"].reindex(m.index,method="ffill")
m["vix"]=vix["ivix"].reindex(m.index,method="ffill")
def erp_vix_sig(df,LO=0.35,HI=0.95,addk=0.4,trimk=0.35,H=20):
    vz=(df["vix"]-df["vix"].rolling(252,min_periods=126).mean())/df["vix"].rolling(252,min_periods=126).std()
    pct=(df["p3"]/100).clip(0,1);base=(LO+(HI-LO)*pct).clip(0,1)
    panic=(vz>1.5).astype(float).rolling(H,min_periods=1).max().fillna(0)
    complac=(vz<-1.5).astype(float)
    return (base+addk*panic-trimk*complac).clip(0,1)

print("== A) 官方 erp_vix 配方 vs 简版 p1/thr25  (上证50) ==")
for tag,s,e in [("OOS 24-26","2024-01-01","2026-06-06"),("全周期 18-26","2018-06-01","2026-06-06")]:
    w=L.slice_range(m,s,e,300)
    eqv,expv=bt(w,erp_vix_sig(w),s)
    eqb,_=bt(w,L.s_buyhold(w),s)
    a,d,c,sh=stats(eqv);ab,db,cb,sbh=stats(eqb)
    print("  [%s] erp_vix: ann=%.3f DD=%.2f Cal=%.2f Shp=%.2f exp=%.2f | BH: ann=%.3f DD=%.2f Cal=%.2f"%(tag,a,d,c,sh,expv,ab,db,cb))
ci=bootstrap_sharpe_ci(bt(L.slice_range(m,"2018-06-01","2026-06-06",300),erp_vix_sig(L.slice_range(m,"2018-06-01","2026-06-06",300)),"2018-06-01")[0],n_bootstrap=1000,confidence=0.95)
print("  全周期 bootstrap Sharpe CI=[%.2f,%.2f] prob_pos=%.0f%%"%(ci.get("ci_lower",0),ci.get("ci_upper",0),ci.get("prob_positive",0)*100))

# ---------- B) 绿灯级价格因子 上证50 全历史 2013-2026 ----------
print("\n== B) 价格因子(donchian_60/dd_ladder/trail_dd) 上证50 全历史 IS 2013-2021 / OOS 2022-2026 ==")
px2=px.copy()
fns=[("donchian_60",L.s_donchian,dict(win=60)),("donchian_120",L.s_donchian,dict(win=120)),
     ("dd_ladder_10_20",L.s_dd_ladder,dict(dd_half=-0.10,dd_flat=-0.20)),
     ("trail_dd15_p120",L.s_trail_dd,dict(peak_win=120,dd=-0.15,re_ma=60)),
     ("trendon_200_20",L.s_trendon,dict(slow=200,sw=20))]
for tag,s,e in [("IS 13-21","2013-06-01","2021-12-31"),("OOS 22-26","2022-01-01","2026-06-06")]:
    w=L.slice_range(px2,s,e,260);eqb,_=bt(w,L.s_buyhold(w),s);ab,db,cb,_=stats(eqb)
    print("  --%s-- buyhold Cal=%.2f DD=%.2f ann=%.3f"%(tag,cb,db,ab))
    for nm,fn,kw in fns:
        eqf,expf=bt(w,fn(w,**kw),s);a,d,c,sh=stats(eqf)
        print("     %-16s Cal=%.2f dCal=%+.2f DD=%.2f ann=%.3f Shp=%.2f exp=%.2f"%(nm,c,c-cb,d,a,sh,expf))
