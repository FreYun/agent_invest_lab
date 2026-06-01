"""Round 2 — does VIX-spike add-timing have PURE alpha that survives OOS?

Lessons from round 1:
  - base=0.5 confounded timing skill with bear-market under-exposure -> measure the
    PURE timing-alpha stream (pos-base)*ret, whose Sharpe is independent of base.
  - The 2024-01 episode added into a falling knife (z spiked mid-crash) -> add a
    price-confirmation filter so we don't catch knives.
  - Select any parameter on IS (<=2023) ONLY; confirm on OOS (2024-2026/05).

Triggers (all designed from economic logic, before looking at OOS):
  raw15    : z>1.5
  raw20    : z>2.0
  upday    : z>1.5 AND today closed up vs yesterday          (price ticked up)
  ma5      : z>1.5 AND close > MA5                            (above short MA)
  vixroll  : z>1.5 AND VIX < VIX.shift(1)                     (panic peaking/receding)

Metrics, split IS vs OOS, per ETF and pooled across the 3 ETFs:
  - event 10d forward EXCESS return = realised 10d ret minus that ETF's unconditional
    10d mean (so we measure timing, not beta)
  - daily timing-alpha annualised Sharpe (pulse.shift(1)*ret stream)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/vix_factor")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIX = BASE / "data" / "vix_50etf.csv"
WIN, H = 252, 10
ETFS = [("HS300", "510300.SH", "2016-06-01"), ("ZZ1000", "512100.SH", "2016-06-01"),
        ("SC50", "588800.SH", "2023-09-01")]
IS_END = pd.Timestamp("2023-12-31")


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_close(code):
    return pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]


def triggers(vix, close_aligned):
    """Return dict[name] -> boolean Series on the ETF date index (causal)."""
    mu = vix.rolling(WIN, min_periods=WIN // 2).mean()
    sd = vix.rolling(WIN, min_periods=WIN // 2).std()
    z = (vix - mu) / sd
    vix_down = vix < vix.shift(1)
    # align VIX-domain series onto ETF dates (VIX_T visible at T close)
    zA = z.reindex(close_aligned.index, method="ffill")
    vixdownA = vix_down.reindex(close_aligned.index, method="ffill").fillna(False)
    up = close_aligned > close_aligned.shift(1)
    ma5 = close_aligned > close_aligned.rolling(5).mean()
    return {
        "raw15":   (zA > 1.5),
        "raw20":   (zA > 2.0),
        "upday":   (zA > 1.5) & up,
        "ma5":     (zA > 1.5) & ma5,
        "vixroll": (zA > 1.5) & vixdownA,
    }, zA


def episodes(trig, min_gap_days=18):
    days = trig.index[trig.fillna(False).values]
    starts, prev = [], None
    for d in days:
        if prev is None or (d - prev).days > min_gap_days:
            starts.append(d)
        prev = d
    return starts


def event_stats(close, starts, split):
    """split: 'IS' or 'OOS'. Return (n, mean_excess, median, win) of 10d excess return."""
    uncond = (close.shift(-H) / close - 1.0).mean()
    vals = []
    for d in starts:
        if (split == "IS" and d > IS_END) or (split == "OOS" and d <= IS_END):
            continue
        seg = close[close.index >= d]
        if len(seg) > H:
            vals.append((seg.iloc[H] / seg.iloc[0] - 1.0) - uncond)
    if not vals:
        return 0, np.nan, np.nan, np.nan
    a = np.array(vals)
    return len(a), float(a.mean()), float(np.median(a)), float((a > 0).mean())


def timing_alpha_sharpe(close, trig, split):
    pulse = trig.astype(float).rolling(H, min_periods=1).max().fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    alpha = pulse.shift(1).fillna(0.0) * ret    # per-unit add exposure pnl (base-independent)
    if split == "IS":
        m = alpha.index <= IS_END
    else:
        m = alpha.index > IS_END
    a = alpha[m]
    a = a[a.index >= close.index[0]]
    if a.std(ddof=1) == 0 or len(a) < 20:
        return np.nan
    return float(a.mean() / a.std(ddof=1) * np.sqrt(252))


def main():
    vix = load_vix()
    names = ["raw15", "raw20", "upday", "ma5", "vixroll"]
    pooled = {n: {"IS": [], "OOS": []} for n in names}
    pooled_alpha = {n: {"IS": [], "OOS": []} for n in names}

    for split in ["IS", "OOS"]:
        print(f"\n{'='*92}\n  {split}  —  event 10d EXCESS return (vs ETF unconditional 10d mean), per trigger\n{'='*92}")
        print(f"{'trigger':<9}{'etf':<8}{'n':>4}{'mean_exc':>10}{'median':>9}{'win%':>7}{'  | timing-α Sharpe':>20}")
        for name in names:
            for label, code, es in ETFS:
                close = load_close(code)
                trig_map, _ = triggers(vix, close)
                trig = trig_map[name]
                starts = episodes(trig)
                n, mean_e, med, win = event_stats(close, starts, split)
                shp = timing_alpha_sharpe(close, trig, split)
                if n > 0:
                    pooled[name][split].append((mean_e, n))
                    pooled_alpha[name][split].append(shp)
                    print(f"{name:<9}{label:<8}{n:>4}{mean_e*100:>+9.2f}%{med*100:>+8.2f}%{win*100:>6.0f}%{shp:>+20.2f}")
            print()

    # pooled summary IS vs OOS
    print(f"{'='*92}\n  POOLED across 3 ETFs — IS vs OOS  (n-weighted mean excess; mean timing-α Sharpe)\n{'='*92}")
    print(f"{'trigger':<9}{'IS_n':>5}{'IS_exc':>9}{'IS_Shp':>8}   {'OOS_n':>5}{'OOS_exc':>9}{'OOS_Shp':>8}   verdict")
    for name in names:
        def wmean(lst):
            if not lst: return np.nan, 0
            tot = sum(n for _, n in lst);
            return sum(m*n for m, n in lst)/tot, tot
        ise, isn = wmean(pooled[name]["IS"])
        ooe, oon = wmean(pooled[name]["OOS"])
        iss = np.nanmean(pooled_alpha[name]["IS"]) if pooled_alpha[name]["IS"] else np.nan
        oos = np.nanmean(pooled_alpha[name]["OOS"]) if pooled_alpha[name]["OOS"] else np.nan
        verdict = "OOS holds" if (ooe > 0 and oos > 0) else ("OOS dead" if ooe <= 0 else "weak")
        print(f"{name:<9}{isn:>5}{ise*100:>+8.2f}%{iss:>+8.2f}   {oon:>5}{ooe*100:>+8.2f}%{oos:>+8.2f}   {verdict}")


if __name__ == "__main__":
    main()
