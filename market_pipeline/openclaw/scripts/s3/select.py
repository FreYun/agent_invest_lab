"""S3 首板接力 — T 日收盘后选股入口."""

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

from candidate_builder import build_candidate, is_s3_allowed
from db_writer import write_select_run
from signal_detector import DEFAULTS, detect_s3
from strategy_common import (
    RegimeNotFound,
    build_regime_gate,
    load_regime_from_db,
    read_hot_industries,
    read_klines,
    read_limit_ups_derived,
    regime_input_summary,
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def run_select(t_date: str, rules_version: str = "v2", config: dict | None = None) -> dict:
    setup_logging()
    cfg = {**DEFAULTS, **(config or {})}
    logging.info("===== S3 select t=%s =====", t_date)

    regime_data = load_regime_from_db(t_date, rules_version=rules_version)
    regime_input = regime_input_summary(regime_data)
    allowed, gate_reason, playbook_entry = is_s3_allowed(regime_data)
    mode = (playbook_entry or {}).get("mode")
    regime_gate = build_regime_gate(allowed=allowed, reason=gate_reason, playbook_mode=mode)
    logging.info("regime: %s score=%s", regime_input["regime_name"], regime_input["score"])
    if not allowed:
        logging.info("S3 regime gate 关闭: %s (信号检测仍照常进行)", gate_reason)

    hot = read_hot_industries(t_date)
    hot = hot[: int(cfg["hot_industry_top_n"])]
    hot_by_industry = {h["name"]: h for h in hot}
    zt_rows = read_limit_ups_derived(t_date)
    first_board_rows = [r for r in zt_rows if int(r.get("streak") or 0) == 1]

    if not zt_rows:
        payload = _empty_payload(t_date, regime_input, regime_gate, "T 日无本地涨停数据 (daily/stk_limit)", mode, cfg)
        write_select_run(payload)
        return payload

    codes = [r["code"] for r in first_board_rows]
    klines = read_klines(codes, t_date, t_date)

    candidates = []
    rejects = []
    for row in first_board_rows:
        code = row["code"]
        t_bar = next((b for b in klines.get(code, []) if b["date"] == t_date), None)
        detection = detect_s3(row, hot_by_industry, t_bar, cfg)
        if detection["passed"]:
            candidates.append(build_candidate(row, detection, t_bar, regime_data))
        else:
            rejects.append({
                "code": code,
                "name": row.get("name") or code,
                "stage_failed": detection["stage_failed"],
                "reject_reason": detection["reject_reason"],
            })

    candidates.sort(key=lambda c: c["signal"]["score"], reverse=True)
    max_candidates = int(cfg.get("max_candidates") or 0)
    if max_candidates > 0:
        candidates = candidates[:max_candidates]
    payload = {
        "date": t_date,
        "strategy": "S3",
        "mode": mode,
        "regime_input": regime_input,
        "regime_gate": regime_gate,
        "candidates": candidates,
        "reject_samples": rejects[:20],
        "skipped_reason": None,
        "config": cfg,
        "stats": {
            "universe_size": len(zt_rows),
            "first_board_pool_size": len(first_board_rows),
            "passed_count": len(candidates),
            "hot_industries": [h["name"] for h in hot],
        },
    }
    write_select_run(payload)
    logging.info("通过 S3: %s 只 / 首板池 %s 只", len(candidates), len(first_board_rows))
    return payload


def _empty_payload(date: str, regime_input: dict, regime_gate: dict, reason: str, mode: str | None, config: dict) -> dict:
    return {
        "date": date,
        "strategy": "S3",
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
    args = ap.parse_args()

    try:
        payload = run_select(args.date, rules_version=args.rules_version)
    except RegimeNotFound as e:
        print(f"❌ {e}")
        raise SystemExit(1)

    n = len(payload["candidates"])
    gate = payload.get("regime_gate") or {}
    gate_note = "" if gate.get("allowed", True) else f"  (⚠️ regime gate 关闭: {gate.get('reason')})"
    if payload["skipped_reason"]:
        print(f"\n⏭️  {args.date} S3 跳过: {payload['skipped_reason']}{gate_note}")
    elif n == 0:
        print(f"\n📭 {args.date} S3 候选: 0 只{gate_note}")
    else:
        print(f"\n✅ {args.date} S3 候选: {n} 只{gate_note}")
        for c in payload["candidates"][:10]:
            print(
                f"  {c['code']} {c['name']} ({c['industry']}) "
                f"score={c['signal']['score']:.1f} 仓位 {c['position_pct']*100:.2f}%"
            )


if __name__ == "__main__":
    main()
