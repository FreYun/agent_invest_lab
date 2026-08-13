#!/usr/bin/env python3
"""生成 A 类 → C 类份额映射表 data/share-class-map.json。

world 的 buildBuyableCodesByBot 读这份表，把每个 bot 可买池里的 A 类代码替换成同基金的
C 类。买入闸门在 fund-portfolio-mcp 侧按池子拒单，卖出不过池子 —— 所以「A 出池」
恰好等于「新建仓强制 C、存量 A 仓位不强制退」。

为什么强制 C：A 类 = 申购费 ~0.12% + 长赎回费尾巴（池内 383 只 A 中 178 只免赎档 ≥180 天，
40 只根本没有零费档）；C 类 = 申购费 0% + 7/30 天后零赎回费 + 销服费 0.2~0.4%/年。
盈亏平衡点约 2.07 年，远长于本实验的持有周期。

配对口径沿用 scripts/assemble_otc_c_universe.py 的「名字去 A 加 C」：fund_info.share_class
在池内 1514 只上全是空的，份额类别只能从基金名末尾大写字母推断。

体检（任一不过 → 不入表，A 原样留在池子里）：
  · 净值覆盖：fund_nav 在窗口内起点 <= 窗口起始日，且行数 >= --min-nav-rows
  · 不能更差：C 的免赎档天数 <= A 的免赎档天数（无零费档记 +∞）
  · purchase_status / redeem_status 均为 open

输出里的 names / skipped 段是给人复核用的，world 运行时只读 map 段。

用法：
  /usr/bin/python3.12 scripts/gen-share-class-map.py
  /usr/bin/python3.12 scripts/gen-share-class-map.py --from 2025-01-02 --to 2026-07-29
  /usr/bin/python3.12 scripts/gen-share-class-map.py --dry-run   # 只打汇总，不写文件
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "fund.db"
OUT = REPO / "data" / "share-class-map.json"

# 默认窗口覆盖现有全部回测配置（world/config/*.yaml 的 replay 区间并集）。
DEFAULT_FROM = "2025-01-02"
DEFAULT_TO = "2026-07-29"

NO_FREE_TIER = 10 ** 9  # 「没有零费档」的免赎档天数哨兵，用于比较大小

_SUFFIX = re.compile(r"([A-Z])$")


def share_class(name: str) -> str:
    """份额类别 = 基金名末尾的单个大写字母；没有则空串（如「华夏成长混合」）。"""
    m = _SUFFIX.search(name.strip())
    return m.group(1) if m else ""


def base_name(name: str) -> str:
    """去掉末尾份额字母后的基名，同一只基金的 A/C 在此相等。"""
    return _SUFFIX.sub("", name.strip())


def free_tier_days(redeem_fee_json: str | None) -> int:
    """免赎档天数：赎回阶梯里 rate>0 的最大 max_days。

    注意字段名是 rate（小数），不是 rate_pct —— fund_info.redeem_fee_json 的实际格式是
    [{"max_days":7,"rate":0.015},{"max_days":365,"rate":0.005},{"max_days":null,"rate":0.0}]。
    若存在 max_days 为 null 且 rate>0 的档，说明持有到永远都要收费 → 无零费档。
    """
    try:
        tiers = json.loads(redeem_fee_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return NO_FREE_TIER
    if any((t.get("rate") or 0) > 0 and t.get("max_days") is None for t in tiers):
        return NO_FREE_TIER
    positive = [t["max_days"] for t in tiers if (t.get("rate") or 0) > 0 and t.get("max_days") is not None]
    return max(positive) if positive else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="win_from", default=DEFAULT_FROM)
    ap.add_argument("--to", dest="win_to", default=DEFAULT_TO)
    ap.add_argument("--min-nav-rows", type=int, default=370)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DB.exists():
        print(f"fund.db not found: {DB}", file=sys.stderr)
        return 1

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    info = {
        r[0]: {"name": r[1] or "", "purchase_status": r[2], "redeem_status": r[3], "redeem_fee_json": r[4]}
        for r in con.execute(
            "SELECT fund_code, fund_name, purchase_status, redeem_status, redeem_fee_json FROM fund_info"
        )
    }

    # 净值覆盖一次查完：按代码聚合窗口内的起点与行数，避免 1600 次单点查询。
    nav = {
        r[0]: (r[1], r[2])
        for r in con.execute(
            "SELECT fund_code, MIN(nav_date), COUNT(*) FROM fund_nav "
            "WHERE nav_date BETWEEN ? AND ? GROUP BY fund_code",
            (args.win_from, args.win_to),
        )
    }

    by_base: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for code, rec in info.items():
        by_base[base_name(rec["name"])][share_class(rec["name"])] = code

    mapping: dict[str, str] = {}
    names: dict[str, str] = {}
    skipped: list[dict[str, str]] = []
    reasons: collections.Counter = collections.Counter()

    for code, rec in sorted(info.items()):
        if share_class(rec["name"]) != "A":
            continue
        sibling = by_base[base_name(rec["name"])].get("C")
        if not sibling:
            reason = "no_c_sibling"
        else:
            c = info[sibling]
            nav_from, nav_rows = nav.get(sibling, (None, 0))
            if nav_from is None or nav_from > args.win_from or nav_rows < args.min_nav_rows:
                reason = "nav_coverage"
            elif (c["purchase_status"] or "open") != "open" or (c["redeem_status"] or "open") != "open":
                reason = "status_closed"
            elif free_tier_days(c["redeem_fee_json"]) > free_tier_days(rec["redeem_fee_json"]):
                reason = "c_worse_than_a"
            else:
                reason = ""
        if reason:
            reasons[reason] += 1
            entry = {"code": code, "name": rec["name"], "reason": reason}
            if sibling:
                entry["c"] = sibling
                entry["c_name"] = info[sibling]["name"]
            skipped.append(entry)
            continue
        mapping[code] = sibling
        names[code] = f'{rec["name"]} -> {sibling} {info[sibling]["name"]}'

    payload = {
        "generated_at": dt.date.today().isoformat(),
        "nav_window": [args.win_from, args.win_to],
        "min_nav_rows": args.min_nav_rows,
        "map": mapping,
        "names": names,
        "skipped": skipped,
    }

    print(f"A 类总数 {len(mapping) + len(skipped)} → 映射 {len(mapping)} 条，保留 A {len(skipped)} 条")
    for reason, n in reasons.most_common():
        print(f"  保留原因 {reason}: {n}")

    if args.dry_run:
        print("(--dry-run，未写文件)")
        return 0

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
