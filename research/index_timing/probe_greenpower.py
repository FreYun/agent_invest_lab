import sys,json,urllib.request
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=90)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"gp","version":"1"}})
# 中证绿色电力 931897(漏测) + 光伏931151(对照,确认归类) + 中证内地新能源/绿色电力候选
cands=["931897.CSI","931897.SH","931151.CSI","000941.CSI","399808.SZ"]
res,_=call("tools/call",{"name":"market_index_quote","arguments":{"market":"cn","symbols":cands,"start_date":"2014-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
def donch60(c):
    hh=c.shift(1).rolling(60).max();ll=c.shift(1).rolling(60).min()
    st=pd.Series(np.nan,index=c.index);st=st.where(~(c>hh),1.0);st=st.where(~(c<ll),0.0);return st.ffill().fillna(0.0)
def stat(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return ann,dd,(ann/abs(dd) if dd<0 else 0),(r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0)
print("绿色电力 931897 补测 + 光伏 931151 对照（close版 donchian_60，全周期指数）:")
for it in json.loads(res["result"]["content"][0]["text"]).get("items",[]):
    sym=it.get("指数标识");ok=it.get("是否可用");rec=it.get("行情记录") or []
    if not ok or not rec: print("  %-12s 不可用/无数据"%sym);continue
    df=pd.DataFrame(rec);df["d"]=pd.to_datetime(df["日期"]);df=df.set_index("d").sort_index()
    c=pd.to_numeric(df["收盘"],errors="coerce").dropna()
    sig=donch60(c).shift(1).fillna(0);ret=c.pct_change().fillna(0);cost=sig.diff().abs().fillna(0)*0.0005
    eq=(1+(sig*ret-cost)).cumprod();eqb=(1+ret).cumprod()
    a,d,cc,sh=stat(eq);ab,db,cb,_=stat(eqb)
    ci=bootstrap_sharpe_ci(eq,n_bootstrap=800,confidence=0.95)
    wf=walk_forward_analysis(eq,[],n_windows=5);shs=[x.get("sharpe") for x in (wf.get("windows") or wf.get("per_window") or [])];pos=sum(1 for x in shs if x and x>0)
    flips=int((sig.diff().abs()>0).sum())
    print("  %-12s rows=%d %s..%s | 因子Cal%.2f(BH%.2f) DD%.0f%%(BH%.0f%%) ann%.0f%% exp%.0f%% flips%d | CI[%.2f,%.2f]p+%.0f%% wf%d/5"%(
        sym,len(c),c.index[0].date(),c.index[-1].date(),cc,cb,d*100,db*100,a*100,sig.mean()*100,flips,ci.get("ci_lower",0),ci.get("ci_upper",0),ci.get("prob_positive",0)*100,pos))
