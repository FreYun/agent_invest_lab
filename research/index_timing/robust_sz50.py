import sys,json,urllib.request
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
DATA=Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing/data")
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
_sid=[None]
def erp_csv(idx_code,fn):
    p=DATA/fn
    if p.exists(): return
    if _sid[0] is None: _,_sid[0]=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"f","version":"1"}})
    res,_=call("tools/call",{"name":"market_index_gzxjb","arguments":{"symbols":[idx_code],"start_date":"2013-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},_sid[0])
    seq=json.loads(res["result"]["content"][0]["text"])["items"][0].get("历史序列",[])
    pd.DataFrame(seq).to_csv(p,index=False); print("pulled",fn,len(seq))
erp_csv("000300.SH","000300_erp.csv")

def build(px_csv,erp_csv_):
    px=pd.read_csv(DATA/px_csv,parse_dates=[0],index_col=0).sort_index(); px.index.name="date"
    e=pd.read_csv(DATA/erp_csv_,parse_dates=["交易日期"]).set_index("交易日期").sort_index()
    e=e.rename(columns={"近1年百分位":"p1","近3年百分位":"p3","近5年百分位":"p5"})
    m=px.copy()
    for c in ["p1","p3","p5"]: m[c]=e[c].reindex(m.index,method="ffill")
    return m
def f_erp(df,col,thr,floor):
    s=(df[col]>thr).astype(float); return s.where(df[col]>=0,floor).clip(floor,1).fillna(floor)
def metrics(df,col,thr,floor,s,en):
    w=L.slice_range(df,s,en,260); mm=L.backtest(w,f_erp(w,col,thr,floor),s)
    bh=L.backtest(w,L.s_buyhold(w),s)
    return mm.get("calmar",0),mm.get("calmar",0)-bh.get("calmar",0),mm.get("annual_return",0),mm.get("avg_exposure",0),mm.get("trade_flips",0),bh.get("calmar",0)

idxs={"上证50":build("510050.SH.csv","000016_erp.csv"),"沪深300":build("510300.SH.csv","000300_erp.csv")}
cols=["p1","p3","p5"]; thrs=[20,30,40,50,60]; floors=[0.0,0.3,0.5]
rows=[]
for name,df in idxs.items():
    for col in cols:
        for thr in thrs:
            for fl in floors:
                cal_is,dc_is,_,exp_is,fl_is,bh_is=metrics(df,col,thr,fl,IS_S,IS_E)
                cal_o,dc_o,ann_o,exp_o,flip_o,bh_o=metrics(df,col,thr,fl,OOS_S,OOS_E)
                rows.append(dict(idx=name,col=col,thr=thr,floor=fl,cal_is=cal_is,dc_is=dc_is,
                    cal_oos=cal_o,dc_oos=dc_o,ann_oos=ann_o,exp_oos=exp_o,flips_oos=flip_o))
R=pd.DataFrame(rows)
# 过滤退化(exposure<0.05当噪声) + 高翻转
valid=R[(R.exp_oos>=0.05)&(R.flips_oos<=100)].copy()
print("总配置=%d  有效(exp>=0.05且flips<=100/期)=%d"%(len(R),len(valid)))
for name in idxs:
    sub=valid[valid.idx==name]
    beat_is=(sub.dc_is>0).mean()*100; beat_oos=(sub.dc_oos>0).mean()*100
    print("\n[%s] IS dCalmar击败buyhold占比=%.0f%%  OOS=%.0f%%  | OOS median dCalmar=%.2f  median ann=%.3f median exp=%.2f"%(
        name,beat_is,beat_oos,sub.dc_oos.median(),sub.ann_oos.median(),sub.exp_oos.median()))
# 按 col×thr 看 OOS dCalmar 均值(floor聚合)看最优区是否在中央
print("\n上证50 OOS dCalmar 热力(行=col,列=thr,floor聚合均值):")
piv=valid[valid.idx=="上证50"].pivot_table(index="col",columns="thr",values="dc_oos",aggfunc="mean")
print(piv.round(2).to_string())
print("\n两标的都OOS击败buyhold的配置数:")
both=valid.pivot_table(index=["col","thr","floor"],columns="idx",values="dc_oos")
both_beat=both[(both.get("上证50",-9)>0)&(both.get("沪深300",-9)>0)]
print("  %d / %d 个参数点两标的同时>0"%(len(both_beat),both.shape[0]))
print(both_beat.round(2).head(15).to_string())
