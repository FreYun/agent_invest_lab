"""候选逻辑富集器 — 1h 跑一次, 给每只候选配 题材归属 + zsxq 纪要. 写 candidate_logic.

纯确定性, 零模型:
  题材归属: stock_concept_map.board_code JOIN concept_board_daily(最新) -> 真题材名+今日涨幅
  基本面逻辑: zsxq.db topics LIKE '%股名%' 最近 N 天纪要

用法:
  python3 logic.py            # 刷全部 S1~S7 最新候选
  python3 logic.py --days 45  # zsxq 回看天数(默认30)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402

ZSXQ_DB = "/home/rooot/database/zsxq.db"
# 2026-06-02 按结构择时: S4 已下线, 从纪要富集列表移除
STRATEGIES = ["s1", "s2", "s3", "s5", "s6", "s7", "s8", "s9"]

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean(text: str, n: int = 80) -> str:
    """剥 zsxq 富文本标签 + 折叠空白, 截断."""
    if not text:
        return ""
    t = _TAG.sub("", text)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = _WS.sub(" ", t).strip()
    return t[:n]


def snippet_around(text: str, kw: str, win: int = 60) -> str:
    """取 text 中首个命中 kw 的上下文窗口(剥标签后). 命中不到则取开头."""
    t = _TAG.sub("", text or "")
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = _WS.sub(" ", t).strip()
    i = t.find(kw)
    if i < 0:
        return t[:win * 2]
    a = max(0, i - win // 3)
    s = t[a:a + win]
    return ("…" if a > 0 else "") + s + ("…" if a + win < len(t) else "")


def candidate_list(c, anchor):
    """(strategy, code, name, ts_code) 去重 — 锚定昨日候选 (anchor 日期)."""
    seen, out = set(), []
    for s in STRATEGIES:
        try:
            recs = c.execute(
                f"SELECT code, name FROM {s}_candidates WHERE date=?", (anchor,)
            ).fetchall()
        except Exception as e:
            print(f"  [{s}] 候选查询失败: {str(e)[:60]}")
            continue
        for r in recs:
            code = str(r["code"]).zfill(6)
            key = (s, code)
            if key in seen:
                continue
            seen.add(key)
            out.append((s, code, r["name"], scout_db.to_suffix(code)))
    return out


def themes_for(c, ts_code, board_pct):
    """候选所属题材 + 今日涨幅, 按涨幅降序. board_pct: {board_code: (name, pct)}."""
    out = []
    for (bc,) in c.execute("SELECT board_code FROM stock_concept_map WHERE ts_code=?", (ts_code,)):
        bp = board_pct.get(bc)
        if bp:
            out.append({"theme": bp[0], "pct": bp[1]})
    out.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))
    return out[:8]


def load_board_pct(c):
    """board_code -> (board_name, pct_change) 最新一日."""
    return {r["board_code"]: (r["board_name"], r["pct_change"]) for r in c.execute(
        "SELECT board_code, board_name, pct_change FROM concept_board_daily "
        "WHERE trade_date=(SELECT MAX(trade_date) FROM concept_board_daily)")}


GROUP_SHORT = {}  # group_id -> 简称, 懒加载


def load_groups(zc):
    try:
        for r in zc.execute("SELECT group_id, name FROM groups"):
            GROUP_SHORT[r["group_id"]] = r["name"]
    except Exception:
        pass


def zsxq_for(zc, name, since, limit=5):
    """股名最近 since 之后的 zsxq 纪要 [{date, group, author, snippet}]."""
    if not name:
        return []
    rows = zc.execute(
        "SELECT topic_id, group_id, create_date, author_name, title, text FROM topics "
        "WHERE text LIKE ? AND create_date>=? ORDER BY create_time DESC LIMIT ?",
        (f"%{name}%", since, limit)).fetchall()
    out = []
    for r in rows:
        snip = snippet_around(r["text"], name) or clean(r["title"], 80)
        out.append({
            "topic_id": str(r["topic_id"]),
            "date": r["create_date"],
            "group": GROUP_SHORT.get(r["group_id"], str(r["group_id"]))[:12],
            "author": (r["author_name"] or "")[:16],
            "snippet": snip,
        })
    return out


def resolve_anchor(c, today, date_override=None):
    """锚定候选日期: 显式 --date 优先(盘后富集当天新池), 否则昨日池(盘中默认)."""
    return date_override or scout_db.anchor_date(c, today)


def enrich_one(c, zc, strategy, code, name, ts_code, since, board_pct, now):
    """单只候选 -> candidate_logic 行 (strategy, code, name, refresh_time, themes_json, zsxq_json, summary)."""
    themes = themes_for(c, ts_code, board_pct)
    zsxq = zsxq_for(zc, name, since) if name else []
    return (strategy, code, name, now,
            json.dumps(themes, ensure_ascii=False),
            json.dumps(zsxq, ensure_ascii=False), None)


def selfheal(c, zc, candidates, since, board_pct, now, stale_min=60):
    """给 candidates [(strategy, code, name, ts_code), ...] 补 candidate_logic.

    只重算 缺失 或 refresh_time 早于 now-stale_min 的, 返回待 upsert 行列表.
    refresh_time 用 'YYYY-MM-DD HH:MM:SS' 文本, 字典序即时间序, 可直接字符串比较.
    """
    existing = {(r["strategy"], r["code"]): r["refresh_time"]
                for r in c.execute("SELECT strategy, code, refresh_time FROM candidate_logic")}
    cutoff = (datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
              - timedelta(minutes=stale_min)).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for strategy, code, name, ts_code in candidates:
        rt = existing.get((strategy, code))
        if rt is not None and rt >= cutoff:
            continue
        rows.append(enrich_one(c, zc, strategy, code, name, ts_code, since, board_pct, now))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="zsxq 回看天数")
    ap.add_argument("--date", help="锚定候选日期 YYYY-MM-DD (默认昨日池; 盘后富集当天新池用)")
    a = ap.parse_args()
    t0 = time.time()
    scout_db.init_schema()
    c = scout_db.conn()
    anchor = resolve_anchor(c, datetime.now().strftime("%Y-%m-%d"), a.date)
    cands = candidate_list(c, anchor)
    board_pct = load_board_pct(c)
    print(f"[scout-logic] {datetime.now():%Y-%m-%d %H:%M:%S} 候选 {len(cands)} 只 (锚定 {anchor}), 板块 {len(board_pct)} 个")

    zc = sqlite3.connect(ZSXQ_DB, timeout=60)
    zc.row_factory = sqlite3.Row
    load_groups(zc)
    since = (datetime.now() - timedelta(days=a.days)).strftime("%Y-%m-%d")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows, n_theme, n_zsxq = [], 0, 0
    for s, code, name, ts_code in cands:
        row = enrich_one(c, zc, s, code, name, ts_code, since, board_pct, now)
        rows.append(row)
        if row[4] != "[]":
            n_theme += 1
        if row[5] != "[]":
            n_zsxq += 1
    zc.close()

    if rows:
        c.executemany(
            "INSERT OR REPLACE INTO candidate_logic VALUES (?,?,?,?,?,?,?)", rows)
        c.commit()
    c.close()
    print(f"  写入 {len(rows)} 条 | 有题材 {n_theme} / 有纪要 {n_zsxq}")
    print(f"[scout-logic] 完成 ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
