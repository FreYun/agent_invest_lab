"""S6 trend-hold selection entrypoint."""

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

from candidate_builder import build_candidate, is_s6_allowed
from db_writer import read_select_run, write_select_run
from signal_detector import TREND_DEFAULTS, config_for_mode, detect_s6
from strategy_common import (
    RegimeNotFound,
    build_regime_gate,
    get_market_db,
    iso_to_yyyymmdd,
    load_regime_from_db,
    read_klines,
    read_latest_industries,
    read_latest_names,
    regime_input_summary,
    shift_trading_days,
    yyyymmdd_to_iso,
)


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger().setLevel(level)


def run_select(
    t_date: str,
    rules_version: str = "v2",
    mode: str = "auto",
    config: dict | None = None,
    persist: bool = True,
    verbose: bool = False,
) -> dict:
    setup_logging(logging.INFO if verbose else logging.WARNING)
    regime_data = load_regime_from_db(t_date, rules_version=rules_version)
    chosen_mode = _choose_mode(mode, regime_data)
    cfg = config_for_mode(chosen_mode, config)
    logging.info("===== S6 select t=%s mode=%s =====", t_date, chosen_mode)

    regime_input = regime_input_summary(regime_data)
    # Regime 软门: 不再早返回, 仅作为元信息附在 payload 里 (对齐 S1-S5)
    allowed, gate_reason, playbook_mode = is_s6_allowed(regime_data, cfg)
    regime_gate = build_regime_gate(allowed=allowed, reason=gate_reason, playbook_mode=playbook_mode)
    if not allowed:
        logging.info(f"S6 regime gate 关闭: {gate_reason} (信号检测仍照常进行)")

    codes = _read_amount_universe(t_date, float(cfg["min_amount_yi"]))
    if not codes:
        payload = _empty_payload(t_date, regime_input, regime_gate, "T day no amount-qualified daily rows", chosen_mode, cfg)
        if persist:
            write_select_run(payload)
        return payload

    start = shift_trading_days(t_date, -int(cfg["lookback_days"]))
    klines = _read_klines_chunked(codes, start, t_date)
    index_closes = _read_index_closes(cfg["index_code"], start, t_date)
    name_map = _read_latest_names_chunked(codes, as_of=t_date)
    industry_map = _read_latest_industries_chunked(codes, as_of=t_date)

    rejects = []
    passed = []
    trend_pool_size = 0
    cooldown_codes = _read_cooldown_codes(t_date, int(cfg.get("cooldown_days") or 0), chosen_mode)
    for code in codes:
        bars = klines.get(code, [])
        detection = detect_s6(bars, t_date, index_closes, cfg)
        if detection["passed"]:
            trend_pool_size += 1
            if code in cooldown_codes:
                rejects.append({
                    "code": code,
                    "name": name_map.get(code, code),
                    "stage_failed": "cooldown",
                    "reject_reason": f"same stock selected within {cfg.get('cooldown_days')} trading days",
                })
                continue
            passed.append((code, detection, bars[-1]))
        else:
            rejects.append({
                "code": code,
                "name": name_map.get(code, code),
                "stage_failed": detection["stage_failed"],
                "reject_reason": detection["reject_reason"],
            })

    candidates = [
        build_candidate(
            code=code,
            name=name_map.get(code, code),
            industry=industry_map.get(code, "未知"),
            detection=detection,
            t_bar=t_bar,
            regime_data=regime_data,
            config=cfg,
        )
        for code, detection, t_bar in passed
    ]
    candidates.sort(key=lambda item: item["signal"]["score"], reverse=True)
    max_candidates = int(cfg.get("max_candidates") or 0)
    if max_candidates > 0:
        candidates = candidates[:max_candidates]

    payload = {
        "date": t_date,
        "strategy": "S6",
        "mode": chosen_mode,
        "regime_input": regime_input,
        "regime_gate": regime_gate,
        "candidates": candidates,
        "reject_samples": rejects[:30],
        "skipped_reason": None,
        "config": cfg,
        "stats": {
            "universe_size": len(codes),
            "trend_pool_size": trend_pool_size,
            "passed_count": len(candidates),
            "cooldown_reject_count": sum(1 for reject in rejects if reject["stage_failed"] == "cooldown"),
        },
    }
    if persist:
        write_select_run(payload)
    logging.info("S6 passed: %s / universe %s", len(candidates), len(codes))
    return payload


def _choose_mode(mode: str, regime_data: dict) -> str:
    if mode != "auto":
        if mode == "defensive":
            return "defensive_observe"
        return mode
    regime_code = regime_data.get("regime_code")
    if regime_code == "WEAK_RANGE":
        return "defensive_observe"
    return "trend"


def _read_amount_universe(t_date: str, min_amount_yi: float) -> list[str]:
    raw_date = iso_to_yyyymmdd(t_date)
    min_amount_raw = min_amount_yi * 100_000
    conn = get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT substr(ts_code, 1, 6) AS code
            FROM daily
            WHERE trade_date = ?
              AND amount >= ?
              AND close IS NOT NULL
              AND close > 0
            ORDER BY amount DESC
            """,
            (raw_date, min_amount_raw),
        ).fetchall()
    finally:
        conn.close()
    return [row["code"] for row in rows]


def _read_klines_chunked(codes: list[str], start: str, end: str, chunk_size: int = 800) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for idx in range(0, len(codes), chunk_size):
        result.update(read_klines(codes[idx:idx + chunk_size], start, end))
    return result


def _read_latest_names_chunked(codes: list[str], as_of: str, chunk_size: int = 800) -> dict[str, str]:
    result: dict[str, str] = {}
    for idx in range(0, len(codes), chunk_size):
        result.update(read_latest_names(codes[idx:idx + chunk_size], as_of=as_of))
    return result


def _read_latest_industries_chunked(codes: list[str], as_of: str, chunk_size: int = 800) -> dict[str, str]:
    result: dict[str, str] = {}
    for idx in range(0, len(codes), chunk_size):
        result.update(read_latest_industries(codes[idx:idx + chunk_size], as_of=as_of))
    return result


def _read_index_closes(index_code: str, start: str, end: str) -> dict[str, float]:
    start_raw = iso_to_yyyymmdd(start)
    end_raw = iso_to_yyyymmdd(end)
    conn = get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT trade_date, close
            FROM index_daily
            WHERE ts_code = ?
              AND trade_date >= ?
              AND trade_date <= ?
            ORDER BY trade_date
            """,
            (index_code, start_raw, end_raw),
        ).fetchall()
    finally:
        conn.close()
    return {yyyymmdd_to_iso(row["trade_date"]): float(row["close"]) for row in rows if row["close"] is not None}


def _read_cooldown_codes(t_date: str, cooldown_days: int, mode: str) -> set[str]:
    if cooldown_days <= 0:
        return set()
    start = shift_trading_days(t_date, -cooldown_days)
    conn = get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT code
            FROM s6_candidates
            WHERE date >= ? AND date < ? AND mode = ?
            """,
            (start, t_date, mode),
        ).fetchall()
    except Exception:
        return set()
    finally:
        conn.close()
    return {row["code"] for row in rows}


def _empty_payload(date: str, regime_input: dict, regime_gate: dict, reason: str, mode: str, config: dict) -> dict:
    return {
        "date": date,
        "strategy": "S6",
        "mode": mode,
        "regime_input": regime_input,
        "regime_gate": regime_gate,
        "candidates": [],
        "reject_samples": [],
        "skipped_reason": reason,
        "config": config,
        "stats": None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="T date YYYY-MM-DD")
    parser.add_argument("--rules-version", default="v2")
    parser.add_argument("--mode", choices=["auto", "trend", "defensive", "defensive_observe"], default="auto")
    parser.add_argument("--no-write-db", action="store_true")
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--min-amount-yi", type=float, default=None)
    parser.add_argument("--entry-low-pct", type=float, default=None)
    parser.add_argument("--entry-high-pct", type=float, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    overrides = {}
    for key in ("max_candidates", "min_amount_yi", "entry_low_pct", "entry_high_pct"):
        value = getattr(args, key)
        if value is not None:
            overrides[key] = value

    try:
        payload = run_select(
            args.date,
            rules_version=args.rules_version,
            mode=args.mode,
            config=overrides or None,
            persist=not args.no_write_db,
            verbose=args.verbose,
        )
    except RegimeNotFound as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)

    count = len(payload["candidates"])
    gate = payload.get("regime_gate") or {}
    gate_note = "" if gate.get("allowed", True) else f" (gate closed: {gate.get('reason')})"
    if payload["skipped_reason"]:
        print(f"S6 {args.date} skipped: {payload['skipped_reason']}{gate_note}")
    elif count == 0:
        print(f"S6 {args.date} candidates: 0{gate_note}")
    else:
        print(f"S6 {args.date} candidates: {count}{gate_note}")
        for candidate in payload["candidates"][:10]:
            signal = candidate["signal"]
            print(
                f"  {candidate['code']} {candidate['name']} {candidate['mode']} "
                f"score={signal['score']:.1f} dist_ma5={signal['dist_ma5_pct']:.2f}% "
                f"ma10s5={signal.get('ma10_slope5_pct')} rel20={signal['rel20_pct']:.2f}% "
                f"pos={candidate['position_pct']*100:.2f}%"
            )


if __name__ == "__main__":
    main()
