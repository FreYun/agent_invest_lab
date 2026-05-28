"""Exploratory backtest — retail subscription contrarian factor on 4 indices.

⚠️ NO IS/OOS split (n ≈ 16 stable months). All results are full-window in-sample.
DO NOT use for live trading without further validation.

Universe: 510300 (HS300), 512100 (ZZ1000), 512480 (semi), 588800 (双创50).
Signal: 60-day rolling z of personal_index_eq cumN; map z → position ∈ [0, 1].
Cost: 5bp one-way; signal aligned to trading days via ffill.
"""
from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/sr_factor")
DATA = BASE / "data"
COMMISSION = 0.0005
ROLL_Z_WIN = 60

UNIVERSE = {
    "hs300":   "510300.SH.csv",
    "zz1000":  "512100.SH.csv",
    "semi":    "512480.SH.csv",
    "sc50":    "588800.SH.csv",
}


# ─────────────────────────── data loading ────────────────────────────────

def load_close(label: str) -> pd.Series:
    df = pd.read_csv(DATA / UNIVERSE[label], parse_dates=[0])
    df.columns = [c.lower() for c in df.columns]
    df = df.set_index(df.columns[0]).sort_index()
    s = df["close"].astype(float)
    s.index = pd.to_datetime(s.index).normalize()
    return s


def load_sr_signals() -> pd.DataFrame:
    sr = pd.read_csv(BASE / "sr_daily.csv", parse_dates=["date"])
    p = sr.pivot_table(
        index="date", columns=["persona", "fund_type"], values="net", aggfunc="sum",
    ).sort_index()
    p.columns = [f"{a}_{b}" for a, b in p.columns]
    p.index = pd.to_datetime(p.index).normalize()
    out = pd.DataFrame(index=p.index)
    out["personal_index_eq"] = p.get("个人_指数型-股票", pd.Series(0, index=p.index))
    out["inst_index_eq"] = p.get("机构_指数型-股票", pd.Series(0, index=p.index))
    return out


def cumN_z(s: pd.Series, n: int, z_win: int = ROLL_Z_WIN) -> pd.Series:
    cum = s.rolling(n, min_periods=max(3, n // 2)).sum()
    mu = cum.rolling(z_win, min_periods=20).mean()
    sd = cum.rolling(z_win, min_periods=20).std()
    return (cum - mu) / sd


# ─────────────────────────── signal definitions ──────────────────────────

def s_buyhold(close: pd.Series, sr: pd.DataFrame) -> pd.Series:
    return pd.Series(1.0, index=close.index)


def s_retail_z_step_20(close: pd.Series, sr: pd.DataFrame) -> pd.Series:
    """z > +1 → 0；z < -1 → 1；中间维持上一态（默认 0.5）。"""
    z = cumN_z(sr["personal_index_eq"], 20)
    z = z.reindex(close.index, method="ffill")
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(z > 1.0), 0.0)
    state = state.where(~(z < -1.0), 1.0)
    return state.ffill().fillna(0.5)


def s_retail_z_step_10(close: pd.Series, sr: pd.DataFrame) -> pd.Series:
    z = cumN_z(sr["personal_index_eq"], 10)
    z = z.reindex(close.index, method="ffill")
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(z > 1.0), 0.0)
    state = state.where(~(z < -1.0), 1.0)
    return state.ffill().fillna(0.5)


def s_retail_z_linear_20(close: pd.Series, sr: pd.DataFrame) -> pd.Series:
    """Linear: pos = clip(0.5 - 0.4*z, 0, 1). z=-1.25 → 1, z=+1.25 → 0, z=0 → 0.5"""
    z = cumN_z(sr["personal_index_eq"], 20)
    z = z.reindex(close.index, method="ffill")
    pos = (0.5 - 0.4 * z).clip(0.0, 1.0)
    return pos.fillna(0.5)


def s_retail_z_step_20_aggressive(close: pd.Series, sr: pd.DataFrame) -> pd.Series:
    """收紧阈值：z > +0.5 → 0；z < -0.5 → 1。更敏感、翻转更多。"""
    z = cumN_z(sr["personal_index_eq"], 20)
    z = z.reindex(close.index, method="ffill")
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(z > 0.5), 0.0)
    state = state.where(~(z < -0.5), 1.0)
    return state.ffill().fillna(0.5)


def s_retail_inst_combo_20(close: pd.Series, sr: pd.DataFrame) -> pd.Series:
    """个人 + 机构 z 都看：两个都 z>+1 → 0；都 z<-1 → 1；其余维持。"""
    z_p = cumN_z(sr["personal_index_eq"], 20).reindex(close.index, method="ffill")
    z_i = cumN_z(sr["inst_index_eq"], 20).reindex(close.index, method="ffill")
    both_hot = (z_p > 1.0) & (z_i > 1.0)
    both_cold = (z_p < -1.0) & (z_i < -1.0)
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~both_hot, 0.0)
    state = state.where(~both_cold, 1.0)
    return state.ffill().fillna(0.5)


GRID = [
    ("buyhold",              s_buyhold),
    ("retail_z_step_20",     s_retail_z_step_20),
    ("retail_z_step_10",     s_retail_z_step_10),
    ("retail_z_linear_20",   s_retail_z_linear_20),
    ("retail_z_step_20_agr", s_retail_z_step_20_aggressive),
    ("retail_inst_combo_20", s_retail_inst_combo_20),
]


# ─────────────────────────── backtest engine ─────────────────────────────

def backtest(close: pd.Series, sig: pd.Series) -> dict:
    sig = sig.clip(0.0, 1.0).fillna(0.0).reindex(close.index).fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    turnover = pos.diff().abs().fillna(pos.iloc[0] if len(pos) > 0 else 0.0)
    cost = turnover * COMMISSION
    strat_ret = pos * ret - cost
    # Only count from first day signal is non-NaN to avoid warmup pollution
    valid_mask = sig.notna().cummax()
    sr = strat_ret.where(valid_mask, 0.0).iloc[60:]  # discard first 60 bars warmup
    pos_e = pos.iloc[60:]
    bh = ret.iloc[60:]
    n = len(sr)
    if n == 0:
        return {"error": "empty"}
    eq = (1.0 + sr).cumprod()
    eq_bh = (1.0 + bh).cumprod()
    total_return = float(eq.iloc[-1] - 1.0)
    annual_return = float((1.0 + total_return) ** (252.0 / max(n, 1)) - 1.0)
    daily_std = float(sr.std(ddof=1)) if n > 1 else 0.0
    sharpe = float(sr.mean() / daily_std * np.sqrt(252.0)) if daily_std > 0 else 0.0
    downside = sr.where(sr < 0, 0.0)
    d_std = float(downside.std(ddof=1)) if n > 1 else 0.0
    sortino = float(sr.mean() / d_std * np.sqrt(252.0)) if d_std > 0 else 0.0
    peak = eq.cummax()
    max_dd = float((eq / peak - 1.0).min())
    calmar = float(annual_return / abs(max_dd)) if max_dd < 0 else 0.0
    flips = int((turnover.iloc[60:] > 0.001).sum())
    avg_exposure = float(pos_e.mean())
    bh_total = float(eq_bh.iloc[-1] - 1.0)
    bh_ann = float((1.0 + bh_total) ** (252.0 / max(n, 1)) - 1.0)
    bh_std = float(bh.std(ddof=1))
    bh_sharpe = float(bh.mean() / bh_std * np.sqrt(252.0)) if bh_std > 0 else 0.0
    peak_bh = eq_bh.cummax()
    bh_max_dd = float((eq_bh / peak_bh - 1.0).min())
    return {
        "ret": total_return, "ann": annual_return, "shp": sharpe,
        "sor": sortino, "dd": max_dd, "cal": calmar,
        "flips": flips, "exp": avg_exposure, "bars": n,
        "bh_ret": bh_total, "bh_ann": bh_ann, "bh_shp": bh_sharpe, "bh_dd": bh_max_dd,
    }


def fmt(name: str, m: dict) -> str:
    g = m.get
    return (
        f"  {name:<24} ret={g('ret',0):+.3f}  ann={g('ann',0):+.3f}  "
        f"shp={g('shp',0):+.2f}  dd={g('dd',0):+.2f}  cal={g('cal',0):+.2f}  "
        f"sor={g('sor',0):+.2f}  exp={g('exp',0):.2f}  flips={int(g('flips',0))}"
    )


def main():
    sr = load_sr_signals()
    print("══ Loaded SR signals: rows={}, range={}..{}".format(len(sr), sr.index.min().date(), sr.index.max().date()))
    print()
    print("⚠️  全样本 in-sample 探索性回测 — n ≈ 16 月稳定数据，无 IS/OOS 切分。")
    print("⚠️  多变体（5 个候选）测试在 4 个指数上 — 多重检验未矫正，看胜出不能宣称发现。")
    print()

    all_rows = []
    for label in UNIVERSE:
        close = load_close(label)
        # Restrict close to overlap with SR signal window
        cstart = max(close.index.min(), sr.index.min())
        cend = min(close.index.max(), sr.index.max())
        close = close.loc[cstart:cend]
        print(f"══════ {label.upper()}  bars={len(close)}  range={close.index.min().date()}..{close.index.max().date()} ══════")
        for name, fn in GRID:
            sig = fn(close, sr)
            m = backtest(close, sig)
            print(fmt(name, m))
            all_rows.append({"index": label, "factor": name, **m})
        print()

    # Summary table
    pd.DataFrame(all_rows).to_csv(BASE / "backtest_summary.csv", index=False)
    print(f"Wrote {BASE / 'backtest_summary.csv'}")


if __name__ == "__main__":
    main()
