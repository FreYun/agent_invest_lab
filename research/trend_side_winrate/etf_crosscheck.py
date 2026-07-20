#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fund.db 行业ETF 近2.4年交叉验证 (同口径)。
样本极薄(每只593条,MA200预热后~1.6年信号期),仅作近期一致性 sanity check,不能下结论。
NAV 仅有收盘(无高低),left/right 信号本就只用 close, 不受影响。
"""
import os, sqlite3, warnings
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd

DB = "/home/rooot/agent_invest_lab/data/fund.db"
COST, HORIZONS = 0.001, [5, 10, 20]
RSI_LOW, LOOKBACK, MOM_N = 30, 20, 20

ETFS = {
 "512010":"医药","512480":"半导体","159995":"芯片华夏","512760":"芯片国泰","512880":"证券",
 "512000":"券商","515020":"银行","515030":"新能源车","516160":"新能源","515790":"光伏",
 "515170":"食品饮料","159928":"消费","516110":"汽车","159996":"家电","512400":"有色",
 "515050":"通信","512980":"传媒","159825":"农业","159611":"电力"}


def rsi_wilder(c, n=14):
    d=c.diff(); g=d.clip(lower=0); l=(-d).clip(lower=0)
    ag=g.ewm(alpha=1/n,adjust=False).mean(); al=l.ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+ag/al.replace(0,np.nan))


def build(df):
    df=df.sort_values("nav_date").reset_index(drop=True).copy()
    c=df["nav"]
    df["ma20"]=c.rolling(20).mean(); df["ma120"]=c.rolling(120).mean(); df["ma200"]=c.rolling(200).mean()
    df["rsi14"]=rsi_wilder(c,14)
    df["hh20"]=c.shift(1).rolling(LOOKBACK).max(); df["ll20"]=c.shift(1).rolling(LOOKBACK).min()
    df["mom20"]=c/c.shift(MOM_N)-1
    df["up"]=(c>df["ma120"])&(c>df["ma200"]); df["down"]=(c<df["ma120"])&(c<df["ma200"])
    df["left"]=(df["rsi14"]<RSI_LOW)|(c<df["ll20"])
    df["right"]=(c>df["hh20"])|((c>df["ma20"])&(df["mom20"]>0))
    df["close"]=c
    return df


def rets(df, mask, H):
    c=df["close"].values; n=len(c); out=[]
    for t in np.where(mask.values)[0]:
        e,x=t+1,t+1+H
        if x>=n: continue
        out.append(c[x]/c[e]-1-COST)
    return np.array(out)


def main():
    con=sqlite3.connect(DB)
    store={}
    for code,name in ETFS.items():
        df=pd.read_sql_query("SELECT nav_date,nav FROM fund_nav WHERE fund_code=? ORDER BY nav_date",con,params=(code,))
        if len(df)<400: continue
        d=build(df); store[code]=d[d["ma200"].notna()].copy()
    print(f"参与交叉验证 ETF: {len(store)} 只, 各~{list(store.values())[0].shape[0]}个信号期交易日 (≈1.6年)\n")
    cells=lambda d:{"A":d["down"]&d["left"],"C":d["down"]&d["right"],"B":d["up"]&d["right"],"D":d["up"]&d["left"]}
    print("="*70)
    print("fund.db 行业ETF 近2.4年 池化胜率 (★样本薄,仅参考)")
    print("="*70)
    for H in HORIZONS:
        agg={t:np.concatenate([rets(d,cells(d)[t],H) for d in store.values()]) for t in "ACBD"}
        w={t:((agg[t]>0).mean() if len(agg[t]) else np.nan) for t in "ACBD"}
        n={t:len(agg[t]) for t in "ACBD"}
        print(f"\n  H={H:2d}日:")
        print(f"    A下跌左侧={w['A']*100:5.1f}%(n={n['A']:4d})  C下跌右侧={w['C']*100:5.1f}%(n={n['C']:4d})  → A-C={(w['A']-w['C'])*100:+5.1f}pp {'✓' if w['A']>w['C'] else '✗'}")
        print(f"    B上涨右侧={w['B']*100:5.1f}%(n={n['B']:4d})  D上涨左侧={w['D']*100:5.1f}%(n={n['D']:4d})  → B-D={(w['B']-w['D'])*100:+5.1f}pp {'✓' if w['B']>w['D'] else '✗'}")


if __name__ == "__main__":
    main()
