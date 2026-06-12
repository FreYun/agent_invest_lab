import sys, json, urllib.request
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import pandas as pd
from pathlib import Path
DATA=Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing/data"); DATA.mkdir(exist_ok=True)

# ---------- 1) akshare: 510050 OHLCV ----------
from backtest.loaders.akshare_loader import DataLoader
ld=DataLoader()
d=ld.fetch(["510050.SH"],"2013-01-01","2026-06-06",interval="1D")
df=d["510050.SH"]; df.to_csv(DATA/"510050.SH.csv")
print("[akshare 510050] rows=%d %s..%s cols=%s"%(len(df),df.index[0].date(),df.index[-1].date(),list(df.columns)))
print(df.tail(2).to_string())

# ---------- 2) simworld: ERP(gzxjb) + 50ETF VIX ----------
URL="http://127.0.0.1:18078/mcp"; H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(method,params,sid=None):
    h=dict(H); 
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(),headers=h),timeout=60)
    sid=r.headers.get("Mcp-Session-Id",sid); body=r.read().decode()
    for ln in body.splitlines():
        if ln.startswith("data:"): return json.loads(ln[5:].strip()),sid
    return json.loads(body),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"f","version":"1"}})
def tool(name,args):
    res,_=call("tools/call",{"name":name,"arguments":dict(args,simulated_datetime="2026-06-05 15:00:00")},sid)
    return json.loads(res["result"]["content"][0]["text"])

erp=tool("market_index_gzxjb",{"symbols":["000016.SH"],"start_date":"2013-01-01","end_date":"2026-06-05"})
seq=erp["items"][0].get("历史序列",[])
e=pd.DataFrame(seq); print("\n[ERP gzxjb] rows=%d cols=%s"%(len(e),list(e.columns)))
if len(e): print(e.head(1).to_string()); print(e.tail(1).to_string()); e.to_csv(DATA/"000016_erp.csv",index=False)

vix=tool("macro_50etf_vix",{"start_date":"2013-01-01","end_date":"2026-06-05"})
it=vix.get("items",vix)
print("\n[50ETF VIX] type=%s"%type(it).__name__, json.dumps(it,ensure_ascii=False)[:300])
