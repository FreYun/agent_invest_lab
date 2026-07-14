#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_index_daily.py — 宽基指数 K 线回填 + 每日 EOD 刷新。

从 tushare index_daily 取满 10 列写入 market.db.index_daily(INSERT OR REPLACE 幂等)。
  一次性回填全历史:  --full  --codes 399006.SZ,000688.SH,...
  每日刷新最近窗口:  --all   --days 20
  校验覆盖:          --verify --all

取数复用 market-regime 的 tushare_client(同目录);串行单进程 + code 间 sleep 限频;
DB 路径 expanduser + env, 不写死 /home。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tushare_client import call, TushareError  # noqa: E402

# 9 大宽基宇宙: (ts_code, 中文名), 有序
BROAD_BASE = [
    ("000001.SH", "上证综指"),
    ("399001.SZ", "深证成指"),
    ("399106.SZ", "深证综指"),
    ("000300.SH", "沪深300"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("932000.CSI", "中证2000"),
    ("899050.BJ", "北证50"),
]
BROAD_BASE_CODES = [c for c, _ in BROAD_BASE]

_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"
_FULL_START = "20100101"  # 覆盖各宽基 inception(最早 2010); tushare 自动截到实际上市日


def resolve_db_path(explicit=None):
    """DB 路径优先级: env MARKET_DB_PATH > explicit > ~/database/market.db(expanduser 可移植)。"""
    return (os.environ.get("MARKET_DB_PATH")
            or explicit
            or os.path.expanduser("~/database/market.db"))


def _cell(v):
    """NaN/None → None(SQL NULL); 其余原样。pandas NaN 是 float 且 != 自身。"""
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    return v


def upsert_index_daily_full(conn, code, df):
    """满 10 列 INSERT OR REPLACE 写 index_daily, 返回写入行数。

    df 需含 trade_date/open/high/low/close/pre_close/pct_chg/vol/amount。
    trade_date 原样 str() 入库(tushare 的 YYYYMMDD), 与既有格式一致。
    NaN(如 inception 日 pre_close/pct_chg) → SQL NULL。
    """
    rows = [
        (str(r["trade_date"]), code,
         _cell(r.get("open")), _cell(r.get("high")), _cell(r.get("low")),
         _cell(r.get("close")), _cell(r.get("pre_close")), _cell(r.get("pct_chg")),
         _cell(r.get("vol")), _cell(r.get("amount")))
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO index_daily "
        "(trade_date, ts_code, open, high, low, close, pre_close, pct_chg, vol, amount) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def fetch_index_daily_full(code, start, end):
    """tushare index_daily 取满字段 → DataFrame。0 行抛 ValueError。"""
    df = call("index_daily",
              {"ts_code": code, "start_date": start, "end_date": end},
              fields=_FIELDS)
    if len(df) == 0:
        raise ValueError(f"tushare index_daily 返回 0 行: {code} {start}~{end}")
    return df


def backfill(codes, start, end, db_path, sleep_s=1.2):
    """串行逐 code 取数+upsert, 返回 {code: 行数}。限频: 单进程 + code 间 sleep。"""
    conn = sqlite3.connect(db_path, timeout=60.0)
    result = {}
    try:
        for code in codes:
            try:
                df = fetch_index_daily_full(code, start, end)
                n = upsert_index_daily_full(conn, code, df)
                result[code] = n
                td = df["trade_date"].astype(str)
                print(f"  {code:12} OK {n} 行 ({td.min()}~{td.max()})")
            except (TushareError, ValueError) as e:
                result[code] = 0
                print(f"  {code:12} FAIL {e}", file=sys.stderr)
            if sleep_s:
                time.sleep(sleep_s)
    finally:
        conn.close()
    return result


def verify(db_path, codes):
    """每 code -> (count, min_date, max_date)。"""
    conn = sqlite3.connect(db_path, timeout=30.0)
    out = {}
    try:
        for code in codes:
            row = conn.execute(
                "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) "
                "FROM index_daily WHERE ts_code=?", (code,)).fetchone()
            out[code] = (row[0], row[1], row[2])
    finally:
        conn.close()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="宽基指数 index_daily 回填/刷新")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", help="逗号分隔 ts_code")
    g.add_argument("--all", action="store_true", help="全部 9 大宽基")
    ap.add_argument("--full", action="store_true", help="回填全历史(start=20100101)")
    ap.add_argument("--days", type=int, default=20, help="刷新窗口天数(非 --full, 默认20)")
    ap.add_argument("--start", help="起始 YYYYMMDD(覆盖 --full/--days)")
    ap.add_argument("--end", help="结束 YYYYMMDD(默认今天)")
    ap.add_argument("--db", help="market.db 路径(默认 env MARKET_DB_PATH / ~/database)")
    ap.add_argument("--sleep", type=float, default=1.2, help="code 间隔秒(限频)")
    ap.add_argument("--verify", action="store_true", help="只校验覆盖, 不取数")
    args = ap.parse_args(argv)

    codes = (BROAD_BASE_CODES if args.all
             else [c.strip() for c in args.codes.split(",") if c.strip()])
    db_path = resolve_db_path(args.db)

    if args.verify:
        print(f"== 校验 {db_path} ==")
        allok = True
        for code, (n, lo, hi) in verify(db_path, codes).items():
            flag = "OK" if n > 0 else "MISSING"
            if n == 0:
                allok = False
            print(f"  {code:12} {flag} {n} 行  {lo}~{hi}")
        return 0 if allok else 2

    end = args.end or date.today().strftime("%Y%m%d")
    if args.start:
        start = args.start
    elif args.full:
        start = _FULL_START
    else:
        start = (date.today() - timedelta(days=args.days)).strftime("%Y%m%d")

    print(f"== 回填 index_daily [{start}~{end}] -> {db_path} ==")
    res = backfill(codes, start, end, db_path, sleep_s=args.sleep)
    ok = sum(1 for n in res.values() if n > 0)
    print(f"完成: {ok}/{len(codes)} 个指数有数据")
    return 0 if ok == len(codes) else 2


if __name__ == "__main__":
    raise SystemExit(main())
