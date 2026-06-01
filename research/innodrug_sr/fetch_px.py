import sys
sys.path.insert(0, "/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
from backtest.loaders.akshare_loader import DataLoader
loader = DataLoader()
codes = ["159992.SZ", "516080.SH", "159839.SZ", "512010.SH"]
data = loader.fetch(codes, "2019-01-01", "2026-05-26", interval="1D")
for code, df in data.items():
    df.to_csv(f"data/{code}.csv")
    print(code, df.index[0].date(), df.index[-1].date(), len(df), "cols=", list(df.columns))
