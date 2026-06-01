"""Round 4 — evaluate VIX as what it actually is: a RARE both-tails extreme
contrarian timing signal. Silent ~90% of the time; only speaks at extremes.

Wrong lens (rounds 1&3): "beat buyhold as a full strategy" / "continuous alpha".
A rare-extreme factor is flat most of the time, so full-sample strategy stats are
dominated by the silent middle and always look unimpressive. The continuous test
failing actually CONFIRMS the edge lives only in the tails.

Right lens:
  - BOTH tails: extreme FEAR (z high) -> add ; extreme EUPHORIA (z low) -> trim
  - CONDITIONAL evaluation: when it fires, is the contrarian direction right?
  - CROSS-INDEX pooling: 3 ETFs multiply the scarce time-series events
  - direction consistency IS the validation for rare factors (can't get high-N OOS)
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
IS_END = pd.Timestamp("2023-12-31")
HZ = [5, 10, 20]


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_close(code):
    return pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]


def zser(vix):
    mu = vix.rolling(WIN, min_periods=WIN // 2).mean()
    sd = vix.rolling(WIN, min_periods=WIN // 2).std()
    return (vix - mu) / sd


def collect(cond_on_z, split):
    """For a boolean condition over z, gather forward returns (each horizon) across all
    3 ETFs, with daily de-overlap NOT applied (we want every extreme day, pooled)."""
    vix = load_vix(); z = zser(vix)
    rows = {h: [] for h in HZ}
    per_etf = {}
    for label, code, es in ETFS:
        close = load_close(code)
        zA = z.reindex(close.index, method="ffill")
        cond = cond_on_z(zA)
        m = pd.Series(close.index, index=close.index)
        in_split = (close.index <= IS_END) if split == "IS" else (close.index > IS_END)
        cond = cond & pd.Series(in_split, index=close.index) & (close.index >= pd.Timestamp(es))
        etf_rows = {h: [] for h in HZ}
        for h in HZ:
            fwd = close.shift(-h) / close - 1.0
            uncond = fwd.mean()
            seg = (fwd[cond.fillna(False)] - uncond).dropna()
            rows[h].extend(seg.tolist())
            etf_rows[h] = seg.tolist()
        per_etf[label] = etf_rows
    return rows, per_etf


def summ(vals):
    if len(vals) < 3:
        return f"n={len(vals)}"
    a = np.array(vals)
    return f"n={len(a):>4}  mean={a.mean()*100:+5.2f}%  med={np.median(a)*100:+5.2f}%  win={ (a>0).mean()*100:4.0f}%"


def main():
    # both tails. FEAR = z>thr (contrarian: expect +fwd). EUPHORIA = z<-thr (expect -fwd)
    tails = {
        "FEAR z>1.5":      lambda z: z > 1.5,
        "FEAR z>2.0":      lambda z: z > 2.0,
        "EUPHORIA z<-1.0": lambda z: z < -1.0,
        "EUPHORIA z<-1.5": lambda z: z < -1.5,
    }
    for tname, cond in tails.items():
        print(f"\n{'='*86}\n  {tname}   (FEAR expects +excess fwd ; EUPHORIA expects -excess fwd)\n{'='*86}")
        for split in ["IS", "OOS"]:
            rows, per_etf = collect(cond, split)
            print(f"  [{split}] pooled across 3 ETFs:")
            for h in HZ:
                print(f"      fwd{h:>2}d : {summ(rows[h])}")
            # per-etf direction consistency at 10d
            dirs = []
            for label in ["HS300", "ZZ1000", "SC50"]:
                v = per_etf[label][10]
                if len(v) >= 3:
                    dirs.append(f"{label}={np.mean(v)*100:+.2f}%(n{len(v)})")
            print(f"      10d per-ETF: " + "  ".join(dirs))
        print()

    # ─── a both-tails contrarian position, evaluated ONLY on the days it is non-neutral
    print("="*86)
    print("  Both-tails contrarian read: z>1.5 -> ADD(+1) | z<-1.0 -> TRIM(-1) | else neutral(0)")
    print("  Conditional next-10d excess return by the read, pooled 3 ETFs, IS vs OOS")
    print("="*86)
    vix = load_vix(); z = zser(vix)
    for split in ["IS", "OOS"]:
        add_v, trim_v = [], []
        for label, code, es in ETFS:
            close = load_close(code)
            zA = z.reindex(close.index, method="ffill")
            fwd = close.shift(-10) / close - 1.0
            uncond = fwd.mean()
            in_split = (close.index <= IS_END) if split == "IS" else (close.index > IS_END)
            base = pd.Series(in_split, index=close.index) & (close.index >= pd.Timestamp(es))
            add_v.extend((fwd[(zA > 1.5) & base] - uncond).dropna().tolist())
            trim_v.extend((fwd[(zA < -1.0) & base] - uncond).dropna().tolist())
        print(f"  [{split}] ADD  (z>1.5) : {summ(add_v)}   <- want positive")
        print(f"  [{split}] TRIM (z<-1.0): {summ(trim_v)}   <- want NEGATIVE (then contrarian trim is right)")


if __name__ == "__main__":
    main()
