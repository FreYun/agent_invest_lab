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
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"p","version":"1"}})
lst,_=call("tools/list",{},sid);tools=[t["name"] for t in lst["result"]["tools"]]
hit=[t for t in tools if any(k in t.lower() for k in ["idx","deviation","congestion","乖离","corrected","variety"])]
print("idx/乖离/拥挤 相关工具:",hit)
print("\n全部工具数:",len(tools))
print("含'index'或'idx':",[t for t in tools if "index" in t.lower() or "idx" in t.lower()])
# 试调 idx_corrected_deviation（红利内码）确认可用 + 试个行业
def tool(n,a):
    res,_=call("tools/call",{"name":n,"arguments":dict(a,simulated_datetime="2026-06-05 15:00:00")},sid)
    return res
for n in ["idx_corrected_deviation","idx_congestion_pctrank"]:
    if n in tools:
        try:
            r=tool(n,{"securityvarietycode":"1000157392","date":"2026-06-05","win":200})
            print("\n[%s] 红利 ->"%n, json.dumps(r,ensure_ascii=False)[:300])
        except Exception as e: print("\n[%s] err %s"%(n,e))
    else: print("\n[%s] 不在工具列表(回测bot不可用)"%n)
