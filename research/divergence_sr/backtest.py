"""Divergence-flow factor backtest — 2nd-pass on subscription/redemption data.

Builds on research/sr_factor/ which exhausted the single-persona contrarian
angle (personal_index_eq z-step / z-linear). This pass mines the orthogonal
divergence dimensions left on the table:

  F1. retail_minus_inst         — 散户净流入 − 机构净流入 (z, reversal)
  F2. retail_minus_inst_persist — 同上但要求分歧持续 N 天 (噪声更低)
  F3. risk_appetite_switch      — 散户股权益类 − 散户债券类 (z, reversal)
  F4. active_vs_passive         — 散户主动股票 − 散户指数股票 (z, reversal)
  F5. consensus_state           — (个人方向, 机构方向) 4 个联合状态 → 仓位映射

All factors share the same backtest engine and 4-index universe as sr_factor/
for direct comparability. n ≈ 16 stable months — IN-SAMPLE ONLY. Statistical
significance verified via permutation Monte Carlo + bootstrap Sharpe CI in
validate.py.
"""
from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/divergence_sr")
SR_BASE = Path("/home/rooot/agent_invest_lab/research/sr_factor")
DATA = SR_BASE / "data"
COMMISSION = 0.0005

UNIVERSE = {
    "hs300":   "510300.SH.csv",
    "zz1000":  "512100.SH.csv",
    "semi":    "512480.SH.csv",
    "sc50":    "588800.SH.csv",
}


def load_close(label: str) -> pd.Series:
    df = pd.read_csv(DATA / UNIVERSE[label], parse_dates=[0])
    df.columns = [c.lower() for c in df.columns]
    df = df.set_index(df.columns[0]).sort_index()
    s = df["close"].astype(float)
    s.index = pd.to_datetime(s.index).normalize()
    return s


def load_sr_panel() -> pd.DataFrame:
    sr = pd.read_csv(SR_BASE / "sr_daily.csv", parse_dates=["date"])
    p = sr.pivot_table(
        index="date", columns=["persona", "fund_type"], values="net", aggfunc="sum",
    ).sort_index()
    p.columns = [f"{a}_{b}" for a, b in p.columns]
    p.index = pd.to_datetime(p.index).normalize()

    out = pd.DataFrame(index=p.index)
    out["P_idx_eq"]    = p.get("个人_指数型-股票",    pd.Series(0, index=p.index))
    out["I_idx_eq"]    = p.get("机构_指数型-股票",    pd.Series(0, index=p.index))
    out["P_stock"]     = p.get("个人_股票型",         pd.Series(0, index=p.index))
    out["P_mix_eq"]    = p.get("个人_混合型-偏股",    pd.Series(0, index=p.index))
    out["P_bond_long"] = p.get("个人_债券型-长债",    pd.Series(0, index=p.index))
    out["P_bond_short"]= p.get("个人_债券型-中短债",  pd.Series(0, index=p.index))
    out["P_bond_mix1"] = p.get("个人_债券型-混合一级", pd.Series(0, index=p.index))
    out["P_bond_mix2"] = p.get("个人_债券型-混合二级", pd.Series(0, index=p.index))

    out["retail_minus_inst_idx"] = out["P_idx_eq"] - out["I_idx_eq"]

    p_equity = out["P_idx_eq"] + out["P_stock"] + out["P_mix_eq"]
    p_bond   = out["P_bond_long"] + out["P_bond_short"] + out["P_bond_mix1"] + out["P_bond_mix2"]
    out["P_equity_minus_bond"] = p_equity - p_bond

    out["P_active_minus_passive"] = (out["P_stock"] + out["P_mix_eq"]) - out["P_idx_eq"]
    return out


def cumN_z(s: pd.Series, n: int, z_win: int) -> pd.Series:
    cum = s.rolling(n, min_periods=max(3, n // 2)).sum()
    mu = cum.rolling(z_win, min_periods=20).mean()
    sd = cum.rolling(z_win, min_periods=20).std()
    return (cum - mu) / sd


def _z_reversal_step(close, sr, col, cumN, z_win, thr):
    z = cumN_z(sr[col], cumN, z_win).reindex(close.index, method="ffill")
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(z > thr), 0.0)
    state = state.where(~(z < -thr), 1.0)
    return state.ffill().fillna(0.5)


def _z_reversal_linear(close, sr, col, cumN, z_win, slope=0.4):
    z = cumN_z(sr[col], cumN, z_win).reindex(close.index, method="ffill")
    return (0.5 - slope * z).clip(0.0, 1.0).fillna(0.5)


def s_buyhold(close, sr):
    return pd.Series(1.0, index=close.index)


def s_baseline_retail_z_step_10(close, sr):
    """sr_factor 里最强的 baseline: 个人指数股票 cumN=10 z>+1→0 / <-1→1."""
    z = cumN_z(sr["P_idx_eq"], 10, 60).reindex(close.index, method="ffill")
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(z > 1.0), 0.0)
    state = state.where(~(z < -1.0), 1.0)
    return state.ffill().fillna(0.5)


def s_f1_diff_step(close, sr):
    return _z_reversal_step(close, sr, "retail_minus_inst_idx", cumN=10, z_win=60, thr=1.0)


def s_f1_diff_step_agg(close, sr):
    return _z_reversal_step(close, sr, "retail_minus_inst_idx", cumN=5, z_win=60, thr=0.5)


def s_f1_diff_linear(close, sr):
    return _z_reversal_linear(close, sr, "retail_minus_inst_idx", cumN=10, z_win=60, slope=0.4)


def s_f2_diff_persist(close, sr):
    """要求分歧 z 极端连续 3 天才动作 — 噪声/翻转更低."""
    z = cumN_z(sr["retail_minus_inst_idx"], 10, 60).reindex(close.index, method="ffill")
    hot  = ((z > 1.0).rolling(3).sum() >= 3)
    cold = ((z < -1.0).rolling(3).sum() >= 3)
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~hot,  0.0)
    state = state.where(~cold, 1.0)
    return state.ffill().fillna(0.5)


def s_f3_risk_switch_step(close, sr):
    return _z_reversal_step(close, sr, "P_equity_minus_bond", cumN=10, z_win=60, thr=1.0)


def s_f3_risk_switch_linear(close, sr):
    return _z_reversal_linear(close, sr, "P_equity_minus_bond", cumN=10, z_win=60, slope=0.4)


def s_f4_active_passive_step(close, sr):
    return _z_reversal_step(close, sr, "P_active_minus_passive", cumN=10, z_win=60, thr=1.0)


def s_f4_active_passive_linear(close, sr):
    return _z_reversal_linear(close, sr, "P_active_minus_passive", cumN=10, z_win=60, slope=0.4)


def s_f5_consensus(close, sr):
    """联合状态:
       散户+/机构+ (both_hot)  → 0   一致看多→透支顶
       散户-/机构- (both_cold) → 1   一致看空→恐慌底
       散户+/机构-             → 0   经典顶
       散户-/机构+             → 1   经典底
       else                    → ffill or 0.5
    """
    z_p = cumN_z(sr["P_idx_eq"], 10, 60).reindex(close.index, method="ffill")
    z_i = cumN_z(sr["I_idx_eq"], 10, 60).reindex(close.index, method="ffill")
    p_hot, p_cold = z_p > 0.7, z_p < -0.7
    i_hot, i_cold = z_i > 0.7, z_i < -0.7
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(p_hot  & i_hot ), 0.0)
    state = state.where(~(p_hot  & i_cold), 0.0)
    state = state.where(~(p_cold & i_cold), 1.0)
    state = state.where(~(p_cold & i_hot ), 1.0)
    return state.ffill().fillna(0.5)


GRID = [
    ("buyhold",                s_buyhold),
    ("baseline_retail_10",     s_baseline_retail_z_step_10),
    ("f1_diff_step",           s_f1_diff_step),
    ("f1_diff_step_agg",       s_f1_diff_step_agg),
    ("f1_diff_linear",         s_f1_diff_linear),
    ("f2_diff_persist",        s_f2_diff_persist),
    ("f3_risk_switch_step",    s_f3_risk_switch_step),
    ("f3_risk_switch_linear",  s_f3_risk_switch_linear),
    ("f4_active_passive_step", s_f4_active_passive_step),
    ("f4_active_passive_lin",  s_f4_active_passive_linear),
    ("f5_consensus",           s_f5_consensus),
]


def backtest(close: pd.Series, sig: pd.Series) -> dict:
    sig = sig.clip(0.0, 1.0).fillna(0.0).reindex(close.index).fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    turnover = pos.diff().abs().fillna(pos.iloc[0] if len(pos) > 0 else 0.0)
    cost = turnover * COMMISSION
    strat_ret = pos * ret - cost
    valid = sig.notna().cummax()
    sr = strat_ret.where(valid, 0.0).iloc[60:]
    pos_e = pos.iloc[60:]
    bh = ret.iloc[60:]
    n = len(sr)
    if n == 0:
        return {"error": "empty"}
    eq = (1.0 + sr).cumprod()
    eq_bh = (1.0 + bh).cumprod()
    total_return = float(eq.iloc[-1] - 1.0)
    ann = float((1.0 + total_return) ** (252.0 / max(n, 1)) - 1.0)
    daily_std = float(sr.std(ddof=1)) if n > 1 else 0.0
    shp = float(sr.mean() / daily_std * np.sqrt(252.0)) if daily_std > 0 else 0.0
    downside = sr.where(sr < 0, 0.0)
    d_std = float(downside.std(ddof=1)) if n > 1 else 0.0
    sor = float(sr.mean() / d_std * np.sqrt(252.0)) if d_std > 0 else 0.0
    peak = eq.cummax()
    dd = float((eq / peak - 1.0).min())
    cal = float(ann / abs(dd)) if dd < 0 else 0.0
    flips = int((turnover.iloc[60:] > 0.001).sum())
    exp = float(pos_e.mean())
    bh_tot = float(eq_bh.iloc[-1] - 1.0)
    bh_ann = float((1.0 + bh_tot) ** (252.0 / max(n, 1)) - 1.0)
    bh_std = float(bh.std(ddof=1))
    bh_shp = float(bh.mean() / bh_std * np.sqrt(252.0)) if bh_std > 0 else 0.0
    peak_bh = eq_bh.cummax()
    bh_dd = float((eq_bh / peak_bh - 1.0).min())
    return {
        "ret": total_return, "ann": ann, "shp": shp, "sor": sor,
        "dd": dd, "cal": cal, "flips": flips, "exp": exp, "bars": n,
        "bh_ret": bh_tot, "bh_ann": bh_ann, "bh_shp": bh_shp, "bh_dd": bh_dd,
    }


def fmt(name: str, m: dict) -> str:
    g = m.get
    return (
        f"  {name:<26} ret={g('ret',0):+.3f}  ann={g('ann',0):+.3f}  "
        f"shp={g('shp',0):+.2f}  dd={g('dd',0):+.2f}  cal={g('cal',0):+.2f}  "
        f"sor={g('sor',0):+.2f}  exp={g('exp',0):.2f}  flips={int(g('flips',0))}"
    )


def main():
    sr = load_sr_panel()
    print(f"══ Loaded SR panel: rows={len(sr)}, range={sr.index.min().date()}..{sr.index.max().date()}")
    print()
    print("⚠️  全样本 in-sample — n ≈ 16 月稳定数据，无 IS/OOS 切分。")
    print("⚠️  11 变体 × 4 指数 = 44 backtests — 多重检验未矫正。看胜出仅作筛选，结论靠 validate.py 蒙特卡洛。")
    print()

    all_rows = []
    for label in UNIVERSE:
        close = load_close(label)
        cstart = max(close.index.min(), sr.index.min())
        cend   = min(close.index.max(), sr.index.max())
        close = close.loc[cstart:cend]
        print(f"══════ {label.upper()}  bars={len(close)}  range={close.index.min().date()}..{close.index.max().date()} ══════")
        for name, fn in GRID:
            sig = fn(close, sr)
            m = backtest(close, sig)
            print(fmt(name, m))
            all_rows.append({"index": label, "factor": name, **m})
        print()

    pd.DataFrame(all_rows).to_csv(BASE / "backtest_summary.csv", index=False)
    print(f"Wrote {BASE / 'backtest_summary.csv'}")


if __name__ == "__main__":
    main()
