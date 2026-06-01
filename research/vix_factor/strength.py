"""How strong is the factor, really? Rigorous, clustering-honest assessment.

Pitfalls avoided:
  - 3 ETFs share the SAME 50ETF-VIX trigger dates and are highly correlated, so
    "3×N backtests" massively overstates independence. The real sample is the number
    of distinct TIME episodes. We de-overlap on the VIX series itself (one date set),
    and average the 3 ETFs' excess return WITHIN each episode -> one number per episode.
  - point estimates hide uncertainty -> bootstrap 95% CI over episodes.
  - "looks good" != "better than random" -> permutation test vs random trigger dates.

Reported per tail (FEAR z>1.5, EUPHORIA z<-1.5), IS (<=2023) and OOS (2024-2026):
  n_episodes, cross-sectional mean/median excess, bootstrap 95% CI, permutation p-value.
FEAR uses regime-adaptive hold (bull 20d / bear 60d); EUPHORIA uses 20d (trim, expect <0).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/vix_factor")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIX = BASE / "data" / "vix_50etf.csv"
WIN = 252
ETFS = [("HS300", "510300.SH", "2016-06-01"), ("ZZ1000", "512100.SH", "2016-06-01"),
        ("SC50", "588800.SH", "2023-09-01")]
IS_END = pd.Timestamp("2023-12-31")
RNG = np.random.default_rng(7)


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_close(code):
    return pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]


def zser(vix):
    mu = vix.rolling(WIN, min_periods=WIN // 2).mean()
    sd = vix.rolling(WIN, min_periods=WIN // 2).std()
    return (vix - mu) / sd


def deoverlap(dates, gap=20):
    out, prev = [], None
    for d in dates:
        if prev is None or (d - prev).days > gap:
            out.append(d)
        prev = d
    return out


# preload ETF closes + MA + unconditional fwd means
ETF = {}
for label, code, es in ETFS:
    c = load_close(code)
    ETF[code] = dict(close=c, ma=c.rolling(200).mean(), es=pd.Timestamp(es),
                     unc={h: (c.shift(-h) / c - 1.0).mean() for h in (20, 60)})


def xsec_excess(date, tail):
    """Cross-sectional mean excess return across ETFs available at `date`.
    FEAR: regime-adaptive hold (bull 20d / bear 60d). EUPHORIA: 20d."""
    vals = []
    for label, code, es in ETFS:
        d = ETF[code]; c = d["close"]
        if date < d["es"] or date not in c.index:
            continue
        i = c.index.get_loc(date)
        if tail == "FEAR":
            ma = d["ma"].iloc[i]
            if pd.isna(ma):
                continue
            H = 20 if c.iloc[i] > ma else 60
        else:
            H = 20
        if i + H < len(c):
            vals.append(c.iloc[i + H] / c.iloc[i] - 1.0 - d["unc"][H])
    return np.mean(vals) if vals else np.nan


def assess(tail, cond, split, all_valid_dates):
    vix = load_vix(); z = zser(vix)
    cmask = cond(z) & (z.index <= IS_END if split == "IS" else z.index > IS_END)
    eps = deoverlap(z.index[cmask.fillna(False).values])
    obs = np.array([xsec_excess(d, tail) for d in eps])
    obs = obs[~np.isnan(obs)]
    n = len(obs)
    if n < 3:
        return f"  {tail:<9}[{split}]  n={n}  (too few)"
    mean = obs.mean(); med = np.median(obs)
    # bootstrap CI over episodes
    boots = np.array([RNG.choice(obs, n, replace=True).mean() for _ in range(5000)])
    lo, hi = np.quantile(boots, [0.025, 0.975])
    # permutation vs random trigger dates (same count, same split window)
    pool = [d for d in all_valid_dates if (d <= IS_END if split == "IS" else d > IS_END)]
    null = []
    for _ in range(3000):
        rd = RNG.choice(len(pool), n, replace=False)
        rv = np.array([xsec_excess(pool[j], tail) for j in rd])
        rv = rv[~np.isnan(rv)]
        if len(rv):
            null.append(rv.mean())
    null = np.array(null)
    if tail == "FEAR":
        p = (null >= mean).mean()
    else:
        p = (null <= mean).mean()
    star = "  <-- significant vs random" if p < 0.05 else ("  (marginal)" if p < 0.10 else "")
    return (f"  {tail:<9}[{split}]  n={n:>2}  mean={mean*100:+5.2f}%  med={med*100:+5.2f}%  "
            f"boot95%CI=[{lo*100:+.2f}%,{hi*100:+.2f}%]  perm_p={p:.3f}{star}")


def main():
    # valid date pool = HS300 trading days with warm VIX (z available) and room for 60d fwd
    vix = load_vix(); z = zser(vix)
    hs = ETF["510300.SH"]["close"]
    valid = [d for d in hs.index[:-60] if d in z.index and not pd.isna(z.reindex(hs.index, method="ffill").get(d, np.nan))]
    valid = [d for d in valid if d >= pd.Timestamp("2017-01-01")]

    print("="*92)
    print("  FACTOR STRENGTH — distinct TIME episodes (not ×3 ETFs), cross-sectional excess,")
    print("  bootstrap CI over episodes, permutation test vs random trigger dates")
    print("="*92)
    print("  FEAR z>1.5 (regime-adaptive hold bull20/bear60, expect >0) | EUPHORIA z<-1.5 (hold 20d, expect <0)\n")
    for tail, cond in [("FEAR", lambda z: z > 1.5), ("EUPHORIA", lambda z: z < -1.5)]:
        for split in ["IS", "OOS"]:
            print(assess(tail, cond, split, valid))
        print()
    print("  Read: CI excluding 0 = effect direction is statistically resolved at that sample size;")
    print("  perm_p<0.05 = the VIX-timed dates beat randomly-placed dates of the same count.")


if __name__ == "__main__":
    main()
