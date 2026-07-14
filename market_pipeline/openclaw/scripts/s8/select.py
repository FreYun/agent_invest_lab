"""S8 主线分歧/借势 自适应选股 — 编排 + CLI.

收盘后跑: python3 scripts/s8/select.py --date 2026-05-21
流程: 大势定调(tone) → 主线识别(mainline) → 两路径筛选(filter) → 写库.
"""

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

from analysis import compute_tone, identify_mainline
from config import full_config
from db_writer import write_select_run
from filter import filter_candidates
from strategy_common import RegimeNotFound, load_regime_from_db


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


_TIER_REASON = {
    "buy_divergence": {
        "core": "低位抗跌+逆势资金",
        "strength_watch": "高强度涨停·偏弹性观察",
    },
    "borrow_strength": {
        "core": "主升接力位·资金进攻",
        "strength_watch": "涨停龙头·强度接力",
    },
}
_TIER_REASON_COMMON = {
    "sentiment_gauge": "连板龙头·情绪风向标",
    "avoid": "充分演绎/过热·主动回避",
}


def _annotate(c: dict) -> None:
    label = _TIER_REASON_COMMON.get(c["tier"]) or \
        _TIER_REASON.get(c.get("play_mode"), {}).get(c["tier"], c["tier"])
    bits = [label]
    if c.get("net_main") is not None:
        bits.append(f"主力净{c['net_main']/1e4:.2f}亿")
    if c.get("dist10_pct") is not None:
        bits.append(f"10日{c['dist10_pct']:+.1f}%")
    if c.get("lub_streak"):
        bits.append(f"{c['lub_streak']}板")
    if c.get("themes"):
        bits.append("/".join(c["themes"][:2]))
    c["reason"] = " · ".join(bits)


def run_select(t_date, rules_version="v2", config=None, persist=True, verbose=False) -> dict:
    setup_logging(verbose)
    regime_data = load_regime_from_db(t_date, rules_version=rules_version)
    cfg = full_config(config)
    logging.info("===== S8 select t=%s =====", t_date)

    tone = compute_tone(t_date, regime_data, cfg)
    mainline = identify_mainline(t_date, cfg)
    logging.info("定调: %s (%s)", tone["tone"], tone["path"])
    logging.info("主线: %s | 分歧位=%s", [t["name"] for t in mainline["themes"]], mainline["is_divergence"])

    result = filter_candidates(t_date, mainline, tone, regime_data, cfg)
    kept, extras = result["candidates"], result["extras"]

    # 看板上限按 signal_score 截断; 全量进 select_runs
    max_board = int(cfg.get("max_board_candidates") or 0)
    board = kept[:max_board] if max_board > 0 else kept
    for rank, c in enumerate(board, 1):
        c["rank"] = rank
    for c in board + extras:
        _annotate(c)

    regime_input = {
        "code": regime_data.get("regime_code"),
        "regime_name": regime_data.get("regime"),
        "score": regime_data.get("score", {}).get("total"),
        "confidence": regime_data.get("confidence"),
        "switched": regime_data.get("switched"),
        "emergency_switch": regime_data.get("emergency_switch"),
        "position_limit_single_base": regime_data.get("playbook", {}).get("position_limit", {}).get("single"),
    }
    payload = {
        "date": t_date,
        "strategy": "S8",
        "tone": tone,
        "mainline": mainline,
        "regime_input": regime_input,
        "candidates": board,         # 进看板(core+strength_watch)
        "extras": extras,            # 只记录(sentiment_gauge+avoid)
        "stats": result["stats"],
        "config": cfg,
    }
    if persist:
        write_select_run(payload)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="T date YYYY-MM-DD")
    ap.add_argument("--rules-version", default="v2")
    ap.add_argument("--no-write-db", action="store_true")
    ap.add_argument("--max-board", type=int, default=None)
    ap.add_argument("--mainline-top-n", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    override = {}
    if args.max_board is not None:
        override["max_board_candidates"] = args.max_board
    if args.mainline_top_n is not None:
        override["mainline"] = {"top_n": args.mainline_top_n}

    try:
        payload = run_select(args.date, rules_version=args.rules_version,
                             config=override or None, persist=not args.no_write_db,
                             verbose=args.verbose)
    except RegimeNotFound as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)

    tone, ml, st = payload["tone"], payload["mainline"], payload["stats"]
    print(f"\nS8 {args.date}  定调={tone['tone']} [{tone['path']}]")
    print(f"  定调依据: {'; '.join(tone['reasons'])}")
    print(f"  温度计: 涨{tone['gauge']['advance']}/跌{tone['gauge']['decline']} "
          f"跌停{tone['gauge']['limit_down']} 3日涨家数{tone['gauge']['advance_trend_3d']}")
    print(f"  主线: {', '.join(t['name'] for t in ml['themes'])} | 分歧位={ml['is_divergence']}")
    if ml.get("top_streak"):
        ts = ml["top_streak"]
        print(f"  最高连板: {ts['name']}({ts['code']}) {ts['streak']}板 在主线内={ml['top_streak_in_mainline']}")
    print(f"  universe={st['universe_size']} 进看板={st['kept_count']} 只记录={st['extras_count']} 淘汰={st['reject_count']}")

    print(f"\n  ── 进看板 (core/strength_watch) ──")
    for c in payload["candidates"]:
        print(f"   #{c['rank']:<2} [{c['tier']:<13}] {c['code']} {c.get('name') or '':<6} "
              f"pos={(c['position_pct'] or 0)*100:.1f}% | {c['reason']}")
    if payload["extras"]:
        print(f"\n  ── 只记录 (sentiment_gauge/avoid) ──")
        for c in payload["extras"]:
            print(f"      [{c['tier']:<13}] {c['code']} {c.get('name') or '':<6} | {c['reason']}")


if __name__ == "__main__":
    main()
