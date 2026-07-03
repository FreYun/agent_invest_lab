import sqlite3, pandas as pd
POLY_DB = "/home/rooot/database/polymarket.db"

def load_signal(factor: str) -> pd.Series:
    con = sqlite3.connect(POLY_DB)
    df = pd.read_sql("SELECT date, value FROM macro_factor WHERE factor=? ORDER BY date",
                     con, params=[factor], parse_dates=["date"])
    con.close()
    return df.set_index("date")["value"]

def align(signal: pd.Series, price: pd.Series, horizon: int, extra_lag: int = 0) -> pd.DataFrame:
    price = price.sort_index()
    sig = signal.sort_index().reindex(price.index).ffill()   # 概率 ffill 到交易日
    sig = sig.shift(1 + extra_lag)                            # PIT: 当日不可用
    fwd = price.pct_change(horizon).shift(-horizon)           # 未来 horizon 收益
    out = pd.DataFrame({"sig": sig, "fwd_ret": fwd}).dropna()
    return out
