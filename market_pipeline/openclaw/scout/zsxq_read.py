"""zsxq 纪要全文读取器 — 给 bot11 盘中点评深读用, 输出 JSON.

zsxq 原文整条已落本地 zsxq.db (topics.text 纯文本), 深读不必走 zsxq-mcp
(有 20 次/分限频 + 1059 重签), 直接查本地库:
  topics WHERE text LIKE '%关键词%' AND create_date>=since ORDER BY create_time DESC

复用 logic.py 的剥标签逻辑保持一致; 只读模式打开, 不会写库.

## 关键词怎么选(命中率经验)
- **个股深读**: 直接用股名 (博杰股份=25 命中/45天). 单 --name 即可.
- **板块深读**: **别用板块标准名** —— 行情软件概念名 (中芯概念/电子化学品Ⅱ/被动元件
  概念/CPO概念) zsxq 纪要不会这么写, LIKE 命中 0~1; "半导体" 这种泛词又命中上千全是噪音.
  改用该板块的 **龙头名 + 成分股名** (scope 的 lead_name + lead_stocks[].name) 多词 OR 搜,
  命中又准又多 (中芯国际=101 拓荆科技=81 盛合晶微=83).

## 防泛词淹没
多词时每个词**独立配额** --per-name (默认 2): 每个关键词各取最新 N 条再合并去重, 这样
某个大词不会占满整个返回, 每只票都有代表性纪要.

用法:
  # 个股: 单词
  python3 zsxq_read.py --name 博杰股份 --days 45
  # 板块: 多词(龙头+成分股), 每词最多 2 条, 合并取 6 条
  python3 zsxq_read.py --names 中芯国际,拓荆科技,精测电子,雅克科技 --per-name 2 --limit 6
  # --max-chars 控单条全文上限防爆 token(默认 4000)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logic  # noqa: E402  复用 _TAG / _WS / clean

ZSXQ_DB = "/home/rooot/database/zsxq.db"


def collect_keywords(names_repeated, names_csv):
    """合并 --name(可重复) 与 --names(逗号分隔), 去空白/去重保序."""
    kws, seen = [], set()
    for src in list(names_repeated or []) + (names_csv.split(",") if names_csv else []):
        k = src.strip()
        if k and k not in seen:
            seen.add(k)
            kws.append(k)
    return kws


def search(zc, keywords, since, per_name, limit, max_chars):
    """每个关键词各取最新 per_name 条, 合并按 topic_id 去重(累加命中词),
    按 create_time DESC 排序取 limit 条, 全文剥标签截到 max_chars."""
    by_id = {}
    for kw in keywords:
        rows = zc.execute(
            "SELECT t.topic_id, t.group_id, t.create_time, t.create_date, "
            "       t.author_name, t.title, t.text, g.name AS group_name "
            "FROM topics t LEFT JOIN groups g ON g.group_id=t.group_id "
            "WHERE t.text LIKE ? AND t.create_date>=? "
            "ORDER BY t.create_time DESC LIMIT ?",
            (f"%{kw}%", since, per_name)).fetchall()
        for r in rows:
            tid = str(r["topic_id"])
            if tid in by_id:
                by_id[tid]["matched"].append(kw)
                continue
            by_id[tid] = {
                "topic_id": tid,
                "_ctime": r["create_time"],
                "date": r["create_date"],
                "group": (r["group_name"] or str(r["group_id"]))[:16],
                "author": (r["author_name"] or "")[:16],
                "title": logic.clean(r["title"], 120),
                "fulltext": logic.clean(r["text"], n=max_chars),
                "matched": [kw],
            }
    items = sorted(by_id.values(), key=lambda x: x["_ctime"], reverse=True)[:limit]
    for it in items:
        del it["_ctime"]
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", action="append", help="关键词(股名/成分股名), 可重复传多个")
    ap.add_argument("--names", help="关键词逗号分隔(板块深读: 龙头名,成分股名,...)")
    ap.add_argument("--days", type=int, default=45, help="回看天数 (默认 45)")
    ap.add_argument("--per-name", type=int, default=2, help="每个关键词最多取几条 (默认 2, 防泛词淹没)")
    ap.add_argument("--limit", type=int, default=6, help="合并去重后最终上限 (默认 6)")
    ap.add_argument("--max-chars", type=int, default=4000, help="单条全文字符上限 (默认 4000, 防爆 token)")
    a = ap.parse_args()

    keywords = collect_keywords(a.name, a.names)
    if not keywords:
        ap.error("至少给一个关键词: --name 股名  或  --names 词1,词2,...")

    since = (datetime.now() - timedelta(days=a.days)).strftime("%Y-%m-%d")
    zc = sqlite3.connect(f"file:{ZSXQ_DB}?mode=ro", uri=True, timeout=60)
    zc.row_factory = sqlite3.Row
    try:
        items = search(zc, keywords, since, a.per_name, a.limit, a.max_chars)
    finally:
        zc.close()

    print(json.dumps(
        {"keywords": keywords, "since": since, "count": len(items), "items": items},
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
