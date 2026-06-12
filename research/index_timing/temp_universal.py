import sys,json,urllib.request
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
DATA=Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing/data");SR=Path("/home/ubuntu/rooot/agent_invest_lab/research/sr_factor/data")
# 温度(缓存)
tp=DATA/"market_temp.csv"
if not tp.exists():
    URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
    def call(m,p,sid=None):
        h=dict(H)
        if sid:h["Mcp-Session-Id"]=sid
        r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=60)
        sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
        for ln in b.splitlines():
            if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
        return json.loads(b),sid
    _,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}})
    res,_=call("tools/call",{"name":"market_temperature","arguments":{"start_date":"2013-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
    seq=json.loads(res["result"]["content"][0]["text"])["items"][0]["历史序列"]
    T=pd.DataFrame(seq);T["d"]=pd.to_datetime(T["交易日期"]);T.set_index("d")[["综合得分","三月分位数"]].to_csv(tp)
T=pd.read_csv(tp,parse_dates=["d"]).set_index("d").sort_index()
temp=pd.to_numeric(T["综合得分"],errors="coerce")

POOL={"半导体":(SR,"512480.SH"),"中证1000":(DATA,"512100.SH"),"创业板":(DATA,"159915.SZ"),
 "科创50":(DATA,"588000.SH"),"新能源":(DATA,"516160.SH"),"军工":(DATA,"512660.SH"),
 "证券":(DATA,"512880.SH"),"白酒":(DATA,"512690.SH"),"红利":(DATA,"515080.SH"),"沪深300":(DATA,"510300.SH")}
def load(d,c):df=pd.read_csv(d/(c+".csv"),parse_dates=[0],index_col=0).sort_index();df.index.name="date";return df
def bt(w,sig,s):
    sig=sig.clip(0,1).fillna(0).reindex(w.index).fillna(0);ret=w["close"].pct_change().fillna(0)
    pos=sig.shift(1).fillna(0);cost=pos.diff().abs().fillna(pos.iloc[0])*0.0005
    sr=(pos*ret-cost);m=w.index>=pd.Timestamp(s);return (1+sr.loc[m]).cumprod(),pos.loc[m].mean()
def cal(eq):
    r=eq.pct_change().fillna(0);n=len(r);ann=eq.iloc[-1]**(252/n)-1;dd=(eq/eq.cummax()-1).min()
    return (ann/abs(dd) if dd<0 else 0)
# 温度反向 overlay：低温满仓 高温减仓（带底仓0.3）
def f_temp(df,lo,hi,floor=0.3):
    t=temp.reindex(df.index,method="ffill")
    s=pd.Series(np.nan,index=df.index);s=s.where(~(t<lo),1.0);s=s.where(~(t>hi),floor)
    return s.ffill().fillna(1.0)
IS_S,IS_E="2019-06-01","2023-12-31";OOS_S,OOS_E="2024-01-01","2026-06-06"
print("市场温度反向(30/70,floor0.3) 全池普适性  [dCalmar vs buyhold]")
print("%-8s | IS dCal | OOS dCal | exp"%"指数")
isb=osb=0;n=0
for nm,(d,c) in POOL.items():
    df=load(d,c)
    wi=L.slice_range(df,IS_S,IS_E,260);wo=L.slice_range(df,OOS_S,OOS_E,260)
    bi=cal(bt(wi,L.s_buyhold(wi),IS_S)[0]);bo=cal(bt(wo,L.s_buyhold(wo),OOS_S)[0])
    ei,xi=bt(wi,f_temp(wi,30,70),IS_S);eo,xo=bt(wo,f_temp(wo,30,70),OOS_S)
    di=cal(ei)-bi;do=cal(eo)-bo;n+=1;isb+=di>0;osb+=do>0
    print("%-8s | %+.2f   | %+.2f    | %.2f"%(nm,di,do,xo))
print("\n击败buyhold占比: IS %d/%d (%.0f%%)  OOS %d/%d (%.0f%%)"%(isb,n,isb/n*100,osb,n,osb/n*100))
