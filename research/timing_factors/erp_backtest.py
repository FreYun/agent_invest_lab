"""ERP (股债性价比 3y-percentile) exposure-tilt timing backtest.

Slow valuation signal -> tilt exposure: cheap(high ERP pct) overweight, expensive
underweight. pos = clip(0.5 + k*(pct-0.5)*2, lo, hi), T+1 entry, 5bp.
Timing test = Sharpe vs buyhold (constant-exposure Sharpe == buyhold, so beating it
is timing, not the exposure tilt). Select k on IS, confirm OOS, both indices.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np, pandas as pd

R = Path("/home/rooot/agent_invest_lab/research/timing_factors")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
DATA = R / "data"
IS_END, OOS_START, END = pd.Timestamp("2023-12-31"), pd.Timestamp("2024-01-01"), pd.Timestamp("2026-05-26")
TARGS = [("HS300", "510300.SH", "000300"), ("ZZ1000", "512100.SH", "000852")]
COMM = 0.0005


def series(code, gz):
    close = pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]
    g = pd.read_csv(DATA / f"gzxjb_{gz}.csv", parse_dates=["date"], index_col="date").sort_index()
    pct = (g["erp_pct3y"].reindex(close.index, method="ffill")) / 100.0
    return close, pct


def metr(close, pos, start, end):
    pos = pos.reindex(close.index).ffill().fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    p = pos.shift(1).fillna(0.0)
    strat = p * ret - p.diff().abs().fillna(0.0) * COMM
    m = (close.index >= start) & (close.index <= end)
    sr, bh, pe = strat[m], ret[m], p[m]
    if len(sr) < 30: return None
    eq, eqb = (1+sr).cumprod(), (1+bh).cumprod(); n=len(sr)
    shp = sr.mean()/sr.std(ddof=1)*np.sqrt(252) if sr.std()>0 else 0
    bshp = bh.mean()/bh.std(ddof=1)*np.sqrt(252) if bh.std()>0 else 0
    ann = eq.iloc[-1]**(252/n)-1; bann = eqb.iloc[-1]**(252/n)-1
    dd = (eq/eq.cummax()-1).min(); bdd=(eqb/eqb.cummax()-1).min()
    cal = ann/abs(dd) if dd<0 else 0; bcal = bann/abs(bdd) if bdd<0 else 0
    return dict(ret=eq.iloc[-1]-1, bh_ret=eqb.iloc[-1]-1, sharpe=shp, bh_sharpe=bshp,
                d_sharpe=shp-bshp, calmar=cal, bh_calmar=bcal, d_calmar=cal-bcal,
                maxdd=dd, exp=pe.mean())


def main():
    dat = {lab: series(code, gz) for lab, code, gz in TARGS}
    KS = [0.3, 0.5, 0.7, 1.0]
    print("="*78)
    print("  IS (2018-2023) select k — pos=clip(0.5+k*(ERPpct-0.5)*2,0,1), mean ΔSharpe")
    print("="*78)
    rows=[]
    for k in KS:
        ds=[]
        for lab, code, gz in TARGS:
            close,pct=dat[lab]; pos=(0.5+k*(pct-0.5)*2).clip(0,1)
            m=metr(close,pos,pd.Timestamp("2018-03-01"),IS_END)
            if m: ds.append(m["d_sharpe"])
        rows.append((k,np.mean(ds),np.min(ds))); print(f"  k={k:<4} IS meanΔShp={np.mean(ds):+.3f} minΔShp={np.min(ds):+.3f}")
    bestk=max(rows,key=lambda r:r[1])[0]
    print(f"\n  >>> IS-best k={bestk}")
    print("\n" + "="*78)
    print(f"  OOS (2024-2026) confirm @ k={bestk}")
    print("="*78)
    print(f"  {'etf':<8}{'ret':>8}{'bh_ret':>8}{'shp':>7}{'bh_shp':>7}{'ΔShp':>7}{'cal':>6}{'bh_cal':>7}{'ΔCal':>7}{'exp':>6}")
    for split,a,b in [("IS",pd.Timestamp("2018-03-01"),IS_END),("OOS",OOS_START,END)]:
        print(f"  --- {split} ---")
        for lab, code, gz in TARGS:
            close,pct=dat[lab]; pos=(0.5+bestk*(pct-0.5)*2).clip(0,1)
            m=metr(close,pos,a,b)
            if m: print(f"  {lab:<8}{m['ret']:>+8.2f}{m['bh_ret']:>+8.2f}{m['sharpe']:>7.2f}{m['bh_sharpe']:>7.2f}{m['d_sharpe']:>+7.2f}{m['calmar']:>6.2f}{m['bh_calmar']:>7.2f}{m['d_calmar']:>+7.2f}{m['exp']:>6.2f}")


if __name__ == "__main__":
    main()
