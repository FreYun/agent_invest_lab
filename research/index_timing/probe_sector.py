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
# schema
lst,_=call("tools/list",{},sid);tools={t["name"]:t for t in lst["result"]["tools"]}
for n in ["sector_search","sector_factor_detail","sector_factor"]:
    pr=tools[n].get("inputSchema",{}).get("properties",{});rq=tools[n].get("inputSchema",{}).get("required",[])
    print("###",n,"required=",rq,"props=",list(pr.keys()))
def tool(n,a):
    res,_=call("tools/call",{"name":n,"arguments":dict(a,simulated_datetime="2026-06-05 15:00:00")},sid)
    return json.loads(res["result"]["content"][0]["text"])
# 搜板块
print("\n=== sector_search 军工/人工智能 ===")
for kw in ["军工","人工智能","半导体"]:
    r=tool("sector_search",{"name":kw})
    its=r.get("items",r)
    print(kw,"->",json.dumps(its[:4] if isinstance(its,list) else its,ensure_ascii=False)[:300])
