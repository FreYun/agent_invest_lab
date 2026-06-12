import sys
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
DATA=Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing/data")
FULL_S,FULL_E="2018-06-01","2026-06-06"

def build(px,erp):
    p=pd.read_csv(DATA/px,parse_dates=[0],index_col=0).sort_index(); p.index.name="date"
    e=pd.read_csv(DATA/erp,parse_dates=["交易日期"]).set_index("交易日期").sort_index().rename(
        columns={"近1年百分位":"p1","近3年百分位":"p3","近5年百分位":"p5"})
    for c in ["p1","p3","p5"]: p[c]=e[c].reindex(p.index,method="ffill")
    return p
def f_erp(df,col,thr,floor):
    s=(df[col]>thr).astype(float); return s.where(df[col]>=0,floor).clip(floor,1).fillna(floor)

# ---- 1) 阈值网格往下延，排除角落拟合 (上证50 OOS) ----
sz=build("510050.SH.csv","000016_erp.csv")
print("== 上证50 OOS dCalmar，阈值延到10/15 (col=p1, floor=0.3) ==")
for thr in [10,15,20,25,30,35]:
    w=L.slice_range(sz,"2024-01-01","2026-06-06",260)
    mm=L.backtest(w,f_erp(w,"p1",thr,0.3),"2024-01-01"); bh=L.backtest(w,L.s_buyhold(w),"2024-01-01")
    print("  thr=%2d  cal=%.2f dCal=%+.2f ann=%.3f exp=%.2f flips=%d"%(thr,mm["calmar"],mm["calmar"]-bh["calmar"],mm["annual_return"],mm["avg_exposure"],mm["trade_flips"]))

# ---- 2) 选定变体 p1/thr25/floor0.3，全周期 walk-forward + bootstrap ----
def eqcurve(df,col,thr,floor,s,e):
    w=L.slice_range(df,s,e,260)
    sig=f_erp(w,col,thr,floor).clip(0,1).fillna(floor).reindex(w.index).fillna(floor)
    ret=w["close"].pct_change().fillna(0); pos=sig.shift(1).fillna(0)
    cost=pos.diff().abs().fillna(pos.iloc[0])*0.0005
    sr=(pos*ret-cost)
    mask=w.index>=pd.Timestamp(s); sr=sr.loc[mask]
    return (1+sr).cumprod(), w["close"].pct_change().loc[mask]

for name,px,erp in [("上证50","510050.SH.csv","000016_erp.csv"),("沪深300","510300.SH.csv","000300_erp.csv")]:
    df=build(px,erp)
    eq,bh_ret=eqcurve(df,"p1",25,0.3,FULL_S,FULL_E)
    eq_bh=(1+bh_ret).cumprod()
    print("\n==== %s  p1/thr25/floor0.3  全周期 %s..%s ===="%(name,FULL_S,FULL_E))
    def st(e):
        r=e.pct_change().fillna(0);n=len(r);ann=(e.iloc[-1])**(252/n)-1
        dd=(e/e.cummax()-1).min();shp=r.mean()/r.std()*np.sqrt(252)
        return ann,dd,ann/abs(dd),shp
    a,d,c,s=st(eq); ab,db,cb,sb=st(eq_bh)
    print("  因子:  ann=%.3f maxDD=%.2f Calmar=%.2f Sharpe=%.2f"%(a,d,c,s))
    print("  buyhold:ann=%.3f maxDD=%.2f Calmar=%.2f Sharpe=%.2f"%(ab,db,cb,sb))
    try:
        ci=bootstrap_sharpe_ci(eq,n_bootstrap=1000,confidence=0.95)
        print("  bootstrap Sharpe: obs=%.2f CI=[%.2f, %.2f] prob_pos=%.0f%%"%(
            ci.get("observed_sharpe",s),ci.get("ci_lower",0),ci.get("ci_upper",0),ci.get("prob_positive",0)*100))
    except Exception as ex: print("  bootstrap err:",ex)
    try:
        wf=walk_forward_analysis(eq,[],n_windows=5)
        sh=[w.get("sharpe") for w in (wf.get("windows") or wf.get("per_window") or [])]
        print("  walk-forward 5窗 Sharpe:",[round(x,2) if x is not None else None for x in sh] if sh else wf)
    except Exception as ex: print("  walkfwd err:",ex)
