"""S5 龙回头 — T 日收盘后选股入口。

用法:
    # 默认: 从 market.db.regime_classify_daily 读 regime (v2 规则)
    python3 select.py --date=2026-04-14

    # override: 手动指定 regime JSON (例如跑历史某天用旧 classifier 输出)
    python3 select.py --date=2026-04-14 --regime-json=/path/to/regime_2026-04-14.json

输出:
    market.db: s5_select_runs / s5_candidates / s5_candidate_rejects

数据流详见 references/strategy.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# 让 scripts/ 下的兄弟模块可以 import
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from data_fetcher import (
    DataNotPrewarmed,
    extract_streaks_from_klines,
    get_klines_batch,
    get_universe,
    get_zt_pool,
    shift_trading_days,
)
from signal_detector import detect_s5
from candidate_builder import is_s5_allowed, build_candidate
from regime_loader import load_regime_from_db, RegimeNotFound

ROOT_SCRIPTS = os.path.dirname(SCRIPT_DIR)
if ROOT_SCRIPTS not in sys.path:
    sys.path.insert(0, ROOT_SCRIPTS)
from strategy_common import build_regime_gate  # noqa: E402


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def load_regime_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_universe(t_date: str) -> tuple:
    """从 DB 读 T 日 universe (由 cron `s5-prewarm.py` 预计算).

    Returns:
        (codes: list, hot_industries: list, code_to_industry: dict)
    """
    try:
        codes, hot, code_to_industry = get_universe(t_date)
    except DataNotPrewarmed as e:
        logging.warning(str(e))
        return [], [], {}
    logging.info(f"T 日 universe: {len(codes)} 只, 热门行业: {[h['name'] for h in hot]}")
    return codes, hot, code_to_industry


def fetch_universe_klines(codes: list, t_date: str) -> dict:
    """从 klines_cache 读 universe 近 35 日 K 线 (由 cron prewarm 填)."""
    start = shift_trading_days(t_date, -35).replace("-", "")
    end = t_date.replace("-", "")
    logging.info(f"从 klines_cache 读 K 线: {len(codes)} 只 {start}~{end}")
    return get_klines_batch(codes, start, end)


def run_select(
    t_date: str,
    regime_path: str | None = None,
    rules_version: str = "v2",
) -> dict:
    """主流程。返回 payload, 并写入 market.db 的 s5_select_runs / s5_candidates / s5_candidate_rejects。

    Args:
        t_date: T 日 'YYYY-MM-DD'
        regime_path: 可选, 指定 classifier regime JSON 路径 (override DB 数据源)
        rules_version: DB 加载时用的规则版本, 默认 'v2'
    """
    setup_logging()
    logging.info(f"===== S5 select t={t_date} =====")

    # 1. 读 regime: 默认 DB, 若传了 --regime-json 则用文件
    if regime_path:
        logging.info(f"regime 数据源: JSON 文件 {regime_path}")
        regime_data = load_regime_json(regime_path)
    else:
        logging.info(f"regime 数据源: market.db.regime_classify_daily ({rules_version})")
        regime_data = load_regime_from_db(t_date, rules_version=rules_version)

    regime_input = {
        "code": regime_data.get("regime_code"),
        "regime_name": regime_data.get("regime"),
        "score": regime_data.get("score", {}).get("total"),
        "confidence": regime_data.get("confidence"),
        "switched": regime_data.get("switched"),
        "emergency_switch": regime_data.get("emergency_switch"),
        "position_limit_single_base": regime_data.get("playbook", {}).get("position_limit", {}).get("single"),
    }
    logging.info(f"regime: {regime_input['regime_name']} score={regime_input['score']}")

    # 2. Regime 软门: 不再早返回, 仅作为元信息附在 payload 里
    allowed, gate_reason = is_s5_allowed(regime_data)
    regime_gate = build_regime_gate(allowed=allowed, reason=gate_reason, playbook_mode=None)
    if not allowed:
        logging.info(f"S5 regime gate 关闭: {gate_reason} (信号检测仍照常进行)")

    # 3. 构造 universe
    codes, hot_industries, code_to_industry = build_universe(t_date)
    if not codes:
        logging.warning("universe 为空, 跳过")
        payload = {
            "date": t_date,
            "strategy": "S5",
            "regime_input": regime_input,
            "regime_gate": regime_gate,
            "candidates": [],
            "skipped_reason": "T 日涨停池为空, 无法确定热门股池",
            "stats": None,
        }
        _write_outputs(payload)
        return payload

    # 4. 批量拉 K 线
    klines_map = fetch_universe_klines(codes, t_date)
    logging.info(f"K 线返回: {len(klines_map)} 只")

    # 5. 拉股票名 (用涨停池或 K 线数据已经够用; 名字从涨停池或额外查询)
    name_map = _build_name_map(t_date, codes)

    # 6. 提取连板历史 + 跑信号检测
    candidates = []
    rejects = []
    dragon_pool_size = 0

    for code in codes:
        if code not in klines_map:
            continue
        klines = klines_map[code]
        if not klines:
            continue

        streaks = extract_streaks_from_klines(klines, t_date)
        if any(s["max_streak"] >= 2 for s in streaks):
            dragon_pool_size += 1

        detection = detect_s5(klines, streaks, t_date)
        if detection["passed"]:
            try:
                cand = build_candidate(
                    code=code,
                    name=name_map.get(code, code),
                    industry=code_to_industry.get(code, "未知"),
                    detection=detection,
                    klines=klines,
                    t_date=t_date,
                    regime_data=regime_data,
                )
                candidates.append(cand)
            except ValueError as e:
                # 一字板等无法成单的情况, 视作软拒因
                logging.info(f"  {code} 跳过: {e}")
                rejects.append({
                    "code": code,
                    "name": name_map.get(code, code),
                    "stage_failed": "build",
                    "reject_reason": str(e),
                })
        else:
            # 只记录到了 cooldown 阶段的拒因 (有龙头但卡在后续两段), 用于调试
            if detection.get("stage_failed") in ("cooldown", "rebound"):
                rejects.append({
                    "code": code,
                    "name": name_map.get(code, code),
                    "stage_failed": detection["stage_failed"],
                    "reject_reason": detection["reject_reason"],
                })

    logging.info(f"通过 S5: {len(candidates)} 只, 龙头池 {dragon_pool_size} 只")

    # 按反包涨幅降序排, 强势优先
    candidates.sort(key=lambda c: c["rebound"]["t_pct"], reverse=True)

    payload = {
        "date": t_date,
        "strategy": "S5",
        "regime_input": regime_input,
        "regime_gate": regime_gate,
        "candidates": candidates,
        "reject_samples": rejects[:10],
        "skipped_reason": None,
        "stats": {
            "universe_size": len(codes),
            "dragon_pool_size": dragon_pool_size,
            "passed_count": len(candidates),
            "hot_industries": [h["name"] for h in hot_industries],
        },
    }

    _write_outputs(payload)
    return payload


def _build_name_map(t_date: str, codes: list) -> dict:
    """补全股票名 — 从 limit_up_pool 取 (cron 已预热), 没有就用代码兜底."""
    name_map = {}
    zt_df = get_zt_pool(t_date)
    if zt_df is not None:
        for _, row in zt_df.iterrows():
            c = str(row["代码"]).zfill(6)
            name_map[c] = row.get("名称", c)
    return name_map


def _write_outputs(payload: dict):
    """落库到 market.db 的 s5_select_runs / s5_candidates / s5_candidate_rejects。"""
    from db_writer import write_select_run
    write_select_run(payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="T 日 YYYY-MM-DD")
    ap.add_argument(
        "--regime-json",
        default=None,
        help="可选: override 从 DB 加载 regime, 改用指定 JSON 文件 (通常跑历史某天时用)",
    )
    ap.add_argument(
        "--rules-version",
        default="v2",
        help="DB 查询用的规则版本 (默认 v2, pipeline.sh 日更的就是 v2)",
    )
    args = ap.parse_args()

    payload = run_select(
        args.date,
        regime_path=args.regime_json,
        rules_version=args.rules_version,
    )

    n = len(payload["candidates"])
    gate = payload.get("regime_gate") or {}
    gate_note = "" if gate.get("allowed", True) else f"  (⚠️ regime gate 关闭: {gate.get('reason')})"
    if payload["skipped_reason"]:
        print(f"\n⏭️  {args.date} S5 跳过: {payload['skipped_reason']}{gate_note}")
    elif n == 0:
        print(f"\n📭 {args.date} S5 候选: 0 只{gate_note}")
    else:
        print(f"\n✅ {args.date} S5 候选: {n} 只{gate_note}")
        for c in payload["candidates"]:
            print(f"  {c['code']} {c['name']} ({c['industry']}) — 反包 {c['rebound']['t_pct']:.2f}% — 仓位 {c['position_pct']*100:.2f}%")


if __name__ == "__main__":
    main()
