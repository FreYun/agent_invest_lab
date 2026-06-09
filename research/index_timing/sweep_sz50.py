"""上证50 (510050) 第一轮 sweep: 价格类44因子 + ERP/ERP×VIX 族。
共同窗口 IS 2018-06..2023-12 / OOS 2024-01..2026-06 (ERP从2018-03起)。
5bp + T+1(shift1) + 砍warmup。Calmar 排名。"""
import sys,json,urllib.request
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np, pandas as pd
from pathlib import Path
import local_sweep as L  # 复用 backtest/slice_range/GRID

DATA=Path("/home/rooot/agent_invest_lab/research/index_timing/data")
IS_S,IS_E="2018-06-01","2023-12-31"; OOS_S,OOS_E="2024-01-01","2026-06-06"

# ---- 价格 ----
px=pd.read_csv(DATA/"510050.SH.csv",parse_dates=[0],index_col=0).sort_index(); px.index.name="date"
# ---- ERP ----
erp=pd.read_csv(DATA/"000016_erp.csv",parse_dates=["交易日期"]).set_index("交易日期").sort_index()
erp=erp.rename(columns={"股债性价比":"erp","近1年百分位":"p1","近3年百分位":"p3","近5年百分位":"p5",
                        "近1年评级":"r1","近3年评级":"r3","近5年评级":"r5"})
# ---- VIX (缓存或拉) ----
vp=DATA/"50etf_vix.csv"
if not vp.exists():
    URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
    def call(m,p,sid=None):
        h=dict(H); 
        if sid:h["Mcp-Session-Id"]=sid
        r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=60)
        sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
        for ln in b.splitlines():
            if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
        return json.loads(b),sid
    _,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"f","version":"1"}})
    res,_=call("tools/call",{"name":"macro_50etf_vix","arguments":{"start_date":"2013-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
    v=json.loads(res["result"]["content"][0]["text"]); v=v.get("items",v)
    pd.DataFrame(v).to_csv(vp,index=False)
vix=pd.read_csv(vp,parse_dates=["交易日期"]).set_index("交易日期").sort_index().rename(columns={"ivix":"vix"})

# 合并到510050交易日 (as-of ffill, PIT安全)
m=px.copy()
for col in ["erp","p1","p3","p5","r1","r3","r5"]: m[col]=erp[col].reindex(m.index,method="ffill")
m["vix"]=vix["vix"].reindex(m.index,method="ffill")
m["vix_p"]=m["vix"].rolling(252,min_periods=60).apply(lambda s:(s.iloc[-1]>=s).mean()*100,raw=False)

# ---- ERP 因子族 ----
def f_erp_pct(df,col="p3",thr=50,lo=0.0):  # 分位>thr满仓,否则lo
    s=(df[col]>thr).astype(float); return s.where(df[col]>=0,lo).fillna(lo).clip(lo,1)
def f_erp_lin(df,col="p3"):  # 线性映射 分位/100
    return (df[col]/100.0).clip(0,1).fillna(0.5)
def f_erp_rating(df,col="r3"):  # 评级 +1->1, 0->0.5, -1->0
    return ((df[col]+1)/2.0).clip(0,1).fillna(0.5)
def f_erp_vix(df,col="p3",thr=40,vixthr=80):  # ERP看多 且 vix不极端高 才满仓
    bull=df[col]>thr; calm=df["vix_p"]<vixthr
    return (bull&calm).astype(float).fillna(0.0)
def f_erp_trend(df,col="p3",thr=40,slow=200):  # ERP看多 且 价格>MA
    ma=df["close"].rolling(slow).mean(); bull=df[col]>thr; up=df["close"]>ma
    return (bull&up).astype(float).where(ma.notna(),(bull).astype(float)).fillna(0.0)

ERP_GRID=[
 ("erp_p3_50",f_erp_pct,dict(col="p3",thr=50)),
 ("erp_p3_40",f_erp_pct,dict(col="p3",thr=40)),
 ("erp_p3_30",f_erp_pct,dict(col="p3",thr=30)),
 ("erp_p5_50",f_erp_pct,dict(col="p5",thr=50)),
 ("erp_p5_40",f_erp_pct,dict(col="p5",thr=40)),
 ("erp_p1_50",f_erp_pct,dict(col="p1",thr=50)),
 ("erp_p3_50_half",f_erp_pct,dict(col="p3",thr=50,lo=0.5)),
 ("erp_lin_p3",f_erp_lin,dict(col="p3")),
 ("erp_lin_p5",f_erp_lin,dict(col="p5")),
 ("erp_rating_r3",f_erp_rating,dict(col="r3")),
 ("erp_rating_r5",f_erp_rating,dict(col="r5")),
 ("erp_vix_p3_40",f_erp_vix,dict(col="p3",thr=40,vixthr=80)),
 ("erp_vix_p3_30",f_erp_vix,dict(col="p3",thr=30,vixthr=85)),
 ("erp_trend_p3_40",f_erp_trend,dict(col="p3",thr=40,slow=200)),
 ("erp_trend_p5_40",f_erp_trend,dict(col="p5",thr=40,slow=200)),
]

def run(win_s,win_e,grid,df,use_erp):
    win=L.slice_range(df,win_s,win_e,warmup=260)
    rows=[]
    for name,fn,kw in grid:
        sig=fn(win,**kw)
        mm=L.backtest(win,sig,win_s); rows.append((name,fn,kw,mm))
    return rows

print("══ 510050 上证50  IS %s..%s ══"%(IS_S,IS_E))
allrows=run(IS_S,IS_E,L.GRID,m,False)+run(IS_S,IS_E,ERP_GRID,m,True)
ranked=sorted(allrows,key=lambda r:r[3].get("calmar",-9e9),reverse=True)
bh=[r for r in allrows if r[0]=="buyhold"][0][3]
print(L.fmt("buyhold",bh))
print("\nIS Top-12 by Calmar:")
for name,fn,kw,mm in ranked[:12]: print(L.fmt(name,mm))

print("\n══ OOS %s..%s (IS top12 验证) ══"%(OOS_S,OOS_E))
oos_bh=L.backtest(L.slice_range(m,OOS_S,OOS_E,260),L.s_buyhold(L.slice_range(m,OOS_S,OOS_E,260)),OOS_S)
print(L.fmt("buyhold(bench)",oos_bh))
for name,fn,kw,_ in ranked[:12]:
    w=L.slice_range(m,OOS_S,OOS_E,260); mm=L.backtest(w,fn(w,**kw),OOS_S)
    print(L.fmt(name,mm))
