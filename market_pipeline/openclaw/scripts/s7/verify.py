"""S7 T+1 verification and historical helper."""

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
from strategy_common import read_klines, shift_trading_days


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def verify_one_backtest(candidate: dict, bars: list[dict], t1_date: str | None = None, config: dict | None = None) -> dict:
    cfg = config or {}
    if not bars:
        return _no_data("T+1 no K line data")
    bars = sorted(bars, key=lambda item: item["date"])
    if t1_date is not None:
        t1_idx = next((idx for idx, bar in enumerate(bars) if bar["date"] >= t1_date), None)
        if t1_idx is None:
            return _no_data("T+1 no K line data")
        bars = bars[t1_idx:]
    if not bars:
        return _no_data("T+1 no K line data")

    t1 = bars[0]
    prev_close = float(candidate.get("t_bar", {}).get("close") or 0)
    open_price = float(t1["open"] or 0)
    gap_pct = round((open_price / prev_close - 1) * 100, 2) if prev_close > 0 else None

    if _is_one_word_up(t1):
        return _skip("limit_up_buy_blocked", t1, gap_pct, "T+1 one-word limit-up, not executable")
    if _is_down_limit_open(t1):
        return _skip("limit_down_open_skip", t1, gap_pct, "T+1 opens at/near down-limit, skip S7 entry")

    entry_price = open_price
    entry_status = "triggered_at_open"
    stop_price = entry_price * (1 + float(cfg.get("stop_pct", -5.0)) / 100)
    take_profit_price = entry_price * (1 + float(cfg.get("take_profit_pct", 8.0)) / 100)
    max_hold_days = max(int(cfg.get("max_hold_days") or 2), 2)
    extend_days = int(cfg.get("limit_down_extend_days") or 5)

    if len(bars) < max_hold_days:
        return _no_data("not enough follow-up K line data for S7 max hold")

    sellable_bars = bars[1:max_hold_days + extend_days]
    for hold_days, bar in enumerate(sellable_bars, start=2):
        if _is_one_word_down(bar):
            continue
        if float(bar["open"] or 0) <= stop_price:
            return _exit("stop_open", entry_status, entry_price, bar["open"], "stop_open", bar, hold_days, gap_pct)
        if float(bar["low"] or 0) <= stop_price:
            return _exit("stop_intraday", entry_status, entry_price, stop_price, "stop_intraday", bar, hold_days, gap_pct)
        if float(bar["open"] or 0) >= take_profit_price:
            return _exit("take_profit_open", entry_status, entry_price, bar["open"], "take_profit_open", bar, hold_days, gap_pct)
        if float(bar["high"] or 0) >= take_profit_price:
            return _exit("take_profit", entry_status, entry_price, take_profit_price, "take_profit", bar, hold_days, gap_pct)
        if hold_days >= max_hold_days:
            status = "limit_down_delayed_exit" if hold_days > max_hold_days else "max_hold_close"
            price = bar["open"] if hold_days > max_hold_days else bar["close"]
            reason = "limit_down_delayed_exit" if hold_days > max_hold_days else "T+2_close"
            return _exit(status, entry_status, entry_price, price, reason, bar, hold_days, gap_pct)

    if sellable_bars and _is_one_word_down(sellable_bars[-1]):
        return _no_data("all follow-up exits blocked by one-word limit-down")
    return _no_data("no sellable S7 exit bar after T+1 entry")


def _is_one_word_up(bar: dict) -> bool:
    up_limit = bar.get("up_limit")
    return (
        up_limit is not None
        and bar["close"] >= up_limit - 0.01
        and abs(bar["high"] - bar["low"]) < 0.01
        and abs(bar["close"] - bar["open"]) < 0.01
    )


def _is_one_word_down(bar: dict) -> bool:
    down_limit = bar.get("down_limit")
    return (
        down_limit is not None
        and bar["close"] <= down_limit + 0.01
        and abs(bar["high"] - bar["low"]) < 0.01
        and abs(bar["close"] - bar["open"]) < 0.01
    )


def _is_down_limit_open(bar: dict) -> bool:
    down_limit = bar.get("down_limit")
    return down_limit is not None and float(bar["open"] or 0) <= float(down_limit) * 1.005


def _no_data(note: str) -> dict:
    return {
        "status": "no_data",
        "entry_status": None,
        "entry_price": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl_pct": None,
        "t1_open": None,
        "t1_high": None,
        "t1_low": None,
        "t1_close": None,
        "t1_open_gap_pct": None,
        "hold_days": 0,
        "exit_date": None,
        "note": note,
    }


def _skip(status: str, bar: dict, gap_pct: float | None, note: str) -> dict:
    return {
        "status": status,
        "entry_status": None,
        "entry_price": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl_pct": None,
        "t1_open": bar["open"],
        "t1_high": bar["high"],
        "t1_low": bar["low"],
        "t1_close": bar["close"],
        "t1_open_gap_pct": gap_pct,
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
    gap_pct: float | None,
) -> dict:
    pnl = (exit_price / entry_price - 1) * 100 if entry_price else None
    return {
        "status": status,
        "entry_status": entry_status,
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "exit_reason": reason,
        "pnl_pct": round(pnl, 2) if pnl is not None else None,
        "t1_open": None,
        "t1_high": None,
        "t1_low": None,
        "t1_close": None,
        "t1_open_gap_pct": gap_pct,
        "hold_days": hold_days,
        "exit_date": bar.get("date"),
        "note": f"hold {hold_days} days -> {reason} @ {exit_price:.2f}, pnl {pnl:+.2f}%",
    }


def run_verify(t1_date: str, mode: str = "backtest") -> list:
    setup_logging()
    t_date = shift_trading_days(t1_date, -1)
    payload = read_select_run(t_date)
    if payload is None:
        write_verification(t1_date, t_date, [], mode)
        return []

    results = []
    cfg = payload.get("config") or {}
    max_hold = max(int(cfg.get("max_hold_days") or 2), 2)
    extend = int(cfg.get("limit_down_extend_days") or 5)
    for candidate in payload.get("candidates", []):
        history_end = shift_trading_days(t1_date, max_hold + extend + 1)
        bars = read_klines([candidate["code"]], t1_date, history_end).get(candidate["code"], [])
        verification = verify_one_backtest(candidate, bars, t1_date=t1_date, config=cfg)
        t1_bar = next((bar for bar in sorted(bars, key=lambda item: item["date"]) if bar["date"] >= t1_date), None)
        if t1_bar and verification.get("t1_open") is None:
            verification["t1_open"] = t1_bar["open"]
            verification["t1_high"] = t1_bar["high"]
            verification["t1_low"] = t1_bar["low"]
            verification["t1_close"] = t1_bar["close"]
        logging.info("%s %s: %s pnl=%s", candidate["code"], candidate.get("name"), verification["status"], verification.get("pnl_pct"))
        results.append({"candidate": candidate, "verification": verification})

    write_verification(t1_date, t_date, results, mode)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="T+1 date YYYY-MM-DD")
    parser.add_argument("--mode", choices=["backtest"], default="backtest")
    args = parser.parse_args()
    results = run_verify(args.date, mode=args.mode)
    print(f"S7 verify {args.date}: {len(results)} rows")


if __name__ == "__main__":
    main()
