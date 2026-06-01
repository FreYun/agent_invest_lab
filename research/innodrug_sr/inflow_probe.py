"""Does 主力净流入 carry signal in ANY cumulative form? (user's hypothesis:
区间累计 not 当天). Sweep cum-window × transform on BOTH 7yr sector & 15mo 159992.

Transforms of cumulative inflow:
  z      : rolling z-score of cumN sum (deviation)            — 已测 cumN≤20, 扩到 120
  rank   : rolling 252 percentile of cumN sum (level/趋势)     — NEW (用户直觉: 累计水平)
  sign   : sign of cumN sum (净累计流入/流出)                  — NEW
  chg    : cumN sum minus its own N-ago (累计动量/加速度)      — NEW
Also: contemporaneous corr (cum vs SAME-day ret) to see if inflow is just coincident beta.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"


def load_sector():
    m = pd.read_csv(DATA / "sector_BK000208_market.csv", parse_dates=["date"]).set_index("date").sort_index()
    m["ret"] = m["ret_pct"] / 100.0
    m["close"] = (1 + m["ret"]).cumprod()
    return m


def load_159992():
    sec = load_sector()
    px = pd.read_csv(DATA / "159992.SZ.csv", parse_dates=[0]); px.columns = [c.lower() for c in px.columns]
    px = px.set_index(px.columns[0]).sort_index(); px.index = pd.to_datetime(px.index).normalize()
    idx = px.loc["2024-08-01":].index
    df = pd.DataFrame(index=idx)
    df["close"] = px["close"]
    df["main_inflow"] = sec["main_inflow"].reindex(idx, method="ffill")
    return df


def sp(a, b):
    d = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    return d["a"].corr(d["b"], method="spearman") if len(d) > 40 else np.nan


def probe(df, label):
    close = df["close"]; inflow = df["main_inflow"]
    fwds = [5, 10, 20]
    print(f"\n══════ {label}  (n={len(df)}, {df.index.min().date()}..{df.index.max().date()}) ══════")
    print("contemporaneous: cum20 inflow vs SAME-day cum20 ret corr = "
          f"{sp(inflow.rolling(20).sum(), close.pct_change(20)):+.3f}  (高=同期beta,信息已反映)")
    print(f"\n  forward-ret spearman   fwd:   5      10      20")
    for N in [5, 10, 20, 40, 60, 120]:
        cum = inflow.rolling(N, min_periods=max(3, N // 2)).sum()
        zw = max(60, N)
        z = (cum - cum.rolling(zw, min_periods=20).mean()) / cum.rolling(zw, min_periods=20).std()
        rank = cum.rolling(252, min_periods=60).rank(pct=True)
        sign = np.sign(cum)
        chg = cum - cum.shift(N)
        for tname, sig in [("z", z), ("rank", rank), ("sign", sign), ("chg", chg)]:
            cs = [sp(sig, close.shift(-k) / close - 1) for k in fwds]
            mark = "  <<" if any(abs(c) > 0.12 for c in cs if c == c) else ""
            print(f"  cum{N:<3} {tname:<5}" + "".join(f"  {c:+.3f}" if c == c else "    NaN" for c in cs) + mark)


def main():
    probe(load_sector(), "7yr 板块 BK000208 (板块自带收益)")
    probe(load_159992(), "15mo 159992 ETF (组合窗口)")
    print("\n注: |corr|>0.12 标 <<. 散户申赎 retail_z fwd20≈−0.16/−0.28 作参照基准.")


if __name__ == "__main__":
    main()
