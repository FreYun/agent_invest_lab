"""S2 T+1 验证与历史回测."""

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
from strategy_common import fetch_followup_bars, shift_trading_days

MAX_HOLD_DAYS = 3


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def verify_one_backtest(candidate: dict, bars: list[dict]) -> dict:
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
    stop_loss = candidate["stop_loss"]["price"]
    open_price = t1["open"]
    prev_close = candidate["t_bar"]["close"]
    t1_open_gap_pct = round((open_price - prev_close) / prev_close * 100, 2) if prev_close else None

    if _is_limit_up_open(t1):
        return _skip(
            "limit_up_open_skip",
            t1,
            f"T+1 开盘涨停 {open_price:.2f}, 排队成交不可验证, 保守跳过",
        )
    if open_price > entry_high:
        return _skip("gap_up_skip", t1, f"T+1 开盘 {open_price:.2f} > 入场区上沿 {entry_high:.2f}")
    if open_price < entry_low and t1["high"] < entry_low:
        return _skip("weak_open_skip", t1, f"T+1 低开 {open_price:.2f} 且日内未收回入场区")

    if entry_low <= open_price <= entry_high:
        entry_price = open_price
        entry_status = "triggered_at_open"
    else:
        if entry_low > float(t1.get("up_limit") or entry_low):
            return _skip("entry_unfillable", t1, f"入场价 {entry_low:.2f} 高于 T+1 涨停价")
        entry_price = entry_low
        entry_status = "triggered_reclaim"

    # T+2 起才允许卖出；T+1 仅作为买入日
    for idx, bar in enumerate(bars[1:MAX_HOLD_DAYS], start=2):
        if _is_limit_down_locked(bar):
            continue

        if bar["low"] <= stop_loss:
            exit_price, exit_reason = _stop_exit_price(bar, stop_loss)
            if exit_price is None:
                continue
            return _exit("stop_hit", entry_status, entry_price, exit_price, exit_reason, bar, idx, t1_open_gap_pct)

        up_limit = bar.get("up_limit")
        if up_limit is not None:
            closed_limit = bar["close"] >= up_limit - 0.01
            touched_limit = bar["high"] >= up_limit - 0.01
            if closed_limit:
                if idx < MAX_HOLD_DAYS and idx < len(bars):
                    continue
                return _exit("limit_follow_close", entry_status, entry_price, bar["close"], "max_hold_reached", bar, idx, t1_open_gap_pct)
            if touched_limit and not closed_limit:
                return _exit("limit_up_broken", entry_status, entry_price, bar["close"], "limit_up_broken", bar, idx, t1_open_gap_pct)

        if _is_limit_down_close_unfillable(bar):
            continue
        return _exit("no_limit_exit", entry_status, entry_price, bar["close"], "T+2 未封板退出", bar, idx, t1_open_gap_pct)

    last = bars[min(len(bars), MAX_HOLD_DAYS) - 1]
    if _is_limit_down_locked(last):
        return {
            "status": "limit_down_unfilled",
            "entry_status": entry_status,
            "entry_price": round(entry_price, 2),
            "exit_price": None,
            "exit_reason": "max_hold_limit_down_unfilled",
            "pnl_pct": None,
            "t1_open": None,
            "t1_high": None,
            "t1_low": None,
            "t1_close": None,
            "t1_open_gap_pct": t1_open_gap_pct,
            "hold_days": min(len(bars), MAX_HOLD_DAYS),
            "exit_date": last.get("date"),
            "note": "达到最长持仓日但跌停封死, 保守不假设卖出成交",
        }
    return _exit("max_hold_reached", entry_status, entry_price, last["close"], "max_hold_reached", last, min(len(bars), MAX_HOLD_DAYS), t1_open_gap_pct)


def _skip(status: str, bar: dict, note: str) -> dict:
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
        "t1_open_gap_pct": None,
        "hold_days": 0,
        "exit_date": None,
        "note": note,
    }


def _is_limit_up_open(bar: dict) -> bool:
    up_limit = bar.get("up_limit")
    return up_limit is not None and bar["open"] >= float(up_limit) - 0.01


def _is_limit_down_locked(bar: dict) -> bool:
    down_limit = bar.get("down_limit")
    return down_limit is not None and bar["high"] <= float(down_limit) + 0.01


def _is_limit_down_close_unfillable(bar: dict) -> bool:
    down_limit = bar.get("down_limit")
    if down_limit is None:
        return False
    if bar["close"] > float(down_limit) + 0.01:
        return False
    return bar["high"] <= float(down_limit) + 0.01


def _stop_exit_price(bar: dict, stop_loss: float) -> tuple[float | None, str]:
    if _is_limit_down_locked(bar):
        return None, "stop_hit_limit_down_unfilled"
    if bar["high"] >= stop_loss:
        return stop_loss, "stop_reclaim_fill"
    if _is_limit_down_close_unfillable(bar):
        return None, "stop_close_limit_down_unfilled"
    return bar["close"], "stop_close_fill"


def _exit(status: str, entry_status: str, entry_price: float, exit_price: float, reason: str, bar: dict, hold_days: int, t1_open_gap_pct: float | None) -> dict:
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


def run_verify(t1_date: str, mode: str = "backtest") -> list:
    setup_logging()
    t_date = shift_trading_days(t1_date, -1)
    payload = read_select_run(t_date)
    if payload is None:
        write_verification(t1_date, t_date, [], mode)
        return []

    results = []
    for cand in payload.get("candidates", []):
        bars = fetch_followup_bars(cand["code"], t1_date, MAX_HOLD_DAYS)
        verification = verify_one_backtest(cand, bars)
        if bars and verification.get("t1_open") is None:
            verification["t1_open"] = bars[0]["open"]
            verification["t1_high"] = bars[0]["high"]
            verification["t1_low"] = bars[0]["low"]
            verification["t1_close"] = bars[0]["close"]
        logging.info("%s %s: %s pnl=%s", cand["code"], cand["name"], verification["status"], verification.get("pnl_pct"))
        results.append({"candidate": cand, "verification": verification})

    write_verification(t1_date, t_date, results, mode)
    return results


def verify_payload(payload: dict, t1_date: str) -> list:
    """Verify an in-memory select payload without reading or writing DB."""
    results = []
    for cand in payload.get("candidates", []):
        bars = fetch_followup_bars(cand["code"], t1_date, MAX_HOLD_DAYS)
        verification = verify_one_backtest(cand, bars)
        if bars and verification.get("t1_open") is None:
            verification["t1_open"] = bars[0]["open"]
            verification["t1_high"] = bars[0]["high"]
            verification["t1_low"] = bars[0]["low"]
            verification["t1_close"] = bars[0]["close"]
        results.append({"candidate": cand, "verification": verification})
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="T+1 日期 YYYY-MM-DD")
    ap.add_argument("--mode", choices=["backtest"], default="backtest")
    args = ap.parse_args()
    results = run_verify(args.date, mode=args.mode)
    print(f"S2 verify {args.date}: {len(results)} 条")


if __name__ == "__main__":
    main()
