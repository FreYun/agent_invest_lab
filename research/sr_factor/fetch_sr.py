"""Bulk-fetch fund_subscription_redemption_summary daily data.

Streaming write — appends to CSV per-day, resumable.
Data window 2024-07-11 → 2026-05-26.
"""
from __future__ import annotations
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

BASE = Path("/home/rooot/agent_invest_lab/research/sr_factor")
MCP = "http://127.0.0.1:18078/mcp"
START = date(2024, 7, 11)
END = date(2026, 5, 26)
OUT = BASE / "sr_daily.csv"
HEADER = ["date", "persona", "fund_type", "applied", "applied_ex_aip", "redeemed", "net", "net_ex_aip"]


def call_sr(d: str) -> dict:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "fund_subscription_redemption_summary", "arguments": {"calc_date": d}},
    }
    r = requests.post(
        MCP,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        data=json.dumps(payload), timeout=30,
    )
    text = r.text
    if text.startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    j = json.loads(text)
    return json.loads(j["result"]["content"][0]["text"])


def existing_dates() -> set[str]:
    if not OUT.exists():
        return set()
    seen: set[str] = set()
    with OUT.open() as f:
        rdr = csv.reader(f)
        header = next(rdr, None)
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
        n_skip = 0
        n_ok = 0
        n_miss = 0
        for i in range(n_total):
            ds = d.isoformat()
            if ds in seen:
                n_skip += 1
                d += timedelta(days=1)
                continue
            try:
                r = call_sr(ds)
                items = r.get("items") or []
                if items:
                    for it in items:
                        w.writerow([
                            ds, it.get("客户类型"), it.get("基金类型"),
                            it.get("申请"), it.get("申请_除定投"),
                            it.get("赎回"), it.get("净申赎"), it.get("净申赎_除定投"),
                        ])
                    n_ok += 1
                else:
                    n_miss += 1
            except Exception as e:
                n_miss += 1
                sys.stderr.write(f"  {ds}  ERR {e}\n")
            f.flush()
            d += timedelta(days=1)
            if (i + 1) % 60 == 0:
                print(f"  progress {i+1}/{n_total}  ok={n_ok}  miss={n_miss}  skip={n_skip}", flush=True)
    print(f"done  ok={n_ok}  miss={n_miss}  skip={n_skip}", flush=True)


if __name__ == "__main__":
    main()
