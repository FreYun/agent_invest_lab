"""Statistical validation of the chosen VIX add-timing config (z>1.5, H=10, base=0.5).

We validate the TIMING ALPHA, not the trivially-positive full strategy:
  timing_alpha_t = (pos_t - base) * ret_t - extra_cost_t   ← excess from the add overlay
A constant-exposure book has the same Sharpe/Calmar as buy&hold, so a significantly
positive timing-alpha Sharpe means the *timing* adds value.

  1) bootstrap_sharpe_ci on the timing-alpha daily series   → ci_lower > 0 ?
  2) add-EVENT one-sample test: realised H-day excess return of each z>1.5 episode,
     win rate + bootstrap mean CI                            → mean > 0, ci_lo > 0 ?
  3) monte_carlo_test on episode pnls (path significance)
  4) walk_forward_analysis on the full-strategy equity        → >=4/5 windows Sharpe>0 ?

HS300 & ZZ1000 only (≈10y). SC50 (≈3y) reported as exploratory in the writeup, no p-values.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
from backtest.validation import monte_carlo_test, bootstrap_sharpe_ci, walk_forward_analysis
from backtest.models import TradeRecord

BASE = Path("/home/rooot/agent_invest_lab/research/vix_factor")
ETF_DIR = Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
VIX = BASE / "data" / "vix_50etf.csv"
WIN = 252
THR, H, BASEPOS, COMMISSION = 1.5, 10, 0.5, 0.0005
TARGETS = [("HS300", "510300.SH", "2016-06-01"), ("ZZ1000", "512100.SH", "2016-06-01")]
EVAL_END = "2026-05-26"


def load_vix():
    return pd.read_csv(VIX, parse_dates=["date"], index_col="date")["vix"].sort_index()


def load_close(code):
    df = pd.read_csv(ETF_DIR / f"{code}.csv", parse_dates=[0], index_col=0).sort_index()
    return df["close"]


def build(code, eval_start):
    vix = load_vix()
    mu = vix.rolling(WIN, min_periods=WIN // 2).mean()
    sd = vix.rolling(WIN, min_periods=WIN // 2).std()
    z = (vix - mu) / sd
    trig = (z > THR)
    pulse = trig.astype(float).rolling(H, min_periods=1).max().fillna(0.0)
    sig = (BASEPOS + (1 - BASEPOS) * pulse).clip(0, 1)

    close = load_close(code)
    sig = sig.reindex(close.index, method="ffill").fillna(BASEPOS)
    pulse_a = pulse.reindex(close.index, method="ffill").fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(BASEPOS)
    turn = pos.diff().abs().fillna(0.0)
    strat = pos * ret - turn * COMMISSION

    mask = (close.index >= pd.Timestamp(eval_start)) & (close.index <= pd.Timestamp(EVAL_END))
    strat, pos, ret, pulse_a = strat[mask], pos[mask], ret[mask], pulse_a[mask]
    close_m = close[mask]

    # timing alpha = excess of overlay over constant base exposure
    alpha = (pos - BASEPOS) * ret - turn[mask] * COMMISSION
    eq_strat = (1 + strat).cumprod()
    eq_alpha = (1 + alpha).cumprod()

    # add-episodes: contiguous runs where the *position* is above base (T+1 aligned)
    above = (pos > BASEPOS + 1e-9).values
    idx = close_m.index
    trades = []
    ep_ret = []
    i = 0
    while i < len(above):
        if above[i]:
            j = i
            while j + 1 < len(above) and above[j + 1]:
                j += 1
            # extra exposure (1-base) held over [i..j]; realised excess return
            seg = ret.iloc[i:j + 1]
            r = float((1 + seg).prod() - 1)
            excess = (1 - BASEPOS) * r
            ep_ret.append(excess)
            trades.append(TradeRecord(
                symbol=code, direction="long",
                entry_price=float(close_m.iloc[i]), exit_price=float(close_m.iloc[j]),
                entry_time=idx[i].to_pydatetime(), exit_time=idx[j].to_pydatetime(),
                size=(1 - BASEPOS), leverage=1.0,
                pnl=excess * 1_000_000, pnl_pct=excess,
                exit_reason="pulse_end", holding_bars=int(j - i + 1),
                commission=0.0,
            ))
            i = j + 1
        else:
            i += 1
    return dict(eq_strat=eq_strat, eq_alpha=eq_alpha, alpha=alpha, strat=strat,
                trades=trades, ep_ret=np.array(ep_ret))


def boot_mean_ci(x, n=2000, seed=42):
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(x, len(x), replace=True).mean() for _ in range(n)])
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def main():
    for label, code, es in TARGETS:
        print(f"\n{'='*70}\n  {label} ({code})   z>{THR}, hold {H}d, base {BASEPOS}   {es}..{EVAL_END}\n{'='*70}")
        b = build(code, es)

        # 1) timing-alpha Sharpe CI
        ci = bootstrap_sharpe_ci(b["eq_alpha"], n_bootstrap=2000, confidence=0.95)
        print(f"\n[1] timing-alpha bootstrap Sharpe CI:")
        print(f"    observed={ci['observed_sharpe']:+.3f}  95%CI=[{ci['ci_lower']:+.3f}, {ci['ci_upper']:+.3f}]  "
              f"prob_positive={ci.get('prob_positive', float('nan')):.3f}")

        # 2) add-event one-sample test
        ep = b["ep_ret"]
        lo, hi = boot_mean_ci(ep)
        print(f"\n[2] add-EVENT excess returns ({len(ep)} events, {H}d hold, (1-base) exposure):")
        print(f"    mean={ep.mean()*100:+.2f}%  median={np.median(ep)*100:+.2f}%  win_rate={(ep>0).mean()*100:.1f}%  "
              f"boot mean 95%CI=[{lo*100:+.2f}%, {hi*100:+.2f}%]")

        # 3) monte carlo on episode pnls (path)
        mc = monte_carlo_test(b["trades"], initial_capital=1_000_000, n_simulations=2000, seed=42)
        print(f"\n[3] monte_carlo (episode path):")
        print(f"    actual_sharpe={mc.get('actual_sharpe'):+.3f}  p_sharpe={mc.get('p_value_sharpe'):.3f}  "
              f"actual_maxdd={mc.get('actual_max_dd'):.3f}  p_maxdd={mc.get('p_value_max_dd'):.3f}")

        # 4) walk-forward on full strategy equity
        wf = walk_forward_analysis(b["eq_strat"], b["trades"], n_windows=5)
        wins = wf.get("windows") or wf.get("window_results") or []
        sh = [w.get("sharpe") for w in wins] if wins else []
        print(f"\n[4] walk_forward (5 windows) strategy Sharpe per window:")
        print(f"    {[round(x,2) for x in sh]}  -> {sum(1 for x in sh if x and x>0)}/{len(sh)} positive")


if __name__ == "__main__":
    main()
