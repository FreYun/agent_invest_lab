"""S7 oversold-rebound selection entrypoint."""

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

from candidate_builder import build_candidate, build_regime_observation
from db_writer import read_select_run, write_select_run
from signal_detector import config_with_overrides, detect_s7
from strategy_common import (
    RegimeNotFound,
    build_regime_gate,
    get_market_db,
    is_strategy_allowed,
    iso_to_yyyymmdd,
    load_regime_from_db,
    read_latest_industries,
    read_latest_names,
    read_klines,
    shift_trading_days,
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
    regime_data = load_regime_from_db(t_date, rules_version=rules_version)
    cfg = config_with_overrides(config)
    logging.info("===== S7 select t=%s =====", t_date)

    regime_input = {
        "code": regime_data.get("regime_code"),
        "regime_name": regime_data.get("regime"),
        "score": regime_data.get("score", {}).get("total"),
        "confidence": regime_data.get("confidence"),
        "switched": regime_data.get("switched"),
        "emergency_switch": regime_data.get("emergency_switch"),
        "position_limit_single_base": regime_data.get("playbook", {}).get("position_limit", {}).get("single"),
    }
    regime_observation = build_regime_observation(regime_data)
    # 2026-06-02 按结构择时: S7 改读 playbook 软门 (熊/弱势震荡 才 allowed; 候选仍照常生成供研究)
    s7_allowed, s7_gate_reason, s7_gate_entry = is_strategy_allowed(regime_data, "S7")
    regime_gate = build_regime_gate(
        allowed=s7_allowed,
        reason=s7_gate_reason,
        playbook_mode=(s7_gate_entry or {}).get("mode") if s7_gate_entry else None,
    )

    codes = _read_amount_universe(t_date, float(cfg["min_amount_yi"]))
    if not codes:
        payload = _empty_payload(t_date, regime_input, regime_observation, "T day no amount-qualified daily rows", cfg)
        payload["regime_gate"] = regime_gate
        if persist:
            write_select_run(payload)
        return payload

    start = shift_trading_days(t_date, -int(cfg["lookback_days"]))
    klines = _read_klines_chunked(codes, start, t_date)
    name_map = _read_latest_names_chunked(codes, as_of=t_date)
    industry_map = _read_latest_industries_chunked(codes, as_of=t_date)

    rejects = []
    passed = []
    oversold_pool_size = 0
    active_exposure_pct = 0.0
    for rank, code in enumerate(codes, start=1):
        bars = klines.get(code, [])
        detection = detect_s7(bars, t_date, code=code, config=cfg)
        if detection["passed"]:
            oversold_pool_size += 1
            candidate = build_candidate(
                code=code,
                name=name_map.get(code, code),
                industry=industry_map.get(code, "未知"),
                detection=detection,
                t_bar=bars[-1],
                regime_data=regime_data,
                config=cfg,
                rank=rank,
                active_exposure_pct=active_exposure_pct,
            )
            active_exposure_pct += float(candidate["position_pct"] or 0)
            passed.append(candidate)
        else:
            rejects.append({
                "code": code,
                "name": name_map.get(code, code),
                "stage_failed": detection["stage_failed"],
                "reject_reason": detection["reject_reason"],
            })

    candidates = sorted(passed, key=lambda item: (item["signal"]["dist_ma20_pct"], -item["signal"]["score"], item["code"]))
    max_candidates = int(cfg.get("max_candidates") or 0)
    if max_candidates > 0:
        candidates = candidates[:max_candidates]

    payload = {
        "date": t_date,
        "strategy": "S7",
        "mode": cfg.get("mode") or "paper_observe",
        "regime_input": regime_input,
        "regime_observation": regime_observation,
        "regime_gate": regime_gate,
        "candidates": candidates,
        "reject_samples": rejects[:30],
        "skipped_reason": None,
        "config": cfg,
        "stats": {
            "universe_size": len(codes),
            "oversold_pool_size": oversold_pool_size,
            "passed_count": len(candidates),
        },
    }
    if persist:
        write_select_run(payload)
    logging.info("S7 passed: %s / universe %s", len(candidates), len(codes))
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


def _empty_payload(date: str, regime_input: dict, regime_observation: dict, reason: str, config: dict) -> dict:
    return {
        "date": date,
        "strategy": "S7",
        "mode": config.get("mode") or "paper_observe",
        "regime_input": regime_input,
        "regime_observation": regime_observation,
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
    parser.add_argument("--no-write-db", action="store_true")
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--min-amount-yi", type=float, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    overrides = {}
    for key in ("max_candidates", "min_amount_yi"):
        value = getattr(args, key)
        if value is not None:
            overrides[key] = value

    try:
        payload = run_select(
            args.date,
            rules_version=args.rules_version,
            config=overrides or None,
            persist=not args.no_write_db,
            verbose=args.verbose,
        )
    except RegimeNotFound as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)

    count = len(payload["candidates"])
    obs = payload.get("regime_observation") or {}
    obs_note = " | {}".format(obs.get("note")) if obs.get("note") else ""
    if payload["skipped_reason"]:
        print("S7 {} skipped: {}{}".format(args.date, payload["skipped_reason"], obs_note))
    elif count == 0:
        print("S7 {} candidates: 0{}".format(args.date, obs_note))
    else:
        print("S7 {} candidates: {}{}".format(args.date, count, obs_note))
        for candidate in payload["candidates"][:10]:
            signal = candidate["signal"]
            print(
                "  {code} {name} score={score:.1f} dist_ma20={dist:.2f}% "
                "ret5={ret5:.2f}% amount={amount:.2f}亿 pos={pos:.2f}%".format(
                    code=candidate["code"],
                    name=candidate["name"],
                    score=signal["score"],
                    dist=signal["dist_ma20_pct"],
                    ret5=signal["ret5_pct"],
                    amount=signal["amount_yi"],
                    pos=candidate["position_pct"] * 100,
                )
            )



if __name__ == "__main__":
    main()
