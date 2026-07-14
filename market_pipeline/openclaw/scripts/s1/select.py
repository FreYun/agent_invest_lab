"""S1 平台突破 — T 日收盘后选股入口."""

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

from candidate_builder import build_candidate, is_s1_allowed
from db_writer import write_select_run
from signal_detector import DEFAULTS, detect_s1
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
    config: dict | None = None,
    persist: bool = True,
    verbose: bool = False,
) -> dict:
    setup_logging(logging.INFO if verbose else logging.WARNING)
    cfg = {**DEFAULTS, **(config or {})}
    logging.info("===== S1 select t=%s =====", t_date)

    regime_data = load_regime_from_db(t_date, rules_version=rules_version)
    regime_input = regime_input_summary(regime_data)
    allowed, gate_reason, mode = is_s1_allowed(regime_data, cfg)
    regime_gate = build_regime_gate(allowed=allowed, reason=gate_reason, playbook_mode=mode)
    if not allowed:
        logging.info("S1 regime gate 关闭: %s (信号检测仍照常进行)", gate_reason)

    codes = _read_amount_universe(t_date, float(cfg["min_amount_yi"]))
    if not codes:
        payload = _empty_payload(t_date, regime_input, regime_gate, "T 日无成交额满足 S1 门槛的本地 daily 数据", mode, cfg)
        if persist:
            write_select_run(payload)
        return payload

    start = shift_trading_days(t_date, -int(cfg["lookback_days"]))
    klines = _read_klines_chunked(codes, start, t_date)
    index_closes = _read_index_closes(cfg["index_code"], start, t_date)
    name_map = _read_latest_names_chunked(codes, as_of=t_date)
    industry_map = _read_latest_industries_chunked(codes, as_of=t_date)

    candidates = []
    rejects = []
    breakout_pool_size = 0
    for code in codes:
        bars = klines.get(code, [])
        detection = detect_s1(bars, t_date, index_closes, cfg)
        if detection["passed"] or detection["stage_failed"] == "volume":
            breakout_pool_size += 1
        if detection["passed"]:
            candidates.append(
                build_candidate(
                    code=code,
                    name=name_map.get(code, code),
                    industry=industry_map.get(code, "未知"),
                    detection=detection,
                    t_bar=bars[-1],
                    regime_data=regime_data,
                    config=cfg,
                )
            )
        else:
            rejects.append({
                "code": code,
                "name": name_map.get(code, code),
                "stage_failed": detection["stage_failed"],
                "reject_reason": detection["reject_reason"],
            })

    candidates.sort(key=lambda item: item["signal"]["score"], reverse=True)
    max_candidates = int(cfg.get("max_candidates") or 0)
    if max_candidates > 0:
        candidates = candidates[:max_candidates]

    payload = {
        "date": t_date,
        "strategy": "S1",
        "mode": mode,
        "regime_input": regime_input,
        "regime_gate": regime_gate,
        "candidates": candidates,
        "reject_samples": rejects[:20],
        "skipped_reason": None,
        "config": cfg,
        "stats": {
            "universe_size": len(codes),
            "breakout_pool_size": breakout_pool_size,
            "passed_count": len(candidates),
        },
    }
    if persist:
        write_select_run(payload)
    logging.info("通过 S1: %s 只 / universe %s 只", len(candidates), len(codes))
    return payload


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


def _empty_payload(date: str, regime_input: dict, regime_gate: dict, reason: str, mode: str | None, config: dict) -> dict:
    return {
        "date": date,
        "strategy": "S1",
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="T 日 YYYY-MM-DD")
    ap.add_argument("--rules-version", default="v2")
    ap.add_argument("--no-write-db", action="store_true", help="只打印结果，不写 s1_* 表")
    ap.add_argument("--max-candidates", type=int, default=None)
    ap.add_argument("--allow-regimes", default=None, help="允许的 regime_code, 逗号分隔；生产默认 STRONG_RANGE")
    ap.add_argument("--min-amount-yi", type=float, default=None)
    ap.add_argument("--min-volume-ratio", type=float, default=None)
    ap.add_argument("--min-breakout-pct", type=float, default=None)
    ap.add_argument("--entry-low-pct", type=float, default=None)
    ap.add_argument("--entry-high-pct", type=float, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    config = {}
    if args.max_candidates is not None:
        config["max_candidates"] = args.max_candidates
    if args.allow_regimes is not None:
        config["allow_regimes"] = args.allow_regimes
    if args.min_amount_yi is not None:
        config["min_amount_yi"] = args.min_amount_yi
    if args.min_volume_ratio is not None:
        config["min_volume_ratio"] = args.min_volume_ratio
    if args.min_breakout_pct is not None:
        config["min_breakout_pct"] = args.min_breakout_pct
    if args.entry_low_pct is not None:
        config["entry_low_pct"] = args.entry_low_pct
    if args.entry_high_pct is not None:
        config["entry_high_pct"] = args.entry_high_pct

    try:
        payload = run_select(
            args.date,
            rules_version=args.rules_version,
            config=config,
            persist=not args.no_write_db,
            verbose=args.verbose,
        )
    except RegimeNotFound as exc:
        print(f"❌ {exc}")
        raise SystemExit(1)

    count = len(payload["candidates"])
    gate = payload.get("regime_gate") or {}
    gate_note = "" if gate.get("allowed", True) else f"  (⚠️ regime gate 关闭: {gate.get('reason')})"
    if payload["skipped_reason"]:
        print(f"\n⏭️  {args.date} S1 跳过: {payload['skipped_reason']}{gate_note}")
    elif count == 0:
        print(f"\n📭 {args.date} S1 候选: 0 只{gate_note}")
    else:
        print(f"\n✅ {args.date} S1 候选: {count} 只{gate_note}")
        for candidate in payload["candidates"][:10]:
            signal = candidate["signal"]
            print(
                f"  {candidate['code']} {candidate['name']} ({candidate['industry']}) "
                f"突破={signal['breakout_pct']:.2f}% 量比={signal['volume_ratio']:.2f} "
                f"额={signal['amount_yi']:.1f}亿 score={signal['score']:.1f} "
                f"仓位 {candidate['position_pct']*100:.2f}%"
            )


if __name__ == "__main__":
    main()
