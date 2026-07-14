"""S1 T+1 开盘入场验证与历史回测."""

from __future__ import annotations

import argparse
import logging
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_SCRIPTS = os.path.dirname(SCRIPT_DIR)
for path in (SCRIPT_DIR, ROOT_SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

from db_writer import read_select_run, write_verification
from signal_detector import DEFAULTS
from strategy_common import fetch_followup_bars, shift_trading_days


MAX_EXTRA_EXIT_DAYS = 5


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def verify_one_backtest(candidate: dict, bars: list[dict], config: dict | None = None) -> dict:
    cfg = {**DEFAULTS, **(config or {})}
    max_hold_days = int(cfg["max_hold_days"])
    if not bars:
        return {
            "status": "no_data",
            "entry_price": None,
            "exit_price": None,
            "pnl_pct": None,
            "hold_days": 0,
            "exit_date": None,
            "t1_open_gap_pct": None,
            "note": "T+1 起无 K 线数据",
        }
    if len(bars) < 2:
        return {
            "status": "no_data",
            "entry_price": None,
            "exit_price": None,
            "pnl_pct": None,
            "hold_days": 0,
            "exit_date": None,
            "t1_open_gap_pct": None,
            "note": "T+2 起无 K 线数据, 无法完成 A 股 T+1 约束下的卖出验证",
        }

    t1 = bars[0]
    entry_low = candidate["entry"]["zone_low"]
    entry_high = candidate["entry"]["zone_high"]
    open_price = t1["open"]
    prev_close = candidate["t_bar"]["close"]
    t1_open_gap_pct = round((open_price - prev_close) / prev_close * 100, 2) if prev_close else None

    if _is_one_word_up(t1):
        return _skip("limit_up_buy_blocked", t1, t1_open_gap_pct, "T+1 一字/极窄幅涨停, 开盘成交可执行性差")
    if open_price > entry_high:
        return _skip("gap_up_skip", t1, t1_open_gap_pct, f"T+1 开盘 {open_price:.2f} > 入场区上沿 {entry_high:.2f}")
    if open_price < entry_low:
        return _skip("gap_down_skip", t1, t1_open_gap_pct, f"T+1 开盘 {open_price:.2f} < 入场区下沿 {entry_low:.2f}")

    entry_price = open_price
    entry_status = "triggered_at_open"
    stop_loss = round(max(candidate["signal"]["base_low"] * 0.98, entry_price * (1 + float(cfg["stop_pct"]) / 100)), 2)
    failure_line = candidate["failure_line"]["price"]
    sell_bars = bars[1:max(len(bars), max_hold_days + 1)]

    for hold_days, bar in enumerate(sell_bars, start=2):
        is_deadline = hold_days >= max_hold_days
        if _is_one_word_down(bar):
            if is_deadline:
                continue
            continue
        if bar["open"] <= stop_loss:
            return _exit("stop_hit_at_open", entry_status, entry_price, bar["open"], "stop_hit_at_open", bar, hold_days, t1_open_gap_pct)
        if bar["low"] <= stop_loss:
            return _exit("stop_hit", entry_status, entry_price, stop_loss, "stop_hit", bar, hold_days, t1_open_gap_pct)
        if bar["close"] < failure_line:
            return _exit("failure_line_lost", entry_status, entry_price, bar["close"], "收盘跌回平台上沿下方", bar, hold_days, t1_open_gap_pct)
        if is_deadline:
            return _exit("max_hold_reached", entry_status, entry_price, bar["close"], "max_hold_reached", bar, hold_days, t1_open_gap_pct)

    last = bars[-1]
    if _is_one_word_down(last):
        return {
            "status": "exit_blocked",
            "entry_status": entry_status,
            "entry_price": round(entry_price, 2),
            "exit_price": None,
            "exit_reason": "one_word_down_blocked_after_deadline",
            "pnl_pct": None,
            "t1_open": t1["open"],
            "t1_high": t1["high"],
            "t1_low": t1["low"],
            "t1_close": t1["close"],
            "t1_open_gap_pct": t1_open_gap_pct,
            "hold_days": len(bars),
            "exit_date": None,
            "note": "到期后仍遇一字跌停/极窄幅跌停，无法假设成交",
        }
    return _exit(
        "forced_exit_after_deadline",
        entry_status,
        entry_price,
        last["open"],
        "deadline_extra_exit",
        last,
        len(bars),
        t1_open_gap_pct,
    )


def _skip(status: str, bar: dict, t1_open_gap_pct: float | None, note: str) -> dict:
    return {
        "status": status,
        "entry_price": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl_pct": None,
        "t1_open": bar["open"],
        "t1_high": bar["high"],
        "t1_low": bar["low"],
        "t1_close": bar["close"],
        "t1_open_gap_pct": t1_open_gap_pct,
        "hold_days": 0,
        "exit_date": None,
        "note": note,
    }


def _exit(
    status: str,
    entry_status: str,
    entry_price: float,
    exit_price: float,
    reason: str,
    bar: dict,
    hold_days: int,
    t1_open_gap_pct: float | None,
) -> dict:
    pnl = (exit_price - entry_price) / entry_price * 100
    return {
        "status": status,
        "entry_status": entry_status,
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "exit_reason": reason,
        "pnl_pct": round(pnl, 2),
        "t1_open": None,
        "t1_high": None,
        "t1_low": None,
        "t1_close": None,
        "t1_open_gap_pct": t1_open_gap_pct,
        "hold_days": hold_days,
        "exit_date": bar.get("date"),
        "note": f"持仓 {hold_days} 日 -> {reason} @ {exit_price:.2f}, 盈亏 {pnl:+.2f}%",
    }


def _is_one_word_up(bar: dict) -> bool:
    up_limit = bar.get("up_limit")
    if up_limit is not None and bar["close"] < up_limit - 0.01:
        return False
    return abs(bar["high"] - bar["low"]) < 0.01 and abs(bar["close"] - bar["open"]) < 0.01


def _is_one_word_down(bar: dict) -> bool:
    down_limit = bar.get("down_limit")
    if down_limit is not None and bar["close"] > down_limit + 0.01:
        return False
    return abs(bar["high"] - bar["low"]) < 0.01 and abs(bar["close"] - bar["open"]) < 0.01


def run_verify(t1_date: str, mode: str = "backtest", config: dict | None = None) -> list:
    setup_logging()
    cfg = {**DEFAULTS, **(config or {})}
    t_date = shift_trading_days(t1_date, -1)
    payload = read_select_run(t_date)
    if payload is None:
        write_verification(t1_date, t_date, [], mode)
        return []

    results = []
    n_days = max(int(cfg["max_hold_days"]) + MAX_EXTRA_EXIT_DAYS, 2)
    for candidate in payload.get("candidates", []):
        bars = fetch_followup_bars(candidate["code"], t1_date, n_days)
        verification = verify_one_backtest(candidate, bars, cfg)
        if bars and verification.get("t1_open") is None:
            verification["t1_open"] = bars[0]["open"]
            verification["t1_high"] = bars[0]["high"]
            verification["t1_low"] = bars[0]["low"]
            verification["t1_close"] = bars[0]["close"]
        logging.info("%s %s: %s pnl=%s", candidate["code"], candidate["name"], verification["status"], verification.get("pnl_pct"))
        results.append({"candidate": candidate, "verification": verification})

    write_verification(t1_date, t_date, results, mode)
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="T+1 日期 YYYY-MM-DD")
    ap.add_argument("--mode", choices=["backtest"], default="backtest")
    args = ap.parse_args()
    results = run_verify(args.date, mode=args.mode)
    print(f"S1 verify {args.date}: {len(results)} 条")


if __name__ == "__main__":
    main()
