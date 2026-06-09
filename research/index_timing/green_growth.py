import sys
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
from backtest.loaders.akshare_loader import DataLoader
DATA=Path("/home/rooot/agent_invest_lab/research/index_timing/data")
SR=Path("/home/rooot/agent_invest_lab/research/sr_factor/data")

# 数据:创业板159915(拉) / 半导体512480(SOP缓存) / zz1000 512100(已缓存,绿灯参照)
ld=DataLoader()
need={"159915.SZ":DATA/"159915.SZ.csv"}
for code,p in need.items():
    if not p.exists():
        d=ld.fetch([code],"2012-01-01","2026-06-06",interval="1D"); d[code].to_csv(p)
def load(p):
    df=pd.read_csv(p,parse_dates=[0],index_col=0).sort_index();df.index.name="date";return df
sets={"创业板159915":load(DATA/"159915.SZ.csv"),
      "半导体512480":load(SR/"512480.SH.csv"),
      "zz1000_512100(参照)":load(DATA/"512100.SH.csv")}
for n,df in sets.items(): print("%s rows=%d %s..%s"%(n,len(df),df.index[0].date(),df.index[-1].date()))

# 价格因子里挑趋势/DD族
PICK=[x for x in L.GRID if x[0] in
 ("buyhold","donchian_60","donchian_120","dd_ladder_10_20","dd_ladder_7_15","trail_dd15_p120",
  "trail_dd10_p120","trendon_200_20","donch60_trail_k3","vt15_x_trend200","chand_22_k3")]
IS_S,IS_E="2019-06-01","2023-12-31"; OOS_S,OOS_E="2024-01-01","2026-06-06"
for n,df in sets.items():
    print("\n══════ %s  IS %s..%s ══════"%(n,IS_S,IS_E))
    wis=L.slice_range(df,IS_S,IS_E,260); bh_is=L.backtest(wis,L.s_buyhold(wis),IS_S)
    wos=L.slice_range(df,OOS_S,OOS_E,260); bh_os=L.backtest(wos,L.s_buyhold(wos),OOS_S)
    rows=[]
    for nm,fn,kw in PICK:
        mis=L.backtest(wis,fn(wis,**kw),IS_S); mos=L.backtest(wos,fn(wos,**kw),OOS_S)
        rows.append((nm,mis,mos))
    print("  buyhold      IS cal=%.2f dd=%.2f | OOS cal=%.2f dd=%.2f ann=%.3f"%(bh_is["calmar"],bh_is["max_drawdown"],bh_os["calmar"],bh_os["max_drawdown"],bh_os["annual_return"]))
    for nm,mis,mos in sorted(rows,key=lambda r:r[2]["calmar"]-bh_os["calmar"],reverse=True):
        if nm=="buyhold":continue
        print("  %-16s IS dCal=%+.2f | OOS cal=%.2f dCal=%+.2f dd=%.2f ann=%.3f exp=%.2f flips=%d"%(
            nm,mis["calmar"]-bh_is["calmar"],mos["calmar"],mos["calmar"]-bh_os["calmar"],mos["max_drawdown"],mos["annual_return"],mos["avg_exposure"],mos["trade_flips"]))
