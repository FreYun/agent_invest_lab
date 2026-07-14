"""申万行业成分同步: 拉 tushare index_member_all (当前在册 out_date IS NULL) -> market.db sw_industry_member.

走 _tushare_client (:18065 代理). 申万分类变动极少, 手动跑或周度 cron 即可.
表 ts_code 主键, 1:1 当前申万一级/二级.
"""
from __future__ import annotations

import os
import sqlite3
import time

DB_PATH = os.environ.get("SCOUT_DB_PATH") or __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))

DDL = """
CREATE TABLE IF NOT EXISTS sw_industry_member (
    ts_code    TEXT PRIMARY KEY,
    l1_code    TEXT,
    l1_name    TEXT,
    l2_code    TEXT,
    l2_name    TEXT,
    updated_at TEXT
)
"""


def ensure_table(conn) -> None:
    conn.execute(DDL)
    conn.commit()


def upsert_members(conn, rows) -> int:
    """rows: iterable of (ts_code, l1_code, l1_name, l2_code, l2_name). 返回写入行数."""
    ensure_table(conn)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    data = [(r[0], r[1], r[2], r[3], r[4], ts) for r in rows if r[0]]
    conn.executemany(
        "INSERT INTO sw_industry_member(ts_code,l1_code,l1_name,l2_code,l2_name,updated_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(ts_code) DO UPDATE SET "
        "l1_code=excluded.l1_code, l1_name=excluded.l1_name, "
        "l2_code=excluded.l2_code, l2_name=excluded.l2_name, "
        "updated_at=excluded.updated_at",
        data,
    )
    conn.commit()
    return len(data)


def fetch_current_members(call=None):
    """分页拉 index_member_all, 仅留 out_date IS NULL (当前在册). 失败抛错, 不重试."""
    if call is None:
        import _tushare_client
        call = _tushare_client.call
    out, off = [], 0
    while True:
        r = call("index_member_all", offset=off, limit=3000)
        rows = r.get("rows") or []
        if not rows:            # 兜底: 空页直接停(正常翻页由下方 has_more 控制)
            break
        for x in rows:
            if x.get("out_date") is None:
                out.append((x.get("ts_code"), x.get("l1_code"), x.get("l1_name"),
                            x.get("l2_code"), x.get("l2_name")))
        # has_more 是权威翻页信号
        if not r.get("has_more"):
            break
        off += len(rows)
    return out


def sync(db_path=None, fetch_fn=fetch_current_members) -> int:
    db_path = db_path or DB_PATH
    rows = fetch_fn()
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        return upsert_members(conn, rows)
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    dbp = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    n = sync(dbp)
    print(f"[sw_industry_sync] upserted {n} members into {dbp}")
