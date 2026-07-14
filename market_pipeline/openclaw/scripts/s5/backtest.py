"""S5 批量历史回测 — 对一段日期范围跑 select + verify(backtest) 并汇总。

用法:
    python3 backtest.py --start=2026-03-12 --end=2026-04-14 \\
        --regime-dir=/home/rooot/.openclaw/workspace-bot11/memory/review-output \\
        --output-dir=/tmp/s5-backtest

流程 (每个交易日 T):
  1. 检查 regime_T.json 是否存在 (不存在跳过)
  2. 跑 select.py T → 产出 candidates_T.{md,json}
  3. 找 T+1 交易日 (交易日历)
  4. 跑 verify.py --mode=backtest t+1 → 产出 verification_T+1.{md,json}

汇总: 遍历所有 verification_*.json, 统计:
  - 总候选数 / 触发数 / 胜率 / 平均盈亏
  - 按 regime 分组
  - 按日期列表 (逐日明细)

输出:
  - 逐日 candidates/verification 文件写到 output_dir
  - backtest_summary.md 汇总
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# select 与 Python 内置 select 模块同名, 用 importlib 强制加载本地的
import importlib.util
_spec = importlib.util.spec_from_file_location("s5_select", os.path.join(SCRIPT_DIR, "select.py"))
_s5_select = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s5_select)
run_select = _s5_select.run_select

from verify import run_verify
from data_fetcher import shift_trading_days

BACKTEST_START_DATE = "2025-01-01"


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def iter_trading_days(start: str, end: str):
    """粗暴枚举日历日, 周末跳过; 真实交易日过滤由 regime_*.json 存在性决定。"""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    d = s
    while d <= e:
        if d.weekday() < 5:
            yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def run_backtest(start: str, end: str, regime_dir: str, output_dir: str, tradable_only: bool = True):
    setup_logging()
    start = max(start, BACKTEST_START_DATE)
    os.makedirs(output_dir, exist_ok=True)
    logging.info(f"===== S5 backtest {start} ~ {end} =====")

    daily_records = []

    for t_date in iter_trading_days(start, end):
        regime_path = os.path.join(regime_dir, f"regime_{t_date}.json")
        if not os.path.exists(regime_path):
            logging.info(f"[{t_date}] 无 regime json, 跳过")
            continue

        logging.info(f"[{t_date}] 跑 select...")
        try:
            payload = run_select(t_date, regime_path=regime_path)
        except Exception as e:
            logging.error(f"[{t_date}] select 失败: {e}")
            continue

        n_cand = len(payload.get("candidates", []))
        skipped = payload.get("skipped_reason")
        gate = payload.get("regime_gate") or {}
        gate_allowed = gate.get("allowed")
        should_verify = n_cand > 0 and (not tradable_only or gate_allowed is not False)

        # 跑 verify(backtest) on T+1 — 读写全走 market.db
        t1_date = shift_trading_days(t_date, 1)
        verify_results = None
        verify_skipped_reason = None
        if t1_date is None:
            verify_skipped_reason = "T+1 行情尚未入库"
        elif not should_verify and gate_allowed is False and tradable_only:
            verify_results = []
            verify_skipped_reason = "regime_gate_closed"
        elif should_verify:
            try:
                logging.info(f"[{t_date}] 跑 verify backtest T+1={t1_date}...")
                verify_results = run_verify(t1_date, mode="backtest")
            except Exception as e:
                logging.error(f"[{t_date}] verify 失败: {e}")

        daily_records.append({
            "t_date": t_date,
            "t1_date": t1_date,
            "regime": payload.get("regime_input", {}).get("regime_name"),
            "regime_score": payload.get("regime_input", {}).get("score"),
            "switched": payload.get("regime_input", {}).get("switched"),
            "emergency_switch": payload.get("regime_input", {}).get("emergency_switch"),
            "candidates_count": n_cand,
            "skipped_reason": skipped,
            "verify_skipped_reason": verify_skipped_reason,
            "verify_results": verify_results,
        })

    # 汇总统计
    summary = _summarize(daily_records)
    _write_summary_md(output_dir, start, end, daily_records, summary)
    _write_summary_json(output_dir, start, end, daily_records, summary)

    # 屏幕打印关键汇总
    print("\n" + "=" * 60)
    print(f"S5 回测汇总 {start} ~ {end}")
    print("=" * 60)
    print(f"  跑过的交易日: {summary['days_total']}")
    print(f"  跳过 (regime 不适用): {summary['days_skipped']}")
    print(f"  跑信号的日子: {summary['days_with_run']}")
    print(f"  出 candidate 的日子: {summary['days_with_candidate']}")
    print(f"  总 candidate 数: {summary['total_candidates']}")
    print(f"  触发入场: {summary['total_triggered']}")
    if summary["total_triggered"] > 0:
        print(f"  胜率 (pnl>0): {summary['wins']}/{summary['total_triggered']} ({summary['win_rate']*100:.0f}%)")
        print(f"  平均盈亏: {summary['avg_pnl']:+.2f}%")
        print(f"  最高盈: {summary['max_pnl']:+.2f}%")
        print(f"  最大亏: {summary['min_pnl']:+.2f}%")


def _summarize(records: list) -> dict:
    days_total = len(records)
    days_skipped = sum(1 for r in records if r["skipped_reason"])
    days_with_run = days_total - days_skipped
    days_with_candidate = sum(1 for r in records if r["candidates_count"] > 0)
    total_candidates = sum(r["candidates_count"] for r in records)

    all_pnls = []
    hold_days_list = []
    total_triggered = 0
    gap_up_skip = 0
    gap_down_skip = 0
    stop_hit = 0
    limit_up_broken = 0
    max_hold_reached = 0
    close_hold = 0
    no_data = 0

    for r in records:
        vr = r.get("verify_results") or []
        for item in vr:
            v = item["verification"]
            status = v.get("status")
            pnl = v.get("pnl_pct")
            if pnl is not None:
                all_pnls.append(pnl)
                total_triggered += 1
            hd = v.get("hold_days")
            if hd:
                hold_days_list.append(hd)
            if status == "gap_up_skip":
                gap_up_skip += 1
            elif status == "gap_down_skip":
                gap_down_skip += 1
            elif status == "stop_hit":
                stop_hit += 1
            elif status == "limit_up_broken":
                limit_up_broken += 1
            elif status == "max_hold_reached":
                max_hold_reached += 1
            elif status == "close_hold":
                close_hold += 1
            elif status == "no_data":
                no_data += 1

    wins = sum(1 for p in all_pnls if p > 0)

    return {
        "days_total": days_total,
        "days_skipped": days_skipped,
        "days_with_run": days_with_run,
        "days_with_candidate": days_with_candidate,
        "total_candidates": total_candidates,
        "total_triggered": total_triggered,
        "gap_up_skip": gap_up_skip,
        "gap_down_skip": gap_down_skip,
        "stop_hit": stop_hit,
        "limit_up_broken": limit_up_broken,
        "max_hold_reached": max_hold_reached,
        "close_hold": close_hold,
        "no_data": no_data,
        "wins": wins,
        "win_rate": wins / total_triggered if total_triggered else 0,
        "avg_pnl": sum(all_pnls) / len(all_pnls) if all_pnls else 0,
        "max_pnl": max(all_pnls) if all_pnls else 0,
        "min_pnl": min(all_pnls) if all_pnls else 0,
        "avg_hold_days": sum(hold_days_list) / len(hold_days_list) if hold_days_list else 0,
    }


def _write_summary_md(output_dir: str, start: str, end: str, records: list, summary: dict):
    lines = []
    lines.append(f"# S5 龙回头回测汇总 {start} ~ {end}")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append(f"- 跑过的交易日: **{summary['days_total']}**")
    lines.append(f"- 跳过 (regime 不适用): {summary['days_skipped']}")
    lines.append(f"- 跑信号的日子: {summary['days_with_run']}")
    lines.append(f"- 出 candidate 的日子: **{summary['days_with_candidate']}**")
    lines.append(f"- 总 candidate 数: **{summary['total_candidates']}**")
    lines.append("")

    if summary["total_triggered"] > 0:
        lines.append("## 触发与盈亏")
        lines.append("")
        lines.append(f"- 触发入场: **{summary['total_triggered']}** / {summary['total_candidates']} ({summary['total_triggered']*100//summary['total_candidates']}%)")
        lines.append(f"- 胜率 (pnl>0): **{summary['wins']}/{summary['total_triggered']} = {summary['win_rate']*100:.0f}%**")
        lines.append(f"- 平均盈亏: **{summary['avg_pnl']:+.2f}%**")
        lines.append(f"- 最高盈: {summary['max_pnl']:+.2f}%")
        lines.append(f"- 最大亏: {summary['min_pnl']:+.2f}%")
        lines.append("")
        lines.append("## 状态分布")
        lines.append("")
        lines.append(f"| 状态 | 数量 | 说明 |")
        lines.append(f"|---|---|---|")
        lines.append(f"| gap_up_skip | {summary['gap_up_skip']} | T+1 高开 >+2%, 未入场 |")
        lines.append(f"| gap_down_skip | {summary['gap_down_skip']} | T+1 低开 <-3% 且日内未回到入场区, 未入场 |")
        lines.append(f"| stop_hit | {summary['stop_hit']} | 入场后跌破 stop_loss, 止损出 |")
        lines.append(f"| limit_up_broken | {summary['limit_up_broken']} | 持仓中触及涨停后炸板, 卖出 |")
        lines.append(f"| max_hold_reached | {summary['max_hold_reached']} | 持仓达 {summary.get('max_hold_days', 15)} 日强制平仓 |")
        lines.append(f"| close_hold | {summary['close_hold']} | 持仓中止 (数据不足等), 按最后一日收盘平仓 |")
        lines.append(f"| no_data | {summary['no_data']} | T+1 无 K 线 (停牌等) |")
        lines.append(f"| 平均持仓天数 | {summary['avg_hold_days']:.1f} 日 | |")
        lines.append("")

    lines.append("## 逐日明细")
    lines.append("")
    lines.append("| 日期 | regime | score | 切换 | 紧急 | 候选 | T+1 平均盈亏 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in records:
        if r["skipped_reason"]:
            lines.append(f"| {r['t_date']} | {r['regime']} | {r['regime_score']} | - | - | **跳过** | - |")
            continue
        avg = None
        if r.get("verify_results"):
            pnls = [x["verification"]["pnl_pct"] for x in r["verify_results"] if x["verification"].get("pnl_pct") is not None]
            if pnls:
                avg = sum(pnls) / len(pnls)
        sw = "✓" if r["switched"] else ""
        em = "🚨" if r["emergency_switch"] else ""
        avg_str = f"{avg:+.2f}%" if avg is not None else "-"
        lines.append(f"| {r['t_date']} | {r['regime']} | {r['regime_score']} | {sw} | {em} | {r['candidates_count']} | {avg_str} |")
    lines.append("")

    path = os.path.join(output_dir, "backtest_summary.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logging.info(f"写入: {path}")


def _write_summary_json(output_dir: str, start: str, end: str, records: list, summary: dict):
    path = os.path.join(output_dir, "backtest_summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"start": start, "end": end, "summary": summary, "daily": records},
            f, ensure_ascii=False, indent=2
        )
    logging.info(f"写入: {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--regime-dir", required=True, help="classifier regime json 目录")
    ap.add_argument("--output-dir", required=True, help="回测输出目录")
    ap.add_argument("--include-gate-closed", action="store_true")
    args = ap.parse_args()
    run_backtest(args.start, args.end, args.regime_dir, args.output_dir, tradable_only=not args.include_gate_closed)


if __name__ == "__main__":
    main()
