#!/usr/bin/env python3
"""S1 open-price entry band grid.

Research-only. Reuses S1 detection but changes entry to: buy only at T+1 open
if the open is inside the configured band relative to signal close.
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import s1_breakout_research as s1


OUT_DIR = Path(__file__).resolve().parent
PRICE_EPS = s1.PRICE_EPS

BASE = s1.Config(
    base_window=20,
    max_base_range_pct=14.0,
    min_breakout_pct=2.0,
    min_volume_ratio=1.5,
    min_amount_yi=5.0,
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

ENTRY_BANDS = [
    ("open_m3_0", -3.0, 0.0),
    ("open_m2_0", -2.0, 0.0),
    ("open_m1_0", -1.0, 0.0),
    ("open_m3_p1", -3.0, 1.0),
    ("open_m2_p1", -2.0, 1.0),
    ("open_m1_p1", -1.0, 1.0),
    ("open_m3_p2", -3.0, 2.0),
    ("open_m2_p2", -2.0, 2.0),
    ("open_m1_p2", -1.0, 2.0),
    ("open_m3_p3", -3.0, 3.0),
    ("open_m2_p3", -2.0, 3.0),
    ("open_m1_p3", -1.0, 3.0),
    ("open_0_p1", 0.0, 1.0),
    ("open_0_p2", 0.0, 2.0),
    ("open_0_p3", 0.0, 3.0),
]


def plan_open_entry(signal: dict, t1: dict, low_pct: float, high_pct: float) -> dict:
    if t1["is_one_word_up"]:
        return {"status": "limit_up_buy_blocked", "entry": None}
    close = signal["close"]
    entry_low = close * (1 + low_pct / 100)
    entry_high = close * (1 + high_pct / 100)
    if t1["up_limit"] is not None:
        entry_high = min(entry_high, t1["up_limit"] - PRICE_EPS)
    open_price = t1["open"]
    if entry_low <= open_price <= entry_high:
        return {"status": "filled", "entry": open_price}
    if open_price > entry_high:
        return {"status": "gap_up_skip", "entry": None}
    return {"status": "weak_open_skip", "entry": None}


def verify_open_entry(signal: dict, bars: list[dict], idx: int, cfg: s1.Config, low_pct: float, high_pct: float) -> dict:
    entry_idx = idx + 1
    if entry_idx >= len(bars) or entry_idx + 1 >= len(bars):
        return {"status": "no_data", "pnl_pct": None}
    t1 = bars[entry_idx]
    entry_plan = plan_open_entry(signal, t1, low_pct, high_pct)
    if entry_plan["status"] != "filled":
        return {"status": entry_plan["status"], "pnl_pct": None}
    entry = entry_plan["entry"]
    stop = max(signal["base_low"] * 0.98, entry * (1 + cfg.stop_pct / 100))
    failure_line = signal["base_high"] * (1 - cfg.failure_buffer_pct / 100)
    take_profit = entry * (1 + cfg.take_profit_pct / 100) if cfg.take_profit_pct > 0 else None

    blocked_sell_days = 0
    deadline_idx = min(len(bars) - 1, entry_idx + cfg.max_hold_days - 1)
    for exit_idx in range(entry_idx + 1, deadline_idx + 1):
        bar = bars[exit_idx]
        hold_days = exit_idx - entry_idx + 1
        if bar["is_one_word_down"]:
            blocked_sell_days += 1
            continue
        if bar["low"] <= stop:
            exit_price = s1.stop_exit_price(bar, stop)
            return {
                "status": "stop_hit",
                "pnl_pct": (exit_price - entry) / entry * 100,
                "hold_days": hold_days,
                "blocked_sell_days": blocked_sell_days,
            }
        if take_profit is not None and bar["high"] >= take_profit:
            return {
                "status": "take_profit",
                "pnl_pct": cfg.take_profit_pct,
                "hold_days": hold_days,
                "blocked_sell_days": blocked_sell_days,
            }
        if bar["close"] < failure_line:
            return {
                "status": "breakout_failed",
                "pnl_pct": (bar["close"] - entry) / entry * 100,
                "hold_days": hold_days,
                "blocked_sell_days": blocked_sell_days,
            }

    forced_start = max(entry_idx + 1, deadline_idx)
    forced_end = min(len(bars), deadline_idx + 6)
    for exit_idx in range(forced_start, forced_end):
        bar = bars[exit_idx]
        hold_days = exit_idx - entry_idx + 1
        if bar["is_one_word_down"]:
            blocked_sell_days += 1
            continue
        if exit_idx == deadline_idx:
            exit_price = bar["close"]
            status = "max_hold"
        else:
            exit_price = bar["open"]
            status = "max_hold_sell_blocked"
        return {
            "status": status,
            "pnl_pct": (exit_price - entry) / entry * 100,
            "hold_days": hold_days,
            "blocked_sell_days": blocked_sell_days,
        }
    return {"status": "sell_blocked_no_exit", "pnl_pct": None, "blocked_sell_days": blocked_sell_days}


def run_open_cached(data: s1.ResearchData, cfg: s1.Config, low_pct: float, high_pct: float) -> dict:
    base_result = s1.run_cached(data, cfg)
    signals = base_result["signals"]
    results = []
    for sig in signals:
        if sig["code"] not in data.by_code or sig["date"] not in data.date_indexes[sig["code"]]:
            continue
        bars = data.by_code[sig["code"]]
        idx = data.date_indexes[sig["code"]][sig["date"]]
        verification = verify_open_entry(sig, bars, idx, cfg, low_pct, high_pct)
        base_status = sig.get("status")
        clean_sig = {k: v for k, v in sig.items() if k not in {"status", "pnl_pct", "hold_days", "blocked_sell_days"}}
        results.append({**clean_sig, "base_status": base_status, **verification})
    summary = s1.summarize_group(results)
    summary.update({
        "start": data.start,
        "end": data.end,
        "eligible_days": base_result["summary"]["eligible_days"],
        "signal_days": base_result["summary"]["signal_days"],
        "raw_signals": base_result["summary"]["raw_signals"],
        "open_entry_low_pct": low_pct,
        "open_entry_high_pct": high_pct,
        "base_triggered": base_result["summary"]["triggered"],
        "base_avg_pnl": base_result["summary"]["avg_pnl"],
        "base_median_pnl": base_result["summary"]["median_pnl"],
    })
    return {"summary": summary, "signals": results}


def compact(name: str, window: str, summary: dict) -> dict:
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
        "open_entry_low_pct": summary["open_entry_low_pct"],
        "open_entry_high_pct": summary["open_entry_high_pct"],
        "base_triggered": summary["base_triggered"],
        "base_avg_pnl": summary["base_avg_pnl"],
        "base_median_pnl": summary["base_median_pnl"],
        "status_counts_json": json.dumps(summary["status_counts"], ensure_ascii=False, sort_keys=True),
    }


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
            "open_entry_low_pct": group[0]["open_entry_low_pct"],
            "open_entry_high_pct": group[0]["open_entry_high_pct"],
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
    rows = []
    for window_name, start, end in WINDOWS:
        data = s1.load_research_data(start, end)
        for i, (name, low_pct, high_pct) in enumerate(ENTRY_BANDS, start=1):
            result = run_open_cached(data, BASE, low_pct, high_pct)
            row = compact(name, window_name, result["summary"])
            rows.append(row)
            print(
                f"[{window_name} {i}/{len(ENTRY_BANDS)}] {name}: "
                f"trig={row['triggered']} win={row['win_rate']:.1%} "
                f"avg={row['avg_pnl']:+.2f}% med={row['median_pnl']:+.2f}% "
                f"p25={row['p25_pnl']:+.2f}%",
                flush=True,
            )
    write_csv(OUT_DIR / "s1_open_entry_grid_20260513.csv", rows)
    agg = aggregate(rows)
    write_csv(OUT_DIR / "s1_open_entry_grid_agg_20260513.csv", agg)

    print("\nTOP_AGG")
    for row in agg[:15]:
        print(
            f"{row['config_name']}: windows>=20={row['windows_triggered_ge20']} "
            f"trig={row['total_triggered']} wavg={row['weighted_avg_pnl']:+.2f}% "
            f"avg_med={row['avg_median_pnl']:+.2f}% min_med={row['min_median_pnl']:+.2f}% "
            f"avg_p25={row['avg_p25_pnl']:+.2f}%"
        )


if __name__ == "__main__":
    main()
