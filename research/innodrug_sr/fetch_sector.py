"""Fetch 创新药板块 BK000208 full history: market (主力净流入/收益/PE/PB) + 拥挤度.

Both endpoints return interval series in ONE call (start_date param) — fast, L1 真 PIT.
"""
from __future__ import annotations
import json
from pathlib import Path
import requests
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"
MCP = "http://127.0.0.1:18078/mcp"
SEC = "BK000208"
NOW = "2026-05-26 15:30:00"
START = "2016-01-01"


def call(tool: str, args: dict, retries: int = 4) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    import time
    last = None
    for _ in range(retries):
        try:
            r = requests.post(MCP, headers={"Content-Type": "application/json",
                              "Accept": "application/json, text/event-stream"},
                              data=json.dumps(payload), timeout=90)
            text = r.text
            if text.startswith("event:"):
                for line in text.splitlines():
                    if line.startswith("data:"):
                        text = line[5:].strip(); break
            res = json.loads(json.loads(text)["result"]["content"][0]["text"])
            if res.get("error"):
                last = res.get("message"); time.sleep(3); continue
            return res
        except Exception as e:
            last = str(e); time.sleep(3)
    return {"_failed": last}


def first_records(res: dict) -> list:
    items = res.get("items") or []
    if items and isinstance(items[0], dict) and "记录" in items[0]:
        return items[0]["记录"]
    return items


def main():
    # 1) market: 主力净流入 + 日收益率 + PE/PB
    print("fetching sector_market ...")
    m = call("sector_market", {"sec_codes": [SEC], "simulated_datetime": NOW, "start_date": START})
    rec = first_records(m)
    dfm = pd.DataFrame(rec)
    dfm = dfm.rename(columns={"日期": "date", "日收益率": "ret_pct", "主力净流入": "main_inflow",
                              "PE": "pe", "PB": "pb"}).sort_values("date")
    dfm.to_csv(DATA / "sector_BK000208_market.csv", index=False)
    print(f"  market: {len(dfm)} rows  {dfm['date'].iloc[0]}..{dfm['date'].iloc[-1]}  cols={list(dfm.columns)}")

    # 2) factor_detail: 拥挤度（成交集中度分位 + MA200乖离分位）
    print("fetching sector_factor_detail ...")
    fd = call("sector_factor_detail", {"sec_codes": [SEC], "simulated_datetime": NOW,
                                        "win": 200, "start_date": START})
    recf = first_records(fd)
    dff = pd.DataFrame(recf)
    print(f"  factor_detail raw cols: {list(dff.columns) if len(dff) else 'EMPTY'}")
    if len(dff):
        # normalize date col
        dcol = [c for c in dff.columns if "日期" in c or c == "date"]
        if dcol:
            dff = dff.rename(columns={dcol[0]: "date"}).sort_values("date")
        dff.to_csv(DATA / "sector_BK000208_factor_detail.csv", index=False)
        print(f"  factor_detail: {len(dff)} rows  {dff['date'].iloc[0]}..{dff['date'].iloc[-1]}")

    # 3) sector_factor: 动量 + 风险分（拥挤）
    print("fetching sector_factor ...")
    sf = call("sector_factor", {"sec_codes": [SEC], "simulated_datetime": NOW, "start_date": START})
    recs = first_records(sf)
    dfs = pd.DataFrame(recs)
    print(f"  factor raw cols: {list(dfs.columns) if len(dfs) else 'EMPTY'}")
    if len(dfs):
        dcol = [c for c in dfs.columns if "日期" in c or c == "date"]
        if dcol:
            dfs = dfs.rename(columns={dcol[0]: "date"}).sort_values("date")
        dfs.to_csv(DATA / "sector_BK000208_factor.csv", index=False)
        print(f"  factor: {len(dfs)} rows  {dfs['date'].iloc[0]}..{dfs['date'].iloc[-1]}")


if __name__ == "__main__":
    main()
