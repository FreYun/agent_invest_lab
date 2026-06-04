"""截面选股因子 sweep — ≥100亿大票池, 短线轮动 (周级调仓).

第一轮 spike: 用 market.db.daily (12年量价) 在 ≥100亿 池上挖截面因子.
universe 市值用近似口径 total_mv ≈ close × total_share (daily_basic 最新非空股本),
zero-cost 本地估算; 代价是幸存者偏差(2025前退市票无股本→剔除)+股本漂移 → 第一轮仅当 hint.

纪律 (SOP):
  - T+1: 信号 t 日收盘可见, 持仓 t+1 起生效 (pos = W.shift(1))
  - 5bp 单边成本, 按调仓换手扣
  - Calmar 排名 (非 Sharpe), IS(≤2021)/OOS(≥2022) 拆分
  - 基准: 同池等权 (universe EW)

用法: /usr/bin/python3.12 cs_backtest.py
"""
from __future__ import annotations

import sqlite3
import sys
import numpy as np
import pandas as pd

DB = "/home/rooot/database/market.db"
MV_THRESHOLD = 1_000_000.0   # 万元; 100亿 = 1,000,000 万元
COST = 0.0005                # 5bp 单边
IS_END = "20211231"          # IS: 2014-07 ~ 2021-12 ; OOS: 2022-01 ~
ANN = 242.0                  # 年化交易日


def load_panel():
    """daily → close/ret/vol 宽表 (date × ts_code). + total_share map."""
    print("[load] 读 daily ...", flush=True)
    c = sqlite3.connect(DB)
    df = pd.read_sql(
        "SELECT trade_date, ts_code, close, pct_chg, vol FROM daily", c)
    print(f"  daily rows={len(df):,}", flush=True)
    close = df.pivot(index="trade_date", columns="ts_code", values="close").sort_index()
    ret = df.pivot(index="trade_date", columns="ts_code", values="pct_chg").sort_index() / 100.0
    vol = df.pivot(index="trade_date", columns="ts_code", values="vol").sort_index()
    # 最新非空 total_share (万股) per ts_code
    ts = pd.read_sql(
        "SELECT ts_code, total_share FROM daily_basic "
        "WHERE total_share IS NOT NULL AND trade_date=("
        "  SELECT MAX(trade_date) FROM daily_basic)", c)
    c.close()
    share = ts.set_index("ts_code")["total_share"]
    close = close.astype("float32")
    ret = ret.astype("float32")
    vol = vol.astype("float32")
    print(f"  panel: {close.shape[0]} dates × {close.shape[1]} stocks "
          f"({close.index[0]}~{close.index[-1]})", flush=True)
    return close, ret, vol, share


def build_universe(close, share):
    """每日 PIT 池 mask: proxy_mv = close × total_share ≥ 100亿. 无股本的票全程剔除."""
    sh = share.reindex(close.columns)               # 万股
    proxy_mv = close * sh.values                    # 万元
    mask = (proxy_mv >= MV_THRESHOLD) & close.notna()
    print(f"[universe] 日均池规模 ≈ {mask.sum(axis=1).mean():.0f} 只 "
          f"(有股本票 {sh.notna().sum()}/{len(sh)})", flush=True)
    return mask


def factor_panels(close, ret, vol):
    """候选因子族 (全 12 年量价可算). 返回 {name: DataFrame(date×stock)}."""
    f = {}
    # 短期反转: -(过去N日收益)
    for n in (3, 5, 10):
        f[f"rev_{n}"] = -(close / close.shift(n) - 1.0)
    # 中期动量 (跳过最近5日, 避免短反污染)
    for n in (20, 60):
        f[f"mom_{n}_sk5"] = (close.shift(5) / close.shift(n) - 1.0)
    # 低波动: -(过去N日日收益波动)
    for n in (20, 60):
        f[f"lowvol_{n}"] = -(ret.rolling(n).std())
    # 相对量能: vol/MA20(vol) 的 N 日均值 (两个方向都测)
    vr = vol / vol.rolling(20).mean()
    for n in (5, 20):
        f[f"volup_{n}"] = vr.rolling(n).mean()      # 放量
        f[f"voldn_{n}"] = -vr.rolling(n).mean()     # 缩量
    return f


def backtest(factor, ret, mask, top_k, rb):
    """截面回测. factor/ret/mask 同形 (date×stock).
    每 rb 日调仓, 选池内 factor 最高的 top_k 只, 等权; T+1 生效; 5bp 换手成本.
    返回 (strat_ret Series, turnover_per_rb list, avg_holdings)."""
    dates = ret.index
    n = len(dates)
    cols = ret.columns
    fa = factor.where(mask)                          # 池外置 NaN
    W = np.zeros((n, len(cols)), dtype="float32")
    rb_idx = list(range(60, n, rb))                  # 60 日 warmup
    holds = []
    for i in rb_idx:
        row = fa.iloc[i]
        valid = row.dropna()
        if len(valid) < top_k:
            continue
        top = valid.nlargest(top_k).index
        ci = cols.get_indexer(top)
        end = min(i + rb, n)
        W[i:end, :] = 0.0
        W[i:end, ci] = 1.0 / top_k
        holds.append(len(top))
    Wdf = pd.DataFrame(W, index=dates, columns=cols)
    pos = Wdf.shift(1).fillna(0.0)                    # T+1
    gross = (pos * ret.fillna(0.0)).sum(axis=1)
    # 换手成本: 持仓权重日间变化的绝对值之和 × 5bp, 计在变动当日
    dW = pos.diff().abs().sum(axis=1).fillna(0.0)
    cost = dW * COST
    strat = gross - cost
    turnover = dW[dW > 0]
    return strat, turnover, (np.mean(holds) if holds else 0)


def bench_ew(ret, mask):
    """同池等权基准 (每日全池等权, T+1)."""
    w = mask.astype("float32")
    w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    pos = w.shift(1).fillna(0.0)
    return (pos * ret.fillna(0.0)).sum(axis=1)


def metrics(r):
    r = r.dropna()
    if len(r) < 20 or r.std() == 0:
        return dict(ann=0, sharpe=0, mdd=0, calmar=0)
    eq = (1 + r).cumprod()
    ann = eq.iloc[-1] ** (ANN / len(r)) - 1
    sharpe = r.mean() / r.std() * np.sqrt(ANN)
    mdd = (eq / eq.cummax() - 1).min()
    calmar = ann / abs(mdd) if mdd < 0 else 0.0
    return dict(ann=ann, sharpe=sharpe, mdd=mdd, calmar=calmar)


def split(r):
    return r[r.index <= IS_END], r[r.index > IS_END]


def main():
    close, ret, vol, share = load_panel()
    mask = build_universe(close, share)
    facs = factor_panels(close, ret, vol)

    bench = bench_ew(ret, mask)
    b_is, b_oos = split(bench)
    mb_is, mb_oos = metrics(b_is), metrics(b_oos)
    print(f"\n[基准 同池等权] IS calmar={mb_is['calmar']:.2f} ann={mb_is['ann']:.1%} "
          f"mdd={mb_is['mdd']:.1%} | OOS calmar={mb_oos['calmar']:.2f} "
          f"ann={mb_oos['ann']:.1%} mdd={mb_oos['mdd']:.1%}\n", flush=True)

    rows = []
    for fname, fdf in facs.items():
        for top_k in (20, 40):
            for rb in (5, 10):
                strat, turn, avgh = backtest(fdf, ret, mask, top_k, rb)
                r_is, r_oos = split(strat)
                m_is, m_oos = metrics(r_is), metrics(r_oos)
                flips_yr = (len(turn) and turn.mean()) and (turn.mean() * (ANN / rb)) or 0
                rows.append(dict(
                    factor=fname, top_k=top_k, rb=rb,
                    is_cal=round(m_is["calmar"], 2), is_ann=round(m_is["ann"], 3),
                    is_shp=round(m_is["sharpe"], 2), is_mdd=round(m_is["mdd"], 3),
                    oos_cal=round(m_oos["calmar"], 2), oos_ann=round(m_oos["ann"], 3),
                    oos_shp=round(m_oos["sharpe"], 2), oos_mdd=round(m_oos["mdd"], 3),
                    d_oos_cal=round(m_oos["calmar"] - mb_oos["calmar"], 2),
                    turn_yr=round(turn.sum() / (len(strat) / ANN), 1) if len(turn) else 0,
                ))
                print(f"  {fname:12s} K={top_k} rb={rb}: "
                      f"IS cal={m_is['calmar']:.2f} ann={m_is['ann']:.1%} | "
                      f"OOS cal={m_oos['calmar']:.2f} ann={m_oos['ann']:.1%} "
                      f"shp={m_oos['sharpe']:.2f} mdd={m_oos['mdd']:.1%}", flush=True)

    res = pd.DataFrame(rows).sort_values("oos_cal", ascending=False)
    res.to_csv("/home/rooot/agent_invest_lab/research/stock_select_factor/sweep_summary.csv", index=False)
    print("\n===== Top 12 by OOS Calmar =====", flush=True)
    pd.set_option("display.width", 200, "display.max_columns", 30)
    print(res.head(12).to_string(index=False), flush=True)
    print(f"\n基准 OOS calmar={mb_oos['calmar']:.2f} — 因子要明显高于它才算有 alpha", flush=True)


if __name__ == "__main__":
    main()
