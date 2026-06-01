"""Event study: forward returns conditioned on 50ETF-VIX state.

Economic-logic gate (cheap, before any backtest): if extreme-HIGH VIX (panic /
capitulation) is a good ADD-position timing, then the top VIX bucket should show
materially higher forward returns than the low buckets, consistently across all
three target ETFs.

VIX state = rolling percentile of 50ETF VIX over `pct_win` trading days (PIT: VIX_T
known at T close). Forward return = close_T -> close_{T+h}.

We bucket by percentile and report mean fwd ret + hit rate + n per bucket, for
h in {5,10,20,60}, for each ETF, over the full overlapping sample and split IS/OOS.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/vix_factor")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIX = BASE / "data" / "vix_50etf.csv"

ETFS = [("HS300", "510300.SH"), ("ZZ1000", "512100.SH"), ("SC50", "588800.SH")]
HORIZONS = [5, 10, 20, 60]
PCT_WIN = 252          # 1y rolling percentile window for VIX
BUCKETS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
IS_END = "2023-12-31"


def load_vix() -> pd.Series:
    v = pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()
    return v


def load_etf(code: str) -> pd.DataFrame:
    df = pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()
    df.index.name = "date"
    return df


def vix_pctrank(v: pd.Series, win: int) -> pd.Series:
    # rolling percentile rank of the latest value within the trailing window
    return v.rolling(win, min_periods=win // 2).apply(
        lambda x: (x < x[-1]).mean(), raw=True)


def study(label: str, code: str, vix: pd.Series, pr: pd.Series) -> list[dict]:
    df = load_etf(code)
    close = df["close"]
    # align VIX state to ETF dates (VIX known at T close -> ffill onto trading days)
    pr_a = pr.reindex(close.index, method="ffill")
    out = []
    for h in HORIZONS:
        fwd = close.shift(-h) / close - 1.0
        sub = pd.DataFrame({"pr": pr_a, "fwd": fwd}).dropna()
        if sub.empty:
            continue
        # full sample
        for lo, hi in zip(BUCKETS[:-1], BUCKETS[1:]):
            m = (sub["pr"] >= lo) & (sub["pr"] < hi if hi < 1.0 else sub["pr"] <= hi)
            seg = sub.loc[m, "fwd"]
            if len(seg) < 10:
                continue
            out.append({
                "etf": label, "h": h, "bucket": f"{int(lo*100)}-{int(hi*100)}",
                "n": len(seg), "mean_fwd": float(seg.mean()),
                "median_fwd": float(seg.median()), "hit": float((seg > 0).mean()),
            })
    return out


def main():
    vix = load_vix()
    pr = vix_pctrank(vix, PCT_WIN)
    rows = []
    for label, code in ETFS:
        rows += study(label, code, vix, pr)
    res = pd.DataFrame(rows)
    pd.set_option("display.width", 160, "display.max_rows", 200)
    for h in HORIZONS:
        print(f"\n════════ forward {h}d return by 50ETF-VIX percentile bucket (full sample) ════════")
        sub = res[res["h"] == h].copy()
        piv = sub.pivot(index="etf", columns="bucket", values="mean_fwd")
        cnt = sub.pivot(index="etf", columns="bucket", values="n")
        hit = sub.pivot(index="etf", columns="bucket", values="hit")
        print("-- mean fwd return --");   print((piv * 100).round(2))
        print("-- hit rate --");          print((hit * 100).round(1))
        print("-- n --");                 print(cnt.astype("Int64"))
    res.to_csv(BASE / "event_study.csv", index=False)
    print(f"\nWrote {BASE/'event_study.csv'}")

    # spread: top bucket (80-100) minus bottom (0-20), per etf/h — the "add edge"
    print("\n════════ ADD-EDGE = mean_fwd[VIX 80-100%] - mean_fwd[VIX 0-20%] (pp) ════════")
    for h in HORIZONS:
        sub = res[res["h"] == h]
        line = []
        for label, _ in ETFS:
            top = sub[(sub.etf == label) & (sub.bucket == "80-100")]["mean_fwd"]
            bot = sub[(sub.etf == label) & (sub.bucket == "0-20")]["mean_fwd"]
            if len(top) and len(bot):
                line.append(f"{label}={ (top.iloc[0]-bot.iloc[0])*100:+.2f}pp")
        print(f"  h={h:>2}d: " + "  ".join(line))


if __name__ == "__main__":
    main()
