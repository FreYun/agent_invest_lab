"""Robustness sweep — does the retail-flow contrarian signal survive parameter perturbation?

Grid:
  signal source  ∈ {personal_index_eq, personal_equity_net, inst_index_eq, personal_minus_inst}
  cumN           ∈ {5, 10, 15, 20}
  z_win          ∈ {30, 60, 90}
  threshold      ∈ {0.5, 1.0, 1.5}     # symmetric step

→ 4 × 4 × 3 × 3 = 144 configs × 4 indices = 576 backtests.

Question we're answering: Is the "best variant" (retail_z_step_10) winning because
the underlying signal is robust, or just lucky parameters? If most configs in the
neighborhood of step_10 also beat buyhold, the signal is real-ish. If only a thin
slice of parameter space wins, it's overfitting.
"""
from __future__ import annotations
from pathlib import Path
from itertools import product

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/sr_factor")
DATA = BASE / "data"
COMMISSION = 0.0005

UNIVERSE = {
    "hs300":  "510300.SH.csv",
    "zz1000": "512100.SH.csv",
    "semi":   "512480.SH.csv",
    "sc50":   "588800.SH.csv",
}

SIGNAL_SOURCES = ["personal_index_eq", "personal_equity_net", "inst_index_eq", "personal_minus_inst"]
CUM_NS = [5, 10, 15, 20]
Z_WINS = [30, 60, 90]
THRESHOLDS = [0.5, 1.0, 1.5]


def load_close(label: str) -> pd.Series:
    df = pd.read_csv(DATA / UNIVERSE[label], parse_dates=[0])
    df.columns = [c.lower() for c in df.columns]
    df = df.set_index(df.columns[0]).sort_index()
    s = df["close"].astype(float)
    s.index = pd.to_datetime(s.index).normalize()
    return s


def load_sr_signals() -> pd.DataFrame:
    sr = pd.read_csv(BASE / "sr_daily.csv", parse_dates=["date"])
    p = sr.pivot_table(index="date", columns=["persona", "fund_type"], values="net", aggfunc="sum").sort_index()
    p.columns = [f"{a}_{b}" for a, b in p.columns]
    p.index = pd.to_datetime(p.index).normalize()
    out = pd.DataFrame(index=p.index)
    out["personal_index_eq"] = p.get("个人_指数型-股票", pd.Series(0, index=p.index))
    out["inst_index_eq"] = p.get("机构_指数型-股票", pd.Series(0, index=p.index))
    eq_pers_cols = [c for c in p.columns if c.startswith("个人_") and any(t in c for t in ["指数型-股票","股票型","混合型-偏股"])]
    out["personal_equity_net"] = p[eq_pers_cols].sum(axis=1)
    eq_inst_cols = [c for c in p.columns if c.startswith("机构_") and any(t in c for t in ["指数型-股票","股票型","混合型-偏股"])]
    out["inst_equity_net"] = p[eq_inst_cols].sum(axis=1)
    out["personal_minus_inst"] = out["personal_equity_net"] - out["inst_equity_net"]
    return out


def cumN_z(s: pd.Series, n: int, z_win: int) -> pd.Series:
    cum = s.rolling(n, min_periods=max(3, n // 2)).sum()
    mu = cum.rolling(z_win, min_periods=20).mean()
    sd = cum.rolling(z_win, min_periods=20).std()
    return (cum - mu) / sd


def make_signal(close: pd.Series, sr: pd.DataFrame, src: str, cumN: int, z_win: int, thr: float) -> pd.Series:
    z = cumN_z(sr[src], cumN, z_win).reindex(close.index, method="ffill")
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(z > thr), 0.0)
    state = state.where(~(z < -thr), 1.0)
    return state.ffill().fillna(0.5)


def backtest(close: pd.Series, sig: pd.Series) -> dict:
    sig = sig.clip(0.0, 1.0).fillna(0.0).reindex(close.index).fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    turnover = pos.diff().abs().fillna(0.0)
    cost = turnover * COMMISSION
    strat_ret = pos * ret - cost
    sr_ = strat_ret.iloc[60:]
    pos_e = pos.iloc[60:]
    bh = ret.iloc[60:]
    n = len(sr_)
    if n == 0: return {"error": "empty"}
    eq = (1.0 + sr_).cumprod()
    eq_bh = (1.0 + bh).cumprod()
    total_return = float(eq.iloc[-1] - 1.0)
    annual_return = float((1.0 + total_return) ** (252.0 / max(n, 1)) - 1.0)
    daily_std = float(sr_.std(ddof=1)) if n > 1 else 0.0
    sharpe = float(sr_.mean() / daily_std * np.sqrt(252.0)) if daily_std > 0 else 0.0
    peak = eq.cummax()
    max_dd = float((eq / peak - 1.0).min())
    calmar = float(annual_return / abs(max_dd)) if max_dd < 0 else 0.0
    flips = int((turnover.iloc[60:] > 0.001).sum())
    avg_exposure = float(pos_e.mean())
    bh_ann = float(((1.0 + bh).cumprod().iloc[-1]) ** (252.0 / max(n, 1)) - 1.0)
    bh_std = float(bh.std(ddof=1))
    bh_sharpe = float(bh.mean() / bh_std * np.sqrt(252.0)) if bh_std > 0 else 0.0
    peak_bh = (1.0 + bh).cumprod().cummax()
    bh_max_dd = float(((1.0 + bh).cumprod() / peak_bh - 1.0).min())
    bh_cal = float(bh_ann / abs(bh_max_dd)) if bh_max_dd < 0 else 0.0
    return {"ret": total_return, "ann": annual_return, "shp": sharpe, "dd": max_dd, "cal": calmar,
            "flips": flips, "exp": avg_exposure,
            "bh_ann": bh_ann, "bh_shp": bh_sharpe, "bh_dd": bh_max_dd, "bh_cal": bh_cal}


def main():
    sr = load_sr_signals()
    closes = {k: load_close(k) for k in UNIVERSE}
    # restrict to overlap
    sr_start = sr.index.min(); sr_end = sr.index.max()
    closes = {k: c.loc[sr_start:sr_end] for k, c in closes.items()}

    # buyhold reference per index
    bh_ref = {}
    for k, c in closes.items():
        m = backtest(c, pd.Series(1.0, index=c.index))
        bh_ref[k] = m
        print(f"  buyhold {k:<8}  shp={m['shp']:+.2f}  cal={m['cal']:+.2f}  dd={m['dd']:+.2f}")
    print()

    rows = []
    n_configs = len(SIGNAL_SOURCES) * len(CUM_NS) * len(Z_WINS) * len(THRESHOLDS)
    print(f"Running {n_configs} configs × 4 indices = {n_configs*4} backtests ...")
    for i, (src, cumN, z_win, thr) in enumerate(product(SIGNAL_SOURCES, CUM_NS, Z_WINS, THRESHOLDS)):
        for idx_name, close in closes.items():
            sig = make_signal(close, sr, src, cumN, z_win, thr)
            m = backtest(close, sig)
            rows.append({
                "src": src, "cumN": cumN, "z_win": z_win, "thr": thr, "index": idx_name,
                **{k: m.get(k) for k in ["shp","cal","dd","exp","flips","ann","ret"]},
                "bh_shp": bh_ref[idx_name]["shp"],
                "bh_cal": bh_ref[idx_name]["cal"],
                "dshp": m["shp"] - bh_ref[idx_name]["shp"],
                "dcal": m["cal"] - bh_ref[idx_name]["cal"],
                "beats_bh_shp": int(m["shp"] > bh_ref[idx_name]["shp"]),
                "beats_bh_cal": int(m["cal"] > bh_ref[idx_name]["cal"]),
            })
    df = pd.DataFrame(rows)
    df.to_csv(BASE / "robustness_full.csv", index=False)
    print(f"Wrote {BASE / 'robustness_full.csv'}  ({len(df)} rows)")

    # ── aggregate: per-config beats_bh_X across 4 indices (0..4) ──
    agg = df.groupby(["src","cumN","z_win","thr"]).agg(
        beats_shp_count=("beats_bh_shp","sum"),
        beats_cal_count=("beats_bh_cal","sum"),
        mean_dshp=("dshp","mean"),
        median_dshp=("dshp","median"),
        mean_dcal=("dcal","mean"),
        median_dcal=("dcal","median"),
        mean_exp=("exp","mean"),
        mean_flips=("flips","mean"),
    ).reset_index()
    agg.to_csv(BASE / "robustness_agg.csv", index=False)

    # ── overall robustness summary ──
    print()
    print("══ Robustness summary across all 144 configs ══")
    print(f"  Configs beating buyhold Sharpe in all 4 indices: {(agg['beats_shp_count']==4).sum()} / {len(agg)} = {(agg['beats_shp_count']==4).mean():.0%}")
    print(f"  Configs beating buyhold Calmar in all 4 indices: {(agg['beats_cal_count']==4).sum()} / {len(agg)} = {(agg['beats_cal_count']==4).mean():.0%}")
    print(f"  Configs beating buyhold Sharpe in ≥3 indices:    {(agg['beats_shp_count']>=3).sum()} / {len(agg)} = {(agg['beats_shp_count']>=3).mean():.0%}")
    print(f"  Configs beating buyhold Calmar in ≥3 indices:    {(agg['beats_cal_count']>=3).sum()} / {len(agg)} = {(agg['beats_cal_count']>=3).mean():.0%}")
    print(f"  Median dSharpe (config−buyhold, avg over indices): {agg['mean_dshp'].median():+.3f}")
    print(f"  Mean   dSharpe:                                     {agg['mean_dshp'].mean():+.3f}")
    print(f"  Median dCalmar:                                    {agg['mean_dcal'].median():+.3f}")
    print(f"  Mean   dCalmar:                                    {agg['mean_dcal'].mean():+.3f}")

    # ── per-source summary ──
    print()
    print("══ Per signal-source (averaged over cumN, z_win, thr — 36 configs each):")
    by_src = df.groupby("src").agg(
        beats_shp_pct=("beats_bh_shp","mean"),
        beats_cal_pct=("beats_bh_cal","mean"),
        mean_dshp=("dshp","mean"),
        mean_dcal=("dcal","mean"),
        mean_exp=("exp","mean"),
    )
    print(by_src.to_string())

    # ── per (cumN, z_win) heatmap of beats_bh_cal_count (out of 4) — averaged over src, thr ──
    print()
    print("══ Heatmap: cumN × z_win — mean #indices beating buyhold Calmar (max=4)")
    print("    (averaged over 4 signal-sources × 3 thresholds = 12 configs per cell)")
    heat = df.groupby(["cumN","z_win"])["beats_bh_cal"].mean() * 4
    print(heat.unstack().to_string(float_format=lambda x: f"{x:.2f}"))

    print()
    print("══ Top-15 configs by (mean dCalmar across 4 indices) — sanity check on best params:")
    top = agg.sort_values("mean_dcal", ascending=False).head(15)
    print(top.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
    print()
    print("══ Bottom-15 configs (worst losers):")
    bot = agg.sort_values("mean_dcal").head(15)
    print(bot.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))


if __name__ == "__main__":
    main()
