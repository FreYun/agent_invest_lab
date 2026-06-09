import sys,json,urllib.request
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
DATA=Path("/home/rooot/agent_invest_lab/research/index_timing/data");SR=Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=60)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"s","version":"1"}})
def crowd(bk):
    p=DATA/("crowd_%s.csv"%bk)
    if not p.exists():
        res,_=call("tools/call",{"name":"sector_factor_detail","arguments":{"sec_codes":[bk],"start_date":"2016-01-01","end_date":"2026-06-05","win":200,"simulated_datetime":"2026-06-05 15:00:00"}},sid)
        rec=json.loads(res["result"]["content"][0]["text"])["items"][0]["记录"]
        df=pd.DataFrame(rec);df["d"]=pd.to_datetime(df["日期"]);df.set_index("d").sort_index().to_csv(p)
    return pd.read_csv(p,parse_dates=["d"]).set_index("d").sort_index()
# theme -> (BK, ETF, dir)
M={"军工":("BK000156","512660.SH",DATA),"人工智能":("BK000217","515980.SH",DATA),
   "机器人":("BK000234","562500.SH",DATA),"新能源":("BK000226","516160.SH",DATA),
   "光伏":("BK000146","515790.SH",DATA),"半导体(对照)":("BK000054","512480.SH",SR)}
def load(d,c):df=pd.read_csv(d/(c+".csv"),parse_dates=[0],index_col=0).sort_index();df.index.name="date";return df
def bt(w,sig,s):
    sig=sig.clip(0,1).fillna(1).reindex(w.index).fillna(1);ret=w["close"].pct_change().fillna(0)
    pos=sig.shift(1).fillna(1);cost=pos.diff().abs().fillna(0)*0.0005
    sr=(pos*ret-cost);m=w.index>=pd.Timestamp(s);return (1+sr.loc[m]).cumprod(),pos.loc[m].mean()
def calc(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return (ann/abs(dd) if dd<0 else 0)
def f_crowd(df,cthr,dthr,floor):
    trim=(df["集中度分位"]>cthr)&(df["乖离分位"]>dthr)
    s=pd.Series(1.0,index=df.index);return s.where(~trim,floor)
VAR=[("c85d85_f30",0.85,0.85,0.3),("c80d80_f30",0.80,0.80,0.3),("c90d70_f0",0.90,0.70,0.0),("c80d80_f0",0.80,0.80,0.0)]
IS_S,IS_E="2019-06-01","2023-12-31";OOS_S,OOS_E="2024-01-01","2026-06-06"
print("板块拥挤度减仓 dCalmar vs buyhold (IS / OOS):")
agg={v[0]:[0,0,0] for v in VAR}
for name,(bk,etf,d) in M.items():
    df=load(d,etf);cr=crowd(bk)
    for col in ["集中度分位","乖离分位"]: df[col]=pd.to_numeric(cr[col],errors="coerce").reindex(df.index,method="ffill")
    wi=L.slice_range(df,IS_S,IS_E,260);wo=L.slice_range(df,OOS_S,OOS_E,260)
    bi=calc(bt(wi,pd.Series(1.0,index=wi.index),IS_S)[0]);bo=calc(bt(wo,pd.Series(1.0,index=wo.index),OOS_S)[0])
    cells=[]
    for vn,c,dd,fl in VAR:
        di=calc(bt(wi,f_crowd(wi,c,dd,fl),IS_S)[0])-bi
        do=calc(bt(wo,f_crowd(wo,c,dd,fl),OOS_S)[0])-bo
        cells.append("%s %+.2f/%+.2f"%(vn,di,do))
        agg[vn][0]+=di>0;agg[vn][1]+=do>0;agg[vn][2]+=1
    print("  %-12s | %s"%(name," | ".join(cells)))
print("\n各变体击败buyhold占比(IS / OOS, 含半导体对照共%d标的):"%len(M))
for vn,(ib,ob,n) in agg.items(): print("  %-12s IS %d/%d  OOS %d/%d"%(vn,ib,n,ob,n))
