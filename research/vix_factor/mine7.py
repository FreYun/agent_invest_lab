"""Round 7 — the user's refined claim: it is about TIMING (lag), not magnitude.
  BULL  panic -> rebound is FAST / front-loaded
  BEAR  panic -> rebound is LAGGED / back-loaded (eventually as big or bigger)

(A) Rebound timing profile: mean RAW fwd return at a fine horizon grid, and the
    fraction of the 60d move already captured by each horizon, split by regime.
(B) Payoff: a regime-ADAPTIVE hold (short H in bull, long H in bear) should beat a
    single fixed H. Test event excess returns, IS/OOS, pooled 3 ETFs.
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
GRID = [3, 5, 10, 15, 20, 30, 40, 50, 60]
IS_END = pd.Timestamp("2023-12-31")


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_close(code):
    return pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]


def zser(vix):
    mu = vix.rolling(WIN, min_periods=WIN // 2).mean()
    sd = vix.rolling(WIN, min_periods=WIN // 2).std()
    return (vix - mu) / sd


def episodes(dates, gap=20):
    starts, prev = [], None
    for d in dates:
        if prev is None or (d - prev).days > gap:
            starts.append(d)
        prev = d
    return starts


def fear_episodes():
    """Return list of (date, code, close_series, i, is_bull) for every de-overlapped
    fear episode across the 3 ETFs."""
    vix = load_vix(); z = zser(vix)
    out = []
    for label, code, es in ETFS:
        close = load_close(code)
        ma = close.rolling(200).mean()
        zA = z.reindex(close.index, method="ffill")
        fear = (zA > 1.5) & (close.index >= pd.Timestamp(es)) & ma.notna()
        for d in episodes(close.index[fear.fillna(False).values]):
            i = close.index.get_loc(d)
            if pd.isna(ma.iloc[i]):
                continue
            out.append((d, code, close, i, bool(close.iloc[i] > ma.iloc[i])))
    return out


def main():
    eps = fear_episodes()

    # (A) timing profile
    print("="*92)
    print("  (A) FEAR rebound timing — mean RAW fwd return at each horizon, by regime at trigger")
    print("="*92)
    for tag, sel in [("BULL (close>MA200)", True), ("BEAR (close<MA200)", False)]:
        paths = {h: [] for h in GRID}
        for d, code, close, i, is_bull in eps:
            if is_bull != sel:
                continue
            for h in GRID:
                if i + h < len(close):
                    paths[h].append(close.iloc[i + h] / close.iloc[i] - 1.0)
        n = len(paths[GRID[0]])
        means = {h: np.mean(paths[h]) if len(paths[h]) >= 3 else np.nan for h in GRID}
        ref = means[60]
        print(f"\n  {tag}  (n={n})")
        print("    horizon :" + "".join(f"{h:>7}d" for h in GRID))
        print("    mean ret:" + "".join(f"{means[h]*100:>+7.1f}%" for h in GRID))
        if ref and not np.isnan(ref) and ref != 0:
            print("    %of 60d :" + "".join(f"{(means[h]/ref)*100:>7.0f}%" for h in GRID))

    # (B) regime-adaptive hold vs fixed hold — event excess return
    print("\n" + "="*92)
    print("  (B) regime-ADAPTIVE hold (BULL: short Hs / BEAR: long Hl) vs fixed H — event excess ret")
    print("="*92)
    # precompute unconditional fwd means per code per horizon
    unc = {}
    for label, code, es in ETFS:
        close = load_close(code)
        unc[code] = {h: (close.shift(-h) / close - 1.0).mean() for h in set([5, 10, 20, 40, 60])}

    def event_excess(Hs, Hl, split):
        vals = []
        for d, code, close, i, is_bull in eps:
            if (split == "IS") != (d <= IS_END):
                continue
            H = Hs if is_bull else Hl
            if i + H < len(close):
                vals.append(close.iloc[i + H] / close.iloc[i] - 1.0 - unc[code][H])
        return np.array(vals)

    configs = [
        ("fixed H=10",            10, 10),
        ("fixed H=20",            20, 20),
        ("fixed H=40",            40, 40),
        ("adaptive bull10/bear40", 10, 40),
        ("adaptive bull10/bear60", 10, 60),
        ("adaptive bull20/bear60", 20, 60),
    ]
    print(f"  {'config':<26}{'IS_n':>5}{'IS_mean':>9}{'IS_win':>8}   {'OOS_n':>6}{'OOS_mean':>10}{'OOS_win':>9}")
    for name, Hs, Hl in configs:
        isv = event_excess(Hs, Hl, "IS"); oov = event_excess(Hs, Hl, "OOS")
        def f(a):
            return (f"{a.mean()*100:+.2f}%", f"{(a>0).mean()*100:.0f}%") if len(a) >= 3 else ("n/a", "")
        ism, isw = f(isv); oom, oow = f(oov)
        print(f"  {name:<26}{len(isv):>5}{ism:>9}{isw:>8}   {len(oov):>6}{oom:>10}{oow:>9}")


if __name__ == "__main__":
    main()
