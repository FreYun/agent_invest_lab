"""Fetch + cache VIX series to CSV (run once).

  data/vix_50etf.csv     50ETF VIX (macro_50etf_vix)  2014-03 .. 2026-05  [date, ivix]   ← PRIMARY (live)
  data/vix_300idx.csv    沪深300 index-option VIX      2019-12 .. 2025-11             ← secondary (stale)
  data/vix_1000idx.csv   中证1000 index-option VIX     2022-07 .. 2025-11             ← secondary (stale)

ETF OHLCV is reused from research/sr_factor/data/{510300,512100,588800}.SH.csv (already cached).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

BASE = "https://research.tiantianfunds.com.cn/strategy"
SIM = "2026-05-26 15:30:00"
OUT = Path("/home/rooot/agent_invest_lab/research/vix_factor/data")
OUT.mkdir(parents=True, exist_ok=True)

S = requests.Session()
S.trust_env = False
S.headers.update({"Content-Type": "application/json"})


def post(path: str, data: dict) -> dict:
    r = S.post(f"{BASE}{path}", data=json.dumps(data, ensure_ascii=False).encode("utf-8"), timeout=120)
    r.raise_for_status()
    return r.json()


# 1) 50ETF VIX — single long live series
j = post("/api/macro/50etf-vix", {"simulated_datetime": SIM, "start_date": "2010-01-01", "end_date": "2026-05-26"})
rows = [(r["交易日期"], r["ivix"]) for r in j.get("items", [])]
df = pd.DataFrame(rows, columns=["date", "vix"]).set_index("date").sort_index()
df.to_csv(OUT / "vix_50etf.csv")
print("vix_50etf.csv", len(df), df.index[0], df.index[-1])

# 2) per-index option VIX (secondary, stale to 2025-11)
for code, fname in [("000300", "vix_300idx.csv"), ("000852", "vix_1000idx.csv")]:
    j = post("/api/option/vix", {"simulated_datetime": SIM, "type_codes": [code],
                                 "start_date": "2010-01-01", "end_date": "2026-05-26"})
    recs = []
    for v in j.get("items", []):
        if v.get("合约类型") == "指数期权":
            recs = v.get("记录") or []
            break
    rr = [(r["日期"], r["VIX"], r.get("GVSpread")) for r in recs]
    df = pd.DataFrame(rr, columns=["date", "vix", "gvspread"]).set_index("date").sort_index()
    df.to_csv(OUT / fname)
    print(fname, len(df), df.index[0] if len(df) else "-", df.index[-1] if len(df) else "-")
