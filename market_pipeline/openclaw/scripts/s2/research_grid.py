"""S2 research grid runner.

Runs a bounded set of parameter combinations without writing DB.
Output is for research only; production select.py defaults stay unchanged.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from typing import Any

from backtest import run_backtest


REGIME_SETS = {
    "bull": "STRONG_BULL",
    "bull_range": "STRONG_BULL,STRONG_RANGE",
    "bull_range_neutral": "STRONG_BULL,STRONG_RANGE,NEUTRAL_RANGE",
}


def row_from_result(name: str, config: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    s = result["summary"]
    status = s.get("status_counts", {})
    gap = s.get("t1_open_gap_buckets", {})
    return {
        "name": name,
        "regimes": config.get("allow_regimes"),
        "min_streak": config.get("min_streak"),
        "max_streak": config.get("max_streak"),
        "reject_one_word_board": config.get("reject_one_word_board"),
        "max_candidates": config.get("max_candidates"),
        "days_skipped": s.get("days_skipped"),
        "total_candidates": s.get("total_candidates"),
        "total_triggered": s.get("total_triggered"),
        "win_rate": s.get("win_rate"),
        "avg_pnl": s.get("avg_pnl"),
        "max_pnl": s.get("max_pnl"),
        "min_pnl": s.get("min_pnl"),
        "gap_up_skip": status.get("gap_up_skip", 0),
        "weak_open_skip": status.get("weak_open_skip", 0),
        "stop_hit": status.get("stop_hit", 0),
        "no_limit_exit": status.get("no_limit_exit", 0),
        "limit_follow_close": status.get("limit_follow_close", 0),
        "gap_-3_-1_count": gap.get("-3~-1%", {}).get("count", 0),
        "gap_-3_-1_avg": gap.get("-3~-1%", {}).get("avg_pnl", 0),
        "gap_-1_1_count": gap.get("-1~1%", {}).get("count", 0),
        "gap_-1_1_avg": gap.get("-1~1%", {}).get("avg_pnl", 0),
        "gap_1_3_count": gap.get("1~3%", {}).get("count", 0),
        "gap_1_3_avg": gap.get("1~3%", {}).get("avg_pnl", 0),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--end", default="2026-05-05")
    ap.add_argument("--output-dir", default="/tmp/s2-grid")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []

    for regime_name, regimes in REGIME_SETS.items():
        for min_streak in (2, 3, 4, 5):
            for reject_one_word in (True, False):
                cfg = {
                    "ignore_playbook": True,
                    "allow_regimes": regimes,
                    "min_streak": min_streak,
                    "max_streak": 8,
                    "reject_one_word_board": reject_one_word,
                    "max_candidates": 5,
                }
                name = f"{regime_name}_s{min_streak}_{'no1w' if reject_one_word else 'allow1w'}"
                print(f"RUN {name}")
                result = run_backtest(args.start, args.end, config=cfg)
                row = row_from_result(name, cfg, result)
                rows.append(row)
                details.append({"name": name, "config": cfg, "summary": result["summary"]})
                print(
                    f"  cand={row['total_candidates']} trig={row['total_triggered']} "
                    f"win={row['win_rate']:.2%} avg={row['avg_pnl']:+.2f}%"
                )

    csv_path = os.path.join(args.output_dir, "summary.csv")
    json_path = os.path.join(args.output_dir, "summary.json")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(details, f, ensure_ascii=False, indent=2)

    ranked = sorted(rows, key=lambda r: (r["avg_pnl"], r["total_triggered"]), reverse=True)
    print("\nTOP by avg_pnl")
    for row in ranked[:10]:
        print(
            f"{row['name']}: cand={row['total_candidates']} trig={row['total_triggered']} "
            f"win={row['win_rate']:.2%} avg={row['avg_pnl']:+.2f}% min={row['min_pnl']:+.2f}%"
        )
    print(f"\nWrote {csv_path}")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
