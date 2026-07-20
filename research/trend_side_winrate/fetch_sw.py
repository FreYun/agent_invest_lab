#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓取申万一级行业指数(31个)日线 OHLC 到 CSV，断点续传，串行单进程。
数据源: akshare ak.index_hist_sw(symbol, period='day')，1999-12-30 起，含 OHLC+量额。
用法: <vibe-trading python> fetch_sw.py
"""
import os, sys, time, warnings
warnings.filterwarnings("ignore")
import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

# 申万一级行业 31 个 (代码不带 .SI 后缀给 index_hist_sw)
SW1 = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁", "801050": "有色金属",
    "801080": "电子", "801880": "汽车", "801110": "家用电器", "801120": "食品饮料",
    "801130": "纺织服饰", "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业",
    "801170": "交通运输", "801180": "房地产", "801200": "商贸零售", "801210": "社会服务",
    "801780": "银行", "801790": "非银金融", "801230": "综合", "801710": "建筑材料",
    "801720": "建筑装饰", "801730": "电力设备", "801890": "机械设备", "801740": "国防军工",
    "801750": "计算机", "801760": "传媒", "801770": "通信", "801950": "煤炭",
    "801960": "石油石化", "801970": "环保", "801980": "美容护理",
}

def main():
    ok, skip, fail = 0, 0, 0
    for code, name in SW1.items():
        out = os.path.join(DATA, f"{code}.csv")
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            skip += 1
            print(f"[skip] {code} {name} 已存在", flush=True)
            continue
        for attempt in range(3):
            try:
                df = ak.index_hist_sw(symbol=code, period="day")
                # 列: 代码,日期,收盘,开盘,最高,最低,成交量,成交额
                df = df.rename(columns={
                    "日期": "date", "收盘": "close", "开盘": "open",
                    "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
                })
                df = df[["date", "open", "high", "low", "close", "volume", "amount"]].copy()
                df["date"] = df["date"].astype(str)
                df.to_csv(out, index=False)
                ok += 1
                print(f"[ok]   {code} {name} {len(df)}行 {df['date'].iloc[0]}~{df['date'].iloc[-1]}", flush=True)
                break
            except Exception as e:
                if attempt == 2:
                    fail += 1
                    print(f"[FAIL] {code} {name}: {repr(e)[:120]}", flush=True)
                else:
                    time.sleep(2)
        time.sleep(0.8)  # 礼貌限速
    print(f"\n完成: ok={ok} skip={skip} fail={fail}")

if __name__ == "__main__":
    main()
