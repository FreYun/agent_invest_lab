"""Round 6 — test the user's hypothesis:
   "In a long bear market, extreme panic -> people react slower -> the rebound is
    slower/weaker." i.e. fear-buying should work in UP regimes (sharp V-bounce) but
    fail or rebound slowly in DOWN regimes (grinding bear).

Split FEAR events (z>1.5) by the trailing trend regime at trigger time (causal):
   above MA200            : price above its 200d MA
   MA200 rising/falling   : slope of MA200 over 20d
   grinding bear          : close<MA200 AND MA200 falling
Then look at the rebound *path* across horizons 5/10/20/40/60d in each regime, pooled
across the 3 ETFs (de-overlapped episodes, gap>20d). Excess = minus that ETF's
unconditional mean fwd return at that horizon.
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
HZ = [5, 10, 20, 40, 60]


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

    # regimes defined causally at trigger time
    regimes = {
        "UP  (close>MA200)":         lambda close, ma, slope: close > ma,
        "DOWN(close<MA200)":         lambda close, ma, slope: close < ma,
        "grinding bear (<MA200 & MA200↓)": lambda close, ma, slope: (close < ma) & (slope < 0),
        "early/V  (<MA200 & MA200↑)":      lambda close, ma, slope: (close < ma) & (slope >= 0),
    }

    # collect fear episodes tagged by regime, store fwd-return paths
    buckets = {rn: {h: [] for h in HZ} for rn in regimes}
    counts = {rn: 0 for rn in regimes}
    for label, code, es in ETFS:
        close = load_close(code)
        ma = close.rolling(200).mean()
        slope = ma.diff(20)
        zA = z.reindex(close.index, method="ffill")
        fear = (zA > 1.5) & (close.index >= pd.Timestamp(es)) & ma.notna()
        days = close.index[fear.fillna(False).values]
        starts = episodes(days)
        unc = {h: (close.shift(-h) / close - 1.0).mean() for h in HZ}
        for d in starts:
            i = close.index.get_loc(d)
            c0, m0, s0 = close.iloc[i], ma.iloc[i], slope.iloc[i]
            if pd.isna(m0) or pd.isna(s0):
                continue
            for rn, fn in regimes.items():
                if bool(fn(c0, m0, s0)):
                    counts[rn] += 1
                    for h in HZ:
                        if i + h < len(close):
                            buckets[rn][h].append(close.iloc[i + h] / c0 - 1.0 - unc[h])

    print("="*88)
    print("  FEAR (z>1.5) rebound PATH by trend regime at trigger — pooled 3 ETFs, de-overlapped")
    print("  cells = mean EXCESS fwd return (vs unconditional) at each horizon; want to see if")
    print("  the bounce is fast+strong in UP and slow/weak/absent in grinding bear")
    print("="*88)
    print(f"  {'regime':<34}{'n':>4}" + "".join(f"{'+'+str(h)+'d':>9}" for h in HZ))
    for rn in regimes:
        row = f"  {rn:<34}{counts[rn]:>4}"
        for h in HZ:
            v = buckets[rn][h]
            row += f"{(np.mean(v)*100 if len(v)>=3 else float('nan')):>+8.2f}%" if len(v) >= 3 else f"{'n/a':>9}"
        print(row)
        # win rate at 20d
        v20 = buckets[rn][20]
        if len(v20) >= 3:
            print(f"  {'':<34}{'':>4}  win@20d={ (np.array(v20)>0).mean()*100:.0f}%  median@20d={np.median(v20)*100:+.2f}%")

    # also: raw (non-excess) forward returns, to see absolute rebound speed
    print("\n" + "="*88)
    print("  Same but RAW (absolute) fwd return — is the bear-panic bounce slow in level terms?")
    print("="*88)
    rawb = {rn: {h: [] for h in HZ} for rn in regimes}
    for label, code, es in ETFS:
        close = load_close(code)
        ma = close.rolling(200).mean(); slope = ma.diff(20)
        zA = z.reindex(close.index, method="ffill")
        fear = (zA > 1.5) & (close.index >= pd.Timestamp(es)) & ma.notna()
        starts = episodes(close.index[fear.fillna(False).values])
        for d in starts:
            i = close.index.get_loc(d); c0, m0, s0 = close.iloc[i], ma.iloc[i], slope.iloc[i]
            if pd.isna(m0) or pd.isna(s0):
                continue
            for rn, fn in regimes.items():
                if bool(fn(c0, m0, s0)):
                    for h in HZ:
                        if i + h < len(close):
                            rawb[rn][h].append(close.iloc[i + h] / c0 - 1.0)
    print(f"  {'regime':<34}{'n':>4}" + "".join(f"{'+'+str(h)+'d':>9}" for h in HZ))
    for rn in regimes:
        row = f"  {rn:<34}{counts[rn]:>4}"
        for h in HZ:
            v = rawb[rn][h]
            row += f"{(np.mean(v)*100 if len(v)>=3 else float('nan')):>+8.2f}%" if len(v) >= 3 else f"{'n/a':>9}"
        print(row)


if __name__ == "__main__":
    main()
