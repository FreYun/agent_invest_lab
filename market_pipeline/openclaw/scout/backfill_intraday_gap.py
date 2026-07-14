#!/usr/bin/env python3.12
"""一次性: 用东财 1min k 线补 intraday_snapshot 的 *_pct 缺口分钟.

仅写 8 个 *_pct 列(截图的分时图只画这几条); up/down/limit_up/blast/total_amount
等实时快照类字段无法回填(realtime_list 无历史),写 NULL 由前端 gap 逻辑跳过.
用 INSERT OR IGNORE, 避免覆盖任何已有真实快照行.

Usage: python3.12 backfill_intraday_gap.py YYYY-MM-DD HH:MM HH:MM
Example: python3.12 backfill_intraday_gap.py 2026-07-06 14:15 14:26
"""
import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))

INDICES = [
    ("1.000001", "sh_pct"),
    ("1.000300", "csi300_pct"),
    ("0.399001", "szcz_pct"),
    ("0.399006", "gem_pct"),
    ("1.000688", "star50_pct"),
    ("0.899050", "bz50_pct"),
    ("1.000852", "csi1000_pct"),
    ("2.932000", "csi2000_pct"),
]


def fetch_klines(secid: str):
    url = ("http://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&klt=1&fqt=0&beg=0&end=20500000&lmt=600"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=8) as resp:
        j = json.loads(resp.read().decode("utf-8"))
    d = j.get("data") or {}
    pre = d.get("preKPrice")
    if not pre:
        return None, []
    out = {}
    for line in d.get("klines") or []:
        parts = line.split(",")
        # parts[0]='YYYY-MM-DD HH:MM', parts[2]=close
        ts, close = parts[0], float(parts[2])
        out[ts] = close
    return pre, out


def parse_hhmm(s: str) -> int:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("start", help="HH:MM inclusive")
    ap.add_argument("end", help="HH:MM inclusive")
    ap.add_argument("--dry", action="store_true", help="不写库,只打印将写入的行")
    args = ap.parse_args()

    trade_date = args.date.replace("-", "")
    start_m = parse_hhmm(args.start)
    end_m = parse_hhmm(args.end)

    per_index = {}
    for secid, col in INDICES:
        pre, klines = fetch_klines(secid)
        if pre is None:
            print(f"  [{col:11}] pre_close 缺失, 跳过", file=sys.stderr)
            per_index[col] = (None, {})
            continue
        # 只保留目标日 & 时间窗内的分钟
        pct_by_minute = {}
        for ts, close in klines.items():
            d, t = ts.split(" ")
            if d != args.date:
                continue
            m = parse_hhmm(t)
            if start_m <= m <= end_m:
                pct_by_minute[t] = round((close - pre) / pre * 100, 2)
        per_index[col] = (pre, pct_by_minute)
        print(f"  [{col:11}] pre={pre:>10}  命中 {len(pct_by_minute)} 分钟")

    # 汇聚所有出现过的分钟, 按分钟写一行
    minutes = set()
    for _, pct in per_index.values():
        minutes.update(pct.keys())
    minutes = sorted(minutes)
    if not minutes:
        print("窗口内没有任何指数分钟数据, 退出.", file=sys.stderr)
        sys.exit(1)

    rows = []
    for hhmm in minutes:
        snapshot_time = f"{args.date} {hhmm}:00"
        row = {"snapshot_time": snapshot_time, "trade_date": trade_date}
        for col, (_pre, pct) in per_index.items():
            row[col] = pct.get(hhmm)
        rows.append(row)

    if args.dry:
        for r in rows:
            print(r)
        return

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    cols_pct = [c for _, c in INDICES]
    cols = ["snapshot_time", "trade_date"] + cols_pct
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT OR IGNORE INTO intraday_snapshot ({','.join(cols)}) VALUES ({placeholders})"
    n_inserted = 0
    for r in rows:
        vals = [r[c] for c in cols]
        cur = conn.execute(sql, vals)
        n_inserted += cur.rowcount
    conn.commit()
    conn.close()
    print(f"写入 {n_inserted}/{len(rows)} 分钟 (其余已有行未覆盖).")


if __name__ == "__main__":
    main()
