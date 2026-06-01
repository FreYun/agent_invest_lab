"""Data-availability probe for the VIX-extreme contrarian add-position factor.

Directly hits the simworld backend (same BASE_URL the MCP wrapper uses) to find:
  - macro_50etf_vix earliest/latest date + fields (single long 50ETF series)
  - option_vix coverage per variety (50/300/500/1000/科创50/创业板) + earliest date
We only PROBE here; fetch+cache happens in fetch_vix.py once we know ranges.
"""
from __future__ import annotations

import json
import requests

BASE = "https://research.tiantianfunds.com.cn/strategy"
SIM = "2026-05-26 15:30:00"  # "now" for PIT
S = requests.Session()
S.trust_env = False
S.headers.update({"Content-Type": "application/json"})


def post(path: str, data: dict) -> dict:
    r = S.post(f"{BASE}{path}", data=json.dumps(data, ensure_ascii=False).encode("utf-8"), timeout=60)
    r.raise_for_status()
    return r.json()


def show(tag, j, n=2):
    print(f"\n===== {tag} =====")
    if not j.get("success"):
        print("  FAIL:", j.get("message"))
        return
    data = j.get("data")
    # try to find the list of rows
    rows = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for k in ("items", "list", "rows", "records", "data"):
            if isinstance(data.get(k), list):
                rows = data[k]; print("  list-key:", k); break
        if rows is None:
            # maybe per-variety dict
            print("  dict keys:", list(data.keys())[:20])
            for k, v in list(data.items())[:6]:
                if isinstance(v, list) and v:
                    print(f"   [{k}] n={len(v)} first={v[0]} last={v[-1]}")
            return
    if rows is None:
        print("  raw:", str(data)[:500]); return
    print(f"  n={len(rows)}")
    if rows:
        print("  first:", rows[0])
        print("  last :", rows[-1])


# 1) macro_50etf_vix — probe earliest by asking for a very early start_date
print("######## macro_50etf_vix ########")
show("50etf_vix latest (no start)", post("/api/macro/50etf-vix", {"simulated_datetime": SIM}))
show("50etf_vix from 2010", post("/api/macro/50etf-vix",
     {"simulated_datetime": SIM, "start_date": "2010-01-01", "end_date": "2026-05-26"}))

# 2) option_vix — all varieties, latest snapshot
print("\n\n######## option_vix ########")
show("option_vix latest all", post("/api/option/vix", {"simulated_datetime": SIM}))

# per-variety earliest probe
for code, name in [("000016", "上证50"), ("000300", "沪深300"), ("000905", "中证500"),
                   ("000852", "中证1000"), ("000688", "科创50"), ("399673", "创业板")]:
    j = post("/api/option/vix", {"simulated_datetime": SIM, "type_codes": [code],
                                 "start_date": "2010-01-01", "end_date": "2026-05-26"})
    show(f"option_vix {code} {name} full", j)
