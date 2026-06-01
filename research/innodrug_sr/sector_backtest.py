"""创新药板块 BK000208 — IS/OOS factor sweep (7yr sample, 2019-04→2026-05).

Diagnostic found: 主力净流入 corr≈0 (no edge), 信息比率动量 +0.18 / 集中度分位 +0.12 (trend).
Test trend-family factors with strict IS/OOS split + buyhold benchmark.

IS  = 2019-04 .. 2022-12   (warmup-heavy early, ~3.7yr)
OOS = 2023-01 .. 2026-05   (~3.4yr)
Trade proxy: 板块自带日收益 (signal validation). 落地用 159992 ETF (separate check).
"""
from __future__ import annotations
from pathlib import Path
from itertools import product
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"
COMMISSION = 0.0005
WARMUP = 60
OOS_START = "2023-01-01"


def load() -> pd.DataFrame:
    m = pd.read_csv(DATA / "sector_BK000208_market.csv", parse_dates=["date"])
    fd = pd.read_csv(DATA / "sector_BK000208_factor_detail.csv", parse_dates=["date"])
    f = pd.read_csv(DATA / "sector_BK000208_factor.csv", parse_dates=["date"])
    df = m.merge(fd, on="date", how="left").merge(f, on="date", how="left").sort_values("date").set_index("date")
    df["ret"] = df["ret_pct"] / 100.0
    return df


def zwin(s, n, w):
    mp = min(max(3, n // 2), n)
    cum = s.rolling(n, min_periods=mp).sum()
    return (cum - cum.rolling(w, min_periods=20).mean()) / cum.rolling(w, min_periods=20).std()


def metrics(r):
    n = len(r)
    if n == 0:
        return {"ann": 0, "shp": 0, "dd": 0, "cal": 0}
    eq = (1 + r).cumprod()
    tot = float(eq.iloc[-1] - 1)
    ann = float((1 + tot) ** (252 / max(n, 1)) - 1)
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    shp = float(r.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    dd = float((eq / eq.cummax() - 1).min())
    cal = float(ann / abs(dd)) if dd < 0 else 0.0
    return {"ann": ann, "shp": shp, "dd": dd, "cal": cal}


def run(df, sig):
    sig = sig.clip(0, 1).reindex(df.index).ffill().fillna(0.0)
    ret = df["ret"].fillna(0)
    pos = sig.shift(1).fillna(0)
    turn = pos.diff().abs().fillna(0)
    strat = pos * ret - turn * COMMISSION
    idx = df.index[WARMUP:]
    s = strat.loc[idx]; p = pos.loc[idx]; b = ret.loc[idx]; t = turn.loc[idx]
    oos = idx >= pd.Timestamp(OOS_START)
    out = {}
    for tag, msk in [("all", np.ones(len(idx), bool)), ("is", ~oos), ("oos", oos)]:
        m = metrics(s[msk]); bm = metrics(b[msk])
        yrs = msk.sum() / 252
        out[tag] = {**m, "exp": float(p[msk].mean()),
                    "flips_yr": float((t[msk] > 0.001).sum() / yrs) if yrs > 0 else 0,
                    "bh_cal": bm["cal"], "bh_shp": bm["shp"], "bh_ann": bm["ann"]}
    return out


def main():
    df = load()
    bh = run(df, pd.Series(1.0, index=df.index))
    print(f"sample {df.index.min().date()}..{df.index.max().date()}  IS<{OOS_START}<=OOS")
    for t in ["all", "is", "oos"]:
        print(f"  buyhold {t:<4} ann={bh[t]['bh_ann']:+.3f} shp={bh[t]['bh_shp']:+.2f} cal={bh[t]['bh_cal']:+.2f}")
    print()

    # ── factor families ──
    def f_ir_trend(thr):       # 信息比率动量 > thr → long
        return lambda d: (d["信息比率动量"] > thr).astype(float)
    def f_mom_trend(thr):
        return lambda d: (d["动量"] > thr).astype(float)
    def f_compburst(zw, thr):  # composite momentum z
        def g(d):
            z = (zwin(d["动量"], 1, zw) + zwin(d["夏普动量"], 1, zw) + zwin(d["信息比率动量"], 1, zw)) / 3
            return (z > thr).astype(float)
        return g
    def f_conc_trend(q):       # 集中度分位 > q → long (trend confirm)
        return lambda d: (d["集中度分位"] > q).astype(float)
    def f_ir_conc(thr, q):     # IR动量>thr AND 集中度分位>q
        return lambda d: ((d["信息比率动量"] > thr) & (d["集中度分位"] > q)).astype(float)
    def f_inflow_z(n, thr):    # 主力净流入 z (对照: expect无效)
        return lambda d: (zwin(d["main_inflow"], n, 90) > thr).astype(float)
    def f_derisk_dev(q):       # 乖离分位 > q → flat (de-risk), else long
        return lambda d: (~(d["乖离分位"] > q)).astype(float)
    def f_pb_mr(win):          # 估值均值回归: PB rolling 分位低→高仓
        return lambda d: (1 - d["pb"].rolling(win, min_periods=60).rank(pct=True)).clip(0, 1)
    def f_pe_mr(win):
        return lambda d: (1 - d["pe"].rolling(win, min_periods=60).rank(pct=True)).clip(0, 1)
    def f_pb_step(win, lo):    # PB 分位<lo 买入(1), >1-lo 空(0), 中间维持
        def g(d):
            r = d["pb"].rolling(win, min_periods=60).rank(pct=True)
            st = pd.Series(np.nan, index=d.index)
            st = st.where(~(r < lo), 1.0); st = st.where(~(r > 1 - lo), 0.0)
            return st.ffill().fillna(0.5)
        return g

    GRID = []
    for thr in [-0.2, 0.0, 0.2, 0.5]:
        GRID.append((f"ir_trend_{thr}", f_ir_trend(thr)))
    for thr in [0.0, 0.3, 0.6]:
        GRID.append((f"mom_trend_{thr}", f_mom_trend(thr)))
    for zw, thr in product([60, 120], [0.0, 0.5]):
        GRID.append((f"compmom_z{zw}_{thr}", f_compburst(zw, thr)))
    for q in [0.4, 0.5, 0.6]:
        GRID.append((f"conc_trend_{q}", f_conc_trend(q)))
    for thr, q in product([0.0, 0.2], [0.4, 0.5]):
        GRID.append((f"ir&conc_{thr}_{q}", f_ir_conc(thr, q)))
    for n, thr in product([5, 10], [0.0, 0.5]):
        GRID.append((f"inflow_z{n}_{thr}", f_inflow_z(n, thr)))
    for q in [0.85, 0.9]:
        GRID.append((f"derisk_dev_{q}", f_derisk_dev(q)))
    for win in [252, 504]:
        GRID.append((f"pb_mr_{win}", f_pb_mr(win)))
        GRID.append((f"pe_mr_{win}", f_pe_mr(win)))
    for win, lo in product([252, 504], [0.2, 0.3]):
        GRID.append((f"pb_step_{win}_{lo}", f_pb_step(win, lo)))

    rows = []
    for name, fn in GRID:
        r = run(df, fn(df))
        rows.append({"factor": name,
                     "is_cal": r["is"]["cal"], "oos_cal": r["oos"]["cal"], "all_cal": r["all"]["cal"],
                     "is_shp": r["is"]["shp"], "oos_shp": r["oos"]["shp"],
                     "oos_dcal": r["oos"]["cal"] - r["oos"]["bh_cal"],
                     "oos_dshp": r["oos"]["shp"] - r["oos"]["bh_shp"],
                     "exp": r["all"]["exp"], "flips_yr": r["all"]["flips_yr"]})
    res = pd.DataFrame(rows).sort_values("oos_cal", ascending=False)
    res.to_csv(BASE / "sector_backtest_summary.csv", index=False)
    print("Factor sweep — sorted by OOS Calmar (buyhold OOS cal={:+.2f}):".format(bh["oos"]["bh_cal"]))
    print(f"  {'factor':<20}{'IS_cal':>7}{'OOS_cal':>8}{'OOSdcal':>8}{'IS_shp':>7}{'OOS_shp':>8}{'exp':>6}{'flip/y':>7}")
    for _, x in res.iterrows():
        print(f"  {x['factor']:<20}{x['is_cal']:>+7.2f}{x['oos_cal']:>+8.2f}{x['oos_dcal']:>+8.2f}"
              f"{x['is_shp']:>+7.2f}{x['oos_shp']:>+8.2f}{x['exp']:>6.2f}{x['flips_yr']:>7.1f}")


if __name__ == "__main__":
    main()
