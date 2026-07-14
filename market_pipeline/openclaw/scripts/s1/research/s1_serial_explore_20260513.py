#!/usr/bin/env python3
"""Serial S1 breakout exploration for 2026-05-13.

Research-only driver. It imports the existing harness, keeps execution strictly
single-process, and runs a small hand-picked parameter set.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, replace
from pathlib import Path

import s1_breakout_research as s1


OUT_DIR = Path(__file__).resolve().parent
RECENT_START = "2024-09-01"
RECENT_END = "2025-03-31"


BASE = s1.Config(
    base_window=20,
    max_base_range_pct=14.0,
    min_breakout_pct=2.0,
    min_volume_ratio=1.25,
    entry_low_pct=-2.0,
    entry_high_pct=0.0,
    max_hold_days=2,
    min_ret20_pct=10.0,
    min_rel20_pct=5.0,
    regimes_csv="STRONG_RANGE",
)


RECENT_CONFIGS = [
    ("base_sr_hold2_e-2_0", BASE),
    ("entry_deeper_e-3_0", replace(BASE, entry_low_pct=-3.0)),
    ("entry_tighter_e-1_0", replace(BASE, entry_low_pct=-1.0)),
    ("allow_small_chase_e-2_1", replace(BASE, entry_high_pct=1.0)),
    ("strict_base_range10", replace(BASE, max_base_range_pct=10.0)),
    ("strict_breakout3", replace(BASE, min_breakout_pct=3.0)),
    ("stronger_ret15_rel8", replace(BASE, min_ret20_pct=15.0, min_rel20_pct=8.0)),
    ("stop5", replace(BASE, stop_pct=-5.0)),
    ("take_profit3", replace(BASE, take_profit_pct=3.0)),
    ("take_profit5", replace(BASE, take_profit_pct=5.0)),
    ("hold3", replace(BASE, max_hold_days=3)),
    ("hold3_tp5", replace(BASE, max_hold_days=3, take_profit_pct=5.0)),
    ("fail_buffer1", replace(BASE, failure_buffer_pct=1.0)),
]


WINDOWS = [
    ("2015", "2015-01-01", "2015-12-31"),
    ("2019", "2019-01-01", "2019-12-31"),
    ("2020", "2020-01-01", "2020-12-31"),
    ("2024q4_2025q1", "2024-09-01", "2025-03-31"),
    ("2025_2026ytd", "2025-04-01", "2026-05-13"),
]


TOPN_FILTERS = [
    ("top1", 1),
    ("top2", 2),
    ("top3", 3),
]


HOT_INDUSTRY_FILTERS = [
    ("hot_rank1", 1),
    ("hot_rank2", 2),
    ("hot_rank3", 3),
]


def compact_summary(name: str, window: str, summary: dict) -> dict:
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
        "avg_hold_days": summary["avg_hold_days"],
        "base_window": cfg["base_window"],
        "max_base_range_pct": cfg["max_base_range_pct"],
        "min_breakout_pct": cfg["min_breakout_pct"],
        "min_volume_ratio": cfg["min_volume_ratio"],
        "entry_low_pct": cfg["entry_low_pct"],
        "entry_high_pct": cfg["entry_high_pct"],
        "stop_pct": cfg["stop_pct"],
        "failure_buffer_pct": cfg["failure_buffer_pct"],
        "take_profit_pct": cfg["take_profit_pct"],
        "max_hold_days": cfg["max_hold_days"],
        "min_ret20_pct": cfg["min_ret20_pct"],
        "min_rel20_pct": cfg["min_rel20_pct"],
        "status_counts_json": json.dumps(summary["status_counts"], ensure_ascii=False, sort_keys=True),
    }


def run_configs(data: s1.ResearchData, window: str, configs: list[tuple[str, s1.Config]]) -> list[dict]:
    rows = []
    total = len(configs)
    for i, (name, cfg) in enumerate(configs, start=1):
        result = s1.run_cached(data, cfg)
        summary = result["summary"]
        row = compact_summary(name, window, summary)
        rows.append(row)
        print(
            f"[{window} {i}/{total}] {name}: "
            f"trig={row['triggered']} win={row['win_rate']:.1%} "
            f"avg={row['avg_pnl']:+.2f}% med={row['median_pnl']:+.2f}% "
            f"p25={row['p25_pnl']:+.2f}%",
            flush=True,
        )
    return rows


def summarize_signals(rows: list[dict]) -> dict:
    pnls = [row["pnl_pct"] for row in rows if row.get("pnl_pct") is not None]
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    return {
        "candidates": len(rows),
        "triggered": len(pnls),
        "win_rate": sum(1 for pnl in pnls if pnl > 0) / len(pnls) if pnls else 0.0,
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
        "median_pnl": s1.quantile(pnls, 0.5),
        "p25_pnl": s1.quantile(pnls, 0.25),
        "p75_pnl": s1.quantile(pnls, 0.75),
        "max_pnl": max(pnls) if pnls else 0.0,
        "min_pnl": min(pnls) if pnls else 0.0,
        "status_counts": status_counts,
    }


def load_industry_maps(start: str, end: str) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], int]]:
    conn = s1.connect()
    try:
        industry_rows = conn.execute(
            """
            SELECT date, code, industry
            FROM s5_daily_universe
            WHERE date BETWEEN ? AND ?
              AND industry IS NOT NULL
            """,
            (start, end),
        ).fetchall()
        hot_rows = conn.execute(
            """
            SELECT date, industry, rank
            FROM hot_industries_daily
            WHERE date BETWEEN ? AND ?
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    code_industry = {
        (row["date"], row["code"]): row["industry"]
        for row in industry_rows
    }
    hot_rank = {
        (row["date"], row["industry"]): int(row["rank"])
        for row in hot_rows
    }
    return code_industry, hot_rank


def annotate_industries(
    signals: list[dict],
    code_industry: dict[tuple[str, str], str],
    hot_rank: dict[tuple[str, str], int],
) -> list[dict]:
    annotated = []
    for signal in signals:
        industry = code_industry.get((signal["date"], signal["code"]))
        rank = hot_rank.get((signal["date"], industry)) if industry else None
        annotated.append({**signal, "industry": industry, "hot_industry_rank": rank})
    return annotated


def filter_topn(signals: list[dict], topn: int) -> list[dict]:
    by_day: dict[str, list[dict]] = {}
    for signal in signals:
        by_day.setdefault(signal["date"], []).append(signal)
    filtered = []
    for day_rows in by_day.values():
        filtered.extend(sorted(day_rows, key=lambda row: row["score"], reverse=True)[:topn])
    return filtered


def compact_filtered_summary(name: str, window: str, start: str, end: str, rows: list[dict]) -> dict:
    summary = summarize_signals(rows)
    return {
        "filter_name": name,
        "window": window,
        "start": start,
        "end": end,
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


def run_filter_study(window: str, start: str, end: str, cfg: s1.Config) -> list[dict]:
    data = s1.load_research_data(start, end)
    result = s1.run_cached(data, cfg)
    signals = result["signals"]
    code_industry, hot_rank = load_industry_maps(start, end)
    annotated = annotate_industries(signals, code_industry, hot_rank)
    rows = [compact_filtered_summary("all", window, start, end, annotated)]

    for name, topn in TOPN_FILTERS:
        rows.append(compact_filtered_summary(name, window, start, end, filter_topn(annotated, topn)))

    for name, max_rank in HOT_INDUSTRY_FILTERS:
        hot_rows = [
            row for row in annotated
            if row.get("hot_industry_rank") is not None and row["hot_industry_rank"] <= max_rank
        ]
        non_hot_rows = [
            row for row in annotated
            if row.get("hot_industry_rank") is None or row["hot_industry_rank"] > max_rank
        ]
        rows.append(compact_filtered_summary(name, window, start, end, hot_rows))
        rows.append(compact_filtered_summary(f"not_{name}", window, start, end, non_hot_rows))
        for top_name, topn in TOPN_FILTERS[:2]:
            rows.append(compact_filtered_summary(
                f"{top_name}_{name}",
                window,
                start,
                end,
                filter_topn(hot_rows, topn),
            ))
            rows.append(compact_filtered_summary(
                f"{top_name}_not_{name}",
                window,
                start,
                end,
                filter_topn(non_hot_rows, topn),
            ))

    for row in rows:
        print(
            f"[filter {window}] {row['filter_name']}: "
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


def main() -> None:
    recent_data = s1.load_research_data(RECENT_START, RECENT_END)
    recent_rows = run_configs(recent_data, "recent", RECENT_CONFIGS)
    write_csv(OUT_DIR / "s1_explore_recent_20260513.csv", recent_rows)

    ranked = sorted(
        recent_rows,
        key=lambda r: (r["median_pnl"], r["avg_pnl"], r["triggered"]),
        reverse=True,
    )
    top_names = {r["config_name"] for r in ranked[:4]}
    cross_configs = [(name, cfg) for name, cfg in RECENT_CONFIGS if name in top_names]
    if "base_sr_hold2_e-2_0" not in top_names:
        cross_configs.insert(0, ("base_sr_hold2_e-2_0", BASE))

    cross_rows = []
    for window_name, start, end in WINDOWS:
        data = s1.load_research_data(start, end)
        cross_rows.extend(run_configs(data, window_name, cross_configs))
    write_csv(OUT_DIR / "s1_explore_cross_20260513.csv", cross_rows)

    filter_rows = []
    for window_name, start, end in WINDOWS:
        filter_rows.extend(run_filter_study(window_name, start, end, BASE))
    write_csv(OUT_DIR / "s1_explore_filters_20260513.csv", filter_rows)

    print("\nTOP_RECENT_BY_MEDIAN")
    for row in ranked[:8]:
        print(
            f"{row['config_name']}: trig={row['triggered']} "
            f"win={row['win_rate']:.1%} avg={row['avg_pnl']:+.2f}% "
            f"med={row['median_pnl']:+.2f}% p25={row['p25_pnl']:+.2f}%"
        )

    print("\nCONFIGS")
    for name, cfg in RECENT_CONFIGS:
        print(name, json.dumps(asdict(cfg), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
