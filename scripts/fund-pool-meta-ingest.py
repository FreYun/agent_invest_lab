#!/usr/bin/env python3
"""把 data/被动指数型基金池（权益黄金）.xlsx 里的主题 / 市值 / 风格 三列灌进 fund.db。

为什么写这个：
- fund-pool-ingest.py 从 research-mcp 拉 info/perf/nav，但显式不动 theme（"保留已有的 theme"）。
- bot101/102/103 是多基金 bot，做组合配置需要"主题分类"和"市值/风格因子"，但 fund_info.theme 与
  fund_style.size_style/invest_style 字段在 schema 上存在却全空。
- 池子 xlsx 已经标好这三列，直接灌进库，bot 通过 fund-portfolio-mcp.get_fund_detail 即可拿到。

写哪：
- 主题 → fund_info.theme（按 fund_code UPSERT）
- 市值 → fund_style.size_style（按 fund_code + as_of_date UPSERT）
- 风格 → fund_style.invest_style（同上）

as_of_date 沿用现有 fund_style 表里最新的快照日期，让 get_fund_detail 的
`ORDER BY as_of_date DESC LIMIT 1` 落到同一行——已有 equity_pct 数据的会保留，没有的新插入。
若库里 fund_style 还没有任何快照，则回落到今天作为初始日期。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd


DEFAULT_DB = os.getenv("FUND_DB_PATH", "data/fund.db")
DEFAULT_XLSX = "data/被动指数型基金池（权益黄金）.xlsx"


def _normalize_code(raw) -> str | None:
    """xlsx 里的基金代码是 int64，DB 里是 6 位 0-padded 字符串。"""
    if pd.isna(raw):
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return f"{n:06d}"


def _normalize_label(raw) -> str | None:
    """主题/市值/风格的字符串字段：去空白；NaN/空串 → None。"""
    if pd.isna(raw):
        return None
    s = str(raw).strip()
    return s or None


def _resolve_as_of_date(conn: sqlite3.Connection, override: str | None) -> str:
    """选 fund_style 的 as_of_date：
    - 显式 --as-of-date 优先；
    - 否则用 fund_style 已有的最新 as_of_date（让新数据落到同一行）；
    - 都没有就 fallback 今天。"""
    if override:
        return override
    row = conn.execute("SELECT MAX(as_of_date) AS d FROM fund_style").fetchone()
    if row and row[0]:
        return row[0]
    return date.today().isoformat()


def ingest(xlsx_path: Path, db_path: Path, as_of_date_override: str | None, dry_run: bool) -> dict:
    df = pd.read_excel(xlsx_path)

    required_cols = ["基金代码", "主题(近一年)", "市值(近一年)", "风格(近一年)"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise SystemExit(f"xlsx 缺少必需列: {missing}（实际列: {list(df.columns)}）")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        as_of_date = _resolve_as_of_date(conn, as_of_date_override)
        print(f"as_of_date = {as_of_date}  (override={'yes' if as_of_date_override else 'no'})")

        stats = {
            "rows_read": len(df),
            "valid_codes": 0,
            "skipped_no_code": 0,
            "skipped_not_in_fund_info": 0,
            "theme_updated": 0,
            "theme_unchanged": 0,
            "style_upserted": 0,
        }

        # 先把 DB 里已有 fund_code 拉出来，xlsx 里池外的基金（虽然 ingest 脚本应该已经覆盖）跳过避免脏写。
        existing_codes = {
            r["fund_code"]
            for r in conn.execute("SELECT fund_code FROM fund_info")
        }

        for _, row in df.iterrows():
            code = _normalize_code(row["基金代码"])
            if not code:
                stats["skipped_no_code"] += 1
                continue
            stats["valid_codes"] += 1
            if code not in existing_codes:
                # 池子里可能有 fund_info 没收录的基金（例如未跑过 fund-pool-ingest）；
                # 这里只补 meta、不创建 fund_info——避免 ingest 的边界混淆。
                stats["skipped_not_in_fund_info"] += 1
                continue

            theme = _normalize_label(row["主题(近一年)"])
            size_style = _normalize_label(row["市值(近一年)"])
            invest_style = _normalize_label(row["风格(近一年)"])

            # theme: UPDATE fund_info（只改 theme，其他字段保留）
            if theme is not None and not dry_run:
                cur = conn.execute(
                    "UPDATE fund_info SET theme = ?, updated_at = datetime('now') "
                    "WHERE fund_code = ? AND (theme IS NULL OR theme <> ?)",
                    (theme, code, theme),
                )
                if cur.rowcount > 0:
                    stats["theme_updated"] += 1
                else:
                    stats["theme_unchanged"] += 1
            elif theme is not None:
                stats["theme_updated"] += 1  # dry-run 也计数

            # size_style / invest_style: UPSERT fund_style
            # ON CONFLICT(fund_code, as_of_date) DO UPDATE 只改这两列，保留 equity_pct/bond_pct/...
            if (size_style is not None or invest_style is not None) and not dry_run:
                conn.execute(
                    """
                    INSERT INTO fund_style (fund_code, as_of_date, size_style, invest_style, updated_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(fund_code, as_of_date) DO UPDATE SET
                        size_style = COALESCE(excluded.size_style, fund_style.size_style),
                        invest_style = COALESCE(excluded.invest_style, fund_style.invest_style),
                        updated_at = datetime('now')
                    """,
                    (code, as_of_date, size_style, invest_style),
                )
                stats["style_upserted"] += 1
            elif size_style is not None or invest_style is not None:
                stats["style_upserted"] += 1  # dry-run 也计数

        if not dry_run:
            conn.commit()
        return stats
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="把 xlsx 池子的主题/市值/风格灌进 fund.db")
    p.add_argument("--xlsx", default=DEFAULT_XLSX, help=f"xlsx 路径（默认 {DEFAULT_XLSX}）")
    p.add_argument("--db", default=DEFAULT_DB, help=f"fund.db 路径（默认 {DEFAULT_DB}）")
    p.add_argument(
        "--as-of-date",
        default=None,
        help="fund_style 写入用的 as_of_date（默认沿用库中最新快照日，没有则用今天）",
    )
    p.add_argument("--dry-run", action="store_true", help="只统计，不真的写库")
    args = p.parse_args()

    xlsx_path = Path(args.xlsx)
    db_path = Path(args.db)
    if not xlsx_path.exists():
        raise SystemExit(f"xlsx 不存在: {xlsx_path}")
    if not db_path.exists():
        raise SystemExit(f"db 不存在: {db_path}")

    stats = ingest(xlsx_path, db_path, args.as_of_date, args.dry_run)
    print()
    print("=== ingest 结果 ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if args.dry_run:
        print("(dry-run, 没有写入)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
