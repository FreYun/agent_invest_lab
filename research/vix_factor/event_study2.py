"""Event study v2: the *correct* contrarian triggers.

v1 (percentile bucketing) showed no robust monotonic edge — high percentile
persists through bear markets (catching falling knives). The classic VIX
contrarian edge lives in:
  (a) acute z-score SPIKES (z>1.5, z>2)   — panic, not chronic elevation
  (b) extreme tail (top 5-10%)            — true capitulation
  (c) spike-then-recede                   — VIX rolling over from a local peak

We measure conditional forward return MINUS the unconditional mean (the edge over
just being long), with n, across 3 ETFs and horizons 5/10/20/60d.
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
WIN = 252


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_close(code):
    df = pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()
    return df["close"]


def main():
    vix = load_vix()
    z = (vix - vix.rolling(WIN, min_periods=WIN // 2).mean()) / vix.rolling(WIN, min_periods=WIN // 2).std()
    pr = vix.rolling(WIN, min_periods=WIN // 2).apply(lambda x: (x < x[-1]).mean(), raw=True)
    peak20 = vix.rolling(20).max()
    receding = (vix < peak20 * 0.85) & (pr > 0.7)   # VIX rolled >15% off a recent peak while still elevated

    conds = {
        "z>1.5":         z > 1.5,
        "z>2.0":         z > 2.0,
        "pct>90":        pr > 0.90,
        "pct>95":        pr > 0.95,
        "spike_recede":  receding,
    }

    print(f"{'cond':<14}{'etf':<8}" + "".join(f"h{h:<10}" for h in HORIZONS))
    rows = []
    for cname, cond in conds.items():
        for label, code in ETFS:
            close = load_close(code)
            c = cond.reindex(close.index, method="ffill").fillna(False)
            cells = []
            for h in HORIZONS:
                fwd = close.shift(-h) / close - 1.0
                uncond = fwd.mean()
                seg = fwd[c & fwd.notna()]
                if len(seg) < 8:
                    cells.append(f"  n={len(seg)}")
                    continue
                edge = (seg.mean() - uncond) * 100
                cells.append(f"{edge:+.2f}pp/{len(seg)}")
                rows.append({"cond": cname, "etf": label, "h": h,
                             "n": len(seg), "edge_pp": edge,
                             "mean_fwd": seg.mean() * 100, "uncond": uncond * 100,
                             "hit": (seg > 0).mean() * 100})
            print(f"{cname:<14}{label:<8}" + "".join(f"{x:<11}" for x in cells))
        print()
    pd.DataFrame(rows).to_csv(BASE / "event_study2.csv", index=False)
    print("edge_pp = conditional mean fwd return minus unconditional mean (pp); /N = sample count")
    print(f"Wrote {BASE/'event_study2.csv'}")


if __name__ == "__main__":
    main()
