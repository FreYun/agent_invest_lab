#!/usr/bin/env python3.12
"""一次性迁移：把 runs.html 内嵌的评测 CSV 搬进 lab fund.db 的 fund_bot_run_eval 表。

用法:
    python3.12 migrate_run_eval.py            # 默认从 runs.html 抽 CSV、写种子、导入默认库
    python3.12 migrate_run_eval.py --db /path/to/fund.db
    python3.12 migrate_run_eval.py --from-seed  # 不再从 HTML 抽，直接用已存在的种子 CSV 重导

设计要点:
  * 新建独立表 fund_bot_run_eval，不动任何现有快照表。
  * 种子 CSV(bot-runs-eval-seed.csv)只是迁移输入/留档，运行时只读 DB。
  * 幂等: CREATE TABLE IF NOT EXISTS + INSERT OR REPLACE，按 (run_id, bot) 主键去重。
  * CSV 值原样入库(保真),数值列空串->NULL,不重算口径。
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.normpath(os.path.join(HERE, "../../../data/fund.db"))
RUNS_HTML = os.path.join(HERE, "runs.html")
SEED_CSV = os.path.join(HERE, "bot-runs-eval-seed.csv")

# CSV 列(按 runs.html 表头顺序) -> (DB 列名, 类型)。类型 t=TEXT r=REAL i=INTEGER。
COLUMNS = [
    ("启动时间", "launch_time", "t"),
    ("run_id", "run_id", "t"),
    ("bot", "bot", "t"),
    ("策略", "strategy", "t"),
    ("对标指数", "target_index", "t"),
    ("可买基金", "buyable_fund", "t"),
    ("状态", "status", "t"),
    ("窗口起", "window_start", "t"),
    ("窗口止", "window_end", "t"),
    ("回测月数", "months", "r"),
    ("进度", "progress", "t"),
    ("绝对收益%", "abs_return_pct", "r"),
    ("年化收益%", "ann_return_pct", "r"),
    ("最大回撤%", "max_drawdown_pct", "r"),
    ("被动满仓%", "passive_full_pct", "r"),
    ("年化被动%", "passive_ann_pct", "r"),
    ("超额_择时%", "excess_timing_pct", "r"),
    ("上行捕获%", "up_capture_pct", "r"),
    ("下行保护%", "down_protect_pct", "r"),
    ("操作_买", "ops_buy", "i"),
    ("操作_卖", "ops_sell", "i"),
    ("操作合计", "ops_total", "i"),
    ("末日现金%", "end_cash_pct", "r"),
    ("评价", "evaluation", "t"),
    ("普通投资者评级", "retail_rating", "t"),
    ("综合点评", "review", "t"),
    ("备注", "note", "t"),
]

SQL_TYPE = {"t": "TEXT", "r": "REAL", "i": "INTEGER"}


def extract_csv_from_html(html_path: str) -> str:
    """从 runs.html 抽出 <script id="csvData"> 块的原文(去前导 BOM/空白)。"""
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    m = re.search(
        r'<script[^>]*id="csvData"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if not m:
        raise SystemExit("在 runs.html 中找不到 id=\"csvData\" 的 <script> 块")
    body = m.group(1)
    return body.lstrip("﻿").strip()


def coerce(value: str, kind: str):
    """按列类型把 CSV 字符串转成 DB 值；空串/无效 -> None。"""
    s = (value or "").strip()
    if kind == "t":
        return s  # 文本原样(可空串)
    if s == "":
        return None
    try:
        return int(s) if kind == "i" else float(s)
    except ValueError:
        return None


def create_table(conn: sqlite3.Connection) -> None:
    cols_sql = ",\n  ".join(f"{db} {SQL_TYPE[t]}" for _, db, t in COLUMNS)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS fund_bot_run_eval (
          {cols_sql},
          PRIMARY KEY (run_id, bot)
        )
        """
    )


def load_rows(csv_text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    idx = {name: header.index(name) for name, _, _ in COLUMNS if name in header}
    missing = [name for name, _, _ in COLUMNS if name not in idx]
    if missing:
        raise SystemExit(f"CSV 缺列: {missing}")
    out = []
    for arr in rows[1:]:
        if not arr:
            continue
        launch = arr[idx["启动时间"]].strip() if idx["启动时间"] < len(arr) else ""
        run_id = arr[idx["run_id"]].strip() if idx["run_id"] < len(arr) else ""
        bot = arr[idx["bot"]].strip() if idx["bot"] < len(arr) else ""
        if launch.startswith("【") or not run_id or not bot:
            continue  # 跳过【整体汇总】/空行(与前端口径一致)
        rec = {}
        for name, db, kind in COLUMNS:
            raw = arr[idx[name]] if idx[name] < len(arr) else ""
            rec[db] = coerce(raw, kind)
        out.append(rec)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--from-seed", action="store_true",
                    help="直接读已存在的种子 CSV,不再从 runs.html 抽")
    args = ap.parse_args(argv)

    if args.from_seed:
        if not os.path.exists(SEED_CSV):
            raise SystemExit(f"种子文件不存在: {SEED_CSV}")
        with open(SEED_CSV, "r", encoding="utf-8") as f:
            csv_text = f.read().lstrip("﻿").strip()
        print(f"[seed] 从种子读取: {SEED_CSV}")
    else:
        csv_text = extract_csv_from_html(RUNS_HTML)
        with open(SEED_CSV, "w", encoding="utf-8", newline="") as f:
            f.write(csv_text + "\n")
        print(f"[seed] 已从 runs.html 抽出并写入: {SEED_CSV}")

    rows = load_rows(csv_text)
    print(f"[parse] 解析到 {len(rows)} 条数据行(已跳过汇总/空行)")

    if not os.path.exists(args.db):
        raise SystemExit(f"DB 不存在: {args.db}")

    conn = sqlite3.connect(args.db, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        create_table(conn)
        db_cols = [db for _, db, _ in COLUMNS]
        placeholders = ",".join("?" for _ in db_cols)
        sql = (f"INSERT OR REPLACE INTO fund_bot_run_eval "
               f"({','.join(db_cols)}) VALUES ({placeholders})")
        conn.executemany(sql, [[r[c] for c in db_cols] for r in rows])
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM fund_bot_run_eval").fetchone()[0]
        print(f"[done] fund_bot_run_eval 现有 {n} 行 -> {args.db}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
