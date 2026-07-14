"""S6 T+1 verification and historical backtest helper."""

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
from strategy_common import moving_average, read_klines, shift_trading_days


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def verify_one_backtest(candidate: dict, bars: list[dict], t1_date: str | None = None) -> dict:
    if not bars:
        return _no_data("T+1 no K line data")
    bars = sorted(bars, key=lambda item: item["date"])
    if t1_date is not None:
        t1_idx = next((idx for idx, bar in enumerate(bars) if bar["date"] >= t1_date), None)
        if t1_idx is None:
            return _no_data("T+1 no K line data")
        prior_bars = bars[:t1_idx]
        bars = bars[t1_idx:]
    else:
        prior_bars = []
    if len(bars) < 2:
        return _no_data("T+2 no K line data; cannot sell under A-share T+1 rule")

    t1 = bars[0]
    prev_close = float(candidate["t_bar"]["close"] or 0)
    open_price = float(t1["open"] or 0)
    t1_open_gap_pct = round((open_price / prev_close - 1) * 100, 2) if prev_close > 0 else None
    entry_low = float(candidate["entry"]["zone_low"])
    entry_high = float(candidate["entry"]["zone_high"])

    if _is_one_word_up(t1):
        return _skip("limit_up_buy_blocked", t1, t1_open_gap_pct, "T+1 one-word limit-up, not executable")
    if open_price > entry_high:
        return _skip("gap_up_skip", t1, t1_open_gap_pct, f"T+1 open {open_price:.2f} > entry high {entry_high:.2f}")
    if open_price < entry_low:
        return _skip("gap_down_skip", t1, t1_open_gap_pct, f"T+1 open {open_price:.2f} < entry low {entry_low:.2f}")

    entry_price = open_price
    entry_status = "triggered_at_open"
    mode = candidate.get("mode") or "trend"
    max_hold_days = 3 if mode == "defensive_observe" else 5
    stop_pct = -7.0 if mode == "defensive_observe" else -5.0
    stop_price = entry_price * (1 + stop_pct / 100)

    closes_so_far = _seed_closes(candidate, prior_bars, bars)
    for hold_days, bar in enumerate(bars[1:max_hold_days + 8], start=2):
        if _is_one_word_down(bar):
            closes_so_far.append(float(bar["close"] or 0))
            continue

        if bar["open"] <= stop_price:
            return _exit("stop_open", entry_status, entry_price, bar["open"], "stop_open", bar, hold_days, t1_open_gap_pct)
        if bar["low"] <= stop_price:
            return _exit("stop_intraday", entry_status, entry_price, stop_price, "stop_intraday", bar, hold_days, t1_open_gap_pct)

        closes_so_far.append(float(bar["close"] or 0))
        ma5 = moving_average(closes_so_far, 5)
        if ma5 is not None and bar["close"] < ma5:
            return _exit("ma5_lost", entry_status, entry_price, bar["close"], "close below MA5", bar, hold_days, t1_open_gap_pct)

        if hold_days >= max_hold_days:
            return _exit("max_hold", entry_status, entry_price, bar["close"], "max_hold_reached", bar, hold_days, t1_open_gap_pct)

    last = bars[-1]
    if _is_one_word_down(last):
        return _no_data("all follow-up exits blocked by one-word limit-down")
    return _exit("forced_after_deadline", entry_status, entry_price, last["open"], "forced_after_deadline", last, len(bars), t1_open_gap_pct)


def _seed_closes(candidate: dict, prior_bars: list[dict], bars: list[dict]) -> list[float]:
    closes = [float(bar["close"] or 0) for bar in prior_bars[-20:]]
    t_close = candidate.get("t_bar", {}).get("close")
    t_date = candidate.get("date")
    if t_close is not None and (not prior_bars or prior_bars[-1].get("date") != t_date):
        closes.append(float(t_close))
    closes.append(float(bars[0]["close"] or 0))
    return closes


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
    max_hold = max(int((payload.get("config") or {}).get("max_hold_days") or 5), 5)
    for candidate in payload.get("candidates", []):
        history_start = shift_trading_days(t1_date, -20)
        history_end = shift_trading_days(t1_date, max_hold + 7)
        bars = read_klines([candidate["code"]], history_start, history_end).get(candidate["code"], [])
        verification = verify_one_backtest(candidate, bars, t1_date=t1_date)
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
    print(f"S6 verify {args.date}: {len(results)} rows")


if __name__ == "__main__":
    main()
