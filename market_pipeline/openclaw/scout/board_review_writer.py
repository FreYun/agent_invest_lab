"""板块点评落库器(多 reviewer) — 读 stdin JSON 数组, upsert 到 intraday_board_review.

输入(技能从 review_scope 的 boards 段透传 board_code/board_name, 自己补 stars/summary):
  [{"board_code":"BK0001","board_name":"高带宽内存",
    "logic_stars":4,"continuation_stars":4,"summary":"……深度分析……"}, ...]

用法:
  echo '[...]' | python3 board_review_writer.py
  python3 board_review_writer.py --date 2026-05-25 < items.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402


def _stars_ok(v):
    try:
        return 1 <= int(v) <= 5
    except (TypeError, ValueError):
        return False


def write_board_reviews(c, trade_date, now, items, reviewer):
    """校验并 upsert, 返回写入条数. 缺 board_code 或星级越界(逻辑/持续性)的条目跳过并打日志."""
    rows = []
    for it in items:
        bc = it.get("board_code")
        ls = it.get("logic_stars")
        cs = it.get("continuation_stars")
        if not bc:
            print(f"  跳过(缺 board_code): {it}"); continue
        if not _stars_ok(ls) or not _stars_ok(cs):
            print(f"  跳过(星级缺失/越界): {bc} logic={ls} cont={cs}"); continue
        rows.append((trade_date, bc, it.get("board_name"),
                     it.get("summary"), int(ls), int(cs), now, reviewer))
    if rows:
        c.executemany(
            "INSERT OR REPLACE INTO intraday_board_review "
            "(trade_date,board_code,board_name,summary,logic_stars,continuation_stars,reviewed_at,reviewer) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
        c.commit()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYY-MM-DD(默认今日)")
    ap.add_argument("--reviewer", default="bot11", help="点评者 id: bot11(小奶龙) | bot7(老K)")
    a = ap.parse_args()
    today = a.date or datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raw = sys.stdin.read().strip()
    items = json.loads(raw) if raw else []
    if isinstance(items, dict):
        items = items.get("items", [])
    scout_db.init_schema()
    c = scout_db.conn()
    n = write_board_reviews(c, today, now, items, a.reviewer)
    c.close()
    print(f"[board-review-writer] {today} reviewer={a.reviewer} 写入 {n} 条板块点评")


if __name__ == "__main__":
    main()
