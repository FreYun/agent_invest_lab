import sys
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
DATA=Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing/data")
# 复用已缓存数据 + 重新拉温度/gv (缓存)
import json,urllib.request
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=60)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
def cache_gv():
    p=DATA/"000016_gv.csv"
    if p.exists(): return pd.read_csv(p,parse_dates=["d"]).set_index("d")
    _,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"x","version":"1"}})
    res,_=call("tools/call",{"name":"option_gvspread_signal","arguments":{"start_date":"2013-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
    g=pd.DataFrame(json.loads(res["result"]["content"][0]["text"])["items"][0]["记录"]);g["d"]=pd.to_datetime(g["日期"]);g=g.set_index("d").sort_index();g.to_csv(p);return g
G=cache_gv()

px=pd.read_csv(DATA/"510050.SH.csv",parse_dates=[0],index_col=0).sort_index();px.index.name="date"
erp=pd.read_csv(DATA/"000016_erp.csv",parse_dates=["交易日期"]).set_index("交易日期").sort_index()
vix=pd.read_csv(DATA/"50etf_vix.csv",parse_dates=["交易日期"]).set_index("交易日期").sort_index()
m=px.copy()
m["p3"]=erp["近3年百分位"].reindex(m.index,method="ffill")
m["vix"]=vix["ivix"].reindex(m.index,method="ffill")
m["gv_z"]=pd.to_numeric(G["价差Zscore"],errors="coerce").reindex(m.index,method="ffill")

def erp_vix(df,LO=0.35,HI=0.95,addk=0.4,trimk=0.35,H=20):
    vz=(df["vix"]-df["vix"].rolling(252,min_periods=126).mean())/df["vix"].rolling(252,min_periods=126).std()
    pct=(df["p3"]/100).clip(0,1);base=(LO+(HI-LO)*pct).clip(0,1)
    panic=(vz>1.5).astype(float).rolling(H,min_periods=1).max().fillna(0)
    complac=(vz<-1.5).astype(float)
    return (base+addk*panic-trimk*complac).clip(0,1)
def erp_vix_gv(df,gvtrim=0.25,gvthr=1.5):
    base=erp_vix(df)
    gv_comp=(df["gv_z"]>gvthr).astype(float)  # 期权市场极度自满/拥挤
    return (base - gvtrim*gv_comp).clip(0,1)

def bt(w,sig,s):
    sig=sig.clip(0,1).fillna(0).reindex(w.index).fillna(0);ret=w["close"].pct_change().fillna(0)
    pos=sig.shift(1).fillna(0);cost=pos.diff().abs().fillna(pos.iloc[0])*0.0005
    sr=(pos*ret-cost);mm=w.index>=pd.Timestamp(s);return (1+sr.loc[mm]).cumprod(),pos.loc[mm].mean()
def stats(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return ann,dd,(ann/abs(dd) if dd<0 else 0),(r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0)

# 正交性检查
sub=m.loc["2018-06-01":].copy()
ev=erp_vix(sub); evg=erp_vix_gv(sub)
print("erp_vix vs erp_vix_gv 仓位相关性:",round(ev.corr(evg),3))
gv_comp=(sub["gv_z"]>1.5).astype(float); erp_cheap=(sub["p3"]<30).astype(float)
print("gv自满 vs erp便宜 信号相关:",round(gv_comp.corr(erp_cheap),3),"  gv自满触发天数=%d/%d"%(int(gv_comp.sum()),len(gv_comp)))

for name,fn in [("erp_vix",erp_vix),("erp_vix_gv",erp_vix_gv)]:
    print("\n==== %s ===="%name)
    for tag,s,e in [("OOS 24-26","2024-01-01","2026-06-06"),("全周期 18-26","2018-06-01","2026-06-06")]:
        w=L.slice_range(m,s,e,300);eq,exp=bt(w,fn(w),s);eqb,_=bt(w,L.s_buyhold(w),s)
        a,d,c,sh=stats(eq);ab,db,cb,_=stats(eqb)
        print("  [%s] ann=%.3f DD=%.2f Cal=%.2f Shp=%.2f exp=%.2f | BH Cal=%.2f DD=%.2f"%(tag,a,d,c,sh,exp,cb,db))
    w=L.slice_range(m,"2018-06-01","2026-06-06",300);eq,_=bt(w,fn(w),"2018-06-01")
    ci=bootstrap_sharpe_ci(eq,n_bootstrap=1000,confidence=0.95)
    wf=walk_forward_analysis(eq,[],n_windows=5)
    sh=[round(x.get("sharpe"),2) for x in (wf.get("windows") or wf.get("per_window") or [])]
    print("  bootstrap CI=[%.2f,%.2f] prob_pos=%.0f%%  walkfwd=%s"%(ci.get("ci_lower",0),ci.get("ci_upper",0),ci.get("prob_positive",0)*100,sh))
