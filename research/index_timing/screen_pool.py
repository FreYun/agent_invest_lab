import sys
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
from backtest.loaders.akshare_loader import DataLoader
DATA=Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing/data");SR=Path("/home/ubuntu/rooot/agent_invest_lab/research/sr_factor/data")
ld=DataLoader()
# name -> (etf_code, 类型)
POOL={
 "半导体":("512480.SH","成长",SR),"双创50":("588800.SH","成长",SR),"中证1000":("512100.SH","成长",DATA),
 "创业板":("159915.SZ","成长",DATA),"科创50":("588000.SH","成长",DATA),
 "新能源":("516160.SH","成长",DATA),"军工":("512660.SH","成长",DATA),"稀土":("159713.SZ","成长",DATA),
 "人工智能":("515980.SH","成长",DATA),"机器人":("562500.SH","成长",DATA),"光伏":("515790.SH","成长",DATA),
 "证券":("512880.SH","周期",DATA),"医疗":("512170.SH","成长",DATA),
 "银行":("512800.SH","价值",DATA),"白酒":("512690.SH","价值",DATA),"红利":("515080.SH","价值",DATA),
}
def load(name,code,d):
    p=d/(code+".csv")
    if not p.exists():
        try: r=ld.fetch([code],"2014-01-01","2026-06-06",interval="1D"); r[code].to_csv(p)
        except Exception as e: print("FETCH FAIL",name,code,e);return None
    try:
        df=pd.read_csv(p,parse_dates=[0],index_col=0).sort_index();df.index.name="date"
        return df if len(df)>300 else None
    except Exception: return None
def bt(w,sig,s):
    sig=sig.clip(0,1).fillna(0).reindex(w.index).fillna(0);ret=w["close"].pct_change().fillna(0)
    pos=sig.shift(1).fillna(0);cost=pos.diff().abs().fillna(pos.iloc[0])*0.0005
    sr=(pos*ret-cost);m=w.index>=pd.Timestamp(s);return (1+sr.loc[m]).cumprod(),pos.loc[m].mean()
def cal(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return (ann/abs(dd) if dd<0 else 0),dd
FACS=[("donchian_60",L.s_donchian,dict(win=60)),("trail_dd15",L.s_trail_dd,dict(peak_win=120,dd=-0.15,re_ma=60))]
print("%-8s %-6s %-5s | %-22s | %-22s"%("指数","类型","年数","donchian_60 (OOSdCal/全Cal vsBH/DD改善)","trail_dd15"))
for name,(code,typ,d) in POOL.items():
    df=load(name,code,d)
    if df is None: print("%-8s %-6s  --数据不可用--"%(name,typ));continue
    yrs=(df.index[-1]-df.index[0]).days/365
    w=L.slice_range(df,"2024-01-01","2026-06-06",260);bh_o,_=bt(w,L.s_buyhold(w),"2024-01-01");bhc_o,_=cal(bh_o)
    fs=df.index[0].strftime("%Y-%m-%d");wf=L.slice_range(df,fs,"2026-06-06",10);bh_f,_=bt(wf,L.s_buyhold(wf),fs);bhc_f,bhdd_f=cal(bh_f)
    cells=[]
    for nm,fn,kw in FACS:
        eo,_=bt(w,fn(w,**kw),"2024-01-01");co,_=cal(eo)
        ef,_=bt(wf,fn(wf,**kw),fs);cf,ddf=cal(ef)
        cells.append("OOSd%+.2f Cal%.2f(BH%.2f) DDΔ%+.0fpp"%(co-bhc_o,cf,bhc_f,(ddf-bhdd_f)*100))
    print("%-8s %-6s %4.1f | %s | %s"%(name,typ,yrs,cells[0],cells[1]))
