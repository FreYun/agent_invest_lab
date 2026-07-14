#!/usr/bin/env python3
"""S1 base-window/platform-width grid.

Research-only. Uses the latest S1 candidate and T+1 open-only entry, varying
only the platform lookback window and max platform width.
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import s1_open_entry_grid_20260513 as oe


OUT_DIR = Path(__file__).resolve().parent

WINDOWS = oe.WINDOWS
BASE = oe.BASE

BASE_WINDOWS = [10, 15, 20, 25, 30, 40, 60]
PLATFORM_WIDTHS = [8.0, 10.0, 12.0, 14.0, 16.0, 18.0]
ENTRY_BANDS = [
    ("open_m3_p2", -3.0, 2.0),
    ("open_m3_p1", -3.0, 1.0),
]


def compact(config_name: str, window_name: str, summary: dict) -> dict:
    cfg = summary["config"]
    return {
        "config_name": config_name,
        "window": window_name,
        "start": summary["start"],
        "end": summary["end"],
        "base_window": cfg["base_window"],
        "max_base_range_pct": cfg["max_base_range_pct"],
        "entry_band": summary["entry_band"],
        "entry_low_pct": summary["open_entry_low_pct"],
        "entry_high_pct": summary["open_entry_high_pct"],
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
        "status_counts_json": json.dumps(summary["status_counts"], ensure_ascii=False, sort_keys=True),
    }


def run_grid() -> list[dict]:
    rows = []
    total = len(BASE_WINDOWS) * len(PLATFORM_WIDTHS) * len(ENTRY_BANDS)
    for window_name, start, end in WINDOWS:
        data = oe.s1.load_research_data(start, end)
        counter = 0
        for base_window in BASE_WINDOWS:
            for width in PLATFORM_WIDTHS:
                cfg = replace(BASE, base_window=base_window, max_base_range_pct=width)
                for band_name, low_pct, high_pct in ENTRY_BANDS:
                    counter += 1
                    result = oe.run_open_cached(data, cfg, low_pct, high_pct)
                    summary = result["summary"]
                    summary["config"] = {
                        "base_window": base_window,
                        "max_base_range_pct": width,
                    }
                    summary["entry_band"] = band_name
                    name = f"base{base_window}_width{width:g}_{band_name}"
                    row = compact(name, window_name, summary)
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
            "windows_triggered_ge20": sum(1 for row in group if row["triggered"] >= 20),
            "total_triggered": total_triggered,
            "weighted_avg_pnl": weighted_avg,
            "avg_median_pnl": sum(medians) / len(medians),
            "min_median_pnl": min(medians),
            "avg_p25_pnl": sum(p25s) / len(p25s),
            "min_p25_pnl": min(p25s),
            "base_window": group[0]["base_window"],
            "max_base_range_pct": group[0]["max_base_range_pct"],
            "entry_band": group[0]["entry_band"],
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
    write_csv(OUT_DIR / "s1_base_window_grid_20260514.csv", rows)
    agg = aggregate(rows)
    write_csv(OUT_DIR / "s1_base_window_grid_agg_20260514.csv", agg)

    print("\nTOP_AGG")
    for row in agg[:25]:
        print(
            f"{row['config_name']}: win>=20={row['windows_triggered_ge20']} "
            f"trig={row['total_triggered']} wavg={row['weighted_avg_pnl']:+.2f}% "
            f"avg_med={row['avg_median_pnl']:+.2f}% min_med={row['min_median_pnl']:+.2f}% "
            f"avg_p25={row['avg_p25_pnl']:+.2f}%"
        )


if __name__ == "__main__":
    main()
