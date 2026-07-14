"""v2 regime 六维信号当"过滤器/仓位表"用的实测回测.

同 close-to-close 引擎, 和 ma_trend_filter_backtest.py 可以横向比较.

测试的策略 (7 个):
    fullhold               满仓 HS300 (baseline)
    v2_no_bear             熊 regime 空仓, 其他满仓       — 最宽松过滤器
    v2_strict_hold         熊+弱势震荡 空仓, 其他满仓     — 中等过滤器
    v2_bullish_only        强牛+强势震荡 满仓, 其他空仓   — 最严过滤器
    v2_dynamic_cc          spec §5 graded (0.9/0.7/0.5/0.3/0.2) — cc 引擎重跑
    v2_entry_only_cc       切入强牛/强势震荡 90%, 否则 50% — cc 引擎重跑
    v2_entry_held_5d_cc    切入后持 5 天 90%, 否则 30%   — cc 引擎重跑

    (对照) ma60_filter_cc   从 HS300 MA60 过滤器的结果导入 (前面脚本已跑过)

信号 T 日收盘计算, T+1 日按信号执行.

用法:
    python3 v2_regime_filter_backtest.py                    # 默认 cc 引擎, 不落库
    python3 v2_regime_filter_backtest.py --markdown x.md    # 同时写 md
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import connect  # noqa: E402
from ma_trend_filter_backtest import (  # noqa: E402
    _daily_ret,
    _daily_pnl_with_execution,
    compute_metrics,
    analyze_crash_windows,
    simulate_fullhold,
)

logger = logging.getLogger("v2_filter")

DEFAULT_START = "2020-01-02"
DEFAULT_END = "2026-04-14"

# spec §5 position_limit.total
POSITION_LIMIT = {
    "强牛":   0.90,
    "强势震荡": 0.70,
    "中性震荡": 0.50,
    "弱势震荡": 0.30,
    "熊":     0.20,
}


def load_hs300_ordered(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT trade_date, open, close FROM index_daily "
        "WHERE ts_code='000300.SH' ORDER BY trade_date ASC"
    ).fetchall()
    out = []
    for d, o, c in rows:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        out.append({"date": iso, "open": float(o), "close": float(c)})
    return out


def load_v2_classify(conn) -> dict:
    """{iso_date: (regime_name, switched)}"""
    out = {}
    for d, r, s in conn.execute(
        "SELECT trade_date, regime_name, switched FROM regime_classify_daily "
        "WHERE rules_version='v2' ORDER BY trade_date"
    ):
        out[d] = (r, int(s))
    return out


# --------------------------------------------------------------------------- #
# 策略函数: 接收 (series, idx, classify_dict) → position (0.0 ~ 1.0)
# idx 是 series 里的下标, T = idx
# 信号用 T-1 日 regime 决定 T 日仓位 (避免 look-ahead)
# --------------------------------------------------------------------------- #


def get_prev_regime(series, idx, classify_dict):
    """返回 (regime, switched) of T-1, 若无则 None."""
    if idx == 0:
        return None
    prev_date = series[idx - 1]["date"]
    return classify_dict.get(prev_date)


def strat_fullhold(series, idx, clf):
    return 1.0


def strat_v2_no_bear(series, idx, clf):
    """熊 → 空仓, 其他 → 满仓."""
    prev = get_prev_regime(series, idx, clf)
    if prev is None:
        return 1.0
    return 0.0 if prev[0] == "熊" else 1.0


def strat_v2_strict_hold(series, idx, clf):
    """熊 + 弱势震荡 → 空仓, 其他 → 满仓."""
    prev = get_prev_regime(series, idx, clf)
    if prev is None:
        return 1.0
    return 0.0 if prev[0] in ("熊", "弱势震荡") else 1.0


def strat_v2_bullish_only(series, idx, clf):
    """只在 强牛 + 强势震荡 时满仓, 其他空仓."""
    prev = get_prev_regime(series, idx, clf)
    if prev is None:
        return 0.0
    return 1.0 if prev[0] in ("强牛", "强势震荡") else 0.0


def strat_v2_neutral_plus(series, idx, clf):
    """强牛 + 强势震荡 + 中性震荡 满仓, 其他空仓."""
    prev = get_prev_regime(series, idx, clf)
    if prev is None:
        return 0.0
    return 1.0 if prev[0] in ("强牛", "强势震荡", "中性震荡") else 0.0


def strat_v2_dynamic_cc(series, idx, clf):
    """spec §5 graded."""
    prev = get_prev_regime(series, idx, clf)
    if prev is None:
        return 0.5
    return POSITION_LIMIT.get(prev[0], 0.5)


def strat_v2_entry_only_cc(series, idx, clf):
    """切入强牛/强势震荡 → 90%, 其他 50%."""
    prev = get_prev_regime(series, idx, clf)
    if prev is None:
        return 0.5
    regime, switched = prev
    if switched == 1 and regime in ("强牛", "强势震荡"):
        return 0.9
    return 0.5


def strat_v2_entry_held_5d_cc(series, idx, clf):
    """切入后持 5 天 90%, 其他 30%."""
    for k in range(1, 6):
        i = idx - k
        if i < 0:
            break
        prev_date = series[i]["date"]
        entry = clf.get(prev_date)
        if entry is None:
            continue
        regime, switched = entry
        if switched == 1 and regime in ("强牛", "强势震荡"):
            return 0.9
    return 0.3


STRATEGIES = {
    "fullhold": (strat_fullhold, "满仓 HS300 (baseline)"),
    "v2_no_bear": (strat_v2_no_bear, "熊 空仓, 其他满仓"),
    "v2_strict_hold": (strat_v2_strict_hold, "熊+弱势震荡 空仓"),
    "v2_neutral_plus": (strat_v2_neutral_plus, "中性+强势+强牛 满仓, 弱势+熊 空仓"),
    "v2_bullish_only": (strat_v2_bullish_only, "强牛+强势震荡 满仓, 其他空仓"),
    "v2_dynamic_cc": (strat_v2_dynamic_cc, "spec §5 graded"),
    "v2_entry_only_cc": (strat_v2_entry_only_cc, "切入 90%, 其他 50%"),
    "v2_entry_held_5d_cc": (strat_v2_entry_held_5d_cc, "切入持 5 天 90%, 其他 30%"),
}


# --------------------------------------------------------------------------- #
# 模拟引擎
# --------------------------------------------------------------------------- #


def simulate(strat_fn, series, classify, start_idx, end_idx, engine="cc", execution="next_open"):
    nav = 1.0
    peak = 1.0
    out = []
    days_in = 0
    days_out = 0
    trade_count = 0
    prev_position_eod = None  # 上一日 EOD 持仓 = 今日隔夜段持仓

    for idx in range(start_idx, end_idx + 1):
        r = series[idx]
        if r["open"] <= 0 or r["close"] <= 0:
            continue

        new_position = strat_fn(series, idx, classify)
        old_position = prev_position_eod if prev_position_eod is not None else new_position

        if prev_position_eod is not None and new_position != prev_position_eod:
            trade_count += 1

        if new_position > 0.01:
            days_in += 1
        else:
            days_out += 1

        if execution == "next_open":
            daily_pnl = _daily_pnl_with_execution(series, idx, old_position, new_position)
        else:  # same_close (有 look-ahead)
            daily_pnl = new_position * _daily_ret(series, idx, engine)

        nav *= (1 + daily_pnl)
        if nav > peak:
            peak = nav
        max_dd = (nav / peak) - 1

        out.append({
            "trade_date": r["date"],
            "position": round(new_position, 4),
            "daily_pnl_pct": daily_pnl * 100,
            "cumulative_nav": nav,
            "max_drawdown_to_date": max_dd,
        })
        prev_position_eod = new_position

    return {
        "rows": out,
        "trade_count": trade_count,
        "days_in": days_in,
        "days_out": days_out,
    }


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def render_markdown(metrics, crashes, start, end, engine):
    lines = []
    lines.append("# v2 Regime 信号作为过滤器/仓位表的实测")
    lines.append("")
    lines.append(f"- **样本**: HS300, {start} ~ {end}, {metrics[0]['n']} 个交易日")
    lines.append(f"- **PnL 引擎**: close-to-close (真实买入持有口径)")
    lines.append(f"- **信号**: T-1 日 v2 regime_classify_daily 决定 T 日仓位")
    lines.append(f"- **对照**: fullhold (真 HS300 基准) + 之前 MA60 过滤器的结果")
    lines.append("")
    lines.append("## 1. 总体对比 (按 ret/dd 降序)")
    lines.append("")
    lines.append("| 策略 | 描述 | 总回报 | 年化 | 最大回撤 | 波动 | Sharpe | ret/dd | 在场 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    sorted_m = sorted(metrics, key=lambda m: -m["ret_dd"])
    for m in sorted_m:
        lines.append(
            f"| {m['label']} | {m.get('desc', '')} | {m['total_return_pct']:+.2f}% | "
            f"{m['ann_return_pct']:+.2f}% | {m['max_dd_pct']:+.2f}% | "
            f"{m['ann_vol_pct']:.2f}% | {m['sharpe']:.2f} | {m['ret_dd']:.2f} | "
            f"{m['in_market_pct']:.0f}% |"
        )
    lines.append("")

    lines.append("## 2. 最深回撤区间")
    lines.append("")
    lines.append("| 策略 | 峰值日期 | 谷底日期 | 回撤 |")
    lines.append("|---|---|---|---:|")
    for m in metrics:
        lines.append(
            f"| {m['label']} | {m['worst_dd_from']} | {m['worst_dd_to']} | {m['max_dd_pct']:+.2f}% |"
        )
    lines.append("")

    lines.append("## 3. 崩盘窗口逐个验证")
    lines.append("")
    for c in crashes:
        lines.append(f"### {c['crash']} ({c['range']})")
        lines.append("")
        lines.append("| 策略 | 回撤 | 期末收益 |")
        lines.append("|---|---:|---:|")
        for m in metrics:
            cell = c.get(m['label'])
            if cell is None:
                lines.append(f"| {m['label']} | — | — |")
            else:
                lines.append(f"| {m['label']} | {cell['dd']:+.2f}% | {cell['ret']:+.2f}% |")
        lines.append("")

    lines.append("---")
    lines.append(f"*生成时间: {datetime.now().isoformat(timespec='seconds')}*")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--engine", default="cc", choices=["cc", "intraday"])
    parser.add_argument(
        "--execution",
        default="next_open",
        choices=["next_open", "same_close"],
        help="执行模型: next_open (T-1 信号 T 日 open 交易, 默认) 或 same_close (有 look-ahead)",
    )
    parser.add_argument("--markdown", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    conn = connect()
    series = load_hs300_ordered(conn)
    classify = load_v2_classify(conn)
    logger.info(f"HS300: {len(series)} 天; v2 classify: {len(classify)} 天")

    date_to_idx = {r["date"]: i for i, r in enumerate(series)}
    start_idx = None
    end_idx = None
    for i, r in enumerate(series):
        if start_idx is None and r["date"] >= args.start:
            start_idx = i
        if r["date"] <= args.end:
            end_idx = i
    if start_idx is None or end_idx is None:
        logger.error("样本区间为空")
        return 1
    logger.info(
        f"回测 [{series[start_idx]['date']} ~ {series[end_idx]['date']}], "
        f"{end_idx - start_idx + 1} 天, 引擎={args.engine}"
    )

    logger.info(f"引擎={args.engine}, 执行={args.execution}")
    sims = {}
    for sid, (fn, desc) in STRATEGIES.items():
        sim = simulate(fn, series, classify, start_idx, end_idx, args.engine, args.execution)
        sims[sid] = sim

    metrics = []
    for sid, sim in sims.items():
        m = compute_metrics(sim, sid)
        m["desc"] = STRATEGIES[sid][1]
        metrics.append(m)

    crashes = analyze_crash_windows(series, sims, None)

    print()
    print("=" * 100)
    print(
        f"{'策略':<22}{'描述':<24}{'总回报':>10}{'最大回撤':>11}"
        f"{'Sharpe':>8}{'ret/dd':>8}{'在场':>7}"
    )
    print("-" * 100)
    sorted_m = sorted(metrics, key=lambda m: -m["ret_dd"])
    for m in sorted_m:
        print(
            f"{m['label']:<22}{m['desc'][:22]:<24}{m['total_return_pct']:>+9.2f}%"
            f"{m['max_dd_pct']:>+10.2f}%{m['sharpe']:>8.2f}{m['ret_dd']:>8.2f}"
            f"{m['in_market_pct']:>6.0f}%"
        )
    print()

    print("崩盘窗口对比:")
    print("-" * 100)
    for c in crashes:
        print(f"[{c['crash']}] {c['range']}")
        for m in metrics:
            cell = c.get(m['label'])
            if cell is None:
                continue
            print(f"    {m['label']:<22}  回撤 {cell['dd']:+6.2f}%  收益 {cell['ret']:+6.2f}%")
        print()

    if args.markdown:
        md = render_markdown(metrics, crashes, args.start, args.end, args.engine)
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(md)
        logger.info(f"Markdown 已写入 {args.markdown}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
