"""S6 trend-hold parameter exploration.

Research-only script. It reads market.db, runs a small sequential grid, and prints
the best parameter sets. It does not write database tables or files.
"""

from __future__ import annotations

import argparse
import itertools
import os
import sqlite3
import sys
from collections import defaultdict
from statistics import median

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_SCRIPTS = os.path.dirname(SCRIPT_DIR)
if ROOT_SCRIPTS not in sys.path:
    sys.path.insert(0, ROOT_SCRIPTS)

from strategy_common import iso_to_yyyymmdd, shift_trading_days, yyyymmdd_to_iso  # noqa: E402

DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))

PRESET_WINDOWS = {
    "preset4": [
        ("2019-01-01", "2019-12-31"),
        ("2020-01-01", "2020-12-31"),
        ("2024-09-01", "2025-03-31"),
        ("2025-04-01", "2026-05-15"),
    ],
    "recent": [("2024-09-01", "2026-05-15")],
}

GRID = {
    "min_amount_yi": [5.0, 10.0],
    "min_ret20_pct": [10.0, 15.0],
    "min_rel20_pct": [5.0, 10.0],
    "max_near_high_pct": [8.0, 12.0],
    "max_hold_days": [5, 7, 10],
    "stop_pct": [-6.0, -8.0],
}

BASE = {
    "entry_low_pct": -3.0,
    "entry_high_pct": 3.0,
    "top_n": 3,
    "index_code": "000852.SH",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="recent", help="preset4/recent or start:end,start:end")
    ap.add_argument("--regimes", default="STRONG_BULL", help="Comma-separated regime_code list")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--max-configs", type=int, default=None, help="Debug cap for grid size")
    args = ap.parse_args()

    windows = parse_windows(args.windows)
    regimes = {item.strip().upper() for item in args.regimes.split(",") if item.strip()}
    configs = build_grid()
    if args.max_configs:
        configs = configs[: args.max_configs]

    print(f"S6 explore windows={windows} regimes={sorted(regimes)} configs={len(configs)}")
    all_results: dict[tuple, list[dict]] = {config_key(c): [] for c in configs}
    config_by_key = {config_key(c): c for c in configs}

    for start, end in windows:
        print(f"\n[window] {start} ~ {end}")
        feature_rows = load_feature_rows(start, end, regimes, BASE["index_code"], GRID)
        print(f"  feature rows: {len(feature_rows)}")
        bars_by_code = load_bars_for_features(feature_rows, start, end, max(GRID["max_hold_days"]) + 4)
        daily_features = group_by_date(feature_rows)
        for idx, cfg in enumerate(configs, start=1):
            if idx % 25 == 0:
                print(f"  evaluated configs: {idx}/{len(configs)}")
            summary = evaluate_config(daily_features, bars_by_code, cfg)
            summary["window"] = f"{start}:{end}"
            all_results[config_key(cfg)].append(summary)

    ranked = []
    for key, summaries in all_results.items():
        ranked.append(aggregate_result(config_by_key[key], summaries))
    ranked.sort(key=rank_score, reverse=True)

    print("\nTop parameter sets")
    print_result_header()
    for row in ranked[: args.top]:
        print_result_row(row)

    print("\nNotes")
    print("- avg/median/worst are percent PnL per triggered trade.")
    print("- score penalizes low trade count, negative median, and weak worst-window average.")
    print("- This is research output only; no DB tables were written.")


def parse_windows(value: str) -> list[tuple[str, str]]:
    if value in PRESET_WINDOWS:
        return PRESET_WINDOWS[value]
    windows = []
    for item in value.split(","):
        start, end = item.split(":")
        windows.append((start.strip(), end.strip()))
    return windows


def build_grid() -> list[dict]:
    keys = list(GRID)
    configs = []
    for values in itertools.product(*(GRID[k] for k in keys)):
        cfg = {**BASE, **dict(zip(keys, values))}
        configs.append(cfg)
    return configs


def config_key(config: dict) -> tuple:
    return (
        config["min_amount_yi"],
        config["min_ret20_pct"],
        config["min_rel20_pct"],
        config["max_near_high_pct"],
        config["max_hold_days"],
        config["stop_pct"],
    )


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_index_ret20(start: str, end: str, index_code: str) -> dict[str, float]:
    lookback = shift_trading_days(start, -30)
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT trade_date, close
            FROM index_daily
            WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date
            """,
            (index_code, iso_to_yyyymmdd(lookback), iso_to_yyyymmdd(end)),
        ).fetchall()
    finally:
        conn.close()
    ret = {}
    closes = [float(r["close"] or 0) for r in rows]
    dates = [yyyymmdd_to_iso(r["trade_date"]) for r in rows]
    for i in range(20, len(rows)):
        prev = closes[i - 20]
        now = closes[i]
        if prev > 0 and now > 0:
            ret[dates[i]] = (now / prev - 1) * 100
    return ret


def load_feature_rows(
    start: str,
    end: str,
    regimes: set[str],
    index_code: str,
    grid: dict,
) -> list[dict]:
    index_ret20 = load_index_ret20(start, end, index_code)
    lookback = shift_trading_days(start, -90)
    start_raw = iso_to_yyyymmdd(start)
    end_raw = iso_to_yyyymmdd(end)
    lookback_raw = iso_to_yyyymmdd(lookback)
    min_amount_raw = min(grid["min_amount_yi"]) * 100_000
    min_ret20 = min(grid["min_ret20_pct"])
    max_near_high = max(grid["max_near_high_pct"])
    placeholders = ",".join("?" * len(regimes))
    params = [lookback_raw, end_raw, start_raw, end_raw, min_amount_raw, *sorted(regimes)]

    conn = get_conn()
    try:
        rows = conn.execute(
            f"""
            WITH e AS (
                SELECT
                    substr(d.ts_code, 1, 6) AS code,
                    d.ts_code,
                    d.trade_date,
                    d.open,
                    d.high,
                    d.low,
                    d.close,
                    d.pre_close,
                    d.pct_chg,
                    d.vol,
                    d.amount,
                    l.up_limit,
                    l.down_limit,
                    AVG(d.close) OVER (
                        PARTITION BY d.ts_code ORDER BY d.trade_date
                        ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
                    ) AS ma10,
                    AVG(d.close) OVER (
                        PARTITION BY d.ts_code ORDER BY d.trade_date
                        ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                    ) AS ma20,
                    AVG(d.close) OVER (
                        PARTITION BY d.ts_code ORDER BY d.trade_date
                        ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                    ) AS ma60,
                    LAG(d.close, 20) OVER (
                        PARTITION BY d.ts_code ORDER BY d.trade_date
                    ) AS close20,
                    LAG(d.close, 60) OVER (
                        PARTITION BY d.ts_code ORDER BY d.trade_date
                    ) AS close60,
                    MAX(d.high) OVER (
                        PARTITION BY d.ts_code ORDER BY d.trade_date
                        ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                    ) AS high60,
                    AVG(d.vol) OVER (
                        PARTITION BY d.ts_code ORDER BY d.trade_date
                        ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                    ) AS vol5_prev
                FROM daily d
                LEFT JOIN stk_limit l
                  ON l.trade_date = d.trade_date AND l.ts_code = d.ts_code
                WHERE d.trade_date >= ? AND d.trade_date <= ?
            )
            SELECT e.*, r.regime_code
            FROM e
            INNER JOIN regime_classify_daily r
              ON r.trade_date = substr(e.trade_date, 1, 4) || '-' || substr(e.trade_date, 5, 2) || '-' || substr(e.trade_date, 7, 2)
             AND r.rules_version = 'v2'
            WHERE e.trade_date >= ? AND e.trade_date <= ?
              AND e.amount >= ?
              AND r.regime_code IN ({placeholders})
              AND e.close > 0
              AND e.close20 > 0
              AND e.close60 > 0
              AND e.ma10 IS NOT NULL
              AND e.ma20 IS NOT NULL
              AND e.ma60 IS NOT NULL
              AND e.high60 > 0
              AND e.close >= e.ma10
              AND e.ma10 >= e.ma20
              AND e.ma20 >= e.ma60
              AND ((e.close / e.close20 - 1) * 100) >= ?
              AND ((e.high60 - e.close) / e.high60 * 100) <= ?
            ORDER BY e.trade_date, e.code
            """,
            (*params, min_ret20, max_near_high),
        ).fetchall()
    finally:
        conn.close()

    features = []
    for r in rows:
        date = yyyymmdd_to_iso(r["trade_date"])
        idx_ret20 = index_ret20.get(date)
        if idx_ret20 is None:
            continue
        close = float(r["close"])
        close20 = float(r["close20"])
        close60 = float(r["close60"])
        high60 = float(r["high60"])
        ret20 = (close / close20 - 1) * 100
        ret60 = (close / close60 - 1) * 100
        near_high = (high60 - close) / high60 * 100
        vol5_prev = float(r["vol5_prev"] or 0)
        volume_ratio = float(r["vol"]) / vol5_prev if vol5_prev > 0 else None
        up_limit = r["up_limit"]
        one_word_up = (
            up_limit is not None
            and close >= float(up_limit) - 0.01
            and abs(float(r["high"]) - float(r["low"])) < 0.01
            and abs(close - float(r["open"])) < 0.01
        )
        if one_word_up:
            continue
        features.append(
            {
                "date": date,
                "code": r["code"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": close,
                "pct_chg": float(r["pct_chg"] or 0),
                "amount_yi": float(r["amount"] or 0) / 100_000,
                "ma10": float(r["ma10"]),
                "ma20": float(r["ma20"]),
                "ma60": float(r["ma60"]),
                "ret20_pct": ret20,
                "ret60_pct": ret60,
                "rel20_pct": ret20 - idx_ret20,
                "near_high_pct": near_high,
                "volume_ratio": volume_ratio,
                "regime_code": r["regime_code"],
            }
        )
    return features


def load_bars_for_features(
    features: list[dict],
    start: str,
    end: str,
    extra_days: int,
) -> dict[str, dict[str, dict]]:
    codes = sorted({r["code"] for r in features})
    if not codes:
        return {}
    start_raw = iso_to_yyyymmdd(shift_trading_days(start, -20))
    end_raw = iso_to_yyyymmdd(shift_trading_days(end, extra_days))
    by_code: dict[str, list[dict]] = defaultdict(list)
    conn = get_conn()
    try:
        for i in range(0, len(codes), 800):
            chunk = codes[i : i + 800]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""
                SELECT d.trade_date, substr(d.ts_code, 1, 6) AS code,
                       d.open, d.high, d.low, d.close, d.pre_close, d.pct_chg,
                       l.up_limit, l.down_limit
                FROM daily d
                LEFT JOIN stk_limit l
                  ON l.trade_date = d.trade_date AND l.ts_code = d.ts_code
                WHERE substr(d.ts_code, 1, 6) IN ({placeholders})
                  AND d.trade_date >= ? AND d.trade_date <= ?
                ORDER BY d.ts_code, d.trade_date
                """,
                (*chunk, start_raw, end_raw),
            ).fetchall()
            for r in rows:
                by_code[r["code"]].append(
                    {
                        "date": yyyymmdd_to_iso(r["trade_date"]),
                        "open": float(r["open"] or 0),
                        "high": float(r["high"] or 0),
                        "low": float(r["low"] or 0),
                        "close": float(r["close"] or 0),
                        "pre_close": float(r["pre_close"] or 0),
                        "pct_chg": float(r["pct_chg"] or 0),
                        "up_limit": float(r["up_limit"]) if r["up_limit"] is not None else None,
                        "down_limit": float(r["down_limit"]) if r["down_limit"] is not None else None,
                    }
                )
    finally:
        conn.close()

    result: dict[str, dict[str, dict]] = {}
    for code, rows in by_code.items():
        closes = []
        mapped = {}
        for bar in rows:
            closes.append(bar["close"])
            bar["ma10"] = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
            bar["ma20"] = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
            mapped[bar["date"]] = bar
        result[code] = mapped
    return result


def group_by_date(features: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in features:
        grouped[row["date"]].append(row)
    return grouped


def evaluate_config(
    daily_features: dict[str, list[dict]],
    bars_by_code: dict[str, dict[str, dict]],
    cfg: dict,
) -> dict:
    trades = []
    candidate_days = 0
    for date in sorted(daily_features):
        candidates = [r for r in daily_features[date] if pass_config(r, cfg)]
        if not candidates:
            continue
        candidate_days += 1
        candidates.sort(key=signal_score, reverse=True)
        for row in candidates[: int(cfg["top_n"])]:
            trade = simulate_trade(row, bars_by_code.get(row["code"], {}), cfg)
            if trade is not None:
                trades.append(trade)
    return summarize_trades(trades, candidate_days)


def pass_config(row: dict, cfg: dict) -> bool:
    return (
        row["amount_yi"] >= cfg["min_amount_yi"]
        and row["ret20_pct"] >= cfg["min_ret20_pct"]
        and row["rel20_pct"] >= cfg["min_rel20_pct"]
        and row["near_high_pct"] <= cfg["max_near_high_pct"]
    )


def signal_score(row: dict) -> float:
    volume_bonus = max((row["volume_ratio"] or 0) - 1.0, 0) * 5
    return (
        row["ret20_pct"] * 1.4
        + row["rel20_pct"] * 2.0
        + row["ret60_pct"] * 0.35
        + max(0, 12 - row["near_high_pct"]) * 2.2
        + min(row["amount_yi"], 30) * 0.35
        + volume_bonus
    )


def simulate_trade(row: dict, bars: dict[str, dict], cfg: dict) -> dict | None:
    t1 = next_bar_after(row["date"], bars)
    if t1 is None:
        return None
    entry_low = row["close"] * (1 + cfg["entry_low_pct"] / 100)
    entry_high = row["close"] * (1 + cfg["entry_high_pct"] / 100)
    if is_one_word_up(t1):
        return skipped_trade("limit_up_buy_blocked", row, t1)
    if t1["open"] > entry_high:
        return skipped_trade("gap_up_skip", row, t1)
    if t1["open"] < entry_low:
        return skipped_trade("gap_down_skip", row, t1)

    entry_price = t1["open"]
    stop_price = entry_price * (1 + cfg["stop_pct"] / 100)
    future = bars_after(t1["date"], bars, int(cfg["max_hold_days"]) + 4)
    if len(future) < 2:
        return None

    for hold_days, bar in enumerate(future[1:], start=2):
        deadline = hold_days >= int(cfg["max_hold_days"])
        if is_one_word_down(bar):
            continue
        if bar["open"] <= stop_price:
            return exited_trade("stop_open", row, t1, bar, entry_price, bar["open"], hold_days)
        if bar["low"] <= stop_price:
            return exited_trade("stop_intraday", row, t1, bar, entry_price, stop_price, hold_days)
        ma10 = bar.get("ma10")
        if ma10 is not None and bar["close"] < ma10:
            return exited_trade("ma10_lost", row, t1, bar, entry_price, bar["close"], hold_days)
        if deadline:
            return exited_trade("max_hold", row, t1, bar, entry_price, bar["close"], hold_days)
    last = future[-1]
    if is_one_word_down(last):
        return None
    return exited_trade("forced_after_deadline", row, t1, last, entry_price, last["open"], len(future))


def next_bar_after(date: str, bars: dict[str, dict]) -> dict | None:
    for d in sorted(bars):
        if d > date:
            return bars[d]
    return None


def bars_after(date: str, bars: dict[str, dict], limit: int) -> list[dict]:
    result = []
    for d in sorted(bars):
        if d >= date:
            result.append(bars[d])
            if len(result) >= limit:
                break
    return result


def skipped_trade(status: str, row: dict, t1: dict) -> dict:
    return {
        "triggered": False,
        "status": status,
        "date": row["date"],
        "code": row["code"],
        "t1_date": t1["date"],
        "pnl_pct": None,
        "hold_days": 0,
    }


def exited_trade(
    status: str,
    row: dict,
    t1: dict,
    exit_bar: dict,
    entry_price: float,
    exit_price: float,
    hold_days: int,
) -> dict:
    pnl = (exit_price / entry_price - 1) * 100
    return {
        "triggered": True,
        "status": status,
        "date": row["date"],
        "code": row["code"],
        "t1_date": t1["date"],
        "exit_date": exit_bar["date"],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": pnl,
        "hold_days": hold_days,
    }


def is_one_word_up(bar: dict) -> bool:
    up_limit = bar.get("up_limit")
    return (
        up_limit is not None
        and bar["close"] >= up_limit - 0.01
        and abs(bar["high"] - bar["low"]) < 0.01
        and abs(bar["close"] - bar["open"]) < 0.01
    )


def is_one_word_down(bar: dict) -> bool:
    down_limit = bar.get("down_limit")
    return (
        down_limit is not None
        and bar["close"] <= down_limit + 0.01
        and abs(bar["high"] - bar["low"]) < 0.01
        and abs(bar["close"] - bar["open"]) < 0.01
    )


def summarize_trades(trades: list[dict], candidate_days: int) -> dict:
    triggered = [t for t in trades if t["triggered"] and t["pnl_pct"] is not None]
    pnls = [t["pnl_pct"] for t in triggered]
    wins = [p for p in pnls if p > 0]
    status_counts = defaultdict(int)
    for t in trades:
        status_counts[t["status"]] += 1
    return {
        "candidate_days": candidate_days,
        "candidates": len(trades),
        "triggered": len(triggered),
        "win_rate": len(wins) / len(triggered) if triggered else 0,
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
        "median_pnl": median(pnls) if pnls else 0,
        "min_pnl": min(pnls) if pnls else 0,
        "max_pnl": max(pnls) if pnls else 0,
        "avg_hold_days": sum(t["hold_days"] for t in triggered) / len(triggered) if triggered else 0,
        "status_counts": dict(status_counts),
    }


def aggregate_result(config: dict, summaries: list[dict]) -> dict:
    total_triggered = sum(s["triggered"] for s in summaries)
    weighted_avg = (
        sum(s["avg_pnl"] * s["triggered"] for s in summaries) / total_triggered
        if total_triggered
        else 0
    )
    weighted_win = (
        sum(s["win_rate"] * s["triggered"] for s in summaries) / total_triggered
        if total_triggered
        else 0
    )
    avg_median = sum(s["median_pnl"] for s in summaries) / len(summaries) if summaries else 0
    worst_avg = min((s["avg_pnl"] for s in summaries), default=0)
    worst_median = min((s["median_pnl"] for s in summaries), default=0)
    return {
        "config": config,
        "windows": summaries,
        "triggered": total_triggered,
        "candidate_days": sum(s["candidate_days"] for s in summaries),
        "avg_pnl": weighted_avg,
        "win_rate": weighted_win,
        "avg_median_pnl": avg_median,
        "worst_avg_pnl": worst_avg,
        "worst_median_pnl": worst_median,
        "avg_hold_days": (
            sum(s["avg_hold_days"] * s["triggered"] for s in summaries) / total_triggered
            if total_triggered
            else 0
        ),
    }


def rank_score(row: dict) -> float:
    count_penalty = min(row["triggered"] / 120, 1.0)
    return (
        row["avg_pnl"] * 1.4
        + row["avg_median_pnl"] * 1.1
        + row["worst_avg_pnl"] * 1.0
        + row["worst_median_pnl"] * 0.6
        + row["win_rate"] * 2.0
    ) * count_penalty


def print_result_header() -> None:
    print(
        "rank  trades  win%  avg%  med%  worst_avg%  worst_med%  hold  "
        "amt ret20 rel20 near hold stop"
    )


def print_result_row(row: dict) -> None:
    cfg = row["config"]
    rank = rank_score(row)
    print(
        f"{rank:5.2f} "
        f"{row['triggered']:7d} "
        f"{row['win_rate']*100:5.1f} "
        f"{row['avg_pnl']:5.2f} "
        f"{row['avg_median_pnl']:5.2f} "
        f"{row['worst_avg_pnl']:10.2f} "
        f"{row['worst_median_pnl']:10.2f} "
        f"{row['avg_hold_days']:5.1f} "
        f"{cfg['min_amount_yi']:3.0f} "
        f"{cfg['min_ret20_pct']:5.0f} "
        f"{cfg['min_rel20_pct']:5.0f} "
        f"{cfg['max_near_high_pct']:4.0f} "
        f"{cfg['max_hold_days']:4d} "
        f"{cfg['stop_pct']:5.0f}"
    )


if __name__ == "__main__":
    main()
