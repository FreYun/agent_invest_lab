import sys,json,urllib.request
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=60)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"r","version":"1"}})
cands=["931743.CSI","931007.CSI","H30184.CSI","931643.CSI","931643.SH","930598.CSI","930598.SH","000688.SH","399006.SZ"]
res,_=call("tools/call",{"name":"market_index_quote","arguments":{"market":"cn","symbols":cands,"start_date":"2014-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
j=json.loads(res["result"]["content"][0]["text"])
def donch60(close):
    win=60;hh=close.shift(1).rolling(win).max();ll=close.shift(1).rolling(win).min()
    st=pd.Series(np.nan,index=close.index);st=st.where(~(close>hh),1.0);st=st.where(~(close<ll),0.0)
    return st.ffill().fillna(0.0)
def stat(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return ann,dd,(ann/abs(dd) if dd<0 else 0),(r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0)
print("候选指数可用性 + close版donchian_60 重验:")
for it in j.get("items",[]):
    sym=it.get("指数标识");ok=it.get("是否可用");rec=it.get("行情记录") or []
    if not ok or not rec: print("  %-12s 不可用/无数据"%sym);continue
    df=pd.DataFrame(rec);df["d"]=pd.to_datetime(df["日期"]);df=df.set_index("d").sort_index()
    close=pd.to_numeric(df["收盘"],errors="coerce").dropna()
    sig=donch60(close).shift(1).fillna(0);ret=close.pct_change().fillna(0)
    cost=sig.diff().abs().fillna(0)*0.0005;sr=sig*ret-cost
    eq=(1+sr).cumprod();eqb=(1+ret).cumprod()
    a,d,c,sh=stat(eq);ab,db,cb,_=stat(eqb)
    try:
        ci=bootstrap_sharpe_ci(eq,n_bootstrap=800,confidence=0.95)
        wf=walk_forward_analysis(eq,[],n_windows=5);shs=[round(x.get("sharpe"),2) for x in (wf.get("windows") or wf.get("per_window") or [])];pos=sum(1 for x in shs if x and x>0)
        cis="CI[%.2f,%.2f]p+%.0f%% wf%d/5"%(ci.get("ci_lower",0),ci.get("ci_upper",0),ci.get("prob_positive",0)*100,pos)
    except Exception as e: cis="stat_err"
    print("  %-12s rows=%d %s..%s | 因子Cal%.2f(BH%.2f) DD%.0f%%(BH%.0f%%) ann%.0f%% exp%.0f%% | %s"%(
        sym,len(close),close.index[0].date(),close.index[-1].date(),c,cb,d*100,db*100,a*100,sig.mean()*100,cis))
