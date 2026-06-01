"""Within the SR overlap window (2024-08 → 2026-05), re-check each candidate
combo component's direction vs forward returns — 7yr conclusions may NOT hold here.

Components: retail flow (申赎), smart-money (主力净流入), retail-vs-smart divergence,
crowding (集中度/乖离分位), momentum (IR). Target: 159992 fwd-K return.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"


def load_all() -> pd.DataFrame:
    sr = pd.read_csv(BASE / "innodrug_sr_daily.csv", parse_dates=["date"])
    sr = sr[sr["persona"] == "个人"].set_index("date").sort_index()
    sr.index = pd.to_datetime(sr.index).normalize()
    sr["imbalance"] = (sr["applied"] - sr["redeemed"]) / (sr["applied"] + sr["redeemed"]).replace(0, np.nan)

    m = pd.read_csv(DATA / "sector_BK000208_market.csv", parse_dates=["date"]).set_index("date")
    fd = pd.read_csv(DATA / "sector_BK000208_factor_detail.csv", parse_dates=["date"]).set_index("date")
    f = pd.read_csv(DATA / "sector_BK000208_factor.csv", parse_dates=["date"]).set_index("date")
    sec = m.join(fd, how="outer").join(f, how="outer")
    sec.index = pd.to_datetime(sec.index).normalize()

    px = pd.read_csv(DATA / "159992.SZ.csv", parse_dates=[0])
    px.columns = [c.lower() for c in px.columns]
    px = px.set_index(px.columns[0]).sort_index()
    px.index = pd.to_datetime(px.index).normalize()

    # trading-day index from price; ffill slow-moving sector + sr onto it
    idx = px.loc["2024-08-01":].index
    df = pd.DataFrame(index=idx)
    df["close"] = px["close"]
    for c in ["net", "net_ex_aip", "imbalance", "applied", "redeemed"]:
        df["sr_" + c] = sr[c].reindex(idx, method="ffill")
    df["main_inflow"] = sec["main_inflow"].reindex(idx, method="ffill")
    df["conc_q"] = sec["集中度分位"].reindex(idx, method="ffill")
    df["dev_q"] = sec["乖离分位"].reindex(idx, method="ffill")
    df["ir_mom"] = sec["信息比率动量"].reindex(idx, method="ffill")
    df["pb"] = sec["pb"].reindex(idx, method="ffill")
    return df


def zwin(s, n, w):
    mp = min(max(3, n // 2), n)
    cum = s.rolling(n, min_periods=mp).sum()
    return (cum - cum.rolling(w, min_periods=20).mean()) / cum.rolling(w, min_periods=20).std()


def main():
    df = load_all()
    print(f"overlap window: {df.index.min().date()}..{df.index.max().date()}  trading days={len(df)}")
    close = df["close"]
    fwds = [5, 10, 20]

    def corr(name, sig):
        d = pd.concat([sig.rename("s")] + [(close.shift(-k)/close-1).rename(f"f{k}") for k in fwds], axis=1).dropna()
        cs = [d["s"].corr(d[f"f{k}"], method="spearman") if len(d) > 40 else np.nan for k in fwds]
        print(f"  {name:<34}" + "".join(f"  {c:+.3f}" if c==c else "    NaN" for c in cs) + f"   (n={len(d)})")

    n, w = 15, 60
    retail_z = zwin(df["sr_imbalance"], n, w)      # 散户情绪 (高=过热)
    inflow_z = zwin(df["main_inflow"], n, w)        # 主力资金 (高=流入)
    diverg = retail_z - inflow_z                     # 散户买&主力卖 → 高 (派发背离)

    print(f"\nIn-window forward-return corr (cumN={n}, z_win={w}):")
    print(f"  signal                              fwd5    fwd10   fwd20")
    print("─ 单分量 ─")
    corr("retail_z (imbalance)  [-=contra]", retail_z)
    corr("retail_z (net)        [-=contra]", zwin(df["sr_net"], n, w))
    corr("inflow_z (主力)       [+=trend]", inflow_z)
    corr("conc_q  (集中度分位)", df["conc_q"])
    corr("dev_q   (乖离分位)", df["dev_q"])
    corr("ir_mom  (信息比率动量)", df["ir_mom"])
    print("─ 组合分量 ─")
    corr("divergence=retail_z - inflow_z [-]", diverg)
    corr("retail_z * (inflow_z<0) 派发门  [-]", retail_z.where(inflow_z < 0, 0))
    # combo: equal-weight standardized retail contrarian + smart-money outflow
    combo = 0.5 * retail_z + 0.5 * (-inflow_z)
    corr("combo=0.5*retail -0.5*inflow  [-]", combo)
    print("\n注: 负相关=该信号高时后续跌(可作减仓/contrarian). retail/combo 期望负, inflow 期望正(trend).")


if __name__ == "__main__":
    main()
