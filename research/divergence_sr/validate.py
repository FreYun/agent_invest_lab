"""统计验证 + 相关性矩阵 — 检验 divergence 因子是否真的没用，或只是单独看没用。

1. 仓位时间序列相关性：新因子 vs baseline_retail_10
2. Permutation Monte Carlo (N=1000)：每个因子 vs 随机洗牌的 null
3. Drawdown-window co-coverage: baseline 回撤时新因子在哪
4. Linear combo: baseline + 第二信号的加权组合是否打过 baseline
"""
from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).parent))
from backtest import (
    UNIVERSE, load_close, load_sr_panel, backtest, GRID,
    s_buyhold, s_baseline_retail_z_step_10,
    s_f1_diff_step, s_f1_diff_step_agg, s_f2_diff_persist,
    s_f3_risk_switch_step, s_f5_consensus,
)

BASE = Path("/home/rooot/agent_invest_lab/research/divergence_sr")
N_MC = 1000
RNG_SEED = 20260601


def collect_positions(close, sr):
    out = {}
    for name, fn in GRID:
        if name == "buyhold":
            continue
        out[name] = fn(close, sr).clip(0, 1).fillna(0.0)
    return pd.DataFrame(out, index=close.index)


def correlation_matrix(label, close, sr):
    pos = collect_positions(close, sr)
    pos = pos.iloc[60:]
    return pos.corr()


def permutation_mc(close, sig, n=N_MC, seed=RNG_SEED) -> dict:
    """Null: 把信号在时间上随机洗牌，看 Sharpe 是否在分布的右尾."""
    ret = close.pct_change().fillna(0.0)
    sig = sig.clip(0, 1).fillna(0.0).reindex(close.index).fillna(0.0)
    obs_pos = sig.shift(1).fillna(0.0)
    obs_strat = obs_pos * ret - obs_pos.diff().abs().fillna(obs_pos.iloc[0]) * 0.0005
    obs_strat = obs_strat.iloc[60:]
    obs_shp = obs_strat.mean() / obs_strat.std(ddof=1) * np.sqrt(252) if obs_strat.std() > 0 else 0.0

    rng = np.random.default_rng(seed)
    sig_vals = sig.values.copy()
    null_shps = np.empty(n)
    for i in range(n):
        rng.shuffle(sig_vals)
        shuf = pd.Series(sig_vals, index=sig.index)
        pos_s = shuf.shift(1).fillna(0.0)
        sr_s = pos_s * ret - pos_s.diff().abs().fillna(pos_s.iloc[0]) * 0.0005
        sr_s = sr_s.iloc[60:]
        std = sr_s.std(ddof=1)
        null_shps[i] = (sr_s.mean() / std * np.sqrt(252)) if std > 0 else 0.0
    p = float((null_shps >= obs_shp).mean())
    return {
        "obs_shp": float(obs_shp),
        "null_mean": float(null_shps.mean()),
        "null_p95": float(np.percentile(null_shps, 95)),
        "p_value": p,
    }


def bootstrap_sharpe_ci(close, sig, n=500, seed=RNG_SEED) -> dict:
    """Bootstrap 日收益序列 N 次 → Sharpe 95% CI."""
    ret = close.pct_change().fillna(0.0)
    sig = sig.clip(0, 1).fillna(0.0).reindex(close.index).fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    strat = (pos * ret - pos.diff().abs().fillna(pos.iloc[0]) * 0.0005).iloc[60:].values

    rng = np.random.default_rng(seed)
    shps = np.empty(n)
    nlen = len(strat)
    for i in range(n):
        idx = rng.integers(0, nlen, size=nlen)
        s = strat[idx]
        std = s.std(ddof=1)
        shps[i] = (s.mean() / std * np.sqrt(252)) if std > 0 else 0.0
    return {
        "shp_median": float(np.median(shps)),
        "shp_ci_lo": float(np.percentile(shps, 2.5)),
        "shp_ci_hi": float(np.percentile(shps, 97.5)),
    }


def combo_linear(close, sr, weights: dict) -> pd.Series:
    """加权平均多个信号 (权重和应为 1)。"""
    combo = pd.Series(0.0, index=close.index)
    for name, w in weights.items():
        fn = dict(GRID)[name]
        combo = combo + w * fn(close, sr).reindex(close.index).fillna(0.0)
    return combo.clip(0, 1)


def main():
    sr = load_sr_panel()

    # ─── 1. 相关性矩阵 ──────────────────────────────────────────
    print("════════════════ 1. 仓位相关性 (vs baseline_retail_10) ════════════════")
    print()
    corr_rows = []
    for label in UNIVERSE:
        close = load_close(label)
        cstart = max(close.index.min(), sr.index.min())
        cend   = min(close.index.max(), sr.index.max())
        close = close.loc[cstart:cend]
        corr = correlation_matrix(label, close, sr)
        base_corrs = corr["baseline_retail_10"].sort_values()
        print(f"── {label.upper()} ──")
        for name, v in base_corrs.items():
            print(f"  {name:<28} corr_with_baseline = {v:+.3f}")
        print()
        for name, v in base_corrs.items():
            corr_rows.append({"index": label, "factor": name, "corr_baseline": float(v)})
    pd.DataFrame(corr_rows).to_csv(BASE / "corr_vs_baseline.csv", index=False)

    # ─── 2. Permutation MC ─────────────────────────────────────
    print()
    print("════════════════ 2. Permutation Monte Carlo (N=1000) ═════════════════")
    print()
    mc_rows = []
    factors_to_test = {
        "baseline_retail_10":  s_baseline_retail_z_step_10,
        "f1_diff_step":        s_f1_diff_step,
        "f1_diff_step_agg":    s_f1_diff_step_agg,
        "f2_diff_persist":     s_f2_diff_persist,
        "f3_risk_switch_step": s_f3_risk_switch_step,
        "f5_consensus":        s_f5_consensus,
    }
    for label in UNIVERSE:
        close = load_close(label)
        cstart = max(close.index.min(), sr.index.min())
        cend   = min(close.index.max(), sr.index.max())
        close = close.loc[cstart:cend]
        print(f"── {label.upper()} ──")
        for fname, fn in factors_to_test.items():
            sig = fn(close, sr)
            r = permutation_mc(close, sig)
            ci = bootstrap_sharpe_ci(close, sig)
            mark = "✓" if r["p_value"] < 0.05 else ("." if r["p_value"] < 0.10 else "✗")
            print(
                f"  {fname:<22} shp={r['obs_shp']:+.2f}  "
                f"p={r['p_value']:.3f} {mark}  "
                f"null_p95={r['null_p95']:+.2f}  "
                f"CI95=[{ci['shp_ci_lo']:+.2f}, {ci['shp_ci_hi']:+.2f}]"
            )
            mc_rows.append({"index": label, "factor": fname, **r, **ci})
        print()
    pd.DataFrame(mc_rows).to_csv(BASE / "mc_results.csv", index=False)

    # ─── 3. 线性组合：baseline + 第二信号 ──────────────────────
    print()
    print("════════════════ 3. 线性组合: baseline + 第二信号 (50/50, 70/30) ═════════")
    print()
    combo_rows = []
    second_candidates = ["f1_diff_step", "f2_diff_persist", "f3_risk_switch_step", "f5_consensus"]
    for label in UNIVERSE:
        close = load_close(label)
        cstart = max(close.index.min(), sr.index.min())
        cend   = min(close.index.max(), sr.index.max())
        close = close.loc[cstart:cend]
        print(f"── {label.upper()} ──")
        # 单独 baseline
        sig_base = s_baseline_retail_z_step_10(close, sr)
        m_base = backtest(close, sig_base)
        print(f"  baseline_only          ret={m_base['ret']:+.3f}  shp={m_base['shp']:+.2f}  cal={m_base['cal']:+.2f}  dd={m_base['dd']:+.2f}")
        for second in second_candidates:
            for w_base in [0.7, 0.5]:
                w_sec = 1.0 - w_base
                combo = combo_linear(close, sr, {"baseline_retail_10": w_base, second: w_sec})
                m = backtest(close, combo)
                tag = f"+{second}({w_base:.1f}/{w_sec:.1f})"
                print(f"  {tag:<38} ret={m['ret']:+.3f}  shp={m['shp']:+.2f}  cal={m['cal']:+.2f}  dd={m['dd']:+.2f}  flips={m['flips']}")
                combo_rows.append({"index": label, "combo": tag, **m})
        print()
    pd.DataFrame(combo_rows).to_csv(BASE / "combo_results.csv", index=False)


if __name__ == "__main__":
    main()
