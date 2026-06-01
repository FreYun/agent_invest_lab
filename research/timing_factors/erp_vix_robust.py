"""Robustness sweep of the ERP×VIX combo params — is the OOS edge a lucky corner or
a broad neighborhood? Params chosen by hand from the matrix; this checks they aren't
cherry-picked. For each config: IS & OOS mean/min ΔSharpe over HS300+ZZ1000, exposure.
Pass bar: a large fraction of configs beat buyhold OOS Sharpe on BOTH indices, with
non-degenerate exposure, AND positive IS.
"""
from __future__ import annotations
from pathlib import Path
import itertools, numpy as np, pandas as pd

R = Path("/home/rooot/agent_invest_lab/research/timing_factors")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIXP = Path("/home/rooot/agent_invest_lab/research/vix_factor/data/vix_50etf.csv")
DATA = R / "data"
IS_START, IS_END = pd.Timestamp("2018-03-01"), pd.Timestamp("2023-12-31")
OOS_START, END = pd.Timestamp("2024-01-01"), pd.Timestamp("2026-05-26")
TARGS = [("HS300", "510300.SH", "000300"), ("ZZ1000", "512100.SH", "000852")]
COMM = 0.0005


def load(code, gz):
    close = pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]
    g = pd.read_csv(DATA / f"gzxjb_{gz}.csv", parse_dates=["date"], index_col="date").sort_index()
    vix = pd.read_csv(VIXP, parse_dates=["date"], index_col="date").sort_index()["vix"]
    vz = (vix - vix.rolling(252, min_periods=126).mean()) / vix.rolling(252, min_periods=126).std()
    return close, g["erp_pct3y"].reindex(close.index, method="ffill")/100.0, vz.reindex(close.index, method="ffill")


def combo(pct, vz, LO, HI, addk, trimk, H):
    base = (LO + (HI - LO) * pct).clip(0, 1)
    panic = (vz > 1.5).astype(float).rolling(H, min_periods=1).max().fillna(0.0)
    complac = (vz < -1.5).astype(float)
    return (base + addk * panic - trimk * complac).clip(0, 1)


def dshp(close, pos, a, b):
    pos = pos.reindex(close.index).ffill().fillna(0.0)
    ret = close.pct_change().fillna(0.0); p = pos.shift(1).fillna(0.0)
    strat = p*ret - p.diff().abs().fillna(0.0)*COMM
    m = (close.index>=a)&(close.index<=b); sr,bh,pe = strat[m],ret[m],p[m]
    if len(sr)<30 or sr.std()==0 or bh.std()==0: return None
    return (sr.mean()/sr.std(ddof=1)-bh.mean()/bh.std(ddof=1))*np.sqrt(252), pe.mean()


def main():
    dat = {lab: load(code, gz) for lab, code, gz in TARGS}
    grid = list(itertools.product([0.30,0.35,0.40],[0.85,0.95,1.0],[0.3,0.4,0.5],[0.25,0.35,0.45],[10,15,20]))
    rows = []
    for LO,HI,addk,trimk,H in grid:
        isd, ood, exps = [], [], []
        for lab, code, gz in TARGS:
            close, pct, vz = dat[lab]; pos = combo(pct, vz, LO, HI, addk, trimk, H)
            ri = dshp(close, pos, IS_START, IS_END); ro = dshp(close, pos, OOS_START, END)
            if ri and ro: isd.append(ri[0]); ood.append(ro[0]); exps.append(ro[1])
        if len(isd) == 2:
            rows.append(dict(LO=LO,HI=HI,addk=addk,trimk=trimk,H=H,
                             is_min=min(isd), is_mean=np.mean(isd),
                             oos_min=min(ood), oos_mean=np.mean(ood), exp=np.mean(exps)))
    df = pd.DataFrame(rows)
    n = len(df)
    print(f"==== ERP×VIX combo robustness: {n} configs × 2 indices ====\n")
    print(f"  beat buyhold OOS Sharpe on BOTH indices : {(df.oos_min>0).mean()*100:.0f}%  ({(df.oos_min>0).sum()}/{n})")
    print(f"  beat OOS on both AND IS on both         : {((df.oos_min>0)&(df.is_min>0)).mean()*100:.0f}%")
    print(f"  OOS meanΔShp: median={df.oos_mean.median():+.3f}  q25={df.oos_mean.quantile(.25):+.3f}  q75={df.oos_mean.quantile(.75):+.3f}")
    print(f"  IS  meanΔShp: median={df.is_mean.median():+.3f}")
    print(f"  exposure OOS: median={df.exp.median():.2f}  range=[{df.exp.min():.2f},{df.exp.max():.2f}]")
    print(f"\n  by param — OOS meanΔShp (mean over other params):")
    for col in ["LO","HI","addk","trimk","H"]:
        agg = df.groupby(col).oos_mean.mean()
        print(f"    {col:<6}: " + "  ".join(f"{k}={v:+.3f}" for k,v in agg.items()))
    print("\n  worst 3 configs (OOS min):")
    print(df.nsmallest(3,"oos_min")[["LO","HI","addk","trimk","H","is_min","oos_min","exp"]].to_string(index=False))
    print("\n  best 3 configs (OOS mean):")
    print(df.nlargest(3,"oos_mean")[["LO","HI","addk","trimk","H","is_mean","oos_mean","exp"]].to_string(index=False))


if __name__ == "__main__":
    main()
