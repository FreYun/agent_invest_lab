"""S3 DB -> Markdown renderer."""

from __future__ import annotations

import argparse
import sys

from db_writer import read_select_run, read_verification
from output_writer import render_md, render_verification_md
from strategy_common import get_market_db, init_market_db


def _output(text: str, output: str | None) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("candidates")
    p.add_argument("--date", required=True)
    p.add_argument("--output")
    p = sub.add_parser("verification")
    p.add_argument("--date", required=True)
    p.add_argument("--output")
    sub.add_parser("list")
    args = ap.parse_args()

    if args.cmd == "candidates":
        payload = read_select_run(args.date)
        if payload is None:
            raise SystemExit(f"DB 中没有 {args.date} 的 S3 select_run")
        _output(render_md(payload), args.output)
    elif args.cmd == "verification":
        payload = read_verification(args.date)
        if payload is None:
            raise SystemExit(f"DB 中没有 {args.date} 的 S3 verification")
        _output(render_verification_md(payload["t1_date"], payload["t_date"], payload["results"], payload["mode"]), args.output)
    else:
        init_market_db()
        conn = get_market_db()
        try:
            print("=== s3_select_runs ===")
            for r in conn.execute("SELECT date, regime_code, passed_count, skipped_reason FROM s3_select_runs ORDER BY date"):
                note = f"skipped: {r['skipped_reason']}" if r["skipped_reason"] else f"{r['passed_count']} 只候选"
                print(f"  {r['date']} {r['regime_code']} -> {note}")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
