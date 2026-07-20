#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""右侧追涨因子族 sweep —— "怎么追涨做右侧比较好"。

已确认: 上涨趋势里右侧(买强)胜过左侧。本脚本比较多种"右侧"的具体做法,
看哪种追涨方式在 31 个申万一级行业上最稳健。

方法(遵 SOP): 自写仓位回测, T+1(pos=sig.shift(1)), 单边 5bp,
排名用 Calmar(非 Sharpe), 稳健性看跨行业"击败 buy&hold"占比。

因子族(全部 long-only 0/1):
  突破族  bo20/bo60/bo120         — 创 N 日新高进, 跌破 M 日低出 (经典右侧)
  均线族  ma20/ma60/ma20_60       — 站上均线/多头排列持有
  动量族  mom20/mom60/mom120      — 时序动量为正持有
  趋势门  gate_ma20/gate_bo20/gate_mom20 — 先要 MA120&MA200 上涨, 再叠右侧 (检验趋势×右侧叠加)
  RSI    rsi50                   — RSI14>50 动量区间持有
  基准   buyhold
"""
import os, glob, warnings
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
COST = 0.0005           # 单边 5bp
YEAR = 244

NAMES = {
 "801010":"农林牧渔","801030":"基础化工","801040":"钢铁","801050":"有色金属","801080":"电子",
 "801880":"汽车","801110":"家用电器","801120":"食品饮料","801130":"纺织服饰","801140":"轻工制造",
 "801150":"医药生物","801160":"公用事业","801170":"交通运输","801180":"房地产","801200":"商贸零售",
 "801210":"社会服务","801780":"银行","801790":"非银金融","801230":"综合","801710":"建筑材料",
 "801720":"建筑装饰","801730":"电力设备","801890":"机械设备","801740":"国防军工","801750":"计算机",
 "801760":"传媒","801770":"通信","801950":"煤炭","801960":"石油石化","801970":"环保","801980":"美容护理"}


def indicators(df):
    df = df.sort_values("date").reset_index(drop=True).copy()
    c, h, l = df["close"], df["high"], df["low"]
    for n in (20, 60, 120, 200):
        df[f"ma{n}"] = c.rolling(n).mean()
    for n in (20, 60, 120):
        df[f"mom{n}"] = c / c.shift(n) - 1
    df["hh20"] = h.shift(1).rolling(20).max(); df["ll10"] = l.shift(1).rolling(10).min()
    df["hh60"] = h.shift(1).rolling(60).max(); df["ll20"] = l.shift(1).rolling(20).min()
    df["hh120"] = h.shift(1).rolling(120).max(); df["ll40"] = l.shift(1).rolling(40).min()
    d = c.diff(); g = d.clip(lower=0); ls = (-d).clip(lower=0)
    rs = g.ewm(alpha=1/14, adjust=False).mean() / ls.ewm(alpha=1/14, adjust=False).mean().replace(0, np.nan)
    df["rsi14"] = 100 - 100/(1+rs)
    df["up"] = (c > df["ma120"]) & (c > df["ma200"])
    return df


def donchian(df, hh, ll):
    """突破上轨进(1)、跌破下轨出(0)、中间保持。"""
    c = df["close"].values; H = df[hh].values; L = df[ll].values
    pos = np.zeros(len(c)); state = 0
    for i in range(len(c)):
        if np.isnan(H[i]) or np.isnan(L[i]):
            pos[i] = 0; continue
        if c[i] > H[i]: state = 1
        elif c[i] < L[i]: state = 0
        pos[i] = state
    return pd.Series(pos, index=df.index)


def signals(df):
    c = df["close"]
    return {
        "bo20":  donchian(df, "hh20", "ll10"),
        "bo60":  donchian(df, "hh60", "ll20"),
        "bo120": donchian(df, "hh120", "ll40"),
        "ma20":  (c > df["ma20"]).astype(float),
        "ma60":  (c > df["ma60"]).astype(float),
        "ma20_60": ((c > df["ma20"]) & (df["ma20"] > df["ma60"])).astype(float),
        "mom20": (df["mom20"] > 0).astype(float),
        "mom60": (df["mom60"] > 0).astype(float),
        "mom120": (df["mom120"] > 0).astype(float),
        "gate_ma20":  (df["up"] & (c > df["ma20"])).astype(float),
        "gate_bo20":  (df["up"].astype(float) * donchian(df, "hh20", "ll10")),
        "gate_mom20": (df["up"] & (df["mom20"] > 0)).astype(float),
        "rsi50": (df["rsi14"] > 50).astype(float),
        "buyhold": pd.Series(1.0, index=df.index),
    }


def metrics(df, sig, eval_mask):
    ret = df["close"].pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    turn = pos.diff().abs().fillna(pos.iloc[0])
    strat = (pos * ret - turn * COST)[eval_mask]
    posv = pos[eval_mask]
    if len(strat) < YEAR:
        return None
    eq = (1 + strat).cumprod()
    yrs = len(strat) / YEAR
    cagr = eq.iloc[-1] ** (1/yrs) - 1
    vol = strat.std() * np.sqrt(YEAR)
    sharpe = strat.mean() / strat.std() * np.sqrt(YEAR) if strat.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    calmar = cagr / abs(dd) if dd < 0 else np.nan
    expo = posv.mean()
    # 交易笔数与单笔胜率
    pv = posv.values; entries = []; instate = False; start = 0
    cc = df["close"][eval_mask].values
    for i in range(len(pv)):
        if pv[i] > 0 and not instate:
            instate = True; start = i
        elif pv[i] == 0 and instate:
            instate = False
            if i > start: entries.append(cc[i]/cc[start]-1 - 2*COST)
    if instate and len(pv) > start: entries.append(cc[-1]/cc[start]-1 - 2*COST)
    ntrades = len(entries)
    winrate = float(np.mean([e > 0 for e in entries])) if entries else np.nan
    return dict(cagr=cagr, vol=vol, sharpe=sharpe, maxdd=dd, calmar=calmar,
                expo=expo, trades_yr=ntrades/yrs, trade_win=winrate)


def main():
    files = sorted(glob.glob(os.path.join(DATA, "*.csv")))
    recs = []
    for f in files:
        code = os.path.basename(f)[:-4]; name = NAMES.get(code, code)
        df = indicators(pd.read_csv(f))
        eval_mask = df["ma200"].notna()
        if eval_mask.sum() < YEAR: continue
        sigs = signals(df)
        bh = metrics(df, sigs["buyhold"], eval_mask)
        for fac, sig in sigs.items():
            m = metrics(df, sig, eval_mask)
            if m is None: continue
            m.update(code=code, name=name, factor=fac,
                     dcalmar=(m["calmar"] - bh["calmar"]) if (pd.notna(m["calmar"]) and pd.notna(bh["calmar"])) else np.nan,
                     bh_calmar=bh["calmar"])
            recs.append(m)
    res = pd.DataFrame(recs)
    res.to_csv(os.path.join(HERE, "right_side_sweep.csv"), index=False)

    facs = [f for f in signals(indicators(pd.read_csv(files[0]))).keys()]
    print("="*100)
    print("右侧追涨因子族 —— 跨31行业汇总 (排名按 median Calmar; 稳健性看 击败buyhold 占比)")
    print("="*100)
    print(f"{'因子':11s}|{'medCalmar':>9s}{'medSharpe':>9s}{'medCAGR':>8s}{'medDD':>7s}|"
          f"{'med dCal':>8s}{'胜buyhold':>9s}|{'仓位':>6s}{'笔/年':>6s}{'单笔胜率':>8s}")
    print("-"*100)
    agg = []
    for fac in facs:
        s = res[res.factor == fac]
        if s.empty: continue
        beat = (s["dcalmar"] > 0).mean()
        agg.append((fac, s["calmar"].median(), s["sharpe"].median(), s["cagr"].median(),
                    s["maxdd"].median(), s["dcalmar"].median(), beat,
                    s["expo"].mean(), s["trades_yr"].mean(), s["trade_win"].mean()))
    agg.sort(key=lambda x: (-(x[5] if pd.notna(x[5]) else -9)))  # 按 med dCalmar 降序
    for fac, cal, shp, cagr, dd, dcal, beat, expo, tpy, tw in agg:
        star = "★" if (pd.notna(dcal) and dcal > 0 and beat >= 0.6) else " "
        print(f"{fac:11s}|{cal:9.2f}{shp:9.2f}{cagr*100:7.1f}%{dd*100:6.0f}%|"
              f"{dcal:+8.2f}{beat*100:8.0f}%{star}|{expo*100:5.0f}%{tpy:6.1f}{tw*100:7.1f}%")
    print("\n注: buyhold 自身 dCalmar=0、击败占比为0属正常(它是基准)。★=median dCalmar>0 且 ≥60%行业击败buyhold。")


if __name__ == "__main__":
    main()
