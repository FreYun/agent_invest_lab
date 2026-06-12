import sys,json,urllib.request
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
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
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"d2","version":"1"}})
NAMES={"云计算":["930851.CSI"],"动漫游戏":["930901.CSI"],"CS电池":["931719.CSI","931719.SH"],
 "电力":["930713.CSI","399994.SZ"],"细分化工":["000813.SH","000813.CSI"],"自由现金流":["932365.CSI","932367.CSI"],
 "通信":["931160.CSI","399970.SZ"],"中证A500":["000510.SH","930050.CSI"],"800消费":["000964.SH"],
 "电网设备":["931596.CSI"],"绿色电力":["931151.CSI"],"机器人":["931494.CSI"],"军工龙头":["931066.CSI"]}
alls=[s for v in NAMES.values() for s in v]
res,_=call("tools/call",{"name":"market_index_quote","arguments":{"market":"cn","symbols":alls,"start_date":"2014-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
items={it.get("指数标识"):it for it in json.loads(res["result"]["content"][0]["text"]).get("items",[])}
def donch60(c):
    hh=c.shift(1).rolling(60).max();ll=c.shift(1).rolling(60).min()
    st=pd.Series(np.nan,index=c.index);st=st.where(~(c>hh),1.0);st=st.where(~(c<ll),0.0);return st.ffill().fillna(0.0)
def stat(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return ann,dd,(ann/abs(dd) if dd<0 else 0)
for nm,syms in NAMES.items():
    it=None;sym=None
    for s in syms:
        if s in items and items[s].get("是否可用") and items[s].get("行情记录"): it=items[s];sym=s;break
    if it is None: print("%-10s %-12s 不可用"%(nm,syms[0]));continue
    df=pd.DataFrame(it["行情记录"]);df["d"]=pd.to_datetime(df["日期"]);df=df.set_index("d").sort_index()
    c=pd.to_numeric(df["收盘"],errors="coerce").dropna()
    if len(c)<300: print("%-10s %-12s 样本<300"%(nm,sym));continue
    sig=donch60(c).shift(1).fillna(0);ret=c.pct_change().fillna(0);cost=sig.diff().abs().fillna(0)*0.0005
    eq=(1+(sig*ret-cost)).cumprod();eqb=(1+ret).cumprod()
    a,d,cc=stat(eq);ab,db,cb=stat(eqb)
    ci=bootstrap_sharpe_ci(eq,n_bootstrap=500,confidence=0.95)
    wf=walk_forward_analysis(eq,[],n_windows=5);shs=[x.get("sharpe") for x in (wf.get("windows") or wf.get("per_window") or [])];pos=sum(1 for x in shs if x and x>0)
    cl=ci.get("ci_lower",0);pl=ci.get("prob_positive",0)*100
    v="✅绿灯" if (cc>cb and cl>0 and pos>=4) else ("🟡" if cc>cb and pl>=88 else "❌")
    print("%-10s %-12s yrs%.1f | Cal%.2f(BH%.2f) DD%+.0fpp ann%.0f%% | CI[%.2f,%.2f]p+%.0f%% wf%d/5 | %s"%(
        nm,sym,(c.index[-1]-c.index[0]).days/365,cc,cb,(d-db)*100,a*100,cl,ci.get("ci_upper",0),pl,pos,v))
