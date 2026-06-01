"""ERP (slow valuation anchor) × VIX extreme (fast sentiment trigger, tails only).

Idea: ERP sets the medium-term lean (cheap/expensive); VIX EXTREMES adjust it.
Highest conviction when they AGREE:
  cheap (ERP pct high) + panic (VIX z>1.5)        -> strong ADD
  expensive (ERP pct low) + complacent (VIX z<-1.5) -> strong TRIM
We use VIX ONLY at the tails (not continuous — continuous VIX has ~0 IC).

(A) interaction matrix: forward 20d & 60d EXCESS return by ERP tercile × VIX state,
    pooled HS300+ZZ1000, IS and OOS, with n.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np, pandas as pd

R = Path("/home/rooot/agent_invest_lab/research/timing_factors")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIXP = Path("/home/rooot/agent_invest_lab/research/vix_factor/data/vix_50etf.csv")
DATA = R / "data"
IS_END = pd.Timestamp("2023-12-31")
TARGS = [("HS300", "510300.SH", "000300"), ("ZZ1000", "512100.SH", "000852")]


def panel(code, gz):
    close = pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]
    g = pd.read_csv(DATA / f"gzxjb_{gz}.csv", parse_dates=["date"], index_col="date").sort_index()
    vix = pd.read_csv(VIXP, parse_dates=["date"], index_col="date").sort_index()["vix"]
    vz = (vix - vix.rolling(252, min_periods=126).mean()) / vix.rolling(252, min_periods=126).std()
    df = pd.DataFrame(index=close.index)
    df["erp_pct"] = g["erp_pct3y"].reindex(close.index, method="ffill")
    df["vz"] = vz.reindex(close.index, method="ffill")
    for h in (20, 60):
        df[f"fwd{h}"] = close.shift(-h) / close - 1.0
        df[f"unc{h}"] = df[f"fwd{h}"].mean()
    return df


def vix_state(vz):
    return np.where(vz > 1.5, "panic", np.where(vz < -1.5, "complac", "normal"))


def erp_state(pct):
    # fixed valuation buckets (pct is 0-100, higher=cheaper)
    return np.where(pct >= 60, "cheap", np.where(pct <= 30, "expensive", "mid"))


def main():
    panels = {lab: panel(code, gz) for lab, code, gz in TARGS}
    for h in (20, 60):
        for split, lo, hi in [("IS", pd.Timestamp("2000-1-1"), IS_END), ("OOS", IS_END, pd.Timestamp("2100-1-1"))]:
            rows = []
            for lab, code, gz in TARGS:
                p = panels[lab]
                sub = p[(p.index > lo) & (p.index <= hi)][["erp_pct", "vz", f"fwd{h}", f"unc{h}"]].dropna()
                sub = sub.assign(es=erp_state(sub["erp_pct"].values), vs=vix_state(sub["vz"].values),
                                 exc=sub[f"fwd{h}"] - sub[f"unc{h}"])
                rows.append(sub)
            allp = pd.concat(rows)
            print(f"\n{'='*70}\n  fwd{h}d EXCESS return — ERP × VIX  [{split}]  (pooled HS300+ZZ1000)\n{'='*70}")
            hdr = "ERP\\VIX"
            print(f"  {hdr:<12}{'panic':>16}{'normal':>16}{'complac':>16}")
            for es in ["cheap", "mid", "expensive"]:
                row = f"  {es:<12}"
                for vs in ["panic", "normal", "complac"]:
                    seg = allp[(allp.es == es) & (allp.vs == vs)]["exc"]
                    row += f"{(seg.mean()*100 if len(seg)>=5 else float('nan')):>+11.2f}%(n{len(seg):>3})" if len(seg) >= 5 else f"{('n='+str(len(seg))):>16}"
                print(row)


if __name__ == "__main__":
    main()
