"""S5 DB → MD 渲染脚本 — 从 s5.db 读出来 → 调 output_writer.render_md → 输出。

用法:
    python3 render.py candidates --date=2026-04-08
    python3 render.py verification --date=2026-04-09
    python3 render.py candidates --date=2026-04-08 --output=/tmp/out.md
    python3 render.py list                                # 列出 DB 里所有日期

打印到 stdout (默认), 或写到 --output 指定的文件。
"""

from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from db_writer import read_select_run, read_verification
from output_writer import render_md, render_verification_md

# 拉 s5/_lib/db
_LIB_DIR = "/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5/_lib"
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import db  # noqa: E402


def cmd_candidates(args):
    payload = read_select_run(args.date)
    if payload is None:
        print(f"❌ DB 中没有 {args.date} 的 select_run 记录", file=sys.stderr)
        sys.exit(1)
    md = render_md(payload)
    _output(md, args.output)


def cmd_verification(args):
    payload = read_verification(args.date)
    if payload is None:
        print(f"❌ DB 中没有 {args.date} 的 verification 记录", file=sys.stderr)
        sys.exit(1)
    md = render_verification_md(
        payload["t1_date"],
        payload["t_date"],
        payload["results"],
        mode=payload.get("mode", "live"),
    )
    _output(md, args.output)


def cmd_list(args):
    db.init_market_db()
    conn = db.get_market_db()
    try:
        print("=== select_runs ===")
        for r in conn.execute(
            "SELECT date, regime_code, regime_score, passed_count, skipped_reason FROM s5_select_runs ORDER BY date"
        ):
            note = (
                f"skipped: {r['skipped_reason']}"
                if r["skipped_reason"]
                else f"{r['passed_count']} 只候选"
            )
            print(f"  {r['date']} {r['regime_code']} score={r['regime_score']} → {note}")

        print("\n=== verifications ===")
        for r in conn.execute(
            "SELECT t1_date, t_date, mode, COUNT(*) AS n, "
            "SUM(CASE WHEN pnl_pct IS NOT NULL THEN 1 ELSE 0 END) AS triggered, "
            "AVG(pnl_pct) AS avg_pnl FROM s5_verifications GROUP BY t1_date ORDER BY t1_date"
        ):
            avg = f"{r['avg_pnl']:+.2f}%" if r["avg_pnl"] is not None else "-"
            print(
                f"  {r['t1_date']} (T={r['t_date']}, mode={r['mode']}): {r['n']} 条, "
                f"{r['triggered']} 触发, 平均 {avg}"
            )
    finally:
        conn.close()


def _output(md: str, output_path: str = None):
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"写入: {output_path}", file=sys.stderr)
    else:
        sys.stdout.write(md)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cand = sub.add_parser("candidates", help="渲染某日的 candidates MD")
    p_cand.add_argument("--date", required=True)
    p_cand.add_argument("--output", default=None, help="写到文件 (默认 stdout)")
    p_cand.set_defaults(func=cmd_candidates)

    p_ver = sub.add_parser("verification", help="渲染某日的 verification MD")
    p_ver.add_argument("--date", required=True, help="T+1 日期")
    p_ver.add_argument("--output", default=None)
    p_ver.set_defaults(func=cmd_verification)

    p_list = sub.add_parser("list", help="列出 DB 中所有 select_run + verification")
    p_list.set_defaults(func=cmd_list)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
