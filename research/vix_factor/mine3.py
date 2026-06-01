"""Round 3 — continuous VIX exposure modulator (high statistical power).

Rare binary spikes give too few OOS events. A continuous daily exposure rule uses
every bar, so its Sharpe is well-estimated and we can bootstrap the OOS edge.

Timing test (exposure-level-neutral): a constant-exposure book has the SAME Sharpe as
buy&hold, so [strategy Sharpe − buyhold Sharpe] isolates *timing*, regardless of how
much cash the rule holds on average. We select formulation+params on IS (HS300+ZZ1000,
2016-2023) ONLY, then confirm OOS (2024-2026/05, all 3 ETFs), bootstrapping the daily
return series for the OOS Sharpe-difference CI.

Both directions tested honestly:
  contrarian  : high VIX  -> MORE invested (the "加仓" hypothesis)
  voltarget   : high VIX  -> LESS invested (vol-targeting / de-risk foil)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/vix_factor")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIX = BASE / "data" / "vix_50etf.csv"
WIN, COMM = 252, 0.0005
ETFS = [("HS300", "510300.SH", "2016-06-01"), ("ZZ1000", "512100.SH", "2016-06-01"),
        ("SC50", "588800.SH", "2023-09-01")]
IS_END, OOS_START, END = pd.Timestamp("2023-12-31"), pd.Timestamp("2024-01-01"), pd.Timestamp("2026-05-26")


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_close(code):
    return pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]


def vix_feats(vix, idx):
    mu = vix.rolling(WIN, min_periods=WIN // 2).mean()
    sd = vix.rolling(WIN, min_periods=WIN // 2).std()
    z = (vix - mu) / sd
    pctl = vix.rolling(WIN, min_periods=WIN // 2).apply(lambda x: (x < x[-1]).mean(), raw=True)
    zA = z.reindex(idx, method="ffill")
    pA = pctl.reindex(idx, method="ffill")
    vA = vix.reindex(idx, method="ffill")
    return zA, pA, vA


# exposure rules: (zA, pA, vA) -> position series in [0,1]
def rule(name, k, zA, pA, vA):
    if name == "contrarian_z":   # high VIX z -> more invested
        return (0.5 + k * zA).clip(0, 1)
    if name == "contrarian_p":   # high VIX percentile -> more invested
        return (0.5 + k * (pA - 0.5) * 2).clip(0, 1)
    if name == "voltarget":      # k = target annual vol (%); pos = target / VIX
        return (k / vA).clip(0, 1)
    if name == "vix_derisk_z":   # high VIX z -> less invested (foil)
        return (0.7 - k * zA).clip(0, 1)
    raise ValueError(name)


def metrics(close, pos, start, end):
    pos = pos.reindex(close.index).ffill().fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    p = pos.shift(1).fillna(0.0)
    turn = p.diff().abs().fillna(0.0)
    strat = p * ret - turn * COMM
    m = (close.index >= start) & (close.index <= end)
    sr, bh, pe = strat[m], ret[m], p[m]
    if len(sr) < 30:
        return None
    eq, eqb = (1 + sr).cumprod(), (1 + bh).cumprod()
    n = len(sr)
    ann = (eq.iloc[-1]) ** (252 / n) - 1
    shp = sr.mean() / sr.std(ddof=1) * np.sqrt(252) if sr.std() > 0 else 0
    bshp = bh.mean() / bh.std(ddof=1) * np.sqrt(252) if bh.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min(); bdd = (eqb / eqb.cummax() - 1).min()
    cal = ann / abs(dd) if dd < 0 else 0
    bann = (eqb.iloc[-1]) ** (252 / n) - 1
    bcal = bann / abs(bdd) if bdd < 0 else 0
    return dict(ret=float(eq.iloc[-1]-1), bh_ret=float(eqb.iloc[-1]-1), sharpe=float(shp),
                bh_sharpe=float(bshp), d_sharpe=float(shp-bshp), calmar=float(cal),
                bh_calmar=float(bcal), d_calmar=float(cal-bcal), maxdd=float(dd),
                exposure=float(pe.mean()), n=n, sr=sr, bh=bh)


GRID = {
    "contrarian_z": [0.1, 0.15, 0.2, 0.25, 0.3, 0.4],
    "contrarian_p": [0.2, 0.3, 0.4, 0.5, 0.6],
    "voltarget":    [12, 15, 18, 20, 25],
    "vix_derisk_z": [0.1, 0.15, 0.2, 0.25, 0.3],
}


def main():
    vix = load_vix()
    feats = {}
    for label, code, es in ETFS:
        close = load_close(code)
        feats[code] = (close, *vix_feats(vix, close.index))

    # ─── IS selection (HS300 + ZZ1000) ──────────────────────────────────────
    print("="*84)
    print("  STEP 1 — IS (2016-2023) selection on HS300+ZZ1000 — mean ΔSharpe vs buyhold")
    print("="*84)
    is_rank = []
    for name, ks in GRID.items():
        for k in ks:
            ds, exps = [], []
            for label, code, es in ETFS[:2]:
                close, zA, pA, vA = feats[code]
                pos = rule(name, k, zA, pA, vA)
                m = metrics(close, pos, pd.Timestamp(es), IS_END)
                if m: ds.append(m["d_sharpe"]); exps.append(m["exposure"])
            is_rank.append((name, k, float(np.mean(ds)), float(np.min(ds)), float(np.mean(exps))))
    is_rank.sort(key=lambda r: r[2], reverse=True)
    print(f"  {'rule':<14}{'k':>6}{'IS_meanΔShp':>13}{'IS_minΔShp':>12}{'avg_exp':>9}")
    for name, k, mean_d, min_d, exp in is_rank[:12]:
        print(f"  {name:<14}{k:>6}{mean_d:>+13.3f}{min_d:>+12.3f}{exp:>9.2f}")
    best = is_rank[0]
    print(f"\n  >>> IS winner: {best[0]} k={best[1]}  (IS meanΔSharpe {best[2]:+.3f})")

    # ─── OOS confirmation (all 3 ETFs) + bootstrap ──────────────────────────
    name, k = best[0], best[1]
    print("\n" + "="*84)
    print(f"  STEP 2 — OOS (2024-01..2026-05) confirm: {name} k={k}, all 3 ETFs")
    print("="*84)
    print(f"  {'etf':<8}{'ret':>9}{'bh_ret':>9}{'sharpe':>8}{'bh_shp':>8}{'ΔShp':>8}{'calmar':>8}{'bh_cal':>8}{'ΔCal':>7}{'exp':>6}{'  bootΔShp 95%CI':>20}")
    rng = np.random.default_rng(42)
    for label, code, es in ETFS:
        close, zA, pA, vA = feats[code]
        pos = rule(name, k, zA, pA, vA)
        m = metrics(close, pos, OOS_START, END)
        if not m:
            print(f"  {label:<8} (insufficient OOS)"); continue
        # bootstrap the daily ΔSharpe (strat - bh) via block resample of the diff... use iid here
        diff = (m["sr"] - m["bh"]).values
        n = len(diff)
        boots = []
        for _ in range(2000):
            s = m["sr"].values; b = m["bh"].values
            idx = rng.integers(0, n, n)
            ss, bb = s[idx], b[idx]
            shp = ss.mean()/ss.std()*np.sqrt(252) if ss.std()>0 else 0
            bshp = bb.mean()/bb.std()*np.sqrt(252) if bb.std()>0 else 0
            boots.append(shp - bshp)
        lo, hi = np.quantile(boots, [0.025, 0.975])
        print(f"  {label:<8}{m['ret']:>+9.3f}{m['bh_ret']:>+9.3f}{m['sharpe']:>8.2f}{m['bh_sharpe']:>8.2f}"
              f"{m['d_sharpe']:>+8.2f}{m['calmar']:>8.2f}{m['bh_calmar']:>8.2f}{m['d_calmar']:>+7.2f}{m['exposure']:>6.2f}"
              f"  [{lo:+.2f},{hi:+.2f}]")

    # also show the top-3 IS rules' OOS to check the IS winner isn't a fluke
    print("\n  --- robustness: OOS mean ΔSharpe across 3 ETFs for IS top-6 rules ---")
    for nm, kk, *_ in is_rank[:6]:
        ds = []
        for label, code, es in ETFS:
            close, zA, pA, vA = feats[code]
            m = metrics(close, rule(nm, kk, zA, pA, vA), OOS_START, END)
            if m: ds.append(m["d_sharpe"])
        print(f"    {nm:<14}k={kk:<5} OOS meanΔShp={np.mean(ds):+.3f}  per-etf={[round(x,2) for x in ds]}")


if __name__ == "__main__":
    main()
