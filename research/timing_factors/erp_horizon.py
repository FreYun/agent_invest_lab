"""Focused: equity-bond risk premium (gzxjb ERP) was the standout in timing_ic.
Valuation/ERP signals work at LONG horizons. Test ERP raw + 3y-percentile IC across
20/60/120/250d forward returns, IS vs OOS, per index + pooled, with non-overlapping
significance (sample every H days) and a permutation p-value.
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

R = Path("/home/rooot/agent_invest_lab/research/timing_factors")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
DATA = R / "data"
IS_END = pd.Timestamp("2023-12-31")
TARGS = [("HS300", "510300.SH", "000300"), ("ZZ1000", "512100.SH", "000852")]
HZ = [20, 60, 120, 250]
rng = np.random.default_rng(13)


def panel(code, gz):
    close = pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]
    g = pd.read_csv(DATA / f"gzxjb_{gz}.csv", parse_dates=["date"], index_col="date").sort_index()
    df = pd.DataFrame(index=close.index)
    df["erp"] = g["erp"].reindex(close.index, method="ffill")
    df["erp_pct3y"] = g["erp_pct3y"].reindex(close.index, method="ffill")
    for h in HZ:
        df[f"fwd{h}"] = close.shift(-h) / close - 1.0
    return df


def ic_p(x, y, h):
    """non-overlapping IC + permutation p (one-sided, expecting +)."""
    s = pd.concat([x, y], axis=1).dropna().iloc[::h]
    if len(s) < 10:
        return np.nan, np.nan, len(s)
    a, b = s.iloc[:, 0], s.iloc[:, 1]
    ic = a.corr(b, method="spearman")
    null = [a.corr(pd.Series(rng.permutation(b.values), index=b.index), method="spearman") for _ in range(3000)]
    return ic, (np.array(null) >= ic).mean(), len(s)


def main():
    panels = {lab: panel(code, gz) for lab, code, gz in TARGS}
    for feat in ["erp", "erp_pct3y"]:
        print("="*92)
        print(f"  ERP feature = '{feat}'  — non-overlapping Spearman IC vs forward return  (higher ERP=cheaper=bullish)")
        print("="*92)
        print(f"  {'horizon':<9}" + "".join(f"{lab+'_IS(p)':>17}{lab+'_OOS(p)':>17}" for lab, *_ in TARGS) + f"{'POOLED_IS(p)':>16}{'POOLED_OOS(p)':>16}")
        for h in HZ:
            row = f"  fwd{h:<6}"
            pooled = {"IS": ([], []), "OOS": ([], [])}
            for lab, code, gz in TARGS:
                p = panels[lab]
                for split, lo, hi in [("IS", pd.Timestamp("2000-1-1"), IS_END), ("OOS", IS_END, pd.Timestamp("2100-1-1"))]:
                    sub = p[(p.index > lo) & (p.index <= hi)][[feat, f"fwd{h}"]].dropna()
                    ic, pv, n = ic_p(sub[feat], sub[f"fwd{h}"], h)
                    row += f"{ic:>+11.2f}({pv:.2f}){'':>1}" if not np.isnan(ic) else f"{'n/a':>17}"
                    s2 = sub.iloc[::h]
                    pooled[split][0].extend(s2[feat].tolist()); pooled[split][1].extend(s2[f"fwd{h}"].tolist())
            for split in ["IS", "OOS"]:
                xs, ys = np.array(pooled[split][0]), np.array(pooled[split][1])
                if len(xs) >= 10:
                    ic = pd.Series(xs).corr(pd.Series(ys), method="spearman")
                    null = [pd.Series(xs).corr(pd.Series(rng.permutation(ys)), method="spearman") for _ in range(3000)]
                    pv = (np.array(null) >= ic).mean()
                    row += f"{ic:>+10.2f}({pv:.2f})"
                else:
                    row += f"{'n/a':>16}"
            print(row)
        print()


if __name__ == "__main__":
    main()
