import sys,json,urllib.request
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
DATA=Path("/home/rooot/agent_invest_lab/research/index_timing/data")
IS_S,IS_E="2018-06-01","2023-12-31"; OOS_S,OOS_E="2024-01-01","2026-06-06"
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=60)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"x","version":"1"}})
def tool(n,a):
    res,_=call("tools/call",{"name":n,"arguments":dict(a,simulated_datetime="2026-06-05 15:00:00")},sid)
    return json.loads(res["result"]["content"][0]["text"])

px=pd.read_csv(DATA/"510050.SH.csv",parse_dates=[0],index_col=0).sort_index();px.index.name="date"
# 温度
tj=tool("market_temperature",{"start_date":"2013-01-01","end_date":"2026-06-05"})
tseq=(tj.get("items",tj))[0]["历史序列"]
T=pd.DataFrame(tseq); T["d"]=pd.to_datetime(T["交易日期"]); T=T.set_index("d").sort_index()
# GV-spread
gj=tool("option_gvspread_signal",{"start_date":"2013-01-01","end_date":"2026-06-05"})
grec=(gj.get("items",gj))[0]["记录"]
G=pd.DataFrame(grec); G["d"]=pd.to_datetime(G["日期"]); G=G.set_index("d").sort_index()
print("温度 rows=%d %s..%s | GVspread rows=%d %s..%s"%(len(T),T.index[0].date(),T.index[-1].date(),len(G),G.index[0].date(),G.index[-1].date()))

m=px.copy()
m["temp"]=pd.to_numeric(T["综合得分"],errors="coerce").reindex(m.index,method="ffill")
m["temp_q"]=pd.to_numeric(T["三月分位数"],errors="coerce").reindex(m.index,method="ffill")
m["gv_z"]=pd.to_numeric(G["价差Zscore"],errors="coerce").reindex(m.index,method="ffill")
m["gv_pct"]=pd.to_numeric(G["价差百分位"],errors="coerce").reindex(m.index,method="ffill")
m["gv_sig"]=pd.to_numeric(G["百分位信号"],errors="coerce").reindex(m.index,method="ffill")

# 温度反向：低温加仓 高温减仓
def f_temp_contra(df,lo=30,hi=70,floor=0.3):
    s=pd.Series(np.nan,index=df.index)
    s=s.where(~(df["temp"]<lo),1.0); s=s.where(~(df["temp"]>hi),floor)
    return s.ffill().fillna(1.0)
def f_temp_lin(df):  # 越冷越满 (100-temp)/100
    return ((100-df["temp"])/100).clip(0,1).fillna(0.5)
# GV信号：两个方向
def f_gv_sig(df,sign=1,floor=0.0):
    s=(df["gv_sig"]*sign>0).astype(float); return s.clip(floor,1).where(df["gv_sig"].notna(),floor).fillna(floor)
def f_gv_z(df,thr=1.0,sign=-1,floor=0.0):  # z高=拥挤/自满→减(sign-1) 默认
    cond=(df["gv_z"]*sign> thr*sign) if sign>0 else (df["gv_z"]<thr)
    # 简化：z<thr 满仓 else floor (sign=-1 语义)
    s=(df["gv_z"]<thr).astype(float) if sign<0 else (df["gv_z"]>thr).astype(float)
    return s.clip(floor,1).where(df["gv_z"].notna(),1.0).fillna(1.0)

GRID=[
 ("temp_contra_30_70",f_temp_contra,dict(lo=30,hi=70,floor=0.3)),
 ("temp_contra_25_75",f_temp_contra,dict(lo=25,hi=75,floor=0.3)),
 ("temp_contra_20_60",f_temp_contra,dict(lo=20,hi=60,floor=0.0)),
 ("temp_lin",f_temp_lin,{}),
 ("gv_sig_pos",f_gv_sig,dict(sign=1)),
 ("gv_sig_neg",f_gv_sig,dict(sign=-1)),
 ("gv_sig_neg_f30",f_gv_sig,dict(sign=-1,floor=0.3)),
 ("gv_z_lt1",f_gv_z,dict(thr=1.0,sign=-1)),
 ("gv_z_lt1.5",f_gv_z,dict(thr=1.5,sign=-1)),
 ("gv_z_gt0",f_gv_z,dict(thr=0.0,sign=1)),
]
def ev(grid,s,e):
    w=L.slice_range(m,s,e,300); bh=L.backtest(w,L.s_buyhold(w),s); out=[]
    for nm,fn,kw in grid:
        mm=L.backtest(w,fn(w,**kw),s); out.append((nm,mm,mm["calmar"]-bh["calmar"]))
    return bh,out
for tag,s,e in [("IS",IS_S,IS_E),("OOS",OOS_S,OOS_E)]:
    bh,out=ev(GRID,s,e)
    print("\n== %s %s..%s  buyhold cal=%.2f dd=%.2f ann=%.3f =="%(tag,s,e,bh["calmar"],bh["max_drawdown"],bh["annual_return"]))
    for nm,mm,dc in sorted(out,key=lambda r:r[2],reverse=True):
        print("  %-18s cal=%.2f dCal=%+.2f dd=%.2f ann=%.3f exp=%.2f flips=%d"%(nm,mm["calmar"],dc,mm["max_drawdown"],mm["annual_return"],mm["avg_exposure"],mm["trade_flips"]))
