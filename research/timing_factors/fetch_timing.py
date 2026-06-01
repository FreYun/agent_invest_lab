"""Fetch continuous PIT timing signals from simworld -> compact CSVs.

These are DAILY signals (unlike rare VIX events) -> high statistical power for IC /
regression. All PIT (15:00 close visible).

  gzxjb_<idx>.csv      equity-bond risk premium (股债性价比) raw + 3y/5y percentile
  temperature.csv      market thermometer 综合得分 + 三月分位 + 6 subfactors
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

BASE = "https://research.tiantianfunds.com.cn/strategy"
SIM = "2026-05-26 15:30:00"
OUT = Path("/home/rooot/agent_invest_lab/research/timing_factors/data")
OUT.mkdir(parents=True, exist_ok=True)
S = requests.Session(); S.trust_env = False; S.headers.update({"Content-Type": "application/json"})


def post(p, d):
    r = S.post(f"{BASE}{p}", data=json.dumps(d, ensure_ascii=False).encode("utf-8"), timeout=180)
    r.raise_for_status(); return r.json()


# 1) gzxjb
for sym in ["000300", "000852"]:
    j = post("/api/market/index-gzxjb", {"symbols": [sym], "simulated_datetime": SIM,
             "calmodel": "pe", "caltype": 1, "start_date": "2004-01-01", "end_date": "2026-05-26"})
    it = j.get("items") or []
    if not it:
        print(sym, "EMPTY"); continue
    hist = it[0].get("历史序列") or []
    rows = [(r["交易日期"], r.get("股债性价比"), r.get("近3年百分位"), r.get("近5年百分位")) for r in hist]
    df = pd.DataFrame(rows, columns=["date", "erp", "erp_pct3y", "erp_pct5y"]).set_index("date").sort_index()
    df.to_csv(OUT / f"gzxjb_{sym}.csv")
    print(f"gzxjb_{sym}.csv n={len(df)} {df.index[0]}..{df.index[-1]}")

# 2) temperature — history under items[0]['历史序列']
j = post("/api/market/temperature", {"simulated_datetime": SIM, "start_date": "2008-01-01", "end_date": "2026-05-26"})
it = j.get("items") or []
hist = (it[0].get("历史序列") if it else []) or []
df = pd.DataFrame(hist).rename(columns={
    "交易日期": "date", "综合得分": "temp", "三月分位数": "temp_pct3m",
    "创新高低得分": "f_newhilo", "股债回报差得分": "f_erp", "升贴水率得分": "f_basis",
    "VIX得分": "f_vix", "价量偏离得分": "f_pxvol", "北向RSI得分": "f_north"})
if "date" in df:
    df = df.set_index("date").sort_index()
    df.to_csv(OUT / "temperature.csv")
    print(f"temperature.csv n={len(df)} {df.index[0]}..{df.index[-1]} cols={list(df.columns)}")
