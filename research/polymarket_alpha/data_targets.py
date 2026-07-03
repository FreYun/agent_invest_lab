import sqlite3, pandas as pd, os
MARKET_DB = "/home/rooot/database/market.db"
ETF_CACHE = "/home/rooot/agent_invest_lab/research/index_timing/data"
LOCAL_CACHE = os.path.join(os.path.dirname(__file__), "data")

def _from_csv(path):
    df = pd.read_csv(path)
    dc = "trade_date" if "trade_date" in df.columns else df.columns[0]
    df[dc] = pd.to_datetime(df[dc])
    return df.set_index(dc)["close"].sort_index()

def load_target(code: str) -> pd.Series:
    # 1) A股指数 -> market.db
    if code.endswith(".SH") or code.endswith(".SZ") or code.endswith(".CSI"):
        etf = os.path.join(ETF_CACHE, f"{code}.csv")
        if os.path.exists(etf):                     # 已缓存 ETF 优先
            return _from_csv(etf)
        con = sqlite3.connect(MARKET_DB)
        df = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code=? ORDER BY trade_date",
                         con, params=[code], parse_dates=["trade_date"])
        con.close()
        if len(df):
            return df.set_index("trade_date")["close"]
    # 2) 本地补拉缓存
    local = os.path.join(LOCAL_CACHE, f"{code}.csv")
    if os.path.exists(local):
        return _from_csv(local)
    raise FileNotFoundError(f"no target data for {code}")
