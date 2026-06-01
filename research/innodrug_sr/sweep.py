"""创新药 retail-flow factor: first-pass sweep + parameter robustness.

⚠️ n ≈ 22 months single index (931152). EXPLORATORY ONLY per SOP step 3
   (<2yr → 不挑单点最优, robustness sweep is the deliverable).

Universe (price proxies for the SAME 931152 signal — tracking-error robustness, NOT
independent indices):
  159992.SZ 广发创新药ETF (tracks 931152, primary)
  516080.SH 创新药ETF沪深
  159839.SZ 创新药ETF
  512010.SH 易方达医药ETF (broader 医药 — generalization probe)

Grid: src × cumN × z_win × thr × direction → backtest on each ETF.
Direction: contrarian (retail-hot → flat) vs trend (retail-hot → long) — to confirm sign.
"""
from __future__ import annotations
from pathlib import Path
from itertools import product
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"
COMMISSION = 0.0005

UNIVERSE = {
    "innodrug_159992": "159992.SZ.csv",
    "innodrug_516080": "516080.SH.csv",
    "innodrug_159839": "159839.SZ.csv",
    "yiyao_512010":    "512010.SH.csv",
}

SOURCES = ["net", "net_ex_aip", "applied", "imbalance"]
CUM_NS = [5, 10, 15, 20]
Z_WINS = [45, 60, 90]
THRESHOLDS = [0.5, 1.0, 1.5]
DIRECTIONS = ["contrarian", "trend"]
WARMUP = 110  # discard z_win(90)+cumN(20) bars to kill warmup pollution


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


def make_signal(close, sr, src, cumN, z_win, thr, direction) -> pd.Series:
    z = cumN_z(sr[src], cumN, z_win).reindex(close.index, method="ffill")
    state = pd.Series(np.nan, index=close.index)
    if direction == "contrarian":   # retail hot (z>thr) → flat; cold (z<-thr) → long
        state = state.where(~(z > thr), 0.0)
        state = state.where(~(z < -thr), 1.0)
    else:                            # trend: retail hot → long; cold → flat
        state = state.where(~(z > thr), 1.0)
        state = state.where(~(z < -thr), 0.0)
    return state.ffill().fillna(0.5)


def backtest(close: pd.Series, sig: pd.Series) -> dict:
    sig = sig.clip(0.0, 1.0).fillna(0.0).reindex(close.index).fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    turnover = pos.diff().abs().fillna(0.0)
    cost = turnover * COMMISSION
    strat = pos * ret - cost
    s = strat.iloc[WARMUP:]
    pos_e = pos.iloc[WARMUP:]
    bh = ret.iloc[WARMUP:]
    n = len(s)
    if n == 0:
        return {"error": "empty"}
    def metrics(r):
        eq = (1 + r).cumprod()
        tot = float(eq.iloc[-1] - 1)
        ann = float((1 + tot) ** (252 / max(n, 1)) - 1)
        sd = float(r.std(ddof=1)) if n > 1 else 0.0
        shp = float(r.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
        dd = float((eq / eq.cummax() - 1).min())
        cal = float(ann / abs(dd)) if dd < 0 else 0.0
        return ann, shp, dd, cal
    ann, shp, dd, cal = metrics(s)
    b_ann, b_shp, b_dd, b_cal = metrics(bh)
    flips = int((turnover.iloc[WARMUP:] > 0.001).sum())
    yrs = n / 252
    return {"ann": ann, "shp": shp, "dd": dd, "cal": cal,
            "flips_yr": flips / yrs if yrs > 0 else 0, "exp": float(pos_e.mean()), "bars": n,
            "bh_ann": b_ann, "bh_shp": b_shp, "bh_dd": b_dd, "bh_cal": b_cal}


def main():
    sr = load_sr()
    closes = {k: load_close(v).loc[sr.index.min():sr.index.max()] for k, v in UNIVERSE.items()}
    print(f"SR window {sr.index.min().date()}..{sr.index.max().date()}")
    print("buyhold reference:")
    bh_ref = {}
    for k, c in closes.items():
        m = backtest(c, pd.Series(1.0, index=c.index))
        bh_ref[k] = m
        print(f"  {k:<18} bars={m['bars']} bh_ann={m['bh_ann']:+.3f} bh_shp={m['bh_shp']:+.2f} "
              f"bh_cal={m['bh_cal']:+.2f} bh_dd={m['bh_dd']:+.2f}")
    print()

    rows = []
    for src, cumN, z_win, thr, direction in product(SOURCES, CUM_NS, Z_WINS, THRESHOLDS, DIRECTIONS):
        for k, c in closes.items():
            sig = make_signal(c, sr, src, cumN, z_win, thr, direction)
            m = backtest(c, sig)
            rows.append({"src": src, "cumN": cumN, "z_win": z_win, "thr": thr, "dir": direction, "etf": k,
                         "shp": m["shp"], "cal": m["cal"], "dd": m["dd"], "ann": m["ann"],
                         "exp": m["exp"], "flips_yr": m["flips_yr"],
                         "dcal": m["cal"] - bh_ref[k]["bh_cal"],
                         "dshp": m["shp"] - bh_ref[k]["bh_shp"],
                         "beats_cal": int(m["cal"] > bh_ref[k]["bh_cal"]),
                         "beats_shp": int(m["shp"] > bh_ref[k]["bh_shp"])})
    df = pd.DataFrame(rows)
    df.to_csv(BASE / "sweep_full.csv", index=False)
    n_cfg = len(SOURCES) * len(CUM_NS) * len(Z_WINS) * len(THRESHOLDS) * len(DIRECTIONS)
    print(f"Wrote sweep_full.csv: {n_cfg} configs × {len(UNIVERSE)} ETFs = {len(df)} backtests\n")

    # direction check: which sign works?
    print("══ Direction check (mean dCalmar vs buyhold, over all params×ETFs):")
    print(df.groupby("dir")[["dcal", "dshp", "beats_cal"]].mean().to_string(float_format=lambda x: f"{x:+.3f}"))
    print()

    # focus on the winning direction's per-config robustness (across 4 ETFs)
    for direction in DIRECTIONS:
        sub = df[df["dir"] == direction]
        agg = sub.groupby(["src", "cumN", "z_win", "thr"]).agg(
            beats_cal_n=("beats_cal", "sum"), mean_dcal=("dcal", "mean"),
            median_dcal=("dcal", "mean"), mean_exp=("exp", "mean"),
            mean_flips=("flips_yr", "mean")).reset_index()
        n_all4 = (agg["beats_cal_n"] == 4).mean()
        n_ge3 = (agg["beats_cal_n"] >= 3).mean()
        print(f"── dir={direction}: {len(agg)} configs ──")
        print(f"   beat buyhold Calmar on ALL 4 ETFs: {(agg['beats_cal_n']==4).sum()}/{len(agg)} = {n_all4:.0%}")
        print(f"   beat buyhold Calmar on ≥3 ETFs:    {(agg['beats_cal_n']>=3).sum()}/{len(agg)} = {n_ge3:.0%}")
        print(f"   median dCalmar (over configs): {agg['mean_dcal'].median():+.3f}   mean: {agg['mean_dcal'].mean():+.3f}")
        print()

    # per-source beat rate for contrarian
    print("══ Per-source beat-rate (contrarian dir, avg over cumN×z_win×thr×ETF):")
    c = df[df["dir"] == "contrarian"]
    print(c.groupby("src")[["beats_cal", "dcal", "dshp", "exp", "flips_yr"]].mean()
          .to_string(float_format=lambda x: f"{x:+.3f}"))
    print()

    # cumN × z_win heatmap (contrarian, mean #ETFs beating, max 4)
    print("══ Heatmap cumN×z_win — mean #ETFs beating buyhold Calmar (contrarian, max=4):")
    heat = c.groupby(["cumN", "z_win"])["beats_cal"].mean() * 4
    print(heat.unstack().to_string(float_format=lambda x: f"{x:.2f}"))
    print()

    print("══ Top-12 configs by mean dCalmar (contrarian):")
    cagg = c.groupby(["src", "cumN", "z_win", "thr"]).agg(
        beats_cal_n=("beats_cal", "sum"), mean_dcal=("dcal", "mean"),
        mean_exp=("exp", "mean"), mean_flips=("flips_yr", "mean")).reset_index()
    print(cagg.sort_values("mean_dcal", ascending=False).head(12)
          .to_string(index=False, float_format=lambda x: f"{x:+.3f}"))


if __name__ == "__main__":
    main()
