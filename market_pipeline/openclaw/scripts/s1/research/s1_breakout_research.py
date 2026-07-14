#!/usr/bin/env python3
"""S1 breakout research harness.

Research only. Does not write production tables.
Runs one process over local market.db daily/stk_limit/regime_classify_daily.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from dataclasses import dataclass, asdict

DB = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))
PRICE_EPS = 0.01


@dataclass
class Config:
    base_window: int = 20
    max_base_range_pct: float = 18.0
    min_breakout_pct: float = 1.0
    min_volume_ratio: float = 1.25
    max_t_pct: float = 10.5
    min_amount_yi: float = 1.0
    entry_low_pct: float = -1.0
    entry_high_pct: float = 2.0
    stop_pct: float = -8.0
    failure_buffer_pct: float = 0.0
    take_profit_pct: float = 0.0
    max_hold_days: int = 5
    max_candidates_per_day: int = 5
    min_ret20_pct: float = 0.0
    min_rel20_pct: float = 0.0
    min_ret60_pct: float = 0.0
    regimes_csv: str = "STRONG_BULL"
    min_regime_score: int | None = None


@dataclass
class ResearchData:
    start: str
    end: str
    days: list[str]
    regime_by_day: dict[str, dict]
    by_code: dict[str, list[dict]]
    date_indexes: dict[str, dict[str, int]]
    index_closes: dict[str, float]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def iso_to_raw(d: str) -> str:
    return d.replace("-", "")


def raw_to_iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def trading_days(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM daily
        WHERE trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (iso_to_raw(start), iso_to_raw(end)),
    ).fetchall()
    return [raw_to_iso(r["trade_date"]) for r in rows]


def warm_start_date(conn: sqlite3.Connection, start: str, warmup_days: int) -> str:
    rows = conn.execute(
        """
        SELECT MIN(trade_date) AS d
        FROM (
            SELECT DISTINCT trade_date FROM daily
            WHERE trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT ?
        )
        """,
        (iso_to_raw(start), max(1, warmup_days)),
    ).fetchone()
    if not rows or not rows["d"]:
        return start
    return raw_to_iso(rows["d"])


def load_regime_rows(conn: sqlite3.Connection, start: str, end: str) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT trade_date, regime_code, total_score, confidence
        FROM regime_classify_daily
        WHERE rules_version='v2'
          AND trade_date BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchall()
    return {
        r["trade_date"]: {
            "regime_code": r["regime_code"],
            "total_score": int(r["total_score"] or 0),
            "confidence": r["confidence"],
        }
        for r in rows
    }


def load_daily_window(conn: sqlite3.Connection, start: str, end: str) -> dict[str, list[dict]]:
    rows = conn.execute(
        """
        SELECT d.trade_date, substr(d.ts_code,1,6) AS code,
               d.open, d.high, d.low, d.close, d.pct_chg, d.vol, d.amount,
               l.up_limit, l.down_limit
        FROM daily d
        LEFT JOIN stk_limit l ON l.trade_date=d.trade_date AND l.ts_code=d.ts_code
        WHERE d.trade_date BETWEEN ? AND ?
        ORDER BY d.ts_code, d.trade_date
        """,
        (iso_to_raw(start), iso_to_raw(end)),
    ).fetchall()
    by_code: dict[str, list[dict]] = {}
    for r in rows:
        close = float(r["close"] or 0)
        up_limit = r["up_limit"]
        down_limit = r["down_limit"]
        by_code.setdefault(r["code"], []).append({
            "date": raw_to_iso(r["trade_date"]),
            "open": float(r["open"] or 0),
            "high": float(r["high"] or 0),
            "low": float(r["low"] or 0),
            "close": close,
            "pct_chg": float(r["pct_chg"] or 0),
            "vol": float(r["vol"] or 0),
            "amount_yi": float(r["amount"] or 0) / 100000.0,
            "up_limit": float(up_limit) if up_limit is not None else None,
            "down_limit": float(down_limit) if down_limit is not None else None,
            "is_one_word_up": bool(up_limit is not None and close >= float(up_limit) - PRICE_EPS and abs(float(r["high"] or 0) - float(r["low"] or 0)) < PRICE_EPS),
            "is_one_word_down": bool(down_limit is not None and close <= float(down_limit) + PRICE_EPS and abs(float(r["high"] or 0) - float(r["low"] or 0)) < PRICE_EPS),
        })
    return by_code


def load_index_closes(conn: sqlite3.Connection, start: str, end: str, index_code: str) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT trade_date, close FROM index_daily
        WHERE ts_code=? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (index_code, iso_to_raw(start), iso_to_raw(end)),
    ).fetchall()
    return {
        raw_to_iso(r["trade_date"]): float(r["close"] or 0)
        for r in rows
        if float(r["close"] or 0) > 0
    }


def load_research_data(start: str, end: str, warmup_days: int = 90) -> ResearchData:
    return load_research_data_with_index(start, end, warmup_days, "000852.SH")


def load_research_data_with_index(
    start: str,
    end: str,
    warmup_days: int = 90,
    index_code: str = "000852.SH",
) -> ResearchData:
    conn = connect()
    try:
        days = trading_days(conn, start, end)
        regime_by_day = load_regime_rows(conn, start, end)
        if not days:
            return ResearchData(start, end, [], regime_by_day, {}, {}, {})
        warm_start = warm_start_date(conn, start, warmup_days)
        by_code = load_daily_window(conn, warm_start, end)
        index_closes = load_index_closes(conn, warm_start, end, index_code)
    finally:
        conn.close()
    date_indexes = {
        code: {bar["date"]: i for i, bar in enumerate(bars)}
        for code, bars in by_code.items()
    }
    return ResearchData(start, end, days, regime_by_day, by_code, date_indexes, index_closes)


def ma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def pct_return(bars: list[dict], idx: int, lookback: int) -> float | None:
    if idx < lookback:
        return None
    base = bars[idx - lookback]["close"]
    if base <= 0:
        return None
    return (bars[idx]["close"] / base - 1) * 100


def index_return(index_closes: dict[str, float], start_date: str, end_date: str) -> float | None:
    start = index_closes.get(start_date)
    end = index_closes.get(end_date)
    if not start or not end:
        return None
    return (end / start - 1) * 100


def parse_regimes(regimes_csv: str) -> set[str]:
    return {r.strip() for r in regimes_csv.split(",") if r.strip()}


def eligible_regime(day: str, data: ResearchData, cfg: Config) -> bool:
    regime = data.regime_by_day.get(day)
    if not regime:
        return False
    allowed = parse_regimes(cfg.regimes_csv)
    if allowed and regime["regime_code"] not in allowed:
        return False
    if cfg.min_regime_score is not None and regime["total_score"] < cfg.min_regime_score:
        return False
    return True


def detect(code: str, bars: list[dict], idx: int, cfg: Config, index_closes: dict[str, float]) -> dict | None:
    if idx < 60 or idx < cfg.base_window + 1:
        return None
    t = bars[idx]
    if t["is_one_word_up"] or t["pct_chg"] > cfg.max_t_pct or t["amount_yi"] < cfg.min_amount_yi:
        return None
    history = bars[:idx + 1]
    closes = [b["close"] for b in history]
    ma20 = ma(closes, 20)
    ma60 = ma(closes, 60)
    if ma20 is None or ma60 is None or not (t["close"] >= ma20 >= ma60):
        return None
    ret20 = pct_return(bars, idx, 20)
    ret60 = pct_return(bars, idx, 60)
    if ret20 is None or ret60 is None:
        return None
    if ret20 < cfg.min_ret20_pct or ret60 < cfg.min_ret60_pct:
        return None
    idx_ret20 = index_return(index_closes, bars[idx - 20]["date"], t["date"])
    rel20 = ret20 - idx_ret20 if idx_ret20 is not None else None
    if cfg.min_rel20_pct and (rel20 is None or rel20 < cfg.min_rel20_pct):
        return None
    base = bars[idx - cfg.base_window:idx]
    base_high = max(b["high"] for b in base)
    base_low = min(b["low"] for b in base)
    if base_low <= 0:
        return None
    base_range = (base_high - base_low) / base_low * 100
    if base_range > cfg.max_base_range_pct:
        return None
    breakout = (t["close"] - base_high) / base_high * 100 if base_high else 0
    if breakout < cfg.min_breakout_pct:
        return None
    vols = [b["vol"] for b in bars[idx - 5:idx]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    volume_ratio = t["vol"] / avg_vol if avg_vol else 0
    if volume_ratio < cfg.min_volume_ratio:
        return None
    score = breakout * 6 + min(volume_ratio, 3.0) * 14 + max(0, 24 - base_range) + min(t["amount_yi"], 10) * 2
    return {
        "code": code,
        "date": t["date"],
        "close": t["close"],
        "base_high": base_high,
        "base_low": base_low,
        "base_range_pct": base_range,
        "breakout_pct": breakout,
        "volume_ratio": volume_ratio,
        "ret20_pct": ret20,
        "ret60_pct": ret60,
        "rel20_pct": rel20,
        "score": score,
    }


def plan_entry(signal: dict, t1: dict, cfg: Config) -> dict:
    close = signal["close"]
    entry_low = close * (1 + cfg.entry_low_pct / 100)
    entry_high = close * (1 + cfg.entry_high_pct / 100)
    if t1["is_one_word_up"]:
        return {"status": "limit_up_buy_blocked", "entry": None}
    if t1["up_limit"] is not None:
        entry_high = min(entry_high, t1["up_limit"] - PRICE_EPS)
        if entry_low > entry_high:
            return {"status": "limit_up_buy_blocked", "entry": None}

    if entry_low <= t1["open"] <= entry_high:
        entry = t1["open"]
    elif t1["open"] < entry_low <= t1["high"]:
        entry = entry_low
    elif t1["open"] > entry_high and t1["low"] <= entry_high:
        entry = entry_high
    elif t1["open"] > entry_high:
        return {"status": "gap_up_skip", "entry": None}
    else:
        return {"status": "weak_open_skip", "entry": None}

    if entry < t1["low"] - PRICE_EPS or entry > t1["high"] + PRICE_EPS:
        return {"status": "entry_not_traded", "entry": None}
    if t1["up_limit"] is not None and entry >= t1["up_limit"] - PRICE_EPS:
        return {"status": "limit_up_buy_blocked", "entry": None}
    return {"status": "filled", "entry": entry}


def stop_exit_price(bar: dict, stop: float) -> float:
    if bar["open"] <= stop:
        return bar["open"]
    return stop


def verify(signal: dict, bars: list[dict], idx: int, cfg: Config) -> dict:
    entry_idx = idx + 1
    if entry_idx >= len(bars) or entry_idx + 1 >= len(bars):
        return {"status": "no_data", "pnl_pct": None}
    t1 = bars[entry_idx]
    entry_plan = plan_entry(signal, t1, cfg)
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
            exit_price = stop_exit_price(bar, stop)
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


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def summarize_group(rows: list[dict]) -> dict:
    pnls = [r["pnl_pct"] for r in rows if r.get("pnl_pct") is not None]
    status_counts: dict[str, int] = {}
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    return {
        "candidates": len(rows),
        "triggered": len(pnls),
        "win_rate": sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0,
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
        "median_pnl": quantile(pnls, 0.5),
        "p25_pnl": quantile(pnls, 0.25),
        "p75_pnl": quantile(pnls, 0.75),
        "max_pnl": max(pnls) if pnls else 0,
        "min_pnl": min(pnls) if pnls else 0,
        "status_counts": status_counts,
    }


def run_cached(data: ResearchData, cfg: Config) -> dict:
    if not data.days:
        return {"summary": {"error": "no trading days"}, "signals": []}
    signals = []
    results = []
    eligible_days = [day for day in data.days if eligible_regime(day, data, cfg)]
    eligible_day_set = set(eligible_days)
    eligible_regime_counts: dict[str, int] = {}
    for day in eligible_days:
        code = data.regime_by_day[day]["regime_code"]
        eligible_regime_counts[code] = eligible_regime_counts.get(code, 0) + 1
    for code, bars in data.by_code.items():
        date_to_idx = data.date_indexes[code]
        for day in data.days:
            if day not in eligible_day_set or day not in date_to_idx:
                continue
            idx = date_to_idx[day]
            sig = detect(code, bars, idx, cfg, data.index_closes)
            if sig:
                regime = data.regime_by_day[day]
                sig["regime_code"] = regime["regime_code"]
                sig["regime_score"] = regime["total_score"]
                signals.append(sig)

    by_day: dict[str, list[dict]] = {}
    for sig in signals:
        by_day.setdefault(sig["date"], []).append(sig)
    trimmed = []
    for day, rows in by_day.items():
        trimmed.extend(sorted(rows, key=lambda r: r["score"], reverse=True)[:cfg.max_candidates_per_day])

    for sig in trimmed:
        bars = data.by_code[sig["code"]]
        idx = data.date_indexes[sig["code"]][sig["date"]]
        v = verify(sig, bars, idx, cfg)
        results.append({**sig, **v})

    pnls = [r["pnl_pct"] for r in results if r.get("pnl_pct") is not None]
    hold_days = [r["hold_days"] for r in results if r.get("hold_days") is not None]
    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    by_regime: dict[str, list[dict]] = {}
    for r in results:
        by_regime.setdefault(r.get("regime_code", "UNKNOWN"), []).append(r)
    summary = {
        "start": data.start,
        "end": data.end,
        "config": asdict(cfg),
        "eligible_days": len(eligible_days),
        "eligible_regime_counts": eligible_regime_counts,
        "signal_days": len(by_day),
        "raw_signals": len(signals),
        "candidates": len(trimmed),
        "triggered": len(pnls),
        "win_rate": sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0,
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
        "median_pnl": quantile(pnls, 0.5),
        "p25_pnl": quantile(pnls, 0.25),
        "p75_pnl": quantile(pnls, 0.75),
        "max_pnl": max(pnls) if pnls else 0,
        "min_pnl": min(pnls) if pnls else 0,
        "avg_hold_days": sum(hold_days) / len(hold_days) if hold_days else 0,
        "status_counts": status_counts,
        "by_regime": {
            regime_code: summarize_group(rows)
            for regime_code, rows in sorted(by_regime.items())
        },
        "execution_model": "signal_after_close_t_buy_t1_sell_from_t2_limit_lock_conservative_v2",
    }
    return {"summary": summary, "signals": results}


def run(start: str, end: str, cfg: Config, warmup_days: int = 90, index_code: str = "000852.SH") -> dict:
    return run_cached(load_research_data_with_index(start, end, warmup_days, index_code), cfg)


def grid_configs() -> list[Config]:
    configs = []
    for base_window in (20, 30):
        for max_base_range_pct in (10.0, 14.0):
            for min_breakout_pct in (2.0, 3.0):
                configs.append(Config(
                    base_window=base_window,
                    max_base_range_pct=max_base_range_pct,
                    min_breakout_pct=min_breakout_pct,
                ))
    return configs


def strength_grid_configs() -> list[Config]:
    return [
        Config(base_window=20, max_base_range_pct=14.0, min_breakout_pct=2.0, min_ret20_pct=5.0),
        Config(base_window=20, max_base_range_pct=14.0, min_breakout_pct=2.0, min_ret20_pct=10.0),
        Config(base_window=20, max_base_range_pct=14.0, min_breakout_pct=2.0, min_ret20_pct=10.0, min_rel20_pct=5.0),
        Config(base_window=20, max_base_range_pct=10.0, min_breakout_pct=2.0, min_ret20_pct=10.0, min_rel20_pct=5.0),
        Config(base_window=30, max_base_range_pct=10.0, min_breakout_pct=2.0, min_ret20_pct=10.0, min_rel20_pct=5.0),
        Config(base_window=20, max_base_range_pct=14.0, min_breakout_pct=3.0, min_ret20_pct=15.0, min_rel20_pct=8.0),
    ]


def print_grid_result(i: int, total: int, cfg: Config, summary: dict, started: float) -> None:
    print(
        f"[{i}/{total}] base={cfg.base_window} range<={cfg.max_base_range_pct:.0f} "
        f"breakout>={cfg.min_breakout_pct:.0f} ret20>={cfg.min_ret20_pct:.0f} "
        f"rel20>={cfg.min_rel20_pct:.0f}: cand={summary['candidates']} "
        f"trig={summary['triggered']} win={summary['win_rate']:.1%} "
        f"avg={summary['avg_pnl']:+.2f}% elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-09-01")
    ap.add_argument("--end", default="2025-03-31")
    ap.add_argument("--output", default="")
    ap.add_argument("--warmup-days", type=int, default=90)
    ap.add_argument("--index-code", default="000852.SH")
    ap.add_argument("--base-window", type=int, default=20)
    ap.add_argument("--max-base-range-pct", type=float, default=18.0)
    ap.add_argument("--min-breakout-pct", type=float, default=1.0)
    ap.add_argument("--min-volume-ratio", type=float, default=1.25)
    ap.add_argument("--entry-low-pct", type=float, default=-1.0)
    ap.add_argument("--entry-high-pct", type=float, default=2.0)
    ap.add_argument("--stop-pct", type=float, default=-8.0)
    ap.add_argument("--max-hold-days", type=int, default=5)
    ap.add_argument("--min-ret20-pct", type=float, default=0.0)
    ap.add_argument("--min-rel20-pct", type=float, default=0.0)
    ap.add_argument("--min-ret60-pct", type=float, default=0.0)
    ap.add_argument("--failure-buffer-pct", type=float, default=0.0)
    ap.add_argument("--take-profit-pct", type=float, default=0.0)
    ap.add_argument("--regimes", default="STRONG_BULL", help="Comma-separated regime_code allowlist.")
    ap.add_argument("--min-regime-score", type=int, default=None)
    ap.add_argument("--grid", action="store_true", help="Run a small sequential grid on key S1 parameters.")
    ap.add_argument("--strength-grid", action="store_true", help="Run a small sequential grid with strong-stock filters.")
    args = ap.parse_args()
    started = time.monotonic()
    data = load_research_data_with_index(args.start, args.end, args.warmup_days, args.index_code)
    if args.grid or args.strength_grid:
        summaries = []
        configs = strength_grid_configs() if args.strength_grid else grid_configs()
        for cfg in configs:
            cfg.regimes_csv = args.regimes
            cfg.min_regime_score = args.min_regime_score
        print(
            f"loaded codes={len(data.by_code)} days={len(data.days)} "
            f"eligible_days={sum(1 for day in data.days if eligible_regime(day, data, configs[0])) if configs else 0} "
            f"index={args.index_code} grid={len(configs)} elapsed={time.monotonic() - started:.1f}s",
            flush=True,
        )
        for i, cfg in enumerate(configs, start=1):
            summary = run_cached(data, cfg)["summary"]
            summaries.append(summary)
            print_grid_result(i, len(configs), cfg, summary, started)
        ranked = sorted(summaries, key=lambda s: (s["avg_pnl"], s["triggered"]), reverse=True)
        print("\nTOP")
        for s in ranked[:10]:
            c = s["config"]
            print(
                f"base={c['base_window']} range<={c['max_base_range_pct']:.0f} "
                f"breakout>={c['min_breakout_pct']:.0f} ret20>={c['min_ret20_pct']:.0f} "
                f"rel20>={c['min_rel20_pct']:.0f}: "
                f"cand={s['candidates']} trig={s['triggered']} win={s['win_rate']:.1%} avg={s['avg_pnl']:+.2f}% "
                f"min={s['min_pnl']:+.2f}% max={s['max_pnl']:+.2f}%"
            )
        result = {"summary": ranked[0] if ranked else {"error": "no grid results"}, "signals": []}
    else:
        cfg = Config(
            base_window=args.base_window,
            max_base_range_pct=args.max_base_range_pct,
            min_breakout_pct=args.min_breakout_pct,
            min_volume_ratio=args.min_volume_ratio,
            entry_low_pct=args.entry_low_pct,
            entry_high_pct=args.entry_high_pct,
            stop_pct=args.stop_pct,
            max_hold_days=args.max_hold_days,
            min_ret20_pct=args.min_ret20_pct,
            min_rel20_pct=args.min_rel20_pct,
            min_ret60_pct=args.min_ret60_pct,
            failure_buffer_pct=args.failure_buffer_pct,
            take_profit_pct=args.take_profit_pct,
            regimes_csv=args.regimes,
            min_regime_score=args.min_regime_score,
        )
        result = run_cached(data, cfg)
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            if result["signals"]:
                fieldnames = sorted({k for row in result["signals"] for k in row.keys()})
            else:
                fieldnames = ["empty"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(result["signals"])


if __name__ == "__main__":
    main()
