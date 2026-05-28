"""Subscription/redemption × index forward-return diagnostic.

NO factor backtesting. Just structural diagnostic:
  - long-format SR data → pivoted daily series of net flows by (persona × fund_type)
  - candidate signal series (raw + rolling-z transforms)
  - correlation with forward 1/5/10/20-day returns on each of 4 indices
  - quantile group analysis: split signal into Q1..Q5, compute mean forward return per quantile

This is exploratory. n ≈ 16 months stable data — NO IS/OOS validation, NO multiplicity
correction, NO trading signal. The output is "is there structure worth modeling".
"""
from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/sr_factor")
DATA = BASE / "data"

# Indices: short label → file under data/
UNIVERSE = {
    "hs300":   "510300.SH.csv",
    "zz1000":  "512100.SH.csv",
    "semi":    "512480.SH.csv",
    "sc50":    "588800.SH.csv",
}

FUND_TYPES_EQUITY = ["指数型-股票", "股票型", "混合型-偏股"]
FUND_TYPE_PRIMARY = "指数型-股票"  # most direct flow indicator

PERSONAS = ["个人", "机构"]

FWD_HORIZONS = [1, 5, 10, 20]
ROLL_Z_WIN = 60


def load_sr() -> pd.DataFrame:
    df = pd.read_csv(BASE / "sr_daily.csv", parse_dates=["date"])
    return df


def load_index_close() -> dict[str, pd.Series]:
    out = {}
    for k, fn in UNIVERSE.items():
        df = pd.read_csv(DATA / fn, parse_dates=[0])
        df.columns = [c.lower() for c in df.columns]
        date_col = df.columns[0]
        df = df.set_index(date_col).sort_index()
        out[k] = df["close"].astype(float)
    return out


def build_signals(sr: pd.DataFrame) -> pd.DataFrame:
    """Return DataFrame indexed by date, columns = candidate signals (raw values)."""
    # pivot to (date) × (persona, fund_type) for net column
    p = sr.pivot_table(
        index="date", columns=["persona", "fund_type"], values="net", aggfunc="sum",
    ).sort_index()
    p.columns = [f"{a}_{b}" for a, b in p.columns]

    sig = pd.DataFrame(index=p.index)
    # Raw daily net flows
    for c in p.columns:
        sig[c] = p[c]
    # Aggregates
    eq_cols = [c for c in p.columns if any(ft in c for ft in FUND_TYPES_EQUITY)]
    sig["all_equity_net"] = p[eq_cols].sum(axis=1)
    sig["personal_equity_net"] = p[[f"个人_{ft}" for ft in FUND_TYPES_EQUITY if f"个人_{ft}" in p.columns]].sum(axis=1)
    sig["inst_equity_net"] = p[[f"机构_{ft}" for ft in FUND_TYPES_EQUITY if f"机构_{ft}" in p.columns]].sum(axis=1)
    sig["index_eq_net"] = p[[c for c in p.columns if FUND_TYPE_PRIMARY in c]].sum(axis=1)
    sig["personal_index_eq"] = p.get(f"个人_{FUND_TYPE_PRIMARY}", pd.Series(0, index=p.index))
    sig["inst_index_eq"] = p.get(f"机构_{FUND_TYPE_PRIMARY}", pd.Series(0, index=p.index))
    sig["personal_minus_inst"] = sig["personal_equity_net"] - sig["inst_equity_net"]

    # Derived: rolling cumsum (5d, 20d) and rolling z (60d) — per methodology III-Z
    derived: dict[str, pd.Series] = {}
    for c in ["personal_equity_net", "inst_equity_net", "personal_index_eq", "inst_index_eq", "personal_minus_inst", "all_equity_net"]:
        s = sig[c]
        derived[f"{c}_cum5"] = s.rolling(5, min_periods=1).sum()
        derived[f"{c}_cum20"] = s.rolling(20, min_periods=5).sum()
        # rolling z over 60d
        rmean = s.rolling(ROLL_Z_WIN, min_periods=20).mean()
        rstd = s.rolling(ROLL_Z_WIN, min_periods=20).std()
        derived[f"{c}_z60"] = (s - rmean) / rstd
    for k, v in derived.items():
        sig[k] = v
    return sig


def align_and_forward(close: pd.Series, sig: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    """Return df indexed by date with cols: signal cols + fwd{h}_ret for each horizon.
    Only rows where signal known on day t and price known on t and t+h."""
    close_d = pd.Series(close.values, index=pd.to_datetime(close.index).normalize())
    sig_d = sig.copy()
    sig_d.index = pd.to_datetime(sig_d.index).normalize()
    # Use union of dates from sig that fall in close's range
    df = sig_d.copy()
    # forward returns
    fr: dict[str, pd.Series] = {}
    for h in horizons:
        fwd = close_d.shift(-h) / close_d - 1.0
        fr[f"fwd{h}"] = fwd
    fr_df = pd.DataFrame(fr)
    # join: only on dates that exist in close index (true trading days)
    df = df.join(fr_df, how="inner")
    return df


def corr_table(joined: pd.DataFrame, signal_cols: list[str], horizons: list[int]) -> pd.DataFrame:
    rows = []
    for s in signal_cols:
        row = {"signal": s, "n": int(joined[s].dropna().shape[0])}
        for h in horizons:
            sub = joined[[s, f"fwd{h}"]].dropna()
            if len(sub) >= 30:
                row[f"r_fwd{h}"] = float(sub[s].corr(sub[f"fwd{h}"], method="pearson"))
                row[f"sp_fwd{h}"] = float(sub[s].corr(sub[f"fwd{h}"], method="spearman"))
            else:
                row[f"r_fwd{h}"] = None
                row[f"sp_fwd{h}"] = None
        rows.append(row)
    return pd.DataFrame(rows)


def quantile_groups(joined: pd.DataFrame, signal: str, horizon: int, q: int = 5) -> pd.DataFrame:
    sub = joined[[signal, f"fwd{horizon}"]].dropna()
    if len(sub) < q * 10:
        return pd.DataFrame()
    sub = sub.copy()
    sub["bin"] = pd.qcut(sub[signal], q=q, labels=[f"Q{i+1}" for i in range(q)], duplicates="drop")
    g = sub.groupby("bin", observed=True)[f"fwd{horizon}"].agg(["mean", "std", "count"])
    g["t"] = g["mean"] / (g["std"] / np.sqrt(g["count"].clip(lower=1)))
    return g


def main():
    sr = load_sr()
    closes = load_index_close()
    sig = build_signals(sr)
    sig_cols = sig.columns.tolist()
    print(f"\n══ Loaded SR rows={len(sr)}  signals={len(sig_cols)}  date range={sig.index.min().date()}..{sig.index.max().date()}\n")

    # Choose a representative set of signals for the table (avoid noise from raw 18 cells)
    summary_signals = [
        "personal_index_eq", "inst_index_eq",
        "personal_equity_net", "inst_equity_net",
        "all_equity_net", "personal_minus_inst",
        "personal_index_eq_cum5", "personal_index_eq_cum20",
        "personal_index_eq_z60",
        "inst_index_eq_cum5", "inst_index_eq_cum20", "inst_index_eq_z60",
        "all_equity_net_cum5", "all_equity_net_cum20", "all_equity_net_z60",
    ]
    summary_signals = [s for s in summary_signals if s in sig_cols]

    # ── correlation table per index ──
    for idx_name, close in closes.items():
        joined = align_and_forward(close, sig, FWD_HORIZONS)
        # restrict to dates with at least the 20-day cum signal available
        ct = corr_table(joined, summary_signals, FWD_HORIZONS)
        ct.to_csv(BASE / f"corr_{idx_name}.csv", index=False)
        print(f"══ {idx_name.upper()}  joined_rows={len(joined)}  cor table top-by-|r_fwd5|:")
        ct_sorted = ct.copy()
        ct_sorted["abs_r5"] = ct_sorted["r_fwd5"].abs()
        ct_sorted = ct_sorted.sort_values("abs_r5", ascending=False, na_position="last")
        cols_show = ["signal", "n", "r_fwd1", "r_fwd5", "r_fwd10", "r_fwd20",
                     "sp_fwd1", "sp_fwd5", "sp_fwd10", "sp_fwd20"]
        with pd.option_context("display.max_rows", None, "display.width", 200, "display.float_format", lambda x: f"{x:+.3f}" if pd.notna(x) else "  -  "):
            print(ct_sorted[cols_show].to_string(index=False))

        # Quantile groups for the top-3 |r_fwd5| signals
        print(f"\n  -- quantile groups (h=5) for top-3 signals --")
        for s in ct_sorted["signal"].head(3):
            qg = quantile_groups(joined, s, 5, q=5)
            if not qg.empty:
                print(f"  signal={s}  (fwd5 by quintile)")
                with pd.option_context("display.float_format", lambda x: f"{x:+.4f}" if pd.notna(x) else "  -  "):
                    print(qg.to_string())
        print()

    print("Done.")


if __name__ == "__main__":
    main()
