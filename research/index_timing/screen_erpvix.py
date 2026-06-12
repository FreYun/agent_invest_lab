import sys,json,urllib.request
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
DATA=Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing/data")
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=90)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e","version":"1"}})
def tool(n,a):
    res,_=call("tools/call",{"name":n,"arguments":dict(a,simulated_datetime="2026-06-05 15:00:00")},sid)
    return json.loads(res["result"]["content"][0]["text"])
vix=pd.read_csv(DATA/"50etf_vix.csv",parse_dates=["交易日期"]).set_index("交易日期").sort_index()["ivix"]
# 价值/宽基 -> 指数代码
VAL={"中证500":"000905.SH","中证红利":"000922.SH","中证银行":"399986.SZ","中证白酒":"399997.SZ",
     "证券公司":"399975.SZ","中证消费":"000932.SH","中证医疗":"399989.SZ"}
def erp_vix(close,erp3,LO=0.35,HI=0.95,addk=0.4,trimk=0.35,H=20):
    vz=(vix-vix.rolling(252,min_periods=126).mean())/vix.rolling(252,min_periods=126).std()
    vz=vz.reindex(close.index,method="ffill")
    pct=(erp3.reindex(close.index,method="ffill")/100).clip(0,1)
    base=(LO+(HI-LO)*pct).clip(0,1)
    panic=(vz>1.5).astype(float).rolling(H,min_periods=1).max().fillna(0);complac=(vz<-1.5).astype(float)
    return (base+addk*panic-trimk*complac).clip(0,1)
def stat(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return ann,dd,(ann/abs(dd) if dd<0 else 0),(r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0)
print("ERP×VIX 在价值/宽基指数上 (黄灯下行保护):")
for nm,code in VAL.items():
    g=tool("market_index_gzxjb",{"symbols":[code],"start_date":"2014-01-01","end_date":"2026-06-05"})
    its=g.get("items",[])
    if not its or not its[0].get("是否可用") or not its[0].get("历史序列"):
        print("  %-8s %-11s gzxjb不可用"%(nm,code));continue
    seq=its[0]["历史序列"];e=pd.DataFrame(seq);e["d"]=pd.to_datetime(e["交易日期"]);e=e.set_index("d").sort_index()
    erp3=pd.to_numeric(e["近3年百分位"],errors="coerce")
    q=tool("market_index_quote",{"market":"cn","symbols":[code],"start_date":"2014-01-01","end_date":"2026-06-05"})
    qi=[x for x in q.get("items",[]) if x.get("是否可用")]
    if not qi: print("  %-8s %-11s quote不可用"%(nm,code));continue
    df=pd.DataFrame(qi[0]["行情记录"]);df["d"]=pd.to_datetime(df["日期"]);df=df.set_index("d").sort_index()
    c=pd.to_numeric(df["收盘"],errors="coerce").dropna()
    s=erp3.index[0].strftime("%Y-%m-%d");c=c[c.index>=erp3.index[0]]
    sig=erp_vix(c,erp3).shift(1).reindex(c.index).fillna(0.65);ret=c.pct_change().fillna(0);cost=sig.diff().abs().fillna(0)*0.0005
    eq=(1+(sig*ret-cost)).cumprod();eqb=(1+ret).cumprod()
    a,d,cc,sh=stat(eq);ab,db,cb,_=stat(eqb)
    ci=bootstrap_sharpe_ci(eq,n_bootstrap=600,confidence=0.95)
    wf=walk_forward_analysis(eq,[],n_windows=5);shs=[x.get("sharpe") for x in (wf.get("windows") or wf.get("per_window") or [])];pos=sum(1 for x in shs if x and x>0)
    v="🟡黄灯" if (cc>cb and d>db and ci.get('prob_positive',0)>=0.80) else "❌"
    print("  %-8s %-11s | 因子Cal%.2f(BH%.2f) DD%.0f%%(BH%.0f%%) ann%.0f%% exp%.0f%% | p+%.0f%% wf%d/5 | %s"%(
        nm,code,cc,cb,d*100,db*100,a*100,sig.mean()*100,ci.get("prob_positive",0)*100,pos,v))
