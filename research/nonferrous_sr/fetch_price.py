"""抓有色三指数 close + 512400 有色金属ETF(可交易代理)。akshare 免费源。"""
import sys
sys.path.insert(0, "/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import akshare as ak
from pathlib import Path

DATA = Path("/home/rooot/agent_invest_lab/research/nonferrous_sr/data")
DATA.mkdir(parents=True, exist_ok=True)
S, E = "20240901", "20260528"

# 三指数：用 index_zh_a_hist（中文列）
IDX = {"000819": "swnf", "930708": "csnf", "399395": "gznf"}
for code, label in IDX.items():
    df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=S, end_date=E)
    df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                            "最高": "high", "最低": "low", "成交量": "volume"})
    df = df[["date", "open", "high", "low", "close", "volume"]]
    df.to_csv(DATA / f"{label}_{code}.csv", index=False)
    print(f"{label} {code}: {len(df)} rows {df['date'].iloc[0]}..{df['date'].iloc[-1]}", flush=True)

# 512400 有色金属ETF（tracks 000819 申万有色）
import pandas as pd
etf = ak.fund_etf_hist_sina(symbol="sh512400")
etf["date"] = pd.to_datetime(etf["date"])
etf = etf[(etf["date"] >= "2024-09-01") & (etf["date"] <= "2026-05-28")]
etf["date"] = etf["date"].dt.strftime("%Y-%m-%d")
etf[["date", "open", "high", "low", "close", "volume"]].to_csv(DATA / "etf_512400.csv", index=False)
print(f"etf 512400: {len(etf)} rows {etf['date'].iloc[0]}..{etf['date'].iloc[-1]}", flush=True)
