#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""趋势×方向 胜率事件研究 (申万一级行业指数)。

假设: 下跌趋势里做左侧(买弱/抄底)、上涨趋势里做右侧(买强/追涨) 胜率高。

口径(全部已和用户确认):
- 趋势: close 同时穿越 MA120 与 MA200。
    up   = close>MA120 且 close>MA200
    down = close<MA120 且 close<MA200
    其余 = neutral(不计入)
- 入场信号:
    左侧(买弱) = RSI14<30  或  close 跌破近20日新低
    右侧(买强) = close 突破近20日新高  或  (close>MA20 且 20日动量>0)
- 2x2 矩阵 cell:
    A=down&左侧(假设主张)  C=down&右侧(对照)
    B=up&右侧(假设主张)    D=up&左侧(对照)
- 胜率: 信号当日 t 收盘可见 → T+1 次日收盘入场(close[t+1]) → 持有 H 日 → close[t+1+H] 平仓。
    H ∈ {5,10,20}; net_ret = close[t+1+H]/close[t+1]-1 - 0.001(买卖各5bp=10bp往返)
    win = net_ret > 0
- 基线: 同 regime 下"任意日买入"(side=ALL) 的前向胜率,用来抵消趋势漂移(上涨趋势里啥都赢)。

输出: results_sw.csv (long format) + 控制台汇总表。
"""
import os, sys, glob, warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

NAMES = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁", "801050": "有色金属",
    "801080": "电子", "801880": "汽车", "801110": "家用电器", "801120": "食品饮料",
    "801130": "纺织服饰", "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业",
    "801170": "交通运输", "801180": "房地产", "801200": "商贸零售", "801210": "社会服务",
    "801780": "银行", "801790": "非银金融", "801230": "综合", "801710": "建筑材料",
    "801720": "建筑装饰", "801730": "电力设备", "801890": "机械设备", "801740": "国防军工",
    "801750": "计算机", "801760": "传媒", "801770": "通信", "801950": "煤炭",
    "801960": "石油石化", "801970": "环保", "801980": "美容护理",
}

COST = 0.001          # 往返 10bp (买5bp+卖5bp)
HORIZONS = [5, 10, 20]
RSI_LOW = 30
LOOKBACK = 20         # 突破/新低窗口
MOM_N = 20            # 动量窗口


def rsi_wilder(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    ag = gain.ewm(alpha=1.0 / n, adjust=False).mean()
    al = loss.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = ag / al.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def build(df):
    df = df.sort_values("date").reset_index(drop=True).copy()
    c = df["close"]
    df["ma20"] = c.rolling(20).mean()
    df["ma120"] = c.rolling(120).mean()
    df["ma200"] = c.rolling(200).mean()
    df["rsi14"] = rsi_wilder(c, 14)
    df["hh20"] = c.shift(1).rolling(LOOKBACK).max()   # 不含当日的近20日最高
    df["ll20"] = c.shift(1).rolling(LOOKBACK).min()   # 不含当日的近20日最低
    df["mom20"] = c / c.shift(MOM_N) - 1.0

    # 趋势
    df["up"] = (c > df["ma120"]) & (c > df["ma200"])
    df["down"] = (c < df["ma120"]) & (c < df["ma200"])
    # 入场信号
    df["left"] = (df["rsi14"] < RSI_LOW) | (c < df["ll20"])               # 买弱
    df["right"] = (c > df["hh20"]) | ((c > df["ma20"]) & (df["mom20"] > 0))  # 买强
    return df


def fwd_winrate(df, mask, H):
    """对 mask 为真的信号日, T+1 收盘入场持有 H 日的净收益。"""
    c = df["close"].values
    n = len(c)
    idx = np.where(mask.values)[0]
    rets = []
    for t in idx:
        e = t + 1          # 入场 bar
        x = t + 1 + H      # 平仓 bar
        if x >= n:
            continue
        r = c[x] / c[e] - 1.0 - COST
        rets.append(r)
    rets = np.array(rets)
    if len(rets) == 0:
        return 0, np.nan, np.nan, np.nan, rets
    win = float((rets > 0).mean())
    return len(rets), win, float(rets.mean()), float(np.median(rets)), rets


def main():
    files = sorted(glob.glob(os.path.join(DATA, "*.csv")))
    rows = []
    raw_store = {}  # (code,regime,side,H) -> rets array, 给后续 bootstrap
    for f in files:
        code = os.path.basename(f)[:-4]
        name = NAMES.get(code, code)
        df = pd.read_csv(f)
        df = build(df)
        valid = df["ma200"].notna()  # 预热后
        df_v = df[valid].copy()
        start = df_v["date"].iloc[0] if len(df_v) else "-"
        end = df_v["date"].iloc[-1] if len(df_v) else "-"
        nyears = round(len(df_v) / 244.0, 1)

        cells = {
            "A_down_left":  df_v["down"] & df_v["left"],
            "C_down_right": df_v["down"] & df_v["right"],
            "B_up_right":   df_v["up"] & df_v["right"],
            "D_up_left":    df_v["up"] & df_v["left"],
            "base_down":    df_v["down"],   # 下跌趋势任意日买
            "base_up":      df_v["up"],     # 上涨趋势任意日买
        }
        for H in HORIZONS:
            for cell, mask in cells.items():
                n, win, mret, medret, rets = fwd_winrate(df_v, mask, H)
                rows.append(dict(code=code, name=name, years=nyears, start=start, end=end,
                                 cell=cell, H=H, n=n, winrate=win, mean_ret=mret, median_ret=medret))
                raw_store[(code, cell, H)] = rets
    res = pd.DataFrame(rows)
    out = os.path.join(HERE, "results_sw.csv")
    res.to_csv(out, index=False)
    # 同时存原始收益(npz)给 bootstrap
    np.savez(os.path.join(HERE, "raw_rets_sw.npz"),
             **{f"{k[0]}|{k[1]}|{k[2]}": v for k, v in raw_store.items() if len(v)})
    print(f"写出 {out}  ({len(res)} 行)\n")

    # ===== 汇总 1: 跨行业平均胜率(每个 cell × H) =====
    print("=" * 78)
    print("【汇总1】31个行业 跨行业平均胜率 (按 cell × 持有期)")
    print("=" * 78)
    piv = res.pivot_table(index="cell", columns="H", values="winrate", aggfunc="mean")
    cnt = res.pivot_table(index="cell", columns="H", values="n", aggfunc="sum")
    order = ["A_down_left", "C_down_right", "base_down", "B_up_right", "D_up_left", "base_up"]
    label = {"A_down_left": "A 下跌·左侧(假设✓)", "C_down_right": "C 下跌·右侧(对照)",
             "base_down": "  下跌·任意日(基线)", "B_up_right": "B 上涨·右侧(假设✓)",
             "D_up_left": "D 上涨·左侧(对照)", "base_up": "  上涨·任意日(基线)"}
    print(f"{'cell':22s} | {'胜率5d':>8s} {'胜率10d':>8s} {'胜率20d':>8s} | {'总信号5d':>9s}")
    for k in order:
        if k in piv.index:
            r = piv.loc[k]
            print(f"{label[k]:22s} | {r.get(5,np.nan)*100:7.1f}% {r.get(10,np.nan)*100:7.1f}% "
                  f"{r.get(20,np.nan)*100:7.1f}% | {int(cnt.loc[k].get(5,0)):9d}")

    # ===== 汇总 2: 假设的核心对比 A>C 和 B>D (跨行业) =====
    print("\n" + "=" * 78)
    print("【汇总2】假设核心对比 (每个持有期, 跨行业平均)")
    print("  假设成立 ⟺ A(下跌左侧) > C(下跌右侧)  且  B(上涨右侧) > D(上涨左侧)")
    print("=" * 78)
    for H in HORIZONS:
        sub = res[res.H == H]
        wA = sub[sub.cell == "A_down_left"]["winrate"].mean()
        wC = sub[sub.cell == "C_down_right"]["winrate"].mean()
        wB = sub[sub.cell == "B_up_right"]["winrate"].mean()
        wD = sub[sub.cell == "D_up_left"]["winrate"].mean()
        # 逐行业方向一致性
        m = sub.pivot_table(index="code", columns="cell", values="winrate")
        m = m.dropna(subset=["A_down_left", "C_down_right", "B_up_right", "D_up_left"])
        ac = (m["A_down_left"] > m["C_down_right"]).mean() if len(m) else np.nan
        bd = (m["B_up_right"] > m["D_up_left"]).mean() if len(m) else np.nan
        print(f"\n  H={H}日:")
        print(f"    下跌趋势: 左侧A={wA*100:5.1f}%  vs  右侧C={wC*100:5.1f}%   "
              f"→ A>C 的行业占比 {ac*100:4.0f}%  ({'✓假设支持' if wA>wC else '✗与假设相反'})")
        print(f"    上涨趋势: 右侧B={wB*100:5.1f}%  vs  左侧D={wD*100:5.1f}%   "
              f"→ B>D 的行业占比 {bd*100:4.0f}%  ({'✓假设支持' if wB>wD else '✗与假设相反'})")

    # ===== 汇总 3: 逐行业 (H=10) =====
    print("\n" + "=" * 78)
    print("【汇总3】逐行业胜率 (H=10日)  A=下跌左侧 C=下跌右侧 B=上涨右侧 D=上涨左侧")
    print("=" * 78)
    sub = res[res.H == 10]
    m = sub.pivot_table(index=["code", "name", "years"], columns="cell", values="winrate")
    mn = sub.pivot_table(index=["code", "name", "years"], columns="cell", values="n")
    print(f"{'行业':8s} {'年':>4s} | {'A下左':>6s} {'C下右':>6s} {'A>C':>4s} | "
          f"{'B上右':>6s} {'D上左':>6s} {'B>D':>4s} | {'nA':>4s} {'nB':>4s}")
    for (code, name, yr), r in m.iterrows():
        a, c_, b, d = r.get("A_down_left", np.nan), r.get("C_down_right", np.nan), r.get("B_up_right", np.nan), r.get("D_up_left", np.nan)
        na = mn.loc[(code, name, yr)].get("A_down_left", 0)
        nb = mn.loc[(code, name, yr)].get("B_up_right", 0)
        ac = "✓" if (a > c_) else "✗"
        bd = "✓" if (b > d) else "✗"
        def p(x): return f"{x*100:5.1f}" if pd.notna(x) else "  -  "
        print(f"{name:8s} {yr:4.1f} | {p(a):>6s} {p(c_):>6s} {ac:>4s} | "
              f"{p(b):>6s} {p(d):>6s} {bd:>4s} | {int(na) if pd.notna(na) else 0:4d} {int(nb) if pd.notna(nb) else 0:4d}")


if __name__ == "__main__":
    main()
