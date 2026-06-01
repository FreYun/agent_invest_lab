#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试 regime_classify_daily.total_score 对 50/50 (沪深300+中证1000) 等权组合的择时有效性。
按 SOP_factor_mining.md: 预测力(IC/分位) + 择时回测(IS/OOS) + validation 套件。

用 vibe-trading venv python 跑:
  /home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python analyze.py
"""
import sys, sqlite3, json
sys.path.insert(0, "/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from backtest.validation import monte_carlo_test, bootstrap_sharpe_ci, walk_forward_analysis
from backtest.models import TradeRecord

DB = "/home/rooot/database/market.db"
RULES = "v2"            # 2014-07 ~ 2026-05, 最长样本
COST = 0.0005           # 5bp 单边
OOS_START = "2021-01-01"  # IS: 2014-07~2020-12 ; OOS: 2021-01~2026-05

# ────────────────────────────── 数据加载 ──────────────────────────────
def load():
    con = sqlite3.connect(DB)
    # regime 分数
    reg = pd.read_sql_query(
        "SELECT trade_date, total_score FROM regime_classify_daily "
        "WHERE rules_version=? AND total_score IS NOT NULL ORDER BY trade_date",
        con, params=[RULES])
    reg["date"] = pd.to_datetime(reg["trade_date"])
    reg = reg.set_index("date")["total_score"].astype(float)

    # 指数 close
    def idx(code):
        df = pd.read_sql_query(
            "SELECT trade_date, close FROM index_daily WHERE ts_code=? ORDER BY trade_date",
            con, params=[code])
        df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        return df.set_index("date")["close"].astype(float)
    hs300 = idx("000300.SH")
    zz1000 = idx("000852.SH")
    con.close()

    # 50/50 每日再平衡等权组合的日收益
    r_hs = hs300.pct_change()
    r_zz = zz1000.pct_change()
    blend_ret = (0.5 * r_hs + 0.5 * r_zz)
    # 对齐到 regime 日期
    df = pd.DataFrame({"score": reg, "blend_ret": blend_ret}).dropna()
    return df

# ────────────────────────────── 回测核心 ──────────────────────────────
def backtest(blend_ret, sig, eval_mask=None):
    """sig: 目标仓位(0..1), 已对齐 index. 返回 metrics dict + 策略日收益."""
    sig = sig.clip(0.0, 1.0).fillna(0.0)
    pos = sig.shift(1).fillna(0.0)                  # T+1: 信号当日可见, 次日生效
    turnover = pos.diff().abs().fillna(pos.iloc[0])
    cost = turnover * COST
    strat = pos * blend_ret - cost
    if eval_mask is not None:
        strat = strat[eval_mask]; pos = pos[eval_mask]; turnover = turnover[eval_mask]
    return strat, pos, turnover

def metrics(strat, pos, turnover, n_years):
    eq = (1 + strat).cumprod()
    ann_ret = eq.iloc[-1] ** (1 / n_years) - 1
    ann_vol = strat.std() * np.sqrt(252)
    sharpe = (strat.mean() * 252) / ann_vol if ann_vol > 0 else 0.0
    dd = (eq / eq.cummax() - 1).min()
    calmar = ann_ret / abs(dd) if dd < 0 else np.nan
    downside = strat[strat < 0].std() * np.sqrt(252)
    sortino = (strat.mean() * 252) / downside if downside > 0 else np.nan
    flips_yr = (turnover > 1e-9).sum() / n_years
    return dict(ann_ret=ann_ret, sharpe=sharpe, calmar=calmar, sortino=sortino,
                max_dd=dd, exposure=pos.mean(), flips_yr=flips_yr,
                final=eq.iloc[-1])

# ────────────────────────────── Part A: 预测力 ──────────────────────────────
def predictive(df):
    print("\n" + "="*70)
    print("PART A — 预测力: total_score vs 未来 N 日组合收益 (Spearman rank IC)")
    print("="*70)
    out = []
    for N in (1, 5, 10, 20):
        fwd = (df["blend_ret"].shift(-1).rolling(N).sum()
               .shift(-(N-1)))  # t+1..t+N 累积收益
        # 更稳妥: 用对数近似的前向窗口
        fwd = pd.Series(
            [df["blend_ret"].iloc[i+1:i+1+N].sum() if i+1+N <= len(df) else np.nan
             for i in range(len(df))], index=df.index)
        sub = pd.DataFrame({"s": df["score"], "f": fwd}).dropna()
        ic, p = spearmanr(sub["s"], sub["f"])
        # IS / OOS 分段
        is_m = sub.index < OOS_START
        ic_is, _ = spearmanr(sub["s"][is_m], sub["f"][is_m])
        ic_oos, _ = spearmanr(sub["s"][~is_m], sub["f"][~is_m])
        out.append((N, ic, p, ic_is, ic_oos, len(sub)))
        print(f"  N={N:>2}d  IC_all={ic:+.4f} (p={p:.1e})  IC_IS={ic_is:+.4f}  IC_OOS={ic_oos:+.4f}  n={len(sub)}")
    return out

def quantiles(df):
    print("\n" + "="*70)
    print("PART B — 分位组合: 按 score 分桶看未来 5/20 日组合均值收益 (是否单调)")
    print("="*70)
    for N in (5, 20):
        fwd = pd.Series(
            [df["blend_ret"].iloc[i+1:i+1+N].sum() if i+1+N <= len(df) else np.nan
             for i in range(len(df))], index=df.index)
        sub = pd.DataFrame({"s": df["score"], "f": fwd}).dropna()
        # 按 score 五分位
        sub["q"] = pd.qcut(sub["s"].rank(method="first"), 5, labels=["Q1低","Q2","Q3","Q4","Q5高"])
        g = sub.groupby("q", observed=True)["f"]
        print(f"\n  未来 {N} 日累计收益 (均值, %):")
        for q, m in (g.mean()*100).items():
            print(f"    {q}: {m:+.3f}%   (score范围 {sub[sub.q==q].s.min():.0f}..{sub[sub.q==q].s.max():.0f}, n={ (sub.q==q).sum()})")
        spread = (g.mean().iloc[-1] - g.mean().iloc[0]) * 100
        print(f"    Q5-Q1 spread = {spread:+.3f}%")

# ────────────────────────────── trades 重建 (给 monte carlo) ──────────────────────────────
def build_trades(blend_ret, pos, cost_series):
    """把 long/flat 仓位变化拆成 round-trip trades. capital=1e6."""
    cap = 1_000_000.0
    trades = []
    in_pos = False
    entry_i = None
    cum = 0.0
    idx = blend_ret.index
    for i in range(len(pos)):
        p = pos.iloc[i]
        if p > 0 and not in_pos:
            in_pos = True; entry_i = i; cum = 0.0
        if in_pos:
            cum += blend_ret.iloc[i] * p - cost_series.iloc[i]
        if (p == 0 or i == len(pos)-1) and in_pos:
            in_pos = False
            pnl = cap * cum
            trades.append(TradeRecord(
                symbol="BLEND5050", direction=1,
                entry_price=1.0, exit_price=1.0+cum,
                entry_time=idx[entry_i], exit_time=idx[i],
                size=cap, leverage=1.0, pnl=pnl, pnl_pct=cum,
                exit_reason="signal", holding_bars=i-entry_i, commission=0.0))
    return trades

# ────────────────────────────── Part C: 择时回测 sweep ──────────────────────────────
def timing(df):
    print("\n" + "="*70)
    print("PART C — 择时回测: score→仓位映射, vs buy&hold(50/50, pos=1)")
    print("="*70)
    blend = df["blend_ret"]
    n_years_all = (df.index[-1] - df.index[0]).days / 365.25

    # 候选因子族
    variants = {}
    for thr in (-2, 0, 1, 2, 3, 4):
        variants[f"long_flat>={thr}"] = (df["score"] >= thr).astype(float)
    # 线性缩放
    for lo, hi in [(-4, 6), (-2, 8), (0, 8), (-6, 10)]:
        variants[f"scale[{lo},{hi}]"] = ((df["score"] - lo) / (hi - lo)).clip(0, 1)
    # 三档
    def ladder(s):
        out = pd.Series(0.5, index=s.index)
        out[s >= 3] = 1.0; out[s <= -2] = 0.0
        return out
    variants["ladder(<=-2:0, >=3:1, else .5)"] = ladder(df["score"])
    # 平滑版 (5/10日均分降翻转)
    for w in (5, 10):
        sm = df["score"].rolling(w).mean()
        variants[f"smooth{w}_long>=2"] = (sm >= 2).astype(float)
        variants[f"smooth{w}_scale[0,8]"] = ((sm - 0) / 8).clip(0, 1)

    rows = []
    # buy&hold 基准
    bh_strat, bh_pos, bh_to = backtest(blend, pd.Series(1.0, index=df.index))
    bh = metrics(bh_strat, bh_pos, bh_to, n_years_all)
    rows.append(("BUY&HOLD_5050", bh, None, None))

    # 全样本 + IS/OOS
    is_mask = df.index < OOS_START
    oos_mask = ~is_mask
    ny_is = (df.index[is_mask][-1]-df.index[is_mask][0]).days/365.25
    ny_oos = (df.index[oos_mask][-1]-df.index[oos_mask][0]).days/365.25
    bh_is = metrics(*backtest(blend, pd.Series(1.0,index=df.index), is_mask), ny_is)
    bh_oos = metrics(*backtest(blend, pd.Series(1.0,index=df.index), oos_mask), ny_oos)

    for name, sig in variants.items():
        s, p, to = backtest(blend, sig)
        m = metrics(s, p, to, n_years_all)
        s_is = backtest(blend, sig, is_mask); m_is = metrics(*s_is, ny_is)
        s_oos = backtest(blend, sig, oos_mask); m_oos = metrics(*s_oos, ny_oos)
        rows.append((name, m, m_is, m_oos))

    # 打印表
    hdr = f"{'factor':<26} {'Sharpe':>7} {'Calmar':>7} {'maxDD':>7} {'annR':>7} {'expo':>5} {'flips/y':>7} | {'Cal_IS':>7} {'Cal_OOS':>8}"
    print(hdr); print("-"*len(hdr))
    for name, m, m_is, m_oos in rows:
        cis = f"{m_is['calmar']:+.2f}" if m_is else "   -"
        coos = f"{m_oos['calmar']:+.2f}" if m_oos else "   -"
        print(f"{name:<26} {m['sharpe']:>7.2f} {m['calmar']:>7.2f} {m['max_dd']*100:>6.1f}% "
              f"{m['ann_ret']*100:>6.1f}% {m['exposure']:>5.2f} {m['flips_yr']:>7.1f} | {cis:>7} {coos:>8}")

    print(f"\n  [IS/OOS 基准] BH Calmar: IS={bh_is['calmar']:+.2f}  OOS={bh_oos['calmar']:+.2f}")
    return df, variants, blend, n_years_all

# ────────────────────────────── Part D: validation ──────────────────────────────
def validate(df, variants, blend, n_years_all, factor_name):
    print("\n" + "="*70)
    print(f"PART D — validation 套件 (因子: {factor_name})")
    print("="*70)
    sig = variants[factor_name]
    sig_c = sig.clip(0,1).fillna(0.0)
    pos = sig_c.shift(1).fillna(0.0)
    turnover = pos.diff().abs().fillna(pos.iloc[0])
    cost_series = turnover * COST
    strat = pos * blend - cost_series
    eq = (1 + strat).cumprod() * 1_000_000.0
    eq.name = "equity"

    trades = build_trades(blend, pos, cost_series)
    print(f"  重建 round-trip trades: {len(trades)} 笔")

    mc = monte_carlo_test(trades, initial_capital=1_000_000, n_simulations=2000, seed=42)
    print(f"  [Monte Carlo] actual_sharpe={mc.get('actual_sharpe')}  "
          f"p_value_sharpe={mc.get('p_value_sharpe')}  p_value_max_dd={mc.get('p_value_max_dd')}")

    ci = bootstrap_sharpe_ci(eq, n_bootstrap=2000, confidence=0.95)
    print(f"  [Bootstrap CI] observed_sharpe={ci.get('observed_sharpe')}  "
          f"95%CI=[{ci.get('ci_lower')}, {ci.get('ci_upper')}]  prob_positive={ci.get('prob_positive')}")

    wf = walk_forward_analysis(eq, trades, n_windows=5)
    wins = wf.get('windows', [])
    pos_n = sum(1 for w in wins if w.get('sharpe', 0) > 0)
    print(f"  [Walk-Forward] {pos_n}/{len(wins)} 窗 Sharpe>0:")
    for w in wins:
        print(f"      win{w['window']} {w['start']}~{w['end']}: ret={w['return']*100:+6.1f}%  "
              f"sharpe={w['sharpe']:+.2f}  maxDD={w['max_dd']*100:+.1f}%")
    return mc, ci, wf

# ────────────────────────────── main ──────────────────────────────
if __name__ == "__main__":
    df = load()
    print(f"样本: {df.index[0].date()} ~ {df.index[-1].date()}  n={len(df)}  "
          f"({(df.index[-1]-df.index[0]).days/365.25:.1f} 年)  rules={RULES}")
    print(f"score 分布: min={df.score.min():.0f} max={df.score.max():.0f} "
          f"mean={df.score.mean():.2f} std={df.score.std():.2f}")
    predictive(df)
    quantiles(df)
    df, variants, blend, ny = timing(df)
    # validation 跑在 Calmar 赢家 long_flat>=2 上
    validate(df, variants, blend, ny, "long_flat>=2")

    # 10bp 成本稳健性: 翻转高的因子在双倍成本下是否还活
    print("\n" + "="*70)
    print("PART E — 10bp 成本稳健性 (翻转敏感性)")
    print("="*70)
    blend = df["blend_ret"]
    for name in ("long_flat>=2", "smooth10_long>=2", "scale[0,8]", "smooth10_scale[0,8]"):
        COST = 0.0005
        m5 = metrics(*backtest(blend, variants[name]), ny)
        COST = 0.0010
        m10 = metrics(*backtest(blend, variants[name]), ny)
        print(f"  {name:<22} 5bp: Sharpe={m5['sharpe']:.2f} Calmar={m5['calmar']:.2f} "
              f"| 10bp: Sharpe={m10['sharpe']:.2f} Calmar={m10['calmar']:.2f} "
              f"(flips/y={m5['flips_yr']:.0f})")
    COST = 0.0005
