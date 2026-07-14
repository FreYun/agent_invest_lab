#!/usr/bin/env python3
"""Serial S1 breakout x volume grid.

Research-only. Tests whether larger breakout thresholds need larger volume
confirmation and whether amount gates add value beyond volume ratio.
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import s1_breakout_research as s1


OUT_DIR = Path(__file__).resolve().parent

BASE = s1.Config(
    base_window=20,
    max_base_range_pct=14.0,
    min_breakout_pct=2.0,
    min_volume_ratio=1.25,
    min_amount_yi=1.0,
    entry_low_pct=-2.0,
    entry_high_pct=0.0,
    max_hold_days=2,
    min_ret20_pct=10.0,
    min_rel20_pct=5.0,
    regimes_csv="STRONG_RANGE",
)

WINDOWS = [
    ("2019", "2019-01-01", "2019-12-31"),
    ("2020", "2020-01-01", "2020-12-31"),
    ("2024q4_2025q1", "2024-09-01", "2025-03-31"),
    ("2025_2026ytd", "2025-04-01", "2026-05-13"),
]

BREAKOUTS = [1.0, 2.0, 3.0, 4.0, 5.0]
VOLUME_RATIOS = [1.0, 1.25, 1.5, 2.0, 2.5]
AMOUNT_GATES = [0.0, 1.0, 3.0, 5.0]


def quantile(values: list[float], q: float) -> float:
    return s1.quantile(values, q)


def summarize(name: str, window: str, summary: dict) -> dict:
    cfg = summary["config"]
    return {
        "config_name": name,
        "window": window,
        "start": summary["start"],
        "end": summary["end"],
        "eligible_days": summary["eligible_days"],
        "signal_days": summary["signal_days"],
        "raw_signals": summary["raw_signals"],
        "candidates": summary["candidates"],
        "triggered": summary["triggered"],
        "win_rate": summary["win_rate"],
        "avg_pnl": summary["avg_pnl"],
        "median_pnl": summary["median_pnl"],
        "p25_pnl": summary["p25_pnl"],
        "p75_pnl": summary["p75_pnl"],
        "max_pnl": summary["max_pnl"],
        "min_pnl": summary["min_pnl"],
        "min_breakout_pct": cfg["min_breakout_pct"],
        "min_volume_ratio": cfg["min_volume_ratio"],
        "min_amount_yi": cfg["min_amount_yi"],
        "status_counts_json": json.dumps(summary["status_counts"], ensure_ascii=False, sort_keys=True),
    }


def run_grid() -> list[dict]:
    rows = []
    total = len(BREAKOUTS) * len(VOLUME_RATIOS) * len(AMOUNT_GATES)
    for window_name, start, end in WINDOWS:
        data = s1.load_research_data(start, end)
        counter = 0
        for breakout in BREAKOUTS:
            for volume_ratio in VOLUME_RATIOS:
                for amount_yi in AMOUNT_GATES:
                    counter += 1
                    name = f"bo{breakout:g}_vr{volume_ratio:g}_amt{amount_yi:g}"
                    cfg = replace(
                        BASE,
                        min_breakout_pct=breakout,
                        min_volume_ratio=volume_ratio,
                        min_amount_yi=amount_yi,
                    )
                    summary = s1.run_cached(data, cfg)["summary"]
                    row = summarize(name, window_name, summary)
                    rows.append(row)
                    print(
                        f"[{window_name} {counter}/{total}] {name}: "
                        f"trig={row['triggered']} win={row['win_rate']:.1%} "
                        f"avg={row['avg_pnl']:+.2f}% med={row['median_pnl']:+.2f}% "
                        f"p25={row['p25_pnl']:+.2f}%",
                        flush=True,
                    )
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def aggregate(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["config_name"], []).append(row)
    out = []
    for name, group in grouped.items():
        valid = [row for row in group if row["triggered"] >= 20]
        total_triggered = sum(row["triggered"] for row in group)
        weighted_avg = (
            sum(row["avg_pnl"] * row["triggered"] for row in group) / total_triggered
            if total_triggered else 0.0
        )
        medians = [row["median_pnl"] for row in group]
        p25s = [row["p25_pnl"] for row in group]
        out.append({
            "config_name": name,
            "windows": len(group),
            "windows_triggered_ge20": len(valid),
            "total_triggered": total_triggered,
            "weighted_avg_pnl": weighted_avg,
            "avg_median_pnl": sum(medians) / len(medians),
            "min_median_pnl": min(medians),
            "avg_p25_pnl": sum(p25s) / len(p25s),
            "min_p25_pnl": min(p25s),
            "min_breakout_pct": group[0]["min_breakout_pct"],
            "min_volume_ratio": group[0]["min_volume_ratio"],
            "min_amount_yi": group[0]["min_amount_yi"],
        })
    return sorted(
        out,
        key=lambda row: (
            row["windows_triggered_ge20"],
            row["avg_median_pnl"],
            row["weighted_avg_pnl"],
            row["total_triggered"],
        ),
        reverse=True,
    )


def main() -> None:
    rows = run_grid()
    write_csv(OUT_DIR / "s1_breakout_volume_grid_20260513.csv", rows)
    agg = aggregate(rows)
    write_csv(OUT_DIR / "s1_breakout_volume_grid_agg_20260513.csv", agg)

    print("\nTOP_AGG")
    for row in agg[:20]:
        print(
            f"{row['config_name']}: windows>=20={row['windows_triggered_ge20']} "
            f"trig={row['total_triggered']} wavg={row['weighted_avg_pnl']:+.2f}% "
            f"avg_med={row['avg_median_pnl']:+.2f}% min_med={row['min_median_pnl']:+.2f}% "
            f"avg_p25={row['avg_p25_pnl']:+.2f}%"
        )


if __name__ == "__main__":
    main()
