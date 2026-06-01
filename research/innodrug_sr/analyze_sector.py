"""Diagnostic: 创新药板块 BK000208 — which signals predict forward returns?

7-year sample (2019-04 → 2026-05). Signals: 主力净流入(资金流), 拥挤度分位, MA200乖离分位,
动量. Target: 板块前瞻 K 日累计收益. Spearman corr, direction + strength.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"


def load() -> pd.DataFrame:
    m = pd.read_csv(DATA / "sector_BK000208_market.csv", parse_dates=["date"])
    fd = pd.read_csv(DATA / "sector_BK000208_factor_detail.csv", parse_dates=["date"])
    f = pd.read_csv(DATA / "sector_BK000208_factor.csv", parse_dates=["date"])
    df = m.merge(fd, on="date", how="left").merge(f, on="date", how="left").sort_values("date").set_index("date")
    df["ret"] = df["ret_pct"] / 100.0
    df["close"] = (1 + df["ret"]).cumprod()
    return df


def zwin(s: pd.Series, n: int, w: int) -> pd.Series:
    cum = s.rolling(n, min_periods=max(3, n // 2)).sum()
    return (cum - cum.rolling(w, min_periods=20).mean()) / cum.rolling(w, min_periods=20).std()


def main():
    df = load()
    print(f"rows={len(df)}  {df.index.min().date()}..{df.index.max().date()}")
    print(f"main_inflow (亿元): mean={df['main_inflow'].mean()/1e8:+.2f} std={df['main_inflow'].std()/1e8:.2f} "
          f"min={df['main_inflow'].min()/1e8:+.1f} max={df['main_inflow'].max()/1e8:+.1f}")
    print(f"集中度分位: mean={df['集中度分位'].mean():.2f}  乖离分位: mean={df['乖离分位'].mean():.2f}")
    print()

    fwds = [5, 10, 20]
    close = df["close"]

    def corr_table(name, sig):
        d = pd.concat([sig.rename("s")] + [(close.shift(-k) / close - 1).rename(f"f{k}") for k in fwds], axis=1).dropna()
        cs = [d["s"].corr(d[f"f{k}"], method="spearman") if len(d) > 50 else np.nan for k in fwds]
        print(f"  {name:<34}" + "".join(f"  {c:+.3f}" for c in cs) + f"   (n={len(d)})")

    print("Spearman corr: signal vs forward-K-ret   fwd:  " + "   ".join(f"{k:>4}" for k in fwds))
    print("─ 主力净流入 (cumN z; +corr=trend钱聪明, -corr=contrarian见顶) ─")
    for n in [3, 5, 10, 20]:
        corr_table(f"main_inflow_z  cumN={n} (zw90)", zwin(df["main_inflow"], n, 90))
    print("─ 拥挤度 / 乖离 (高分位=过热; -corr 表示过热→后续跌, de-risk 有效) ─")
    corr_table("集中度分位", df["集中度分位"])
    corr_table("乖离分位", df["乖离分位"])
    corr_table("MA200乖离率", df["MA200乖离率"])
    print("─ 动量 (+corr=趋势延续) ─")
    for c in ["动量", "夏普动量", "信息比率动量"]:
        if c in df:
            corr_table(c, df[c])
    print()
    # raw inflow sign hit-rate vs next-20d
    f20 = close.shift(-20) / close - 1
    inflow5 = df["main_inflow"].rolling(5).sum()
    both = pd.concat([inflow5, f20], axis=1).dropna()
    print(f"主力5日净流入>0 时, 后20日上涨比例: {(both[both.iloc[:,0]>0].iloc[:,1]>0).mean():.0%}  "
          f"(<0 时: {(both[both.iloc[:,0]<0].iloc[:,1]>0).mean():.0%})  base={(f20>0).mean():.0%}")


if __name__ == "__main__":
    main()
