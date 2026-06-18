"""探查金融 3 指数 PIT 可用性：保险主题399809 / 中证银行399986 / 证券公司399975。
确认起止日期、bar数、有无 high/low、估值数据可用性。"""
import sys, json, urllib.request
URL = "http://127.0.0.1:18078/mcp"
H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def call(m, p, sid=None):
    h = dict(H)
    if sid: h["Mcp-Session-Id"] = sid
    r = urllib.request.urlopen(urllib.request.Request(URL, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode(), headers=h), timeout=90)
    sid = r.headers.get("Mcp-Session-Id", sid); b = r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"): return json.loads(ln[5:].strip()), sid
    return json.loads(b), sid

_, sid = call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "probe-f3", "version": "1"}})
IDX = [("保险主题", "399809.SZ"), ("中证银行", "399986.SZ"), ("证券公司", "399975.SZ")]
syms = [c for _, c in IDX]
res, _ = call("tools/call", {"name": "market_index_quote", "arguments": {"market": "cn", "symbols": syms, "start_date": "2009-01-01", "end_date": "2026-06-12", "simulated_datetime": "2026-06-12 15:00:00", "simulated_today": "2026-06-12"}}, sid)
txt = res["result"]["content"][0]["text"]
try:
    items = {it.get("指数标识"): it for it in json.loads(txt).get("items", [])}
except Exception:
    print("非JSON返回：", txt[:800]); sys.exit(1)

for nm, code in IDX:
    it = items.get(code)
    if not it or not it.get("是否可用") or not it.get("行情记录"):
        print(f"{nm} {code}: 不可用 -> {str(it)[:200] if it else 'None'}")
        continue
    rec = it["行情记录"]
    has_hl = rec[0].get("最高") is not None
    print(f"{nm} {code}: rows={len(rec)} 起={rec[0]['日期']} 止={rec[-1]['日期']} high/low={has_hl}")

for nm, code in IDX:
    try:
        res, _ = call("tools/call", {"name": "market_index_val", "arguments": {"market": "cn", "symbols": [code], "simulated_datetime": "2026-06-12 15:00:00", "simulated_today": "2026-06-12"}}, sid)
        t = res["result"]["content"][0]["text"]
        print(f"VAL {nm} {code}: {t[:240]}")
    except Exception as e:
        print(f"VAL {nm} {code}: ERR {e}")
