"""Bulk-fetch fund_index_subscription_redemption for 创新药产业指数 931152.

Streaming write — appends to CSV per-day, resumable. Sequential (no concurrency:
upstream resets on parallel calls). Data window ~2024-08-01 → 2026-05-26.
"""
from __future__ import annotations
import csv
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
MCP = "http://127.0.0.1:18078/mcp"
INDEX = "931152"
START = date(2024, 7, 22)   # probe showed empty up to 07-22, data from ~08-01; start early to catch真起点
END = date(2026, 5, 26)
OUT = BASE / "innodrug_sr_daily.csv"
HEADER = ["date", "persona", "applied", "applied_ex_aip", "redeemed", "net", "net_ex_aip"]


def call_idx(d: str, retries: int = 3) -> dict:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "fund_index_subscription_redemption",
                   "arguments": {"index_code": INDEX, "calc_date": d}},
    }
    last = None
    for _ in range(retries):
        try:
            r = requests.post(
                MCP,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                data=json.dumps(payload), timeout=45,
            )
            text = r.text
            if text.startswith("event:"):
                for line in text.splitlines():
                    if line.startswith("data:"):
                        text = line[5:].strip()
                        break
            j = json.loads(text)
            res = json.loads(j["result"]["content"][0]["text"])
            # upstream read timeout shows as success=absent + error -> retry
            if res.get("error"):
                last = res.get("message")
                time.sleep(2)
                continue
            return res
        except Exception as e:
            last = str(e)
            time.sleep(2)
    return {"_failed": last}


def existing_dates() -> set[str]:
    if not OUT.exists():
        return set()
    seen: set[str] = set()
    with OUT.open() as f:
        rdr = csv.reader(f)
        next(rdr, None)
        for row in rdr:
            if row:
                seen.add(row[0])
    return seen


def main():
    seen = existing_dates()
    new_file = not OUT.exists()
    with OUT.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(HEADER)
        d = START
        n_total = (END - START).days + 1
        n_ok = n_miss = n_skip = n_fail = 0
        for i in range(n_total):
            ds = d.isoformat()
            if ds in seen:
                n_skip += 1
                d += timedelta(days=1)
                continue
            r = call_idx(ds)
            if r.get("_failed"):
                n_fail += 1
                sys.stderr.write(f"  {ds}  FAIL {r['_failed']}\n")
            else:
                items = r.get("items") or []
                if items:
                    for it in items:
                        w.writerow([
                            ds, it.get("客户类型"),
                            it.get("申请"), it.get("申请_除定投"),
                            it.get("赎回"), it.get("净申赎"), it.get("净申赎_除定投"),
                        ])
                    n_ok += 1
                else:
                    n_miss += 1
            f.flush()
            d += timedelta(days=1)
            if (i + 1) % 30 == 0:
                print(f"  {i+1}/{n_total} ok={n_ok} miss={n_miss} skip={n_skip} fail={n_fail}", flush=True)
    print(f"done ok={n_ok} miss={n_miss} skip={n_skip} fail={n_fail}", flush=True)


if __name__ == "__main__":
    main()
