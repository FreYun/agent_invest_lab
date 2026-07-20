#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""右侧赢家(ma_trend)的参数稳健性 + vibe-trading 统计验证。

Part1 稳健性: 均线长度网格 {5,10,15,20,25,30,40,60} 看 median Calmar 梯度/最优是否在角落;
             成本敏感性 5/10/20bp (快速趋势跟随翻转高, 必查).
Part2 验证: 31行业等权组合权益曲线 → bootstrap_sharpe_ci + walk_forward; 全交易序列 → monte_carlo.
"""
import os, glob, warnings
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
import sys
VBA = "/home/rooot/.local/share/uv/tools/vibe-trading-ai"
sys.path.insert(0, f"{VBA}/lib/python3.11/site-packages")
from backtest.validation import monte_carlo_test, bootstrap_sharpe_ci, walk_forward_analysis
from backtest.models import TradeRecord

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
YEAR = 244


def load():
    out = {}
    for f in sorted(glob.glob(os.path.join(DATA, "*.csv"))):
        code = os.path.basename(f)[:-4]
        df = pd.read_csv(f).sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        df["ma200"] = df["close"].rolling(200).mean()
        out[code] = df
    return out


def strat_ret(df, ma_len, cost):
    c = df["close"]
    sig = (c > c.rolling(ma_len).mean()).astype(float)
    ret = c.pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    turn = pos.diff().abs().fillna(0.0)
    sr = pos * ret - turn * cost
    mask = df["ma200"].notna().values
    return sr[mask], pos[mask], df["close"][mask].reset_index(drop=True), df["date"][mask].reset_index(drop=True)


def calmar_of(sr):
    sr = sr.reset_index(drop=True)
    if len(sr) < YEAR: return np.nan, np.nan, np.nan
    eq = (1+sr).cumprod(); yrs = len(sr)/YEAR
    cagr = eq.iloc[-1]**(1/yrs)-1
    dd = (eq/eq.cummax()-1).min()
    return (cagr/abs(dd) if dd < 0 else np.nan), cagr, dd


def main():
    data = load()
    print("="*78)
    print("【Part1-a】均线长度稳健性 (ma_len 网格, 单边5bp, 跨31行业)")
    print("="*78)
    print(f"{'ma_len':>7s}{'medCalmar':>11s}{'medCAGR':>9s}{'medDD':>8s}{'胜buyhold':>10s}{'仓位':>7s}")
    # buyhold calmar per industry
    bh = {}
    for code, df in data.items():
        sr, *_ = strat_ret(df, 1, 0.0)  # ma1 ~ always above? use buyhold sep
        c = df["close"]; ret = c.pct_change().fillna(0.0); mask = df["ma200"].notna().values
        bh[code] = calmar_of(ret[mask])[0]
    for ml in [5,10,15,20,25,30,40,60]:
        cals=[]; cagrs=[]; dds=[]; beats=[]; expos=[]
        for code, df in data.items():
            sr, pos, *_ = strat_ret(df, ml, 0.0005)
            cal, cagr, dd = calmar_of(sr)
            if pd.isna(cal): continue
            cals.append(cal); cagrs.append(cagr); dds.append(dd)
            beats.append(cal > bh[code] if pd.notna(bh[code]) else False)
            expos.append(pos.mean())
        print(f"{ml:>7d}{np.median(cals):>11.2f}{np.median(cagrs)*100:>8.1f}%{np.median(dds)*100:>7.0f}%"
              f"{np.mean(beats)*100:>9.0f}%{np.mean(expos)*100:>6.0f}%")

    print("\n" + "="*78)
    print("【Part1-b】成本敏感性 (ma20, 跨31行业 median Calmar)")
    print("="*78)
    for cost in [0.0005, 0.0010, 0.0020]:
        cals=[]; beats=[]
        for code, df in data.items():
            sr,*_=strat_ret(df,20,cost); cal,_,_=calmar_of(sr)
            if pd.notna(cal):
                cals.append(cal); beats.append(cal>bh[code] if pd.notna(bh[code]) else False)
        print(f"  {int(cost*1e4):2d}bp 单边: medCalmar={np.median(cals):.2f}  击败buyhold={np.mean(beats)*100:.0f}%")

    # ===== Part2: 验证 ma20 =====
    print("\n" + "="*78)
    print("【Part2】ma20 右侧策略 统计验证 (31行业等权组合)")
    print("="*78)
    # 组合权益: 各行业 strat 日收益按日期等权平均
    daily = {}
    trades = []
    for code, df in data.items():
        sr, pos, close, dates = strat_ret(df, 20, 0.0005)
        s = pd.Series(sr.values, index=dates.values)
        daily[code] = s
        # 提取交易
        pv = pos.values; cc = close.values; dd = dates.values
        instate=False; st=0
        for i in range(len(pv)):
            if pv[i]>0 and not instate: instate=True; st=i
            elif pv[i]==0 and instate:
                instate=False
                if i>st:
                    pnl_pct = cc[i]/cc[st]-1-0.001
                    trades.append(TradeRecord(symbol=code,direction=1,entry_price=float(cc[st]),
                        exit_price=float(cc[i]),entry_time=pd.Timestamp(dd[st]),exit_time=pd.Timestamp(dd[i]),
                        size=1.0,leverage=1.0,pnl=pnl_pct*1_000_000,pnl_pct=pnl_pct,
                        exit_reason="ma20_break",holding_bars=i-st,commission=0.0))
    port = pd.DataFrame(daily).mean(axis=1).sort_index()  # 等权日收益
    eq = (1+port).cumprod()
    yrs=len(port)/YEAR
    print(f"  组合: {port.index.min().date()}~{port.index.max().date()}, {len(port)}日, {len(trades)}笔交易")
    print(f"  组合 CAGR={eq.iloc[-1]**(1/yrs)-1:.1%}  Sharpe={port.mean()/port.std()*np.sqrt(YEAR):.2f}  "
          f"MaxDD={(eq/eq.cummax()-1).min():.1%}")

    print("\n  -- bootstrap_sharpe_ci (日收益重采样, 95%CI) --")
    ci = bootstrap_sharpe_ci(eq, n_bootstrap=1000, confidence=0.95, bars_per_year=YEAR, seed=42)
    print(f"     Sharpe={ci.get('observed_sharpe',float('nan')):.2f}  "
          f"95%CI[{ci.get('ci_lower',float('nan')):.2f},{ci.get('ci_upper',float('nan')):.2f}]  "
          f"prob_positive={ci.get('prob_positive',float('nan'))*100:.0f}%  "
          f"{'★CI>0显著' if ci.get('ci_lower',0)>0 else '含0'}")

    print("\n  -- monte_carlo_test (随机洗牌交易顺序 vs 实际) --")
    mc = monte_carlo_test(trades, initial_capital=1_000_000, n_simulations=1000, seed=42)
    print(f"     actual_sharpe={mc.get('actual_sharpe',float('nan')):.2f}  p_value_sharpe={mc.get('p_value_sharpe',float('nan')):.3f}  "
          f"{'★p<0.05' if mc.get('p_value_sharpe',1)<0.05 else '不显著'}")
    print(f"     actual_max_dd={mc.get('actual_max_dd',float('nan')):.1%}  p_value_max_dd={mc.get('p_value_max_dd',float('nan')):.3f}")

    print("\n  -- walk_forward_analysis (5个时间子窗一致性) --")
    wf = walk_forward_analysis(eq, trades, n_windows=5, bars_per_year=YEAR)
    wins = wf.get('windows', wf.get('per_window', []))
    if isinstance(wins, list) and wins:
        pos_sharpe = 0
        for i,w in enumerate(wins):
            shp = w.get('sharpe', w.get('sharpe_ratio', float('nan')))
            ret = w.get('return', w.get('total_return', float('nan')))
            pos_sharpe += int((shp or 0) > 0)
            print(f"     窗{i+1}: return={ret*100 if pd.notna(ret) else float('nan'):+6.1f}%  sharpe={shp:+.2f}")
        print(f"     → {pos_sharpe}/{len(wins)} 子窗 Sharpe>0  {'★≥4/5时间稳定' if pos_sharpe>=4 else '时间不够稳'}")
    else:
        print("     walk_forward 返回:", {k: wf[k] for k in list(wf)[:6]})


if __name__ == "__main__":
    main()
