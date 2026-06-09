import sys
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
DATA=Path("/home/rooot/agent_invest_lab/research/index_timing/data");SR=Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
def load(p):df=pd.read_csv(p,parse_dates=[0],index_col=0).sort_index();df.index.name="date";return df
def bt(w,sig,s):
    sig=sig.clip(0,1).fillna(0).reindex(w.index).fillna(0);ret=w["close"].pct_change().fillna(0)
    pos=sig.shift(1).fillna(0);cost=pos.diff().abs().fillna(pos.iloc[0])*0.0005
    sr=(pos*ret-cost);m=w.index>=pd.Timestamp(s)
    eq=(1+sr.loc[m]).cumprod();flips=int((pos.loc[m].diff().abs()>0).sum())
    return eq,pos.loc[m].mean(),flips
def stat(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    shp=r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0
    return ann,dd,(ann/abs(dd) if dd<0 else 0),shp
CAND=[("中证1000(已在产)","512100.SH",DATA,"2016-11-04"),
      ("半导体(强候选)","512480.SH",SR,"2019-06-12"),
      ("稀土(黄灯)","159713.SZ",DATA,"2021-06-01"),
      ("双创50(样本短)","588800.SH",SR,"2023-06-01")]
OOS_S,OOS_E="2024-01-01","2026-06-06"
for name,code,d,fs in CAND:
    df=load(d/(code+".csv"))
    print("\n"+"="*70);print("【%s】 %s  donchian_60   数据 %s..%s"%(name,code,df.index[0].date(),df.index[-1].date()))
    # 全周期
    wf_=L.slice_range(df,fs,"2026-06-06",60)
    ef,exf,flf=bt(wf_,L.s_donchian(wf_,win=60),fs);eb,_,_=bt(wf_,L.s_buyhold(wf_),fs)
    a,dd,c,sh=stat(ef);ab,ddb,cb,shb=stat(eb)
    # OOS
    wo=L.slice_range(df,OOS_S,OOS_E,260);eo,exo,flo=bt(wo,L.s_donchian(wo,win=60),OOS_S);ebo,_,_=bt(wo,L.s_buyhold(wo),OOS_S)
    ao,ddo,co,sho=stat(eo);abo,ddbo,cbo,_=stat(ebo)
    print("  全周期    因子: 年化%+.1f%% 回撤%.0f%% Calmar%.2f Sharpe%.2f 暴露%.0f%% 翻转%d"%(a*100,dd*100,c,sh,exf*100,flf))
    print("            死拿: 年化%+.1f%% 回撤%.0f%% Calmar%.2f Sharpe%.2f"%(ab*100,ddb*100,cb,shb))
    print("  OOS24-26  因子: 年化%+.1f%% 回撤%.0f%% Calmar%.2f 暴露%.0f%% 翻转%d  | 死拿 年化%+.1f%% 回撤%.0f%% Calmar%.2f"%(ao*100,ddo*100,co,exo*100,flo,abo*100,ddbo*100,cbo))
    try:
        ci=bootstrap_sharpe_ci(ef,n_bootstrap=1000,confidence=0.95)
        w5=walk_forward_analysis(ef,[],n_windows=5);shs=[round(x.get("sharpe"),2) for x in (w5.get("windows") or w5.get("per_window") or [])]
        pos=sum(1 for x in shs if x and x>0)
        print("  统计      bootstrap Sharpe CI=[%.2f, %.2f] prob_pos=%.0f%%  | walk-forward 5窗=%s (%d/5正)"%(ci.get("ci_lower",0),ci.get("ci_upper",0),ci.get("prob_positive",0)*100,shs,pos))
    except Exception as e: print("  统计 err",e)
