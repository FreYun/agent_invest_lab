"""S7 batch historical runner."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_SCRIPTS = os.path.dirname(SCRIPT_DIR)
for path in (SCRIPT_DIR, ROOT_SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

_spec = importlib.util.spec_from_file_location("s7_select", os.path.join(SCRIPT_DIR, "select.py"))
_s7_select = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s7_select)
run_select = _s7_select.run_select

from strategy_common import get_trading_days, next_trading_day
from verify import run_verify

BACKTEST_START_DATE = "2025-01-01"


def run_backtest(start: str, end: str, rules_version: str = "v2", persist: bool = True, tradable_only: bool = True) -> dict:
    start = max(start, BACKTEST_START_DATE)
    records = []
    for t_date in get_trading_days(start, end):
        t1 = next_trading_day(t_date)
        if not t1:
            continue
        payload = run_select(t_date, rules_version=rules_version, persist=persist)
        gate = payload.get("regime_gate") or {}
        gate_allowed = gate.get("allowed")
        should_verify = bool(payload.get("candidates")) and (not tradable_only or gate_allowed is not False)
        if not should_verify and gate_allowed is False and tradable_only:
            results = []
            verify_skipped_reason = "regime_gate_closed"
        else:
            results = run_verify(t1, mode="backtest") if should_verify else []
            verify_skipped_reason = None
        records.append({"date": t_date, "t1_date": t1, "candidates": len(payload.get("candidates") or []), "verify_skipped_reason": verify_skipped_reason, "results": results})
    return _summarize(records)


def _summarize(records: list[dict]) -> dict:
    pnls = []
    status_counts = {}
    for record in records:
        for item in record.get("results") or []:
            v = item.get("verification") or {}
            status = v.get("status") or "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
            if v.get("pnl_pct") is not None:
                pnls.append(float(v["pnl_pct"]))
    wins = sum(1 for p in pnls if p > 0)
    summary = {
        "days": len(records),
        "candidate_days": sum(1 for r in records if r.get("candidates")),
        "candidates": sum(r.get("candidates") or 0 for r in records),
        "trades": len(pnls),
        "wins": wins,
        "win_rate": wins / len(pnls) if pnls else 0,
        "mean_pnl": sum(pnls) / len(pnls) if pnls else 0,
        "sum_pnl": sum(pnls),
        "status_counts": status_counts,
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="end date YYYY-MM-DD")
    parser.add_argument("--rules-version", default="v2")
    parser.add_argument("--include-gate-closed", action="store_true")
    args = parser.parse_args()
    summary = run_backtest(args.start, args.end, rules_version=args.rules_version, tradable_only=not args.include_gate_closed)
    print(
        "S7 backtest {}~{}: days={} candidates={} trades={} mean={:+.2f}% win={:.1f}% sum={:+.2f}%".format(
            args.start,
            args.end,
            summary["days"],
            summary["candidates"],
            summary["trades"],
            summary["mean_pnl"],
            summary["win_rate"] * 100,
            summary["sum_pnl"],
        )
    )
    print(summary["status_counts"])




if __name__ == "__main__":
    main()
