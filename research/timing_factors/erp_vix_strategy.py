"""Combined ERP×VIX-extreme timing strategy.

Matrix (erp_vix_combo.py) showed: ERP sets the medium-term lean; VIX EXTREMES adjust
it; the VIX overlay fixes ERP's OOS flaw (don't reduce 'expensive+panic', only really
reduce 'expensive+complacent'). Build that and test honestly vs ERP-only and buyhold.

  erp_lean = erp_pct3y/100  (0..1, cheap=high)
  base     = LO + (HI-LO)*erp_lean        # capped so 'expensive' isn't fully out
  panic    = z>1.5, held H days  -> add (overrides ERP-expensive)
  complac  = z<-1.5              -> trim (confirms ERP-expensive)
  pos = clip(base + addk*panic_pulse - trimk*complac, 0, 1), T+1, 5bp

Timing test = Sharpe/Calmar vs buyhold (exposure-neutral on the ratio). Select on IS,
confirm OOS. Compare to ERP-only (k=1 linear) to show the VIX adjustment adds value.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np, pandas as pd

R = Path("/home/rooot/agent_invest_lab/research/timing_factors")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIXP = Path("/home/rooot/agent_invest_lab/research/vix_factor/data/vix_50etf.csv")
DATA = R / "data"
IS_END, OOS_START, END = pd.Timestamp("2023-12-31"), pd.Timestamp("2024-01-01"), pd.Timestamp("2026-05-26")
IS_START = pd.Timestamp("2018-03-01")
TARGS = [("HS300", "510300.SH", "000300"), ("ZZ1000", "512100.SH", "000852")]
COMM = 0.0005


def load(code, gz):
    close = pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]
    g = pd.read_csv(DATA / f"gzxjb_{gz}.csv", parse_dates=["date"], index_col="date").sort_index()
    vix = pd.read_csv(VIXP, parse_dates=["date"], index_col="date").sort_index()["vix"]
    vz = (vix - vix.rolling(252, min_periods=126).mean()) / vix.rolling(252, min_periods=126).std()
    pct = g["erp_pct3y"].reindex(close.index, method="ffill") / 100.0
    vz = vz.reindex(close.index, method="ffill")
    return close, pct, vz


def erp_only(pct, vz, k=1.0):
    return (0.5 + k * (pct - 0.5) * 2).clip(0, 1)


def combo(pct, vz, LO=0.35, HI=0.95, addk=0.4, trimk=0.35, H=15):
    base = (LO + (HI - LO) * pct).clip(0, 1)
    panic = (vz > 1.5).astype(float).rolling(H, min_periods=1).max().fillna(0.0)
    complac = (vz < -1.5).astype(float)
    return (base + addk * panic - trimk * complac).clip(0, 1)


def metr(close, pos, a, b):
    pos = pos.reindex(close.index).ffill().fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    p = pos.shift(1).fillna(0.0)
    strat = p * ret - p.diff().abs().fillna(0.0) * COMM
    m = (close.index >= a) & (close.index <= b)
    sr, bh, pe = strat[m], ret[m], p[m]
    if len(sr) < 30: return None
    eq, eqb = (1+sr).cumprod(), (1+bh).cumprod(); n=len(sr)
    shp = sr.mean()/sr.std(ddof=1)*np.sqrt(252) if sr.std()>0 else 0
    bshp = bh.mean()/bh.std(ddof=1)*np.sqrt(252) if bh.std()>0 else 0
    ann=eq.iloc[-1]**(252/n)-1; bann=eqb.iloc[-1]**(252/n)-1
    dd=(eq/eq.cummax()-1).min(); bdd=(eqb/eqb.cummax()-1).min()
    cal=ann/abs(dd) if dd<0 else 0; bcal=bann/abs(bdd) if bdd<0 else 0
    return dict(ret=eq.iloc[-1]-1,bh=eqb.iloc[-1]-1,shp=shp,bshp=bshp,dshp=shp-bshp,
                cal=cal,bcal=bcal,dcal=cal-bcal,dd=dd,exp=pe.mean())


def main():
    dat = {lab: load(code, gz) for lab, code, gz in TARGS}
    strats = {"buyhold": lambda pct,vz: pd.Series(1.0,index=pct.index),
              "erp_only_k1": lambda pct,vz: erp_only(pct,vz,1.0),
              "combo": lambda pct,vz: combo(pct,vz)}
    for split, a, b in [("IS", IS_START, IS_END), ("OOS", OOS_START, END)]:
        print(f"\n{'='*86}\n  [{split}]  {a.date()}..{b.date()}\n{'='*86}")
        print(f"  {'strategy':<14}{'etf':<8}{'ret':>8}{'bh_ret':>8}{'shp':>7}{'bh_shp':>7}{'ΔShp':>7}{'cal':>6}{'ΔCal':>7}{'exp':>6}")
        for sname, fn in strats.items():
            for lab, code, gz in TARGS:
                close, pct, vz = dat[lab]
                m = metr(close, fn(pct, vz), a, b)
                if m:
                    print(f"  {sname:<14}{lab:<8}{m['ret']:>+8.2f}{m['bh']:>+8.2f}{m['shp']:>7.2f}{m['bshp']:>7.2f}{m['dshp']:>+7.2f}{m['cal']:>6.2f}{m['dcal']:>+7.2f}{m['exp']:>6.2f}")
            if sname != "buyhold": print()


if __name__ == "__main__":
    main()
