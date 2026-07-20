"""拉取中证红利(1000157392)四信号回测所需数据,缓存到 data/dividend_bt/。

- market_index_quote: 000922(中证红利全收益价格)、000985(中证全指) 区间一次拿全
- idx_corrected_deviation / idx_congestion_pctrank: 逐交易日串行拉(上游不许并发)
断点续传:已有日期跳过。
"""
import json, time, urllib.request, csv, sys
from pathlib import Path

URL = "http://127.0.0.1:18078/mcp"
H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
OUT = Path(__file__).parent / "data" / "dividend_bt"
OUT.mkdir(parents=True, exist_ok=True)
START, END = "2023-06-01", "2026-07-03"   # 前置半年热身,信号回测从2024-01-01起
SIM = "2026-07-04 15:00:00"

def call(m, p, sid=None):
    h = dict(H)
    if sid: h["Mcp-Session-Id"] = sid
    r = urllib.request.urlopen(urllib.request.Request(
        URL, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode(), headers=h), timeout=60)
    sid = r.headers.get("Mcp-Session-Id", sid); b = r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"): return json.loads(ln[5:].strip()), sid
    return json.loads(b), sid

_, SID = call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "divbt", "version": "1"}})

def tool(n, a, retries=3):
    for i in range(retries):
        try:
            res, _ = call("tools/call", {"name": n, "arguments": dict(a, simulated_datetime=SIM)}, SID)
            j = json.loads(res["result"]["content"][0]["text"])
            if j.get("error") == "rate_limited":
                time.sleep(5); continue
            return j
        except Exception as e:
            if i == retries - 1: raise
            time.sleep(3)
    return {}

# 1) 行情区间
for sym, name in [("000922", "idx_000922"), ("000985", "idx_000985")]:
    f = OUT / f"{name}.csv"
    r = tool("market_index_quote", {"symbols": [sym], "market": "cn", "start_date": START, "end_date": END})
    items = r.get("items", [])
    seq = items[0].get("行情记录", []) if items else []
    with f.open("w", newline="") as fh:
        w = None
        for row in seq:
            if w is None:
                w = csv.DictWriter(fh, fieldnames=list(row.keys())); w.writeheader()
            w.writerow(row)
    print(f"[quote {sym}] rows={len(seq)}", flush=True)

# 交易日列表来自 000922 行情
import re
rows = list(csv.DictReader((OUT / "idx_000922.csv").open()))
datekey = next(k for k in rows[0] if "日期" in k or "date" in k.lower())
days = sorted({r[datekey][:10] for r in rows})
days = [d for d in days if d >= "2023-09-01"]   # 因子只需信号期+少量热身
print(f"trading days to fetch factors: {len(days)}", flush=True)

# 2) 因子逐日
def fetch_daily(toolname, fname, fields):
    f = OUT / fname
    done = set()
    if f.exists():
        done = {r["date"] for r in csv.DictReader(f.open())}
    mode = "a" if done else "w"
    with f.open(mode, newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["date"] + fields)
        if not done: w.writeheader()
        for i, d in enumerate(days):
            if d in done: continue
            j = tool(toolname, {"securityvarietycode": "1000157392", "date": d, "win": 200})
            items = j.get("items") or []
            row = {"date": d}
            if items:
                for k in fields: row[k] = items[0].get(k, "")
            w.writerow(row); fh.flush()
            if i % 50 == 0: print(f"[{toolname}] {d} ({i}/{len(days)})", flush=True)
            time.sleep(0.15)
    print(f"[{toolname}] done -> {fname}", flush=True)

fetch_daily("idx_corrected_deviation", "corrected_deviation.csv",
            ["乖离率", "修正相对乖离度", "乖离业务分位_由低至高", "修正乖离业务分位_由低至高", "样本数"])
fetch_daily("idx_congestion_pctrank", "congestion.csv",
            ["成交集中度", "复合拥挤度", "拥挤业务分位_由低至高", "样本数"])
print("ALL DONE", flush=True)
