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
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"n","version":"1"}})
lst,_=call("tools/list",{},sid);tools={t["name"]:t for t in lst["result"]["tools"]}
# 1 看 idx_corrected_deviation 参数 + sector_index_match 参数
for n in ["idx_corrected_deviation","sector_index_match","idx_constituents"]:
    if n in tools:
        pr=tools[n].get("inputSchema",{}).get("properties",{});rq=tools[n].get("inputSchema",{}).get("required",[])
        print(n,"req=",rq,"props=",list(pr.keys()))
def tool(n,a):
    res,_=call("tools/call",{"name":n,"arguments":dict(a,simulated_datetime="2026-06-05 15:00:00")},sid)
    return json.loads(res["result"]["content"][0]["text"])
# 2 试标准代码喂 idx_corrected_deviation
print("\n=== 试行业标准代码 ===")
for code in ["399986.SZ","399997.SZ","000932.SH"]:
    try:
        r=tool("idx_corrected_deviation",{"securityvarietycode":code,"date":"2026-06-05","win":200})
        print(code,"->",json.dumps(r,ensure_ascii=False)[:160])
    except Exception as e: print(code,"err",str(e)[:100])
# 3 sector_index_match 能否给内码
print("\n=== sector_index_match 银行 ===")
try:
    r=tool("sector_index_match",{"keyword":"银行"})
    print(json.dumps(r,ensure_ascii=False)[:400])
except Exception as e: print("err",e)
