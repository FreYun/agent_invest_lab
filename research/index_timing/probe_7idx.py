"""探查 7 份新 methodology 对应指数的行情覆盖（2026-06-11）。
沿用 probe_coverage.py 的 MCP 直连方式；urllib 强制不走代理。"""
import json, urllib.request
URL = "http://127.0.0.1:18078/mcp"
H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def call(m, p, sid=None):
    h = dict(H)
    if sid: h["Mcp-Session-Id"] = sid
    r = opener.open(urllib.request.Request(URL, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode(), headers=h), timeout=120)
    sid = r.headers.get("Mcp-Session-Id", sid); b = r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"): return json.loads(ln[5:].strip()), sid
    return json.loads(b), sid

_, sid = call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "p7", "version": "1"}})

NAMES = [
    ("通信设备", ["931160.CSI"]),
    ("通信技术", ["931144.CSI"]),
    ("CS人工智", ["930713.CSI"]),
    ("创业板人工智能", ["970070.CNI", "970070.SZ"]),
    ("科创芯片", ["000685.SH", "000685.CSI"]),
    ("国证芯片", ["980017.SZ", "980017.CNI"]),
    ("中证半导", ["931865.CSI"]),
    ("云计算", ["930851.CSI"]),
    ("动漫游戏", ["930901.CSI"]),
]
allsyms = [s for _, v in NAMES for s in v]
res, _ = call("tools/call", {"name": "market_index_quote", "arguments": {
    "market": "cn", "symbols": allsyms, "start_date": "2012-01-01", "end_date": "2026-06-10",
    "simulated_datetime": "2026-06-10 15:00:00"}}, sid)
items = {it.get("指数标识"): it for it in json.loads(res["result"]["content"][0]["text"]).get("items", [])}
print("=== 7 份新 methodology 指数覆盖 ===")
for nm, syms in NAMES:
    hit = None
    for s in syms:
        it = items.get(s)
        if it and it.get("是否可用") and it.get("行情记录"):
            rec = it["行情记录"]; hit = (s, len(rec), rec[0]["日期"], rec[-1]["日期"]); break
    if hit:
        yrs = hit[1] / 244
        print("  ✓ %-14s %-12s rows=%-5d %.1fy  %s..%s" % (nm, hit[0], hit[1], yrs, hit[2], hit[3]))
    else:
        print("  ✗ %-14s 不可用（候选:%s）" % (nm, ",".join(syms)))

# 估值可用性（market_index_val）
res2, _ = call("tools/call", {"name": "market_index_val", "arguments": {
    "market": "cn", "symbols": [v[0] for _, v in NAMES],
    "simulated_datetime": "2026-06-10 15:00:00"}}, sid)
try:
    vit = json.loads(res2["result"]["content"][0]["text"]).get("items", [])
    print("\n=== 估值（PE/分位）可用性 ===")
    for it in vit:
        print(" ", it.get("指数标识"), {k: it.get(k) for k in ("PE_TTM", "PE分位", "是否可用") if k in it} or it)
except Exception as e:
    print("val probe err:", e, str(res2)[:300])
