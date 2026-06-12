import sys,json,urllib.request
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from pathlib import Path
import local_sweep as L
DATA=Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing/data")
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=60)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"x","version":"1"}})
def tool(n,a):
    res,_=call("tools/call",{"name":n,"arguments":dict(a,simulated_datetime="2026-06-05 15:00:00")},sid)
    t=res.get("result",res).get("content",[{}])[0].get("text","")
    return t

# ---- 1) market_temperature 格式 ----
print("### market_temperature 样例 ###")
t=tool("market_temperature",{"start_date":"2018-01-01","end_date":"2026-06-05"})
j=json.loads(t); it=j.get("items",j)
print("type",type(it).__name__, json.dumps(it[:1] if isinstance(it,list) else it,ensure_ascii=False)[:400])

# ---- 2) 北向资金: 搜 EDB 指标 ----
print("\n### 北向/陆股通 EDB 指标搜索 ###")
for kw in ["北向","陆股通","沪股通"]:
    try:
        r=tool("macro_indicator_search",{"name":kw})
        jj=json.loads(r); items=jj.get("items",jj)
        print(kw,"->",json.dumps(items[:3] if isinstance(items,list) else items,ensure_ascii=False)[:300])
    except Exception as e: print(kw,"ERR",e)

# ---- 3) option_gvspread_signal schema+样例 ----
print("\n### option_gvspread_signal 样例 ###")
try:
    r=tool("option_gvspread_signal",{"start_date":"2018-01-01","end_date":"2026-06-05"})
    print(r[:400])
except Exception as e:
    print("ERR",e)
    # 试不带range
    try: print("noRange:",tool("option_gvspread_signal",{})[:400])
    except Exception as e2: print("ERR2",e2)
