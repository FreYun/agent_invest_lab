import json,urllib.request
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=60)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"s","version":"1"}})
def tool(n,a):
    res,_=call("tools/call",{"name":n,"arguments":dict(a,simulated_datetime="2026-06-05 15:00:00")},sid)
    return json.loads(res["result"]["content"][0]["text"])
codes={}
for kw in ["军工","人工智能","半导体","机器人","新能源","光伏"]:
    r=tool("sector_search",{"keyword":kw});its=r.get("items",r)
    hit=[x for x in its if isinstance(x,dict)][:3] if isinstance(its,list) else its
    print(kw,"->",json.dumps(hit,ensure_ascii=False)[:260])
    if isinstance(its,list) and its and isinstance(its[0],dict): codes[kw]=its[0].get("板块代码")
# 拉一个板块 factor_detail 看格式+范围
bk=codes.get("半导体") or "BK000026"
print("\n=== sector_factor_detail %s 范围/字段 ==="%bk)
r=tool("sector_factor_detail",{"sec_codes":[bk],"start_date":"2016-01-01","end_date":"2026-06-05","win":200})
its=r.get("items",r)
if isinstance(its,list) and its:
    it=its[0]
    print("keys:",list(it.keys()) if isinstance(it,dict) else type(it))
    hist=it.get("历史序列") or it.get("记录") or None
    if hist: print("rows=%d %s..%s\nsample=%s"%(len(hist),hist[0].get("交易日期") or hist[0].get("日期"),hist[-1].get("交易日期") or hist[-1].get("日期"),json.dumps(hist[0],ensure_ascii=False)[:400]))
    else: print(json.dumps(it,ensure_ascii=False)[:600])
else: print(json.dumps(its,ensure_ascii=False)[:600])
