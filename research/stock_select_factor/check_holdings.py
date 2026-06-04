"""查 lowvol_60 K=40 的实际持仓 — 行业集中度 + 跨年漂移. 用 akshare 取名字."""
import sqlite3, sys
import numpy as np, pandas as pd

DB = "/home/rooot/database/market.db"
MV = 1_000_000.0

c = sqlite3.connect(DB)
df = pd.read_sql("SELECT trade_date, ts_code, close, pct_chg FROM daily", c)
ts = pd.read_sql("SELECT ts_code, total_share FROM daily_basic WHERE total_share IS NOT NULL "
                 "AND trade_date=(SELECT MAX(trade_date) FROM daily_basic)", c)
c.close()
close = df.pivot(index="trade_date", columns="ts_code", values="close").sort_index().astype("float32")
ret = df.pivot(index="trade_date", columns="ts_code", values="pct_chg").sort_index().astype("float32")/100
share = ts.set_index("ts_code")["total_share"].reindex(close.columns)
mask = (close * share.values >= MV) & close.notna()
lowvol = -(ret.rolling(60).std()).where(mask)

# 名字 (akshare 一次调用)
try:
    import akshare as ak
    nm = ak.stock_info_a_code_name()  # columns: code, name
    nm["ts"] = nm["code"].astype(str).str.zfill(6)
    name_map = dict(zip(nm["ts"], nm["name"]))
except Exception as e:
    print("akshare 取名失败:", str(e)[:80]); name_map = {}

def holdings(date):
    row = lowvol.loc[date].dropna()
    top = row.nlargest(40).index
    return [(t, name_map.get(t.split(".")[0], "?")) for t in top]

for d in ["20220104", "20240102", close.index[-1]]:
    if d not in lowvol.index:
        d = lowvol.index[lowvol.index.get_indexer([d], method="nearest")[0]]
    hs = holdings(d)
    print(f"\n===== {d} lowvol_60 top40 持仓 =====")
    print("  " + " | ".join(n for _, n in hs))
