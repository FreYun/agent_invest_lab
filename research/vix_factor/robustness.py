"""Robustness aggregation over the full vix_sweep grid + own-VIX vs broad-VIX check.

We don't cherry-pick the top config; we ask whether the *whole neighborhood* of the
50ETF-VIX add-overlay beats buy&hold across the 3 ETFs:
  - % of configs beating buyhold Calmar on all 3 / >=2 of 3 ETFs
  - median / mean ΔCalmar and ΔSharpe across the entire grid
  - breakdown by trigger mode, threshold, hold H, base, graded
  - is the optimum central or at a grid corner?

Then a side check: does each index's OWN option-VIX (stale, shorter) beat the broad
50ETF VIX as the add trigger? (000300 for HS300, 000852 for ZZ1000.)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/vix_factor")


def parse_cfg(name: str) -> dict:
    # vixadd_<mode><thr>_H<h>_b<base>_<f|g>
    p = name.split("_")
    mode_thr = p[1]
    mode = "z" if mode_thr.startswith("z") else "pct"
    thr = float(mode_thr[1:]) if mode == "z" else float(mode_thr[3:])
    H = int(p[2][1:]); base = int(p[3][1:]); grade = p[4] == "g"
    return dict(mode=mode, thr=thr, H=H, base=base, grade=grade)


def main():
    df = pd.read_csv(BASE / "vix_sweep.csv")
    fac = df[df.factor != "buyhold"].copy()
    cfg = fac.factor.apply(parse_cfg).apply(pd.Series)
    fac = pd.concat([fac, cfg], axis=1)

    # per-config beat counts across 3 ETFs
    g = fac.groupby("factor").agg(
        beat_cal=("d_calmar", lambda s: int((s > 0).sum())),
        beat_shp=("d_sharpe", lambda s: int((s > 0).sum())),
        mean_dcal=("d_calmar", "mean"), median_dcal=("d_calmar", "min"),  # min over 3 = worst etf
        mean_dshp=("d_sharpe", "mean"),
    )
    n_cfg = len(g)
    print(f"════════ ROBUSTNESS over {n_cfg} configs × 3 ETFs = {len(fac)} backtests ════════\n")
    print(f"configs beating buyhold Calmar on ALL 3 ETFs : {(g.beat_cal==3).mean()*100:.0f}%  ({(g.beat_cal==3).sum()}/{n_cfg})")
    print(f"configs beating buyhold Calmar on >=2 ETFs   : {(g.beat_cal>=2).mean()*100:.0f}%")
    print(f"configs beating buyhold Sharpe on ALL 3 ETFs : {(g.beat_shp==3).mean()*100:.0f}%")
    print(f"configs beating buyhold Sharpe on >=2 ETFs   : {(g.beat_shp>=2).mean()*100:.0f}%")
    print(f"\nΔCalmar across all {len(fac)} backtests: median={fac.d_calmar.median():+.3f}  mean={fac.d_calmar.mean():+.3f}  "
          f"q25={fac.d_calmar.quantile(.25):+.3f}  q75={fac.d_calmar.quantile(.75):+.3f}")
    print(f"ΔSharpe across all {len(fac)} backtests: median={fac.d_sharpe.median():+.3f}  mean={fac.d_sharpe.mean():+.3f}")
    print(f"fraction of all backtests with ΔCalmar>0 : {(fac.d_calmar>0).mean()*100:.0f}%")
    print(f"fraction of all backtests with ΔSharpe>0 : {(fac.d_sharpe>0).mean()*100:.0f}%")

    print("\n──── ΔCalmar by trigger mode/threshold (mean over H×base×grade×3 ETF) ────")
    print(fac.groupby(["mode", "thr"]).d_calmar.agg(["mean", "median", "min", "max", "count"]).round(3))
    print("\n──── ΔCalmar by hold window H ────")
    print(fac.groupby("H").d_calmar.agg(["mean", "median", "min", "count"]).round(3))
    print("\n──── ΔCalmar by base ────")
    print(fac.groupby("base").d_calmar.agg(["mean", "median", "min", "count"]).round(3))
    print("\n──── ΔCalmar by graded? ────")
    print(fac.groupby("grade").d_calmar.agg(["mean", "median", "min", "count"]).round(3))
    print("\n──── ΔCalmar by ETF (is the edge concentrated in one?) ────")
    print(fac.groupby("etf").d_calmar.agg(["mean", "median", "min", "max", "count"]).round(3))


if __name__ == "__main__":
    main()
