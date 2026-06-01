"""Round 5 — verify the EUPHORIA tail (extreme-low VIX -> trim) is robust, not one
clustered regime. De-overlap into episodes (gap>20d), break down per calendar year,
and confirm direction consistency. Also re-confirm the FEAR tail's regime dependence.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/vix_factor")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIX = BASE / "data" / "vix_50etf.csv"
WIN = 252
ETFS = [("HS300", "510300.SH", "2016-06-01"), ("ZZ1000", "512100.SH", "2016-06-01"),
        ("SC50", "588800.SH", "2023-09-01")]
H = 20


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_close(code):
    return pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]


def zser(vix):
    mu = vix.rolling(WIN, min_periods=WIN // 2).mean()
    sd = vix.rolling(WIN, min_periods=WIN // 2).std()
    return (vix - mu) / sd


def episodes(dates, gap=20):
    starts, prev = [], None
    for d in dates:
        if prev is None or (d - prev).days > gap:
            starts.append(d)
        prev = d
    return starts


def main():
    vix = load_vix(); z = zser(vix)

    for tag, cond_fn in [("FEAR z>1.5", lambda zA: zA > 1.5),
                         ("EUPHORIA z<-1.5", lambda zA: zA < -1.5)]:
        print(f"\n{'='*78}\n  {tag}  — de-overlapped episodes (gap>20d), 20d excess fwd return\n{'='*78}")
        # per-year pooled across ETFs
        by_year = {}
        all_ep = []
        for label, code, es in ETFS:
            close = load_close(code)
            zA = z.reindex(close.index, method="ffill")
            cond = cond_fn(zA) & (close.index >= pd.Timestamp(es))
            days = close.index[cond.fillna(False).values]
            starts = episodes(days)
            uncond = (close.shift(-H) / close - 1.0).mean()
            for d in starts:
                seg = close[close.index >= d]
                if len(seg) > H:
                    exc = (seg.iloc[H] / seg.iloc[0] - 1.0) - uncond
                    by_year.setdefault(d.year, []).append(exc)
                    all_ep.append((d, label, exc))
        print(f"  {'year':<6}{'n_episodes':>11}{'mean_excess':>13}{'win%':>7}")
        for yr in sorted(by_year):
            v = np.array(by_year[yr])
            print(f"  {yr:<6}{len(v):>11}{v.mean()*100:>+12.2f}%{(v>0).mean()*100:>6.0f}%")
        allv = np.array([e[2] for e in all_ep])
        n_yr_correct = sum(1 for yr in by_year if (tag.startswith("FEAR")) == (np.mean(by_year[yr]) > 0))
        print(f"  ----  total episodes={len(allv)}  mean={allv.mean()*100:+.2f}%  "
              f"years with expected sign: {n_yr_correct}/{len(by_year)}")


if __name__ == "__main__":
    main()
