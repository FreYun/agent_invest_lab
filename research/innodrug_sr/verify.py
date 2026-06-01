"""Verification pass — address two red flags from sweep.py:
  (1) top configs all sat at thr=1.5 (grid edge) → extend thr to {1.0,1.5,2.0,2.5}
  (2) 159839/512010 buyhold Calmar ~0 → dCalmar inflated by tiny denominator.
      Look at ABSOLUTE Calmar/Sharpe/DD/exposure on the primary proxy 159992.
  (3) <2yr sample → no monte-carlo; instead split-half (H1/H2) consistency check.
"""
from __future__ import annotations
from pathlib import Path
from itertools import product
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"
COMMISSION = 0.0005
WARMUP = 110

UNIVERSE = {
    "innodrug_159992": "159992.SZ.csv",
    "innodrug_516080": "516080.SH.csv",
    "innodrug_159839": "159839.SZ.csv",
    "yiyao_512010":    "512010.SH.csv",
}


def load_close(fname):
    df = pd.read_csv(DATA / fname, parse_dates=[0])
    df.columns = [c.lower() for c in df.columns]
    df = df.set_index(df.columns[0]).sort_index()
    s = df["close"].astype(float)
    s.index = pd.to_datetime(s.index).normalize()
    return s


def load_sr():
    sr = pd.read_csv(BASE / "innodrug_sr_daily.csv", parse_dates=["date"])
    sr = sr[sr["persona"] == "个人"].set_index("date").sort_index()
    sr.index = pd.to_datetime(sr.index).normalize()
    sr["imbalance"] = (sr["applied"] - sr["redeemed"]) / (sr["applied"] + sr["redeemed"]).replace(0, np.nan)
    return sr[["applied", "redeemed", "net", "net_ex_aip", "imbalance"]]


def cumN_z(s, n, z_win):
    cum = s.rolling(n, min_periods=max(3, n // 2)).sum()
    mu = cum.rolling(z_win, min_periods=20).mean()
    sd = cum.rolling(z_win, min_periods=20).std()
    return (cum - mu) / sd


def make_signal(close, sr, src, cumN, z_win, thr):  # contrarian only
    z = cumN_z(sr[src], cumN, z_win).reindex(close.index, method="ffill")
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(z > thr), 0.0)
    state = state.where(~(z < -thr), 1.0)
    return state.ffill().fillna(0.5)


def metrics_of(r, n):
    eq = (1 + r).cumprod()
    tot = float(eq.iloc[-1] - 1)
    ann = float((1 + tot) ** (252 / max(n, 1)) - 1)
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    shp = float(r.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    dd = float((eq / eq.cummax() - 1).min())
    cal = float(ann / abs(dd)) if dd < 0 else 0.0
    return {"ann": ann, "shp": shp, "dd": dd, "cal": cal}


def backtest(close, sig, eval_slice=None):
    sig = sig.clip(0, 1).fillna(0).reindex(close.index).fillna(0)
    ret = close.pct_change().fillna(0)
    pos = sig.shift(1).fillna(0)
    turnover = pos.diff().abs().fillna(0)
    strat = pos * ret - turnover * COMMISSION
    s = strat.iloc[WARMUP:]
    pos_e = pos.iloc[WARMUP:]
    bh = ret.iloc[WARMUP:]
    if eval_slice is not None:
        s, pos_e, bh = s.iloc[eval_slice], pos_e.iloc[eval_slice], bh.iloc[eval_slice]
    n = len(s)
    if n == 0:
        return None
    m = metrics_of(s, n)
    m["exp"] = float(pos_e.mean())
    m["flips_yr"] = float((turnover.iloc[WARMUP:].iloc[eval_slice] if eval_slice is not None
                           else turnover.iloc[WARMUP:]).gt(0.001).sum()) / (n / 252)
    b = metrics_of(bh, n)
    m["bh_cal"], m["bh_shp"], m["bh_dd"], m["bh_ann"] = b["cal"], b["shp"], b["dd"], b["ann"]
    return m


def main():
    sr = load_sr()
    closes = {k: load_close(v).loc[sr.index.min():sr.index.max()] for k, v in UNIVERSE.items()}

    # ── (1) extended thr robustness (contrarian) ──
    SOURCES = ["net", "net_ex_aip", "imbalance"]
    CUM_NS = [5, 10, 15, 20]
    Z_WINS = [45, 60, 90]
    THRS = [1.0, 1.5, 2.0, 2.5]
    rows = []
    bh_cal = {k: backtest(c, pd.Series(1.0, index=c.index))["bh_cal"] for k, c in closes.items()}
    for src, n_, zw, thr in product(SOURCES, CUM_NS, Z_WINS, THRS):
        beats = 0
        for k, c in closes.items():
            m = backtest(c, make_signal(c, sr, src, n_, zw, thr))
            beats += int(m["cal"] > bh_cal[k])
        rows.append({"src": src, "cumN": n_, "z_win": zw, "thr": thr, "beats_n": beats})
    ext = pd.DataFrame(rows)
    print("══ (1) Extended thr robustness — mean #ETFs beating buyhold Calmar by thr:")
    print(ext.groupby("thr")["beats_n"].agg(["mean", lambda x: (x == 4).mean()])
          .rename(columns={"mean": "mean_beats(of4)", "<lambda_0>": "frac_all4"})
          .to_string(float_format=lambda x: f"{x:.2f}"))
    print()

    # ── (2) ABSOLUTE metrics on primary 159992 for a few honest center configs ──
    print("══ (2) Primary proxy 159992 — ABSOLUTE metrics (buyhold: ann=+0.13 shp=0.61 cal=0.52 dd=-0.25)")
    c992 = closes["innodrug_159992"]
    reps = [("net", 15, 90, 1.0), ("net", 10, 60, 1.0), ("imbalance", 15, 90, 1.0),
            ("imbalance", 20, 90, 1.0), ("net", 10, 60, 1.5), ("imbalance", 15, 60, 1.5)]
    print(f"  {'config':<28} {'ann':>7} {'shp':>6} {'cal':>6} {'dd':>7} {'exp':>5} {'flip/y':>7}")
    for src, n_, zw, thr in reps:
        m = backtest(c992, make_signal(c992, sr, src, n_, zw, thr))
        tag = f"{src}_{n_}_{zw}_thr{thr}"
        print(f"  {tag:<28} {m['ann']:+.3f} {m['shp']:+.2f} {m['cal']:+.2f} {m['dd']:+.3f} "
              f"{m['exp']:.2f} {m['flips_yr']:.1f}")
    print()

    # ── (3) split-half consistency on 159992 (H1 vs H2) ──
    print("══ (3) Split-half consistency on 159992 (does it work in BOTH halves?)")
    n_eval = len(c992) - WARMUP
    half = n_eval // 2
    print(f"  eval bars={n_eval}, H1=[0:{half}] H2=[{half}:{n_eval}]")
    print(f"  {'config':<28} {'H1_cal':>7} {'H2_cal':>7} {'H1_shp':>7} {'H2_shp':>7} {'H1bh':>6} {'H2bh':>6}")
    for src, n_, zw, thr in reps:
        sig = make_signal(c992, sr, src, n_, zw, thr)
        h1 = backtest(c992, sig, eval_slice=slice(0, half))
        h2 = backtest(c992, sig, eval_slice=slice(half, n_eval))
        tag = f"{src}_{n_}_{zw}_thr{thr}"
        print(f"  {tag:<28} {h1['cal']:+.2f}  {h2['cal']:+.2f}  {h1['shp']:+.2f}  {h2['shp']:+.2f}  "
              f"{h1['bh_cal']:+.2f} {h2['bh_cal']:+.2f}")


if __name__ == "__main__":
    main()
