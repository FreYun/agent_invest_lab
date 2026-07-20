#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""显著性检验: 趋势×方向 胜率假设。

重叠持有窗口让逐日信号高度自相关 → naive 二项检验会把 +1pp 也判"显著"。
所以用三层更诚实的检验:
  1) 跨行业符号检验: 31 个行业当作较独立单元, A>C / B>D 的行业数 vs 0.5 二项 p
     (caveat: A股行业同涨同跌, 31 不完全独立, p 偏乐观)
  2) Block bootstrap: 对每行业收益序列按 block=H+1 重采样, 给胜率差 (A-C)/(B-D) 的 95% CI
  3) 非重叠交易: 入场后跳过 H 天再找下一笔, 得近独立样本, 复算胜率确认不是重叠制造的
"""
import os, glob, warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
from scipy import stats

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
COST = 0.001
HORIZONS = [5, 10, 20]
RSI_LOW, LOOKBACK, MOM_N = 30, 20, 20
RNG = np.random.default_rng(42)

NAMES = {  # 简表
    "801010":"农林牧渔","801030":"基础化工","801040":"钢铁","801050":"有色金属","801080":"电子",
    "801880":"汽车","801110":"家用电器","801120":"食品饮料","801130":"纺织服饰","801140":"轻工制造",
    "801150":"医药生物","801160":"公用事业","801170":"交通运输","801180":"房地产","801200":"商贸零售",
    "801210":"社会服务","801780":"银行","801790":"非银金融","801230":"综合","801710":"建筑材料",
    "801720":"建筑装饰","801730":"电力设备","801890":"机械设备","801740":"国防军工","801750":"计算机",
    "801760":"传媒","801770":"通信","801950":"煤炭","801960":"石油石化","801970":"环保","801980":"美容护理"}


def rsi_wilder(close, n=14):
    d = close.diff(); g = d.clip(lower=0); l = (-d).clip(lower=0)
    ag = g.ewm(alpha=1/n, adjust=False).mean(); al = l.ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + ag/al.replace(0, np.nan))


def build(df):
    df = df.sort_values("date").reset_index(drop=True).copy()
    c = df["close"]
    df["ma20"]=c.rolling(20).mean(); df["ma120"]=c.rolling(120).mean(); df["ma200"]=c.rolling(200).mean()
    df["rsi14"]=rsi_wilder(c,14)
    df["hh20"]=c.shift(1).rolling(LOOKBACK).max(); df["ll20"]=c.shift(1).rolling(LOOKBACK).min()
    df["mom20"]=c/c.shift(MOM_N)-1
    df["up"]=(c>df["ma120"])&(c>df["ma200"]); df["down"]=(c<df["ma120"])&(c<df["ma200"])
    df["left"]=(df["rsi14"]<RSI_LOW)|(c<df["ll20"])
    df["right"]=(c>df["hh20"])|((c>df["ma20"])&(df["mom20"]>0))
    return df


def cell_rets(df, mask, H, nonoverlap=False):
    c = df["close"].values; n = len(c)
    idx = np.where(mask.values)[0]
    rets = []; last_exit = -1
    for t in idx:
        if nonoverlap and t <= last_exit:
            continue
        e, x = t+1, t+1+H
        if x >= n: continue
        rets.append(c[x]/c[e]-1-COST)
        last_exit = x
    return np.array(rets)


def block_boot_winrate_diff(r1, r2, H, n_boot=2000):
    """block bootstrap 两组胜率差 (r1胜率 - r2胜率) 的 95% CI。"""
    block = H + 1
    def bw(r):
        if len(r) < block:
            return (r > 0).mean() if len(r) else np.nan
        nb = int(np.ceil(len(r)/block))
        starts = RNG.integers(0, len(r)-block+1, size=nb)
        samp = np.concatenate([r[s:s+block] for s in starts])[:len(r)]
        return (samp > 0).mean()
    diffs = np.array([bw(r1)-bw(r2) for _ in range(n_boot)])
    return np.nanpercentile(diffs, 2.5), np.nanpercentile(diffs, 97.5)


def main():
    files = sorted(glob.glob(os.path.join(DATA, "*.csv")))
    store = {}  # code -> df_v
    for f in files:
        code = os.path.basename(f)[:-4]
        df = build(pd.read_csv(f))
        store[code] = df[df["ma200"].notna()].copy()

    cells = lambda d: {
        "A": d["down"]&d["left"], "C": d["down"]&d["right"],
        "B": d["up"]&d["right"],  "D": d["up"]&d["left"]}

    print("="*78)
    print("【检验1】跨行业符号检验  (A>C 与 B>D 的行业数, 二项 p vs 0.5)")
    print("  注: A股行业同涨跌, 31个不完全独立, p 偏乐观; 但远好于把数万重叠日当独立")
    print("="*78)
    for H in HORIZONS:
        ac_wins = bd_wins = nrec = 0
        for code, d in store.items():
            cm = cells(d)
            wA=(cell_rets(d,cm["A"],H)>0).mean() if cell_rets(d,cm["A"],H).size else np.nan
            wC=(cell_rets(d,cm["C"],H)>0).mean() if cell_rets(d,cm["C"],H).size else np.nan
            wB=(cell_rets(d,cm["B"],H)>0).mean() if cell_rets(d,cm["B"],H).size else np.nan
            wD=(cell_rets(d,cm["D"],H)>0).mean() if cell_rets(d,cm["D"],H).size else np.nan
            if np.isnan([wA,wC,wB,wD]).any(): continue
            nrec += 1
            ac_wins += int(wA>wC); bd_wins += int(wB>wD)
        p_ac = stats.binomtest(ac_wins, nrec, 0.5, alternative="greater").pvalue
        p_bd = stats.binomtest(bd_wins, nrec, 0.5, alternative="greater").pvalue
        print(f"\n  H={H:2d}日 (n={nrec}行业):")
        print(f"    A(下跌左侧)>C(下跌右侧): {ac_wins}/{nrec} 行业  二项p={p_ac:.4f}  {'★显著' if p_ac<0.05 else '不显著'}")
        print(f"    B(上涨右侧)>D(上涨左侧): {bd_wins}/{nrec} 行业  二项p={p_bd:.4f}  {'★显著' if p_bd<0.05 else '不显著'}")

    print("\n" + "="*78)
    print("【检验2】Block bootstrap 胜率差 95% CI (池化全行业收益; CI不含0才显著)")
    print("="*78)
    for H in HORIZONS:
        rA=np.concatenate([cell_rets(d,cells(d)["A"],H) for d in store.values()])
        rC=np.concatenate([cell_rets(d,cells(d)["C"],H) for d in store.values()])
        rB=np.concatenate([cell_rets(d,cells(d)["B"],H) for d in store.values()])
        rD=np.concatenate([cell_rets(d,cells(d)["D"],H) for d in store.values()])
        wA,wC,wB,wD=[(r>0).mean() for r in (rA,rC,rB,rD)]
        lo1,hi1=block_boot_winrate_diff(rA,rC,H)
        lo2,hi2=block_boot_winrate_diff(rB,rD,H)
        print(f"\n  H={H:2d}日:")
        print(f"    A-C 胜率差 = {(wA-wC)*100:+5.1f}pp   95%CI [{lo1*100:+5.1f}, {hi1*100:+5.1f}]pp   {'★显著>0' if lo1>0 else ('★显著<0' if hi1<0 else '含0,不显著')}")
        print(f"    B-D 胜率差 = {(wB-wD)*100:+5.1f}pp   95%CI [{lo2*100:+5.1f}, {hi2*100:+5.1f}]pp   {'★显著>0' if lo2>0 else ('★显著<0' if hi2<0 else '含0,不显著')}")

    print("\n" + "="*78)
    print("【检验3】非重叠交易稳健性 (入场后跳过H天, 近独立样本; 池化胜率)")
    print("="*78)
    for H in HORIZONS:
        out=[]
        for tag in ["A","C","B","D"]:
            r=np.concatenate([cell_rets(d,cells(d)[tag],H,nonoverlap=True) for d in store.values()])
            out.append((tag,(r>0).mean(),len(r)))
        d=dict((t,(w,n)) for t,w,n in out)
        print(f"\n  H={H:2d}日 (非重叠):")
        print(f"    A下左={d['A'][0]*100:5.1f}%(n={d['A'][1]})  C下右={d['C'][0]*100:5.1f}%(n={d['C'][1]})  → A-C={(d['A'][0]-d['C'][0])*100:+.1f}pp {'✓' if d['A'][0]>d['C'][0] else '✗'}")
        print(f"    B上右={d['B'][0]*100:5.1f}%(n={d['B'][1]})  D上左={d['D'][0]*100:5.1f}%(n={d['D'][1]})  → B-D={(d['B'][0]-d['D'][0])*100:+.1f}pp {'✓' if d['B'][0]>d['D'][0] else '✗'}")


if __name__ == "__main__":
    main()
