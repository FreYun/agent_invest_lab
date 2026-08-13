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
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"sc","version":"1"}})
# 缺口清单 CN 指数（多写几个备选后缀，服务端只回可用的）
NAMES={ "中证500":["000905.SH"],"中证军工":["399967.SZ"],"创新药":["931152.CSI"],
 "中证白酒":["399997.SZ"],"有色金属":["000819.CSI"],"中证新能源":["930997.CSI"],
 "中证医疗":["399989.SZ"],"证券公司":["399975.SZ"],"中证银行":["399986.SZ"],
 "中证半导":["931865.CSI"],"中证煤炭":["399998.SZ"],"光伏产业":["931151.CSI"],
 "中证红利":["000922.SH"],"国证芯片":["980017.SZ"],"中证消费":["000932.SH"]}
allsyms=[s for v in NAMES.values() for s in v]
res,_=call("tools/call",{"name":"market_index_quote","arguments":{"market":"cn","symbols":allsyms,"start_date":"2014-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
items={it.get("指数标识"):it for it in json.loads(res["result"]["content"][0]["text"]).get("items",[])}
def donch60(c):
    hh=c.shift(1).rolling(60).max();ll=c.shift(1).rolling(60).min()
    st=pd.Series(np.nan,index=c.index);st=st.where(~(c>hh),1.0);st=st.where(~(c<ll),0.0);return st.ffill().fillna(0.0)
def stat(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return ann,dd,(ann/abs(dd) if dd<0 else 0),(r.mean()/r.std()*np.sqrt(252) if r.std()>0 else 0)
print("%-10s %-12s | 因子Cal(BH) DD改善 ann exp翻转 | bootstrap walk-fwd | 判定"%("指数","代码"))
rows=[]
for nm,syms in NAMES.items():
    it=None;sym=None
    for s in syms:
        if s in items and items[s].get("是否可用") and items[s].get("行情记录"): it=items[s];sym=s;break
    if it is None: print("%-10s %-12s | 不可用"%(nm,syms[0]));continue
    df=pd.DataFrame(it["行情记录"]);df["d"]=pd.to_datetime(df["日期"]);df=df.set_index("d").sort_index()
    c=pd.to_numeric(df["收盘"],errors="coerce").dropna()
    sig=donch60(c).shift(1).fillna(0);ret=c.pct_change().fillna(0);cost=sig.diff().abs().fillna(0)*0.0005
    eq=(1+(sig*ret-cost)).cumprod();eqb=(1+ret).cumprod()
    a,d,cc,sh=stat(eq);ab,db,cb,_=stat(eqb)
    ci=bootstrap_sharpe_ci(eq,n_bootstrap=600,confidence=0.95)
    wf=walk_forward_analysis(eq,[],n_windows=5);shs=[x.get("sharpe") for x in (wf.get("windows") or wf.get("per_window") or [])];pos=sum(1 for x in shs if x and x>0)
    pl=ci.get("prob_positive",0)*100;cl=ci.get("ci_lower",0)
    verdict="✅绿灯" if (cc>cb and cl>0 and pos>=4) else ("🟢近绿" if (cc>cb and pl>=90 and pos>=4) else ("🟡看" if cc>cb else "❌"))
    print("%-10s %-12s | %.2f(%.2f) %+.0fpp ann%.0f%% e%.0f%%/%d | CI[%.2f,%.2f]p+%.0f%% wf%d/5 | %s"%(
        nm,sym,cc,cb,(d-db)*100,a*100,sig.mean()*100,int((sig.diff().abs()>0).sum()),cl,ci.get("ci_upper",0),pl,pos,verdict))
