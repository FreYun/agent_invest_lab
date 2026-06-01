"""Diagnostic: does 创新药 retail subscription flow predict forward returns?

Raw signal→forward-return correlation BEFORE any backtest machinery.
Negative corr on a contrarian signal (high retail net-subscription → lower fwd ret)
= the contrarian hypothesis has legs. This is the honest first look.

Signal: 931152 个人 net subscription (and variants), cumN-summed, z-scored.
Target: 159992.SZ (广发创新药ETF, tracks 931152) forward N-day return.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"


def load_close(fname: str) -> pd.Series:
    df = pd.read_csv(DATA / fname, parse_dates=[0])
    df.columns = [c.lower() for c in df.columns]
    df = df.set_index(df.columns[0]).sort_index()
    s = df["close"].astype(float)
    s.index = pd.to_datetime(s.index).normalize()
    return s


def load_sr() -> pd.DataFrame:
    sr = pd.read_csv(BASE / "innodrug_sr_daily.csv", parse_dates=["date"])
    sr = sr[sr["persona"] == "个人"].set_index("date").sort_index()
    sr.index = pd.to_datetime(sr.index).normalize()
    sr["imbalance"] = (sr["applied"] - sr["redeemed"]) / (sr["applied"] + sr["redeemed"]).replace(0, np.nan)
    return sr[["applied", "redeemed", "net", "net_ex_aip", "imbalance"]]


def cumN_z(s: pd.Series, n: int, z_win: int) -> pd.Series:
    cum = s.rolling(n, min_periods=max(3, n // 2)).sum()
    mu = cum.rolling(z_win, min_periods=20).mean()
    sd = cum.rolling(z_win, min_periods=20).std()
    return (cum - mu) / sd


def main():
    sr = load_sr()
    close = load_close("159992.SZ.csv")
    # align to trading days
    close = close.loc[sr.index.min():]
    print(f"SR window: {sr.index.min().date()}..{sr.index.max().date()}  rows={len(sr)}")
    print(f"ETF 159992 trading days in window: {len(close)}  ({close.index.min().date()}..{close.index.max().date()})")
    print(f"Raw 个人 net: mean={sr['net'].mean():.4f}  std={sr['net'].std():.4f}  "
          f"min={sr['net'].min():.4f}  max={sr['net'].max():.4f} (亿元)")
    print(f"  net>0 (净申购) days: {(sr['net']>0).mean():.0%}   net<0 (净赎回) days: {(sr['net']<0).mean():.0%}")
    print()

    ret = close.pct_change()
    sources = ["net", "net_ex_aip", "applied", "imbalance"]
    cumNs = [5, 10, 15, 20]
    fwds = [5, 10, 20]
    z_win = 60

    print(f"Spearman corr: z(cumN signal) vs forward-K-day return  (z_win={z_win})")
    print("NEGATIVE = contrarian works (retail piling in → lower fwd return)")
    print()
    for src in sources:
        z = cumN_z(sr[src], 10, z_win).reindex(close.index, method="ffill")
        print(f"── source = {src} ──")
        hdr = "  cumN\\fwd " + "".join(f"  fwd{k:>3}" for k in fwds)
        print(hdr)
        for n in cumNs:
            z = cumN_z(sr[src], n, z_win).reindex(close.index, method="ffill")
            cells = []
            for k in fwds:
                fwd = close.shift(-k) / close - 1.0
                d = pd.concat([z, fwd], axis=1).dropna()
                c = d.iloc[:, 0].corr(d.iloc[:, 1], method="spearman") if len(d) > 30 else np.nan
                cells.append(f"  {c:+.3f}" if c == c else "    NaN")
            print(f"  n={n:<6}" + "".join(cells))
        print()


if __name__ == "__main__":
    main()
