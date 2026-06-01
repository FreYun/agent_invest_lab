"""Which continuous PIT signals actually predict forward index returns?

Daily signals (gzxjb ERP, market temperature + subfactors, VIX z, MA200 dev, momentum)
have high power, unlike rare VIX events. We measure:
  (1) Spearman IC = corr(signal_t, fwd-Hd-return_t), full sample (descriptive), IS & OOS.
  (2) Significance on NON-OVERLAPPING H-day samples (every Hth obs) -> honest t/p, since
      overlapping forward windows massively inflate naive t-stats.
  (3) Multivariate OLS fit on IS (HS300+ZZ1000 pooled) -> OUT-OF-SAMPLE R² and the
      combined signal's OOS IC. This is the real "combined timing power" test.

PIT/causal: every signal is as-of T (15:00 visible); target is close_T -> close_{T+H};
entry would be T+1 in a live book (handled later). No future data in features.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

R = Path("/home/rooot/agent_invest_lab/research/timing_factors")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIXP = Path("/home/rooot/agent_invest_lab/research/vix_factor/data/vix_50etf.csv")
DATA = R / "data"
IS_END = pd.Timestamp("2023-12-31")
H = 20
# (etf label, etf code, gzxjb index code)
TARGS = [("HS300", "510300.SH", "000300"), ("ZZ1000", "512100.SH", "000852")]


def rd(p, **kw):
    return pd.read_csv(p, **kw)


def build(code, gz):
    close = rd(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]
    idx = close.index
    # features
    g = rd(DATA / f"gzxjb_{gz}.csv", parse_dates=["date"], index_col="date").sort_index()
    t = rd(DATA / "temperature.csv", parse_dates=["date"], index_col="date").sort_index()
    vix = rd(VIXP, parse_dates=["date"], index_col="date").sort_index()["vix"]
    vz = (vix - vix.rolling(252, min_periods=126).mean()) / vix.rolling(252, min_periods=126).std()
    ma200 = close.rolling(200).mean()
    feats = pd.DataFrame(index=idx)
    feats["erp"] = g["erp"].reindex(idx, method="ffill")
    feats["erp_pct3y"] = g["erp_pct3y"].reindex(idx, method="ffill")
    feats["temp"] = t["temp"].reindex(idx, method="ffill")
    feats["temp_pct3m"] = t["temp_pct3m"].reindex(idx, method="ffill")
    feats["f_newhilo"] = t["f_newhilo"].reindex(idx, method="ffill")
    feats["f_basis"] = t["f_basis"].reindex(idx, method="ffill")
    feats["f_pxvol"] = t["f_pxvol"].reindex(idx, method="ffill")
    feats["f_north"] = t["f_north"].reindex(idx, method="ffill")
    feats["vix_z"] = vz.reindex(idx, method="ffill")
    feats["ma200_dev"] = close / ma200 - 1.0
    feats["mom120"] = close / close.shift(120) - 1.0
    feats["fwd"] = close.shift(-H) / close - 1.0
    return feats


# expected sign of IC (contrarian/value signals)
EXP = {"erp": +1, "erp_pct3y": +1, "temp": -1, "temp_pct3m": -1, "f_newhilo": -1,
       "f_basis": -1, "f_pxvol": -1, "f_north": -1, "vix_z": +1, "ma200_dev": -1, "mom120": -1}
FEATS = list(EXP.keys())


def spearman(a, b):
    s = pd.concat([a, b], axis=1).dropna()
    if len(s) < 20:
        return np.nan, 0
    return s.iloc[:, 0].corr(s.iloc[:, 1], method="spearman"), len(s)


def main():
    panels = {lab: build(code, gz) for lab, code, gz in TARGS}

    print("="*100)
    print(f"  (1) Spearman IC vs forward {H}d return — full sample (descriptive). EXP=expected sign")
    print("="*100)
    print(f"  {'feature':<12}{'exp':>4}" + "".join(f"{lab+'_IS':>11}{lab+'_OOS':>11}" for lab, *_ in TARGS))
    ic_store = {}
    for f in FEATS:
        row = f"  {f:<12}{EXP[f]:>+4}"
        for lab, code, gz in TARGS:
            p = panels[lab]
            isr, _ = spearman(p.loc[p.index <= IS_END, f], p.loc[p.index <= IS_END, "fwd"])
            oosr, _ = spearman(p.loc[p.index > IS_END, f], p.loc[p.index > IS_END, "fwd"])
            ic_store[(f, lab, "IS")] = isr; ic_store[(f, lab, "OOS")] = oosr
            row += f"{isr:>+11.3f}{oosr:>+11.3f}"
        print(row)

    # (2) non-overlapping significance
    print("\n" + "="*100)
    print(f"  (2) NON-overlapping (every {H}th obs) Spearman IC + perm p — honest significance")
    print("     pooled across HS300+ZZ1000; sign flipped to expected so +IC = works as expected")
    print("="*100)
    rng = np.random.default_rng(11)
    print(f"  {'feature':<12}{'IS_IC':>9}{'IS_p':>8}{'IS_n':>6}   {'OOS_IC':>9}{'OOS_p':>8}{'OOS_n':>6}")
    for f in FEATS:
        for split, lo, hi in [("IS", pd.Timestamp("2000-1-1"), IS_END), ("OOS", IS_END, pd.Timestamp("2100-1-1"))]:
            xs, ys = [], []
            for lab, code, gz in TARGS:
                p = panels[lab]
                sub = p[(p.index > lo) & (p.index <= hi)][[f, "fwd"]].dropna()
                sub = sub.iloc[::H]  # non-overlapping
                xs.extend((sub[f] * EXP[f]).tolist()); ys.extend(sub["fwd"].tolist())
            xs, ys = np.array(xs), np.array(ys)
            if len(xs) < 10:
                res = (np.nan, np.nan, len(xs))
            else:
                ic = pd.Series(xs).corr(pd.Series(ys), method="spearman")
                # permutation p: shuffle ys
                null = []
                for _ in range(3000):
                    null.append(pd.Series(xs).corr(pd.Series(rng.permutation(ys)), method="spearman"))
                p_val = (np.array(null) >= ic).mean()
                res = (ic, p_val, len(xs))
            if split == "IS":
                isres = res
            else:
                oosres = res
        star = ""
        if not np.isnan(oosres[0]) and oosres[1] < 0.05 and oosres[0] > 0:
            star = "  <-- OOS sig"
        print(f"  {f:<12}{isres[0]:>+9.3f}{isres[1]:>8.3f}{isres[2]:>6}   "
              f"{oosres[0]:>+9.3f}{oosres[1]:>8.3f}{oosres[2]:>6}{star}")

    # (3) multivariate OLS, fit IS pooled, OOS R²
    print("\n" + "="*100)
    print("  (3) multivariate OLS: fit fwd ~ features on IS (pooled HS300+ZZ1000), test OOS")
    print("="*100)
    use = ["erp_pct3y", "temp", "ma200_dev", "mom120", "vix_z"]
    def stack(split):
        X, Y = [], []
        for lab, code, gz in TARGS:
            p = panels[lab]
            sub = p[use + ["fwd"]].copy()
            sub = sub[(sub.index <= IS_END)] if split == "IS" else sub[sub.index > IS_END]
            sub = sub.dropna()
            X.append(sub[use].values); Y.append(sub["fwd"].values)
        return np.vstack(X), np.concatenate(Y)
    Xi, Yi = stack("IS"); Xo, Yo = stack("OOS")
    # standardize by IS stats
    mu, sd = Xi.mean(0), Xi.std(0)
    Xi_s = (Xi - mu) / sd; Xo_s = (Xo - mu) / sd
    Ai = np.column_stack([np.ones(len(Xi_s)), Xi_s])
    beta, *_ = np.linalg.lstsq(Ai, Yi, rcond=None)
    pred_i = Ai @ beta
    Ao = np.column_stack([np.ones(len(Xo_s)), Xo_s])
    pred_o = Ao @ beta
    r2_is = 1 - ((Yi - pred_i) ** 2).sum() / ((Yi - Yi.mean()) ** 2).sum()
    r2_oos = 1 - ((Yo - pred_o) ** 2).sum() / ((Yo - Yo.mean()) ** 2).sum()
    ic_is = pd.Series(pred_i).corr(pd.Series(Yi), method="spearman")
    ic_oos = pd.Series(pred_o).corr(pd.Series(Yo), method="spearman")
    print(f"  features: {use}")
    print(f"  IS  coefs (standardized): " + "  ".join(f"{u}={b:+.4f}" for u, b in zip(["const"]+use, beta)))
    print(f"  IS  R²={r2_is:+.4f}  IC={ic_is:+.3f}   (n={len(Yi)})")
    print(f"  OOS R²={r2_oos:+.4f}  IC={ic_oos:+.3f}   (n={len(Yo)})   <-- OOS R²>0 & IC>0 = real combined timing power")


if __name__ == "__main__":
    main()
