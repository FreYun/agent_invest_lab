import sys,json,urllib.request
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=90)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"ne","version":"1"}})
# 新能源子板块候选（含不确定后缀，服务端只回可用的）
NAMES={
 "中证新能源(母)":["930997.CSI"],
 "中证光伏产业":["931151.CSI"],
 "中证电池/CS电池":["931719.CSI"],
 "中证绿色电力":["931897.CSI"],
 "国证新能源车":["399976.SZ"],
 "中证储能产业":["931746.CSI","931746.SH"],
 "中证风电产业":["931790.CSI","H30533.CSI","930652.CSI"],
 "中证光伏龙头30":["931555.CSI"],
 "国证新能源电池":["980032.SZ"],
 "中证新能源(深)":["399808.SZ"],
 "中证内地新能源":["000941.CSI"],
 "中证细分有色/锂":["000812.SH","931898.CSI"],
}
allsyms=[s for v in NAMES.values() for s in v]
res,_=call("tools/call",{"name":"market_index_quote","arguments":{"market":"cn","symbols":allsyms,"start_date":"2014-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
items={it.get("指数标识"):it for it in json.loads(res["result"]["content"][0]["text"]).get("items",[])}
print("子板块候选数据可用性（simworld 18078）:")
for nm,syms in NAMES.items():
    hit=None
    for s in syms:
        it=items.get(s)
        if it and it.get("是否可用") and it.get("行情记录"):
            rec=it["行情记录"];d0=rec[0]["日期"];d1=rec[-1]["日期"];hit=(s,len(rec),d0,d1);break
    if hit: print("  %-16s %-12s rows=%-5d %s..%s"%(nm,hit[0],hit[1],hit[2],hit[3]))
    else:   print("  %-16s %-12s 不可用（候选:%s）"%(nm,syms[0],",".join(syms)))
