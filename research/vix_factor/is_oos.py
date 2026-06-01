"""IS/OOS split + per-year breakdown + trigger-event audit.

Addresses two questions the full-sample sweep left open:
  (1) Was z=1.5 a real signal or in-sample selection bias? -> pick the best z on IS
      (2016-2023) ONLY, then confirm OOS (2024-2026/05) untouched.
  (2) 2025-2026 effectiveness -> per-calendar-year strategy vs buyhold + event list.

Causal checks (no lookahead): z = trailing rolling(252); pulse = trailing rolling(H).max();
entry = sig.shift(1) (T+1); VIX aligned to ETF dates by ffill (VIX_T visible at T close).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/vix_factor")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIX = BASE / "data" / "vix_50etf.csv"
WIN, H, BASEPOS, COMM = 252, 10, 0.5, 0.0005
ETFS = [("HS300", "510300.SH", "2016-06-01"), ("ZZ1000", "512100.SH", "2016-06-01"),
        ("SC50", "588800.SH", "2023-09-01")]
IS_END = "2023-12-31"
OOS_START = "2024-01-01"
END = "2026-05-26"


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_close(code):
    return pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()["close"]


def zscore(vix):
    mu = vix.rolling(WIN, min_periods=WIN // 2).mean()
    sd = vix.rolling(WIN, min_periods=WIN // 2).std()
    return (vix - mu) / sd


def signal(z, idx, thr, H, base):
    pulse = (z > thr).astype(float).rolling(H, min_periods=1).max().fillna(0.0)
    sig = (base + (1 - base) * pulse).clip(0, 1)
    return sig.reindex(idx, method="ffill").fillna(base)


def metrics(close, sig, start, end):
    sig = sig.clip(0, 1).reindex(close.index).ffill().fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    turn = pos.diff().abs().fillna(pos.iloc[0])
    strat = pos * ret - turn * COMM
    m = (close.index >= pd.Timestamp(start)) & (close.index <= pd.Timestamp(end))
    sr, bh, pe = strat[m], ret[m], pos[m]
    if len(sr) < 20:
        return None
    eq, eqb = (1 + sr).cumprod(), (1 + bh).cumprod()
    n = len(sr)
    ann = (1 + (eq.iloc[-1] - 1)) ** (252 / n) - 1
    bann = (1 + (eqb.iloc[-1] - 1)) ** (252 / n) - 1
    shp = sr.mean() / sr.std(ddof=1) * np.sqrt(252) if sr.std() > 0 else 0
    bshp = bh.mean() / bh.std(ddof=1) * np.sqrt(252) if bh.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    bdd = (eqb / eqb.cummax() - 1).min()
    cal = ann / abs(dd) if dd < 0 else 0
    bcal = bann / abs(bdd) if bdd < 0 else 0
    return dict(ret=float(eq.iloc[-1] - 1), bh_ret=float(eqb.iloc[-1] - 1),
                ann=float(ann), sharpe=float(shp), bh_sharpe=float(bshp),
                maxdd=float(dd), bh_maxdd=float(bdd), calmar=float(cal), bh_calmar=float(bcal),
                exposure=float(pe.mean()), d_calmar=float(cal - bcal), d_sharpe=float(shp - bshp), n=n)


def main():
    vix = load_vix()
    z = zscore(vix)

    # ─── (1) PICK best z on IS ONLY, then confirm OOS ────────────────────────
    print("="*78)
    print("  STEP 1 — pick z on IS (2016..2023) only, confirm OOS (2024..2026/05) untouched")
    print("="*78)
    Z_GRID = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]
    print(f"\nIS mean ΔCalmar over HS300+ZZ1000 (SC50 not listed in IS — data starts 2023):")
    is_score = {}
    for thr in Z_GRID:
        ds = []
        for label, code, es in ETFS[:2]:
            close = load_close(code)
            sig = signal(z, close.index, thr, H, BASEPOS)
            m = metrics(close, sig, es, IS_END)
            if m: ds.append(m["d_calmar"])
        is_score[thr] = np.mean(ds)
        print(f"  z>{thr:<4}: IS mean ΔCalmar = {np.mean(ds):+.3f}   (per-etf {[round(x,3) for x in ds]})")
    best_z = max(is_score, key=is_score.get)
    print(f"\n  >>> IS-optimal z threshold = {best_z}  (selected WITHOUT touching OOS)")

    print(f"\n--- OOS confirmation @ z>{best_z}, H={H}, base={BASEPOS} (2024-01-01..2026-05-26) ---")
    print(f"{'etf':<7}{'ret':>9}{'bh_ret':>9}{'sharpe':>8}{'bh_shp':>8}{'maxdd':>8}{'bh_dd':>8}{'calmar':>8}{'bh_cal':>8}{'ΔCal':>7}{'exp':>6}")
    for label, code, es in ETFS:
        close = load_close(code)
        sig = signal(z, close.index, best_z, H, BASEPOS)
        m = metrics(close, sig, OOS_START, END)
        if m:
            print(f"{label:<7}{m['ret']:>+9.3f}{m['bh_ret']:>+9.3f}{m['sharpe']:>8.2f}{m['bh_sharpe']:>8.2f}"
                  f"{m['maxdd']:>8.2f}{m['bh_maxdd']:>8.2f}{m['calmar']:>8.2f}{m['bh_calmar']:>8.2f}{m['d_calmar']:>+7.2f}{m['exposure']:>6.2f}")

    # ─── (2) per-calendar-year breakdown @ best_z ────────────────────────────
    print("\n" + "="*78)
    print(f"  STEP 2 — per-year strategy vs buyhold @ z>{best_z}, H={H}, base={BASEPOS}")
    print("="*78)
    for label, code, es in ETFS:
        close = load_close(code)
        sig = signal(z, close.index, best_z, H, BASEPOS)
        print(f"\n{label} ({code}):")
        print(f"  {'year':<6}{'strat_ret':>11}{'bh_ret':>10}{'Δret':>9}{'strat_dd':>10}{'bh_dd':>9}{'exposure':>10}")
        for yr in range(2016, 2027):
            ys, ye = f"{yr}-01-01", f"{yr}-12-31"
            if pd.Timestamp(ye) < pd.Timestamp(es):
                continue
            m = metrics(close, sig, max(ys, es), min(ye, END))
            if m and m["n"] >= 20:
                print(f"  {yr:<6}{m['ret']:>+11.3f}{m['bh_ret']:>+10.3f}{m['ret']-m['bh_ret']:>+9.3f}"
                      f"{m['maxdd']:>10.2f}{m['bh_maxdd']:>9.2f}{m['exposure']:>10.2f}")

    # ─── (3) trigger events 2024-2026 (the recent regime) ────────────────────
    print("\n" + "="*78)
    print(f"  STEP 3 — z>{best_z} trigger events since 2024 (10d fwd return on each ETF)")
    print("="*78)
    trig = z[(z > best_z) & (z.index >= pd.Timestamp("2024-01-01"))]
    # collapse to episode starts (gap > H days = new episode)
    starts = []
    prev = None
    for d in trig.index:
        if prev is None or (d - prev).days > 18:
            starts.append(d)
        prev = d
    print(f"  episode starts: {len(starts)}")
    print(f"  {'date':<12}{'z':>6}" + "".join(f"{l+'_10d':>12}" for l, _, _ in ETFS))
    for d in starts:
        row = f"  {d.strftime('%Y-%m-%d'):<12}{z.loc[d]:>6.2f}"
        for label, code, es in ETFS:
            close = load_close(code)
            # forward 10d from first ETF date >= d
            fwd = close[close.index >= d]
            if len(fwd) > H:
                r = fwd.iloc[H] / fwd.iloc[0] - 1
                row += f"{r*100:>+11.2f}%"
            else:
                row += f"{'n/a':>12}"
        print(row)


if __name__ == "__main__":
    main()
