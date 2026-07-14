"""S2 龙头板接力 — T 日收盘后选股入口."""

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

from candidate_builder import build_candidate, is_s2_allowed
from db_writer import write_select_run
from signal_detector import DEFAULTS, detect_s2
from strategy_common import (
    RegimeNotFound,
    build_regime_gate,
    load_regime_from_db,
    read_hot_industries,
    read_klines,
    read_limit_ups_derived,
    regime_input_summary,
)


PRODUCTION_CONFIG = {
    "ignore_playbook": True,
    "allow_regimes": "STRONG_BULL,STRONG_RANGE,NEUTRAL_RANGE",
    "mode": "leader_relay_v1",
    "min_streak": 3,
    "max_streak": 8,
    "reject_one_word_board": True,
    "max_candidates": 5,
}


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
    persist: bool = False,
    verbose: bool = False,
) -> dict:
    setup_logging(logging.INFO if verbose else logging.WARNING)
    cfg = {**DEFAULTS, **(config or {})}
    logging.info("===== S2 select t=%s =====", t_date)

    regime_data = load_regime_from_db(t_date, rules_version=rules_version)
    regime_input = regime_input_summary(regime_data)
    allow_regimes = _normalize_allow_regimes(cfg.get("allow_regimes"))
    if allow_regimes is not None:
        cfg["allow_regimes"] = sorted(allow_regimes)

    if cfg.get("ignore_playbook"):
        allowed = True
        gate_reason = None
        playbook_entry = None
        if allow_regimes is not None and regime_input.get("code") not in allow_regimes:
            allowed = False
            gate_reason = (
                f"regime={regime_input['regime_name']}, S2 research allow only "
                f"{sorted(allow_regimes)}"
            )
        mode = cfg.get("mode") or "research"
    else:
        allowed, gate_reason, playbook_entry = is_s2_allowed(regime_data)
        mode = cfg.get("mode") or (playbook_entry or {}).get("mode")
    regime_gate = build_regime_gate(allowed=allowed, reason=gate_reason, playbook_mode=mode)
    logging.info("regime: %s score=%s", regime_input["regime_name"], regime_input["score"])
    if not allowed:
        logging.info("S2 regime gate 关闭: %s (信号检测仍照常进行)", gate_reason)

    hot = read_hot_industries(t_date)
    hot = hot[: int(cfg["hot_industry_top_n"])]
    hot_by_industry = {h["name"]: h for h in hot}
    zt_rows = read_limit_ups_derived(t_date)
    height_board_rows = [
        r for r in zt_rows
        if int(r.get("streak") or 0) >= int(cfg["min_streak"])
    ]

    if not zt_rows:
        payload = _empty_payload(t_date, regime_input, regime_gate, "T 日无本地涨停数据 (daily/stk_limit)", mode, cfg)
        if persist:
            write_select_run(payload)
        return payload

    codes = [r["code"] for r in height_board_rows]
    klines = read_klines(codes, t_date, t_date)

    candidates = []
    rejects = []
    for row in height_board_rows:
        code = row["code"]
        t_bar = next((b for b in klines.get(code, []) if b["date"] == t_date), None)
        detection = detect_s2(row, hot_by_industry, t_bar, cfg)
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
        "strategy": "S2",
        "mode": mode,
        "regime_input": regime_input,
        "regime_gate": regime_gate,
        "candidates": candidates,
        "reject_samples": rejects[:20],
        "skipped_reason": None,
        "config": cfg,
        "stats": {
            "universe_size": len(zt_rows),
            "height_board_pool_size": len(height_board_rows),
            "passed_count": len(candidates),
            "hot_industries": [h["name"] for h in hot],
        },
    }
    if persist:
        write_select_run(payload)
    logging.info("通过 S2: %s 只 / 高度板池 %s 只", len(candidates), len(height_board_rows))
    return payload


def _empty_payload(date: str, regime_input: dict, regime_gate: dict, reason: str, mode: str | None, config: dict) -> dict:
    return {
        "date": date,
        "strategy": "S2",
        "mode": mode,
        "regime_input": regime_input,
        "regime_gate": regime_gate,
        "candidates": [],
        "reject_samples": [],
        "skipped_reason": reason,
        "config": config,
        "stats": None,
    }


def _normalize_allow_regimes(value) -> set[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    return {str(item).strip().upper() for item in items if str(item).strip()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="T 日 YYYY-MM-DD")
    ap.add_argument("--rules-version", default="v2")
    ap.add_argument("--write-db", action="store_true", help="写入 s2_* 表；探索默认不写库")
    ap.add_argument("--production", action="store_true", help="使用 S2 当前生产口径写库")
    ap.add_argument("--ignore-playbook", action="store_true", help="研究模式忽略 playbook gate")
    ap.add_argument("--allow-regimes", default=None, help="研究模式允许的 regime_code, 逗号分隔")
    ap.add_argument("--min-streak", type=int, default=None)
    ap.add_argument("--max-streak", type=int, default=None)
    ap.add_argument("--max-candidates", type=int, default=None)
    ap.add_argument("--allow-one-word-board", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    config = dict(PRODUCTION_CONFIG) if args.production else {}
    config.update({
        "ignore_playbook": args.ignore_playbook,
        "allow_regimes": args.allow_regimes,
    })
    if args.production:
        config["ignore_playbook"] = True
        config["allow_regimes"] = PRODUCTION_CONFIG["allow_regimes"]
        config["mode"] = PRODUCTION_CONFIG["mode"]
    if args.min_streak is not None:
        config["min_streak"] = args.min_streak
    if args.max_streak is not None:
        config["max_streak"] = args.max_streak
    if args.max_candidates is not None:
        config["max_candidates"] = args.max_candidates
    if args.allow_one_word_board:
        config["reject_one_word_board"] = False

    try:
        payload = run_select(
            args.date,
            rules_version=args.rules_version,
            config=config,
            persist=args.write_db,
            verbose=args.verbose,
        )
    except RegimeNotFound as e:
        print(f"❌ {e}")
        raise SystemExit(1)

    n = len(payload["candidates"])
    gate = payload.get("regime_gate") or {}
    gate_note = "" if gate.get("allowed", True) else f"  (⚠️ regime gate 关闭: {gate.get('reason')})"
    if payload["skipped_reason"]:
        print(f"\n⏭️  {args.date} S2 跳过: {payload['skipped_reason']}{gate_note}")
    elif n == 0:
        print(f"\n📭 {args.date} S2 候选: 0 只{gate_note}")
    else:
        print(f"\n✅ {args.date} S2 候选: {n} 只{gate_note}")
        for c in payload["candidates"][:10]:
            print(
                f"  {c['code']} {c['name']} ({c['industry']}) "
                f"score={c['signal']['score']:.1f} 仓位 {c['position_pct']*100:.2f}%"
            )


if __name__ == "__main__":
    main()
