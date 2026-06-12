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
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"ne6","version":"1"}})
NE6=[("光伏产业","931151.CSI"),("CS电池","931719.CSI"),("绿色电力","931897.CSI"),
     ("CS新能车","399976.SZ"),("电力指数","H30199.CSI"),("电网设备主题","931994.CSI")]
syms=[c for _,c in NE6]
res,_=call("tools/call",{"name":"market_index_quote","arguments":{"market":"cn","symbols":syms,"start_date":"2014-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
items={it.get("指数标识"):it for it in json.loads(res["result"]["content"][0]["text"]).get("items",[])}
def donch(c,win=60):
    hh=c.shift(1).rolling(win).max();ll=c.shift(1).rolling(win).min()
    st=pd.Series(np.nan,index=c.index);st=st.where(~(c>hh),1.0);st=st.where(~(c<ll),0.0);return st.ffill().fillna(0.0)
def traildd(c,peak=120,dd=-0.15,rema=60):
    pk=c.rolling(peak,min_periods=20).max();cur=c/pk-1.0;ma=c.rolling(rema).mean()
    st=pd.Series(1.0,index=c.index);st=st.where(~(cur<dd),0.0);st=st.where(~((st.shift(1)==0)&(c>ma)),1.0)
    # 简化：跌破dd离场，价格回到rema均线上方再进
    out=[];pos=1.0
    for i in range(len(c)):
        if cur.iloc[i]<dd: pos=0.0
        elif c.iloc[i]>(ma.iloc[i] if not np.isnan(ma.iloc[i]) else c.iloc[i]): pos=1.0
        out.append(pos)
    return pd.Series(out,index=c.index)
def ev(c,sig):
    sig=sig.shift(1).fillna(0);ret=c.pct_change().fillna(0);cost=sig.diff().abs().fillna(0)*0.0005
    eq=(1+(sig*ret-cost)).cumprod();r=eq.pct_change().fillna(0);n=len(r)
    ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min();cal=ann/abs(dd) if dd<0 else 0
    ci=bootstrap_sharpe_ci(eq,n_bootstrap=700,confidence=0.95)
    wf=walk_forward_analysis(eq,[],n_windows=5);shs=[x.get("sharpe") for x in (wf.get("windows") or wf.get("per_window") or [])];pos=sum(1 for x in shs if x and x>0)
    flips=int((sig.diff().abs()>0).sum())
    return ann,dd,cal,ci.get("ci_lower",0),ci.get("ci_upper",0),ci.get("prob_positive",0)*100,pos,flips
print("新能源6子板块择时因子 validation（close版，全周期指数）:")
for nm,code in NE6:
    it=items.get(code)
    if not it or not it.get("是否可用") or not it.get("行情记录"): print("  %-12s %s 不可用"%(nm,code));continue
    df=pd.DataFrame(it["行情记录"]);df["d"]=pd.to_datetime(df["日期"]);df=df.set_index("d").sort_index()
    c=pd.to_numeric(df["收盘"],errors="coerce").dropna()
    bh_ann=c.iloc[-1]/c.iloc[0];bh_ann=bh_ann**(252/len(c))-1;bh_dd=(c/c.cummax()-1).min();bh_cal=bh_ann/abs(bh_dd) if bh_dd<0 else 0
    for fn,tag in [(lambda x:donch(x,60),"donchian_60"),(lambda x:traildd(x),"trail_dd15")]:
        a,d,cal,cl,cu,pl,pos,flips=ev(c,fn(c))
        verd="✅" if (cal>bh_cal and cl>0 and pos>=4) else ("🟢" if (cal>bh_cal and pl>=90 and pos>=4) else ("🟡" if cal>bh_cal else "❌"))
        print("  %-12s %-11s %-12s Cal%.2f(BH%.2f) DD%.0f%%(BH%.0f%%) ann%.0f%% CI[%.2f,%.2f]p+%.0f%% wf%d/5 flips%d %s"%(
            nm,code,tag,cal,bh_cal,d*100,bh_dd*100,a*100,cl,cu,pl,pos,flips,verd))
