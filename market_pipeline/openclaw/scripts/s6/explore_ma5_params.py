"""S6 MA5-continuation parameter exploration.

Research-only script. It evaluates named, economically motivated variants around
the current S6 trend/defensive baselines. It reads market.db and writes nothing.
Keep runs single-process; callers should also set BLAS thread env vars to 1.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from copy import deepcopy
from statistics import median
from typing import Iterable

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_SCRIPTS = os.path.dirname(SCRIPT_DIR)
if ROOT_SCRIPTS not in sys.path:
    sys.path.insert(0, ROOT_SCRIPTS)

from strategy_common import get_trading_days, iso_to_yyyymmdd, shift_trading_days, yyyymmdd_to_iso  # noqa: E402

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

WARMUP_DAYS = 90
EXIT_EXTRA_DAYS = 12


TREND_BASE = {
    "mode": "trend",
    "regimes": ("STRONG_BULL",),
    "min_total_score": None,
    "min_volume_trend": 1,
    "min_advance_decline": None,
    "min_sentiment_index": None,
    "allow_switched": True,
    "allow_emergency": True,
    "min_amount_yi": 5.0,
    "min_ret20_pct": 10.0,
    "min_rel20_pct": 5.0,
    "min_dist_ma5_pct": 0.0,
    "max_dist_ma5_pct": 3.0,
    "min_volume_ratio": None,
    "max_pct_chg": None,
    "min_pct_chg": None,
    "min_ma10_ma20_spread_pct": None,
    "min_ma20_ma60_spread_pct": None,
    "entry_low_pct": -3.0,
    "entry_high_pct": 1.0,
    "max_hold_days": 5,
    "stop_pct": -5.0,
    "profit_take_pct": None,
    "exit_ma": "ma5",
    "top_n": 1,
    "cooldown_days": 0,
}


DEF_BASE = {
    "mode": "defensive",
    "regimes": ("WEAK_RANGE",),
    "min_total_score": -3,
    "min_volume_trend": None,
    "min_advance_decline": None,
    "min_sentiment_index": None,
    "allow_switched": True,
    "allow_emergency": True,
    "min_amount_yi": 5.0,
    "min_ret20_pct": 10.0,
    "min_rel20_pct": 5.0,
    "min_dist_ma5_pct": 0.0,
    "max_dist_ma5_pct": 3.0,
    "min_volume_ratio": None,
    "max_pct_chg": None,
    "min_pct_chg": None,
    "min_ma10_ma20_spread_pct": None,
    "min_ma20_ma60_spread_pct": None,
    "entry_low_pct": -3.0,
    "entry_high_pct": 1.0,
    "max_hold_days": 3,
    "stop_pct": -6.0,
    "profit_take_pct": None,
    "exit_ma": "ma5",
    "top_n": 1,
    "cooldown_days": 0,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", default="preset4", help="preset4/recent or start:end,start:end")
    parser.add_argument("--top", type=int, default=18)
    parser.add_argument("--groups", default="all", help="Comma list: trend,defensive,exit,volume,dist,score,pct,cooldown,all")
    args = parser.parse_args()

    windows = parse_windows(args.windows)
    configs = build_configs(parse_groups(args.groups))
    regimes = sorted({regime for _, cfg in configs for regime in cfg["regimes"]})
    print(f"S6 MA5 param explore windows={windows} configs={len(configs)} regimes={regimes}")
    print("single-process research run; no writes")

    config_results: dict[str, list[dict]] = defaultdict(list)
    config_by_name = {name: cfg for name, cfg in configs}

    for start, end in windows:
        print(f"\n[window] {start} ~ {end}")
        features = load_feature_rows(start, end, regimes, configs)
        print(f"  feature rows: {len(features)}")
        bars_by_code = load_bars_for_features(features, start, end)
        daily_features = group_by_date(features)
        trading_days = get_trading_days(start, end)
        date_index = {date: idx for idx, date in enumerate(trading_days)}
        for idx, (name, cfg) in enumerate(configs, start=1):
            if idx % 20 == 0:
                print(f"  evaluated configs: {idx}/{len(configs)}")
            summary = evaluate_config(daily_features, bars_by_code, date_index, cfg)
            summary["window"] = f"{start}:{end}"
            config_results[name].append(summary)

    rows = []
    for name, summaries in config_results.items():
        rows.append(aggregate_result(name, config_by_name[name], summaries))

    print("\nOverall top")
    print_result_header()
    for row in sorted(rows, key=rank_score, reverse=True)[: args.top]:
        print_result_row(row)

    print("\nGroup best")
    print_result_header()
    for group in sorted({r["group"] for r in rows}):
        group_rows = [r for r in rows if r["group"] == group]
        best = max(group_rows, key=rank_score)
        print_result_row(best)

    print("\nBaselines")
    print_result_header()
    for name in ("trend_base", "def_base"):
        row = next((r for r in rows if r["name"] == name), None)
        if row:
            print_result_row(row)

    print("\nNotes")
    print("- avg/med/worst are percent PnL per triggered trade.")
    print("- worst_avg is the weakest single-window average; avg_win_med is the mean of window medians.")
    print("- rank penalizes tiny samples and weak worst-window performance; it is only a triage aid.")


def parse_windows(value: str) -> list[tuple[str, str]]:
    if value in PRESET_WINDOWS:
        return PRESET_WINDOWS[value]
    windows = []
    for item in value.split(","):
        start, end = item.split(":")
        windows.append((start.strip(), end.strip()))
    return windows


def parse_groups(value: str) -> set[str]:
    groups = {item.strip().lower() for item in value.split(",") if item.strip()}
    if not groups or "all" in groups:
        return {"trend", "defensive", "exit", "volume", "dist", "score", "pct", "cooldown"}
    return groups


def build_configs(groups: set[str]) -> list[tuple[str, dict]]:
    configs: list[tuple[str, dict]] = []

    def add(name: str, base: dict, group: str, **updates: object) -> None:
        cfg = deepcopy(base)
        cfg.update(updates)
        cfg["group"] = group
        configs.append((name, cfg))

    if {"trend", "exit", "volume", "dist", "score", "pct", "cooldown"} & groups:
        add("trend_base", TREND_BASE, "trend")

    if {"defensive", "exit", "volume", "dist", "score", "pct", "cooldown"} & groups:
        add("def_base", DEF_BASE, "defensive")

    if "volume" in groups:
        for value in (0.8, 1.0, 1.2, 1.5, 2.0):
            add(f"trend_volr_ge_{value:g}", TREND_BASE, "volume", min_volume_ratio=value)
        for value in (0.8, 1.0, 1.2, 1.5):
            add(f"def_volr_ge_{value:g}", DEF_BASE, "volume", min_volume_ratio=value)

    if "dist" in groups:
        for value in (1.5, 2.0, 2.5, 3.5, 4.0, 5.0):
            add(f"trend_dist_le_{value:g}", TREND_BASE, "dist", max_dist_ma5_pct=value)
        for low, high in ((0.5, 3.0), (1.0, 3.0), (0.0, 2.0), (0.0, 4.0)):
            add(f"trend_dist_{low:g}_{high:g}", TREND_BASE, "dist", min_dist_ma5_pct=low, max_dist_ma5_pct=high)
        for value in (2.0, 2.5, 3.5, 4.0, 5.0):
            add(f"def_dist_le_{value:g}", DEF_BASE, "dist", max_dist_ma5_pct=value)

    if "exit" in groups:
        for stop in (-4.0, -5.0, -6.0, -7.0):
            add(f"trend_stop_{abs(stop):g}", TREND_BASE, "exit", stop_pct=stop)
        for hold in (4, 5, 6, 7):
            add(f"trend_hold_{hold}", TREND_BASE, "exit", max_hold_days=hold)
        for take in (6.0, 8.0, 10.0, 12.0):
            add(f"trend_take_{take:g}", TREND_BASE, "exit", profit_take_pct=take)
        add("trend_exit_ma10", TREND_BASE, "exit", exit_ma="ma10")
        add("trend_exit_time_only", TREND_BASE, "exit", exit_ma=None)

        for stop in (-5.0, -6.0, -7.0, -8.0):
            add(f"def_stop_{abs(stop):g}", DEF_BASE, "exit", stop_pct=stop)
        for hold in (2, 3, 4, 5):
            add(f"def_hold_{hold}", DEF_BASE, "exit", max_hold_days=hold)
        for take in (5.0, 7.0, 9.0):
            add(f"def_take_{take:g}", DEF_BASE, "exit", profit_take_pct=take)
        add("def_exit_ma10", DEF_BASE, "exit", exit_ma="ma10")

    if "score" in groups:
        for amount in (3.0, 5.0, 8.0, 10.0):
            add(f"trend_amt_{amount:g}", TREND_BASE, "score", min_amount_yi=amount)
        for ret20 in (8.0, 10.0, 12.0, 15.0):
            add(f"trend_ret20_{ret20:g}", TREND_BASE, "score", min_ret20_pct=ret20)
        for rel20 in (0.0, 3.0, 5.0, 8.0, 10.0):
            add(f"trend_rel20_{rel20:g}", TREND_BASE, "score", min_rel20_pct=rel20)
        for spread in (0.5, 1.0, 2.0, 3.0):
            add(f"trend_ma10_20_spread_{spread:g}", TREND_BASE, "score", min_ma10_ma20_spread_pct=spread)
        for spread in (1.0, 2.0, 4.0, 6.0):
            add(f"trend_ma20_60_spread_{spread:g}", TREND_BASE, "score", min_ma20_ma60_spread_pct=spread)
        add("trend_advance_ge_0", TREND_BASE, "score", min_advance_decline=0)
        add("trend_sentiment_ge_0", TREND_BASE, "score", min_sentiment_index=0)
        add("trend_no_switched", TREND_BASE, "score", allow_switched=False)
        add("trend_no_emergency", TREND_BASE, "score", allow_emergency=False)

        for total in (-4, -3, -2, -1):
            add(f"def_total_ge_{total}", DEF_BASE, "score", min_total_score=total)
        for amount in (3.0, 5.0, 8.0, 10.0):
            add(f"def_amt_{amount:g}", DEF_BASE, "score", min_amount_yi=amount)
        for rel20 in (0.0, 3.0, 5.0, 8.0, 10.0):
            add(f"def_rel20_{rel20:g}", DEF_BASE, "score", min_rel20_pct=rel20)
        add("def_no_switched", DEF_BASE, "score", allow_switched=False)
        add("def_no_emergency", DEF_BASE, "score", allow_emergency=False)

    if "pct" in groups:
        for max_pct in (3.0, 5.0, 7.0, 9.8):
            add(f"trend_tday_pct_le_{max_pct:g}", TREND_BASE, "pct", max_pct_chg=max_pct)
        for min_pct in (-1.0, 0.0, 1.0, 2.0):
            add(f"trend_tday_pct_ge_{min_pct:g}", TREND_BASE, "pct", min_pct_chg=min_pct)
        for max_pct in (3.0, 5.0, 7.0, 9.8):
            add(f"def_tday_pct_le_{max_pct:g}", DEF_BASE, "pct", max_pct_chg=max_pct)
        for min_pct in (-1.0, 0.0, 1.0, 2.0):
            add(f"def_tday_pct_ge_{min_pct:g}", DEF_BASE, "pct", min_pct_chg=min_pct)

    if "cooldown" in groups:
        for days in (5, 10, 20, 30):
            add(f"trend_cool_{days}", TREND_BASE, "cooldown", cooldown_days=days)
        for days in (5, 10, 20, 30):
            add(f"def_cool_{days}", DEF_BASE, "cooldown", cooldown_days=days)

    seen = set()
    unique = []
    for name, cfg in configs:
        if name in seen:
            continue
        seen.add(name)
        unique.append((name, cfg))
    return unique


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_index_ret20(start: str, end: str, index_code: str = "000852.SH") -> dict[str, float]:
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
    closes = [float(r["close"] or 0) for r in rows]
    dates = [yyyymmdd_to_iso(r["trade_date"]) for r in rows]
    result = {}
    for i in range(20, len(rows)):
        prev = closes[i - 20]
        now = closes[i]
        if prev > 0 and now > 0:
            result[dates[i]] = (now / prev - 1) * 100
    return result


def load_feature_rows(
    start: str,
    end: str,
    regimes: list[str],
    configs: list[tuple[str, dict]],
) -> list[dict]:
    index_ret20 = load_index_ret20(start, end)
    lookback = shift_trading_days(start, -WARMUP_DAYS)
    start_raw = iso_to_yyyymmdd(start)
    end_raw = iso_to_yyyymmdd(end)
    lookback_raw = iso_to_yyyymmdd(lookback)
    broad_min_amount = min(float(cfg["min_amount_yi"]) for _, cfg in configs) * 100_000
    broad_min_ret20 = min(float(cfg["min_ret20_pct"]) for _, cfg in configs)
    broad_max_dist = max(float(cfg["max_dist_ma5_pct"]) for _, cfg in configs)
    placeholders = ",".join("?" * len(regimes))

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
                        ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                    ) AS ma5,
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
                    AVG(d.vol) OVER (
                        PARTITION BY d.ts_code ORDER BY d.trade_date
                        ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                    ) AS vol5_prev
                FROM daily d
                LEFT JOIN stk_limit l
                  ON l.trade_date = d.trade_date AND l.ts_code = d.ts_code
                WHERE d.trade_date >= ? AND d.trade_date <= ?
            )
            SELECT e.*,
                   r.total_score,
                   r.score_advance_decline,
                   r.score_sentiment_index,
                   r.score_volume_trend,
                   r.regime_code,
                   r.switched,
                   r.emergency_switch
            FROM e
            INNER JOIN regime_classify_daily r
              ON r.trade_date = substr(e.trade_date, 1, 4) || '-' || substr(e.trade_date, 5, 2) || '-' || substr(e.trade_date, 7, 2)
             AND r.rules_version = 'v2'
            WHERE e.trade_date >= ? AND e.trade_date <= ?
              AND e.amount >= ?
              AND r.regime_code IN ({placeholders})
              AND e.close > 0
              AND e.ma5 IS NOT NULL
              AND e.ma10 IS NOT NULL
              AND e.ma20 IS NOT NULL
              AND e.ma60 IS NOT NULL
              AND e.close20 > 0
              AND e.close60 > 0
              AND e.close >= e.ma5
              AND e.ma10 >= e.ma20
              AND e.ma20 >= e.ma60
              AND ((e.close / e.close20 - 1) * 100) >= ?
              AND ((e.close / e.ma5 - 1) * 100) <= ?
            ORDER BY e.trade_date, e.code
            """,
            (
                lookback_raw,
                end_raw,
                start_raw,
                end_raw,
                broad_min_amount,
                *regimes,
                broad_min_ret20,
                broad_max_dist,
            ),
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
        ma5 = float(r["ma5"])
        ma10 = float(r["ma10"])
        ma20 = float(r["ma20"])
        ma60 = float(r["ma60"])
        close20 = float(r["close20"])
        close60 = float(r["close60"])
        vol5_prev = float(r["vol5_prev"] or 0)
        volume_ratio = float(r["vol"] or 0) / vol5_prev if vol5_prev > 0 else None
        ret20 = (close / close20 - 1) * 100
        ret60 = (close / close60 - 1) * 100
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
                "open": float(r["open"] or 0),
                "high": float(r["high"] or 0),
                "low": float(r["low"] or 0),
                "close": close,
                "pct_chg": float(r["pct_chg"] or 0),
                "amount_yi": float(r["amount"] or 0) / 100_000,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20,
                "ma60": ma60,
                "dist_ma5_pct": (close / ma5 - 1) * 100,
                "ma10_ma20_spread_pct": (ma10 / ma20 - 1) * 100 if ma20 > 0 else 0,
                "ma20_ma60_spread_pct": (ma20 / ma60 - 1) * 100 if ma60 > 0 else 0,
                "ret20_pct": ret20,
                "ret60_pct": ret60,
                "rel20_pct": ret20 - idx_ret20,
                "volume_ratio": volume_ratio,
                "total_score": none_to_int(r["total_score"]),
                "score_advance_decline": none_to_int(r["score_advance_decline"]),
                "score_sentiment_index": none_to_int(r["score_sentiment_index"]),
                "score_volume_trend": none_to_int(r["score_volume_trend"]),
                "regime_code": r["regime_code"],
                "switched": bool(r["switched"]),
                "emergency_switch": bool(r["emergency_switch"]),
            }
        )
    return features


def none_to_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def load_bars_for_features(
    features: list[dict],
    start: str,
    end: str,
) -> dict[str, dict[str, dict]]:
    codes = sorted({row["code"] for row in features})
    if not codes:
        return {}
    start_raw = iso_to_yyyymmdd(shift_trading_days(start, -30))
    end_raw = iso_to_yyyymmdd(shift_trading_days(end, EXIT_EXTRA_DAYS))
    by_code: dict[str, list[dict]] = defaultdict(list)
    conn = get_conn()
    try:
        for i in range(0, len(codes), 800):
            chunk = codes[i : i + 800]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""
                SELECT d.trade_date,
                       substr(d.ts_code, 1, 6) AS code,
                       d.open,
                       d.high,
                       d.low,
                       d.close,
                       d.pre_close,
                       d.pct_chg,
                       l.up_limit,
                       l.down_limit
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
            bar["ma5"] = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
            bar["ma10"] = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
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
    date_index: dict[str, int],
    cfg: dict,
) -> dict:
    trades = []
    candidate_days = 0
    last_entry_idx_by_code: dict[str, int] = {}

    for date in sorted(daily_features):
        candidates = []
        today_idx = date_index.get(date)
        for row in daily_features[date]:
            if not pass_config(row, cfg):
                continue
            cooldown = int(cfg["cooldown_days"])
            if cooldown > 0 and today_idx is not None:
                last_idx = last_entry_idx_by_code.get(row["code"])
                if last_idx is not None and today_idx - last_idx <= cooldown:
                    continue
            candidates.append(row)
        if not candidates:
            continue
        candidate_days += 1
        candidates.sort(key=signal_score, reverse=True)
        for row in candidates[: int(cfg["top_n"])]:
            trade = simulate_trade(row, bars_by_code.get(row["code"], {}), cfg)
            if trade is None:
                continue
            trades.append(trade)
            if trade["triggered"] and today_idx is not None:
                last_entry_idx_by_code[row["code"]] = today_idx
    return summarize_trades(trades, candidate_days)


def pass_config(row: dict, cfg: dict) -> bool:
    if row["regime_code"] not in cfg["regimes"]:
        return False
    if cfg["min_total_score"] is not None and safe_score(row["total_score"]) < cfg["min_total_score"]:
        return False
    if cfg["min_volume_trend"] is not None and safe_score(row["score_volume_trend"]) < cfg["min_volume_trend"]:
        return False
    if cfg["min_advance_decline"] is not None and safe_score(row["score_advance_decline"]) < cfg["min_advance_decline"]:
        return False
    if cfg["min_sentiment_index"] is not None and safe_score(row["score_sentiment_index"]) < cfg["min_sentiment_index"]:
        return False
    if not cfg["allow_switched"] and row["switched"]:
        return False
    if not cfg["allow_emergency"] and row["emergency_switch"]:
        return False
    if row["amount_yi"] < cfg["min_amount_yi"]:
        return False
    if row["ret20_pct"] < cfg["min_ret20_pct"]:
        return False
    if row["rel20_pct"] < cfg["min_rel20_pct"]:
        return False
    if row["dist_ma5_pct"] < cfg["min_dist_ma5_pct"] or row["dist_ma5_pct"] > cfg["max_dist_ma5_pct"]:
        return False
    if cfg["min_volume_ratio"] is not None:
        if row["volume_ratio"] is None or row["volume_ratio"] < cfg["min_volume_ratio"]:
            return False
    if cfg["max_pct_chg"] is not None and row["pct_chg"] > cfg["max_pct_chg"]:
        return False
    if cfg["min_pct_chg"] is not None and row["pct_chg"] < cfg["min_pct_chg"]:
        return False
    if (
        cfg["min_ma10_ma20_spread_pct"] is not None
        and row["ma10_ma20_spread_pct"] < cfg["min_ma10_ma20_spread_pct"]
    ):
        return False
    if (
        cfg["min_ma20_ma60_spread_pct"] is not None
        and row["ma20_ma60_spread_pct"] < cfg["min_ma20_ma60_spread_pct"]
    ):
        return False
    return True


def safe_score(value: int | None) -> int:
    return -99 if value is None else int(value)


def signal_score(row: dict) -> float:
    volume_ratio = row["volume_ratio"] or 0
    volume_bonus = max(volume_ratio - 1.0, 0) * 4
    dist_bonus = max(0, 4.0 - row["dist_ma5_pct"]) * 3
    pct_penalty = max(row["pct_chg"] - 7.0, 0) * 1.5
    return (
        row["ret20_pct"] * 1.35
        + row["rel20_pct"] * 1.8
        + row["ret60_pct"] * 0.25
        + dist_bonus
        + min(row["amount_yi"], 30) * 0.25
        + volume_bonus
        - pct_penalty
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
    take_price = None
    if cfg["profit_take_pct"] is not None:
        take_price = entry_price * (1 + cfg["profit_take_pct"] / 100)

    future = bars_after(t1["date"], bars, int(cfg["max_hold_days"]) + EXIT_EXTRA_DAYS)
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
        if take_price is not None:
            if bar["open"] >= take_price:
                return exited_trade("profit_open", row, t1, bar, entry_price, bar["open"], hold_days)
            if bar["high"] >= take_price:
                return exited_trade("profit_intraday", row, t1, bar, entry_price, take_price, hold_days)
        exit_ma = cfg["exit_ma"]
        if exit_ma:
            ma_value = bar.get(exit_ma)
            if ma_value is not None and bar["close"] < ma_value:
                return exited_trade(f"{exit_ma}_lost", row, t1, bar, entry_price, bar["close"], hold_days)
        if deadline:
            return exited_trade("max_hold", row, t1, bar, entry_price, bar["close"], hold_days)

    last = future[-1]
    if is_one_word_down(last):
        return None
    return exited_trade("forced_after_deadline", row, t1, last, entry_price, last["open"], len(future))


def next_bar_after(date: str, bars: dict[str, dict]) -> dict | None:
    for day in sorted(bars):
        if day > date:
            return bars[day]
    return None


def bars_after(date: str, bars: dict[str, dict], limit: int) -> list[dict]:
    result = []
    for day in sorted(bars):
        if day >= date:
            result.append(bars[day])
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
    return {
        "triggered": True,
        "status": status,
        "date": row["date"],
        "code": row["code"],
        "t1_date": t1["date"],
        "exit_date": exit_bar["date"],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": (exit_price / entry_price - 1) * 100,
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
    triggered = [trade for trade in trades if trade["triggered"] and trade["pnl_pct"] is not None]
    pnls = [trade["pnl_pct"] for trade in triggered]
    status_counts = defaultdict(int)
    for trade in trades:
        status_counts[trade["status"]] += 1
    return {
        "candidate_days": candidate_days,
        "candidates": len(trades),
        "triggered": len(triggered),
        "win_rate": sum(1 for pnl in pnls if pnl > 0) / len(pnls) if pnls else 0,
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
        "median_pnl": median(pnls) if pnls else 0,
        "min_pnl": min(pnls) if pnls else 0,
        "max_pnl": max(pnls) if pnls else 0,
        "avg_hold_days": sum(trade["hold_days"] for trade in triggered) / len(triggered) if triggered else 0,
        "status_counts": dict(status_counts),
    }


def aggregate_result(name: str, cfg: dict, summaries: list[dict]) -> dict:
    total_triggered = sum(item["triggered"] for item in summaries)
    weighted = weighted_metrics(summaries, total_triggered)
    worst_avg = min((item["avg_pnl"] for item in summaries), default=0)
    worst_med = min((item["median_pnl"] for item in summaries), default=0)
    avg_win_med = sum(item["median_pnl"] for item in summaries) / len(summaries) if summaries else 0
    return {
        "name": name,
        "group": cfg["group"],
        "mode": cfg["mode"],
        "config": cfg,
        "windows": summaries,
        "triggered": total_triggered,
        "candidate_days": sum(item["candidate_days"] for item in summaries),
        "avg_pnl": weighted["avg_pnl"],
        "win_rate": weighted["win_rate"],
        "avg_hold_days": weighted["avg_hold_days"],
        "avg_win_med": avg_win_med,
        "worst_avg": worst_avg,
        "worst_med": worst_med,
    }


def weighted_metrics(summaries: list[dict], total_triggered: int) -> dict[str, float]:
    if total_triggered <= 0:
        return {"avg_pnl": 0.0, "win_rate": 0.0, "avg_hold_days": 0.0}
    return {
        "avg_pnl": sum(item["avg_pnl"] * item["triggered"] for item in summaries) / total_triggered,
        "win_rate": sum(item["win_rate"] * item["triggered"] for item in summaries) / total_triggered,
        "avg_hold_days": sum(item["avg_hold_days"] * item["triggered"] for item in summaries) / total_triggered,
    }


def rank_score(row: dict) -> float:
    min_count = 70 if row["mode"] == "trend" else 20
    count_penalty = min(row["triggered"] / min_count, 1.0)
    tiny_sample_drag = -2.0 if row["triggered"] < min_count * 0.6 else 0.0
    return (
        row["avg_pnl"] * 1.1
        + row["avg_win_med"] * 1.0
        + row["worst_avg"] * 1.25
        + row["worst_med"] * 0.5
        + row["win_rate"] * 1.5
        + tiny_sample_drag
    ) * count_penalty


def print_result_header() -> None:
    print(
        "rank  name                         grp        trd  win%   avg%   medW% worstA% worstM% hold  key"
    )


def print_result_row(row: dict) -> None:
    cfg = row["config"]
    print(
        f"{rank_score(row):5.2f} "
        f"{row['name'][:28]:28s} "
        f"{row['group'][:10]:10s} "
        f"{row['triggered']:4d} "
        f"{row['win_rate']*100:5.1f} "
        f"{row['avg_pnl']:6.2f} "
        f"{row['avg_win_med']:6.2f} "
        f"{row['worst_avg']:7.2f} "
        f"{row['worst_med']:7.2f} "
        f"{row['avg_hold_days']:4.1f} "
        f"{compact_key(cfg)}"
    )


def compact_key(cfg: dict) -> str:
    bits: list[str] = [
        ",".join(cfg["regimes"]),
        f"amt>={cfg['min_amount_yi']:g}",
        f"r20>={cfg['min_ret20_pct']:g}",
        f"rel>={cfg['min_rel20_pct']:g}",
        f"dist={cfg['min_dist_ma5_pct']:g}-{cfg['max_dist_ma5_pct']:g}",
        f"hold={cfg['max_hold_days']}",
        f"stop={cfg['stop_pct']:g}",
    ]
    optional_keys = (
        ("min_volume_ratio", "volr>="),
        ("max_pct_chg", "pct<="),
        ("min_pct_chg", "pct>="),
        ("min_total_score", "tot>="),
        ("min_volume_trend", "vtrend>="),
        ("min_advance_decline", "adv>="),
        ("min_sentiment_index", "sent>="),
        ("min_ma10_ma20_spread_pct", "m10/20>="),
        ("min_ma20_ma60_spread_pct", "m20/60>="),
        ("profit_take_pct", "take="),
    )
    for key, label in optional_keys:
        if cfg.get(key) is not None:
            bits.append(f"{label}{cfg[key]:g}")
    if not cfg["allow_switched"]:
        bits.append("no_switch")
    if not cfg["allow_emergency"]:
        bits.append("no_emerg")
    if cfg["cooldown_days"]:
        bits.append(f"cool={cfg['cooldown_days']}")
    return " ".join(bits)


if __name__ == "__main__":
    main()
