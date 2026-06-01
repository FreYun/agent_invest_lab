"""VIX-extreme add-position overlay — first-round sweep on 3 ETFs.

Factor = a tactical ADD overlay on a partially-invested book:
  pos = base   (normal)
  pos = 1.0    for H trading days after the 50ETF-VIX hits an extreme trigger
(optionally graded by how extreme z is). T+1 entry via sig.shift(1) in backtest().

Why this shape: event_study2 showed acute VIX panic (z>2 / pct>95) earns a positive
5-20d forward return across HS300/ZZ1000/SC50, but a NEGATIVE 60d return — so the
edge is a short tactical add, held a few weeks, not a long hold.

Timing test: a constant-exposure book has the SAME Sharpe/Calmar as buy&hold (scaling
exposure scales return and DD proportionally). So if the add-overlay beats buy&hold on
Calmar/Sharpe, the *timing* (not just the exposure tilt) is adding risk-adjusted value.
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/vix_factor")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIX = BASE / "data" / "vix_50etf.csv"
ETFS = [("HS300", "510300.SH"), ("ZZ1000", "512100.SH"), ("SC50", "588800.SH")]
COMMISSION = 0.0005
WIN = 252
# HS300/ZZ1000 have ETF data from 2016; SC50 from 2023. Use a common eval start per ETF.
EVAL_START = {"510300.SH": "2016-06-01", "512100.SH": "2016-06-01", "588800.SH": "2023-09-01"}
EVAL_END = "2026-05-26"


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_etf(code):
    df = pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()
    df.index.name = "date"
    return df


def vix_features(vix: pd.Series):
    mu = vix.rolling(WIN, min_periods=WIN // 2).mean()
    sd = vix.rolling(WIN, min_periods=WIN // 2).std()
    z = (vix - mu) / sd
    pr = vix.rolling(WIN, min_periods=WIN // 2).apply(lambda x: (x < x[-1]).mean(), raw=True)
    return z, pr


def hold_pulse(trigger: pd.Series, H: int) -> pd.Series:
    """1.0 on the trigger day and the following H-1 days (a max over a forward... no:
    pulse must be causal — active on days where a trigger fired within the last H days)."""
    return trigger.astype(float).rolling(H, min_periods=1).max().fillna(0.0)


def make_signal(z, pr, idx, *, mode, thr, H, base, grade=False, gcap=3.0):
    if mode == "z":
        trig = (z > thr)
        strength = ((z - thr) / (gcap - thr)).clip(0, 1) if grade else None
    else:  # pct
        trig = (pr > thr)
        strength = ((pr - thr) / (1.0 - thr)).clip(0, 1) if grade else None
    pulse = hold_pulse(trig, H)
    if grade:
        # graded add: how far above baseline, scaled by extremeness held over window
        gstr = (strength.where(trig, 0.0)).rolling(H, min_periods=1).max().fillna(0.0)
        add = (1.0 - base) * gstr
    else:
        add = (1.0 - base) * pulse
    sig = (base + add).clip(0, 1)
    return sig.reindex(idx, method="ffill").fillna(base)


def backtest(close: pd.Series, sig: pd.Series, eval_start, eval_end):
    sig = sig.clip(0, 1).reindex(close.index).ffill().fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    turnover = pos.diff().abs().fillna(pos.iloc[0])
    strat = pos * ret - turnover * COMMISSION
    mask = (close.index >= pd.Timestamp(eval_start)) & (close.index <= pd.Timestamp(eval_end))
    sr, pe, bh = strat[mask], pos[mask], ret[mask]
    if sr.empty:
        return None
    eq = (1 + sr).cumprod(); eqb = (1 + bh).cumprod()
    n = len(sr)
    tot = float(eq.iloc[-1] - 1); ann = float((1 + tot) ** (252 / n) - 1)
    sd = sr.std(ddof=1); shp = float(sr.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    dd = float((eq / eq.cummax() - 1).min()); cal = float(ann / abs(dd)) if dd < 0 else 0.0
    bsd = bh.std(ddof=1); bshp = float(bh.mean() / bsd * np.sqrt(252)) if bsd > 0 else 0.0
    bdd = float((eqb / eqb.cummax() - 1).min())
    btot = float(eqb.iloc[-1] - 1); bann = float((1 + btot) ** (252 / n) - 1)
    bcal = float(bann / abs(bdd)) if bdd < 0 else 0.0
    flips = int((turnover[mask] > 1e-9).sum())
    yrs = n / 252
    return dict(ret=tot, ann=ann, sharpe=shp, maxdd=dd, calmar=cal,
                exposure=float(pe.mean()), flips_per_yr=flips / yrs,
                bh_ret=btot, bh_sharpe=bshp, bh_maxdd=bdd, bh_calmar=bcal,
                d_sharpe=shp - bshp, d_calmar=cal - bcal, bars=n)


GRID = []
for mode, thrs in [("z", [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]), ("pct", [0.80, 0.85, 0.90, 0.95])]:
    for thr in thrs:
        for H in [3, 5, 10, 20]:
            for base in [0.3, 0.4, 0.5, 0.6, 0.7]:
                for grade in [False]:
                    g = "g" if grade else "f"
                    name = f"vixadd_{mode}{thr}_H{H}_b{int(base*100)}_{g}"
                    GRID.append((name, dict(mode=mode, thr=thr, H=H, base=base, grade=grade)))


def main():
    vix = load_vix()
    z, pr = vix_features(vix)
    rows = []
    for label, code in ETFS:
        close = load_etf(code)["close"]
        es, ee = EVAL_START[code], EVAL_END
        bh = backtest(close, pd.Series(1.0, index=close.index), es, ee)
        rows.append([label, code, "buyhold"] + [bh[k] for k in COLS])
        for name, kw in GRID:
            sig = make_signal(z, pr, close.index, **kw)
            m = backtest(close, sig, es, ee)
            if m:
                rows.append([label, code, name] + [m[k] for k in COLS])
    df = pd.DataFrame(rows, columns=["etf", "code", "factor"] + COLS)
    df.to_csv(BASE / "vix_sweep.csv", index=False)

    # rank: factors that beat buyhold Calmar on ALL 3 etfs, by mean d_calmar
    fac = df[df.factor != "buyhold"].copy()
    agg = fac.groupby("factor").agg(
        mean_dcal=("d_calmar", "mean"), min_dcal=("d_calmar", "min"),
        mean_dshp=("d_sharpe", "mean"), min_dshp=("d_sharpe", "min"),
        mean_exp=("exposure", "mean"), mean_flips=("flips_per_yr", "mean"),
        beat_cal=("d_calmar", lambda s: int((s > 0).sum())),
        beat_shp=("d_sharpe", lambda s: int((s > 0).sum())),
    ).reset_index()
    agg = agg.sort_values("mean_dcal", ascending=False)
    pd.set_option("display.width", 200, "display.max_rows", 120)
    print("════════ factor ranking (vs buyhold, across 3 ETFs) — top 25 by mean ΔCalmar ════════")
    print(agg.head(25).to_string(index=False,
          formatters={c: "{:+.3f}".format for c in ["mean_dcal", "min_dcal", "mean_dshp", "min_dshp"]}))
    print("\nbeat_cal/beat_shp out of 3 ETFs; mean_exp=avg exposure; mean_flips=flips/yr")
    agg.to_csv(BASE / "vix_sweep_agg.csv", index=False)
    print(f"\nWrote {BASE/'vix_sweep.csv'} and vix_sweep_agg.csv")


COLS = ["ret", "ann", "sharpe", "maxdd", "calmar", "exposure", "flips_per_yr",
        "bh_ret", "bh_sharpe", "bh_maxdd", "bh_calmar", "d_sharpe", "d_calmar", "bars"]

if __name__ == "__main__":
    main()
