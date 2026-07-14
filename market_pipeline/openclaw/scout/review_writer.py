"""个股点评落库器(多 reviewer) — 读 stdin JSON 数组, upsert 到 intraday_review.

输入(技能从 review_scope 透传 code/name/sources, 自己补 stars/summary):
  [{"code":"002975","name":"博杰股份","sources":["s3","trend"],
    "logic_stars":4,"action_stars":3,"summary":"……"}, ...]

用法:
  echo '[...]' | python3 review_writer.py
  python3 review_writer.py --date 2026-05-25 < items.json
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


def write_reviews(c, trade_date, now, items, reviewer):
    """校验并 upsert, 返回写入条数. 缺 code/stars 或星级越界的条目跳过并打日志."""
    rows = []
    for it in items:
        code = it.get("code")
        ls, as_ = it.get("logic_stars"), it.get("action_stars")
        if not code:
            print(f"  跳过(缺 code): {it}"); continue
        if not _stars_ok(ls) or not _stars_ok(as_):
            print(f"  跳过(星级缺失/越界): {code} {ls}/{as_}"); continue
        src = it.get("sources")
        src = ",".join(src) if isinstance(src, list) else (src or "")
        rows.append((trade_date, scout_db.bare(code), it.get("name"), src,
                     int(ls), int(as_), it.get("summary"), now, reviewer))
    if rows:
        c.executemany(
            "INSERT OR REPLACE INTO intraday_review "
            "(trade_date,code,name,sources,logic_stars,action_stars,summary,reviewed_at,reviewer) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
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
    n = write_reviews(c, today, now, items, a.reviewer)
    c.close()
    print(f"[review-writer] {today} reviewer={a.reviewer} 写入 {n} 条点评")


if __name__ == "__main__":
    main()
