"""S3 批量历史回测."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_SCRIPTS = os.path.dirname(SCRIPT_DIR)
for path in (SCRIPT_DIR, ROOT_SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

from strategy_common import get_trading_days, next_trading_day
from verify import run_verify

BACKTEST_START_DATE = "2025-01-01"

_spec = importlib.util.spec_from_file_location("s3_select", os.path.join(SCRIPT_DIR, "select.py"))
_s3_select = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_s3_select)
run_select = _s3_select.run_select


def run_backtest(start: str, end: str, output_dir: str | None = None, rules_version: str = "v2", tradable_only: bool = True) -> dict:
    start = max(start, BACKTEST_START_DATE)
    records = []
    for t_date in get_trading_days(start, end):
        try:
            payload = run_select(t_date, rules_version=rules_version)
        except Exception as e:
            records.append({"t_date": t_date, "error": str(e)})
            continue
        t1_date = next_trading_day(t_date)
        gate = payload.get("regime_gate") or {}
        gate_allowed = gate.get("allowed")
        should_verify = bool(payload.get("candidates")) and (not tradable_only or gate_allowed is not False)
        if t1_date is None:
            results = []
            verify_skipped_reason = "T+1 行情尚未入库"
        elif not should_verify and gate_allowed is False and tradable_only:
            results = []
            verify_skipped_reason = "regime_gate_closed"
        else:
            results = run_verify(t1_date, mode="backtest") if should_verify else []
            verify_skipped_reason = None
        records.append({
            "t_date": t_date,
            "t1_date": t1_date,
            "regime": payload.get("regime_input", {}).get("regime_name"),
            "score": payload.get("regime_input", {}).get("score"),
            "candidates_count": len(payload.get("candidates", [])),
            "skipped_reason": payload.get("skipped_reason"),
            "verify_skipped_reason": verify_skipped_reason,
            "verify_results": results,
        })

    summary = summarize(records)
    result = {"start": start, "end": end, "strategy": "S3", "tradable_only": tradable_only, "summary": summary, "daily": records}
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "s3_backtest_summary.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def summarize(records: list) -> dict:
    pnls = []
    total_candidates = 0
    skipped = 0
    verify_skipped = 0
    status_counts = {}
    gap_buckets = {
        "<=-3%": [],
        "-3~-1%": [],
        "-1~1%": [],
        "1~3%": [],
        ">3%": [],
        "no_gap": [],
    }

    def bucket_name(gap: float | None) -> str:
        if gap is None:
            return "no_gap"
        if gap <= -3:
            return "<=-3%"
        if gap <= -1:
            return "-3~-1%"
        if gap <= 1:
            return "-1~1%"
        if gap <= 3:
            return "1~3%"
        return ">3%"

    for r in records:
        if r.get("skipped_reason"):
            skipped += 1
        if r.get("verify_skipped_reason"):
            verify_skipped += 1
        total_candidates += r.get("candidates_count", 0) or 0
        for item in r.get("verify_results") or []:
            v = item["verification"]
            status_counts[v["status"]] = status_counts.get(v["status"], 0) + 1
            if v.get("pnl_pct") is not None:
                pnls.append(v["pnl_pct"])
                gap = v.get("t1_open_gap_pct")
                gap_buckets[bucket_name(gap)].append(v["pnl_pct"])

    wins = sum(1 for p in pnls if p > 0)
    bucket_summary = {}
    for name, vals in gap_buckets.items():
        if vals:
            bucket_summary[name] = {
                "count": len(vals),
                "wins": sum(1 for p in vals if p > 0),
                "win_rate": sum(1 for p in vals if p > 0) / len(vals),
                "avg_pnl": sum(vals) / len(vals),
                "max_pnl": max(vals),
                "min_pnl": min(vals),
            }
        else:
            bucket_summary[name] = {"count": 0, "wins": 0, "win_rate": 0, "avg_pnl": 0, "max_pnl": 0, "min_pnl": 0}

    return {
        "days_total": len(records),
        "days_skipped": skipped,
        "days_verify_skipped": verify_skipped,
        "total_candidates": total_candidates,
        "total_triggered": len(pnls),
        "wins": wins,
        "win_rate": wins / len(pnls) if pnls else 0,
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
        "max_pnl": max(pnls) if pnls else 0,
        "min_pnl": min(pnls) if pnls else 0,
        "status_counts": status_counts,
        "t1_open_gap_buckets": bucket_summary,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--rules-version", default="v2")
    ap.add_argument("--include-gate-closed", action="store_true")
    args = ap.parse_args()
    result = run_backtest(args.start, args.end, args.output_dir, args.rules_version, tradable_only=not args.include_gate_closed)
    s = result["summary"]
    print(f"S3 回测 {args.start} ~ {args.end}")
    print(f"  days={s['days_total']} skipped={s['days_skipped']} candidates={s['total_candidates']}")
    print(f"  triggered={s['total_triggered']} win_rate={s['win_rate']*100:.0f}% avg_pnl={s['avg_pnl']:+.2f}%")


if __name__ == "__main__":
    main()
