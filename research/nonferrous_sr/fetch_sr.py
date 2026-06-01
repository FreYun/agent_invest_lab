"""抓有色三指数(000819申万有色/930708中证有色/399395国证有色)的按指数申赎。

直连后端 /api/fund/index-subscription-redemption（与 simworld-mcp wrapper 同口径）。
三指数一次请求(逗号分隔)；流式 append、可断点续传、不囤内存。
数据窗口 2024-09-01 → 2026-05-28，calc_date 即 PIT 时点闸门。
顺序请求、不并发（上游并发会 reset）。
"""
from __future__ import annotations
import csv, json, sys, time
from datetime import date, timedelta
from pathlib import Path
import requests

BASE_URL = "https://research.tiantianfunds.com.cn/strategy"
CODES = "000819,930708,399395"
START = date(2024, 9, 1)
END = date(2026, 5, 28)
OUT = Path("/home/rooot/agent_invest_lab/research/nonferrous_sr/sr_daily.csv")
HEADER = ["date", "index_code", "persona", "applied", "applied_ex_aip", "redeemed", "net", "net_ex_aip"]

S = requests.Session(); S.trust_env = False
S.headers.update({"Content-Type": "application/json"})


def call(d: str) -> list:
    r = S.post(f"{BASE_URL}/api/fund/index-subscription-redemption",
               data=json.dumps({"index_code": CODES, "calc_date": d, "cutoff_time": "15:00:00"},
                               ensure_ascii=False).encode("utf-8"), timeout=30)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        return []
    return j.get("items") or []


def existing() -> set:
    if not OUT.exists():
        return set()
    seen = set()
    with OUT.open() as f:
        rdr = csv.reader(f); next(rdr, None)
        for row in rdr:
            if row:
                seen.add(row[0])
    return seen


def main():
    seen = existing()
    new = not OUT.exists()
    n_total = (END - START).days + 1
    n_ok = n_miss = n_skip = 0
    with OUT.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(HEADER)
        d = START
        for i in range(n_total):
            ds = d.isoformat()
            if ds in seen:
                n_skip += 1; d += timedelta(days=1); continue
            try:
                items = call(ds)
                if items:
                    for it in items:
                        w.writerow([ds, it.get("指数代码"), it.get("客户类型"),
                                    it.get("申请"), it.get("申请_除定投"),
                                    it.get("赎回"), it.get("净申赎"), it.get("净申赎_除定投")])
                    n_ok += 1
                else:
                    n_miss += 1
            except Exception as e:
                n_miss += 1
                sys.stderr.write(f"  {ds} ERR {e}\n")
            f.flush()
            d += timedelta(days=1)
            time.sleep(0.5)
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{n_total} ok={n_ok} miss={n_miss} skip={n_skip}", flush=True)
    print(f"done ok={n_ok} miss={n_miss} skip={n_skip}", flush=True)


if __name__ == "__main__":
    main()
