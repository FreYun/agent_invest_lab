"""严格无 look-ahead 的 v2 信号有效性评估。

核心口径:
    T 日 classifier 在收盘后产出 → T+1 日开盘操作
    forward return N 日 = HS300(T+1+N close) / HS300(T+1 open) - 1

评估维度:
    1. IC: T 日 total_score vs T+1 开始的 N 日 forward return (Spearman)
    2. 各 regime 的 N 日 forward return 分布 (mean/median/std/dir_win)
    3. 切换首日 effectiveness: switched=1 子集的 forward return 分布
    4. 按 regime 方向的胜率 (多头 regime → 正收益算赢; 空头 regime → 负收益算赢)

输出:
    stdout: 对齐表格
    --markdown 选项: 输出 markdown 片段供报告复用
"""

from __future__ import annotations

import argparse
import logging
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import connect  # noqa: E402

logger = logging.getLogger("eval")

HORIZONS = [1, 3, 5, 10, 20]
BULL_REGIMES = {"强牛", "强势震荡"}
BEAR_REGIMES = {"熊"}


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #


def load_v2_with_hs300(conn) -> list[dict]:
    """返回按日升序的 v2 分类 + HS300 open/close (iso date)。"""
    rows = conn.execute(
        """
        SELECT c.trade_date, c.regime_name, c.switched, c.total_score,
               c.score_ma_position, c.score_advance_decline,
               c.score_sentiment_delta, c.score_sentiment_index,
               c.score_streak_height, c.score_volume_trend
        FROM regime_classify_daily c
        WHERE c.rules_version = 'v2'
        ORDER BY c.trade_date ASC
        """
    ).fetchall()

    hs300 = {}
    for d, o, c in conn.execute(
        "SELECT trade_date, open, close FROM index_daily "
        "WHERE ts_code='000300.SH' ORDER BY trade_date"
    ):
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        hs300[iso] = (float(o), float(c))

    out = []
    for r in rows:
        date = r[0]
        if date not in hs300:
            continue
        out.append({
            "date": date,
            "regime": r[1],
            "switched": int(r[2]),
            "total_score": int(r[3]),
            "ma_position": int(r[4]),
            "advance_decline": int(r[5]),
            "sentiment_delta": int(r[6]),
            "sentiment_index": int(r[7]),
            "streak_height": int(r[8]),
            "volume_trend": int(r[9]),
            "open": hs300[date][0],
            "close": hs300[date][1],
        })
    return out


# --------------------------------------------------------------------------- #
# forward return: 严格 T+1 open → T+1+N close
# --------------------------------------------------------------------------- #


def compute_forward_returns(rows: list[dict], horizons: list[int]) -> None:
    """给每行 rows[i] 加上 fwd_{h}: 用 T+1 open 进, T+h close 出, 持 h 个交易日。

    h=1: T+1 open → T+1 close (当日日内)
    h=5: T+1 open → T+5 close
    越界的 fwd 为 None。
    """
    n = len(rows)
    for i in range(n):
        entry_idx = i + 1
        if entry_idx >= n:
            for h in horizons:
                rows[i][f"fwd_{h}"] = None
            continue
        p0 = rows[entry_idx]["open"]
        for h in horizons:
            exit_idx = i + h  # T+1 open → T+h close = 持 h 天
            if exit_idx >= n:
                rows[i][f"fwd_{h}"] = None
            else:
                rows[i][f"fwd_{h}"] = rows[exit_idx]["close"] / p0 - 1


# --------------------------------------------------------------------------- #
# 统计工具
# --------------------------------------------------------------------------- #


def _rank(values: list[float]) -> list[float]:
    """返回 values 的秩 (平均秩处理并列)。"""
    indexed = sorted(range(len(values)), key=lambda k: values[k])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return 0.0
    rx = _rank(xs)
    ry = _rank(ys)
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(len(rx)))
    dx = sum((r - mx) ** 2 for r in rx) ** 0.5
    dy = sum((r - my) ** 2 for r in ry) ** 0.5
    return num / (dx * dy) if dx * dy > 0 else 0.0


def describe(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": 0, "median": 0, "std": 0}
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "std": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


# --------------------------------------------------------------------------- #
# 评估块
# --------------------------------------------------------------------------- #


def ic_analysis(rows: list[dict]) -> dict:
    """IC = Spearman(T 日 total_score, T+1 开始持 N 日的 return)"""
    out = {}
    for h in HORIZONS:
        xs, ys = [], []
        for r in rows:
            if r.get(f"fwd_{h}") is not None:
                xs.append(r["total_score"])
                ys.append(r[f"fwd_{h}"])
        out[h] = {"ic": spearman(xs, ys), "n": len(xs)}
    return out


def six_dim_ic(rows: list[dict], horizon: int = 5) -> dict:
    """每个维度单独跟 N 日 forward return 算 IC (找哪个维度最有预测力)"""
    dims = [
        "ma_position", "advance_decline", "sentiment_delta",
        "sentiment_index", "streak_height", "volume_trend",
    ]
    out = {}
    for d in dims:
        xs, ys = [], []
        for r in rows:
            fwd = r.get(f"fwd_{horizon}")
            if fwd is not None:
                xs.append(r[d])
                ys.append(fwd)
        out[d] = spearman(xs, ys)
    return out


def per_regime_forward_return(rows: list[dict]) -> dict:
    """每个 regime 下的 N 日 forward return 分布 + 方向胜率。"""
    buckets: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        reg = r["regime"]
        for h in HORIZONS:
            v = r.get(f"fwd_{h}")
            if v is not None:
                buckets[reg][h].append(v)

    out = {}
    for reg, by_h in buckets.items():
        reg_out = {}
        for h, vals in by_h.items():
            d = describe(vals)
            if not vals:
                reg_out[h] = d | {"win_raw": 0.0, "dir_win": 0.0}
                continue
            wins_raw = sum(1 for v in vals if v > 0) / len(vals)
            # 方向胜率: 多头 regime 看涨赢, 空头 regime 看跌赢, 震荡看波动小?
            # 这里按 regime 方向性定义: bull → 涨赢, bear → 跌赢, 其他不算方向
            if reg in BULL_REGIMES:
                dir_win = wins_raw
            elif reg in BEAR_REGIMES:
                dir_win = 1 - wins_raw
            else:
                dir_win = max(wins_raw, 1 - wins_raw)  # 震荡取两侧最大 (作为对比)
            reg_out[h] = d | {"win_raw": wins_raw, "dir_win": dir_win}
        out[reg] = reg_out
    return out


def switched_day_analysis(rows: list[dict]) -> dict:
    """切换首日 (switched=1) 子集的 forward return 分布, 按目标 regime 分。"""
    buckets: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["switched"] != 1:
            continue
        reg = r["regime"]
        for h in HORIZONS:
            v = r.get(f"fwd_{h}")
            if v is not None:
                buckets[reg][h].append(v)
    out = {}
    for reg, by_h in buckets.items():
        reg_out = {}
        for h, vals in by_h.items():
            d = describe(vals)
            if not vals:
                reg_out[h] = d | {"win_raw": 0.0}
                continue
            wins = sum(1 for v in vals if v > 0) / len(vals)
            reg_out[h] = d | {"win_raw": wins}
        out[reg] = reg_out
    return out


# --------------------------------------------------------------------------- #
# 打印 / 输出
# --------------------------------------------------------------------------- #


def _pct(x: float, sign: bool = True) -> str:
    sign_s = "+" if sign and x >= 0 else ""
    return f"{sign_s}{x*100:.2f}%"


def print_text_report(rows, ic, dim_ic, per_regime, switched):
    n = len(rows)
    print()
    print("=" * 78)
    print(f"v2 信号有效性评估  (无 look-ahead, T+1 open → T+1+N close)")
    print(f"样本: {rows[0]['date']} ~ {rows[-1]['date']}  共 {n} 个交易日")
    print("=" * 78)

    print("\n【1】total_score 与 forward return 的 Spearman IC")
    print(f"  {'horizon':<12} {'N':>6} {'IC':>10}")
    for h in HORIZONS:
        d = ic[h]
        print(f"  {str(h)+'d':<12} {d['n']:>6} {d['ic']:>+10.4f}")

    print("\n【2】单维度 5d IC (哪些维度有预测力)")
    print(f"  {'dimension':<20} {'IC':>10}")
    for dim, val in sorted(dim_ic.items(), key=lambda x: abs(x[1]), reverse=True):
        print(f"  {dim:<20} {val:>+10.4f}")

    print("\n【3】各 regime 下, 5d forward return 分布")
    print(f"  {'regime':<10} {'N':>5} {'mean':>10} {'median':>10} {'std':>10} "
          f"{'raw胜率':>9} {'方向胜率':>10}")
    order = ["强牛", "强势震荡", "中性震荡", "弱势震荡", "熊"]
    for reg in order:
        d = per_regime.get(reg, {}).get(5)
        if not d or d["n"] == 0:
            continue
        print(f"  {reg:<8} {d['n']:>5} "
              f"{_pct(d['mean']):>10} {_pct(d['median']):>10} {_pct(d['std'], False):>10} "
              f"{d['win_raw']*100:>8.1f}% {d['dir_win']*100:>9.1f}%")

    print("\n【4】各 regime 下, 5d/10d/20d 持有平均收益 (绝对)")
    print(f"  {'regime':<10} {'5d':>10} {'10d':>10} {'20d':>10}")
    for reg in order:
        ds = [per_regime.get(reg, {}).get(h) for h in (5, 10, 20)]
        if not any(d and d["n"] > 0 for d in ds):
            continue
        vals = [_pct(d["mean"]) if d and d["n"] > 0 else "—" for d in ds]
        print(f"  {reg:<8} {vals[0]:>10} {vals[1]:>10} {vals[2]:>10}")

    print("\n【5】切换首日 (switched=1) 后 5d 持有收益")
    print(f"  {'target_regime':<14} {'N':>5} {'mean':>10} {'median':>10} {'raw胜率':>9}")
    for reg in order:
        d = switched.get(reg, {}).get(5)
        if not d or d["n"] == 0:
            continue
        print(f"  {reg:<12} {d['n']:>5} "
              f"{_pct(d['mean']):>10} {_pct(d['median']):>10} "
              f"{d['win_raw']*100:>8.1f}%")

    print()
    print("=" * 78)


def print_markdown_report(rows, ic, dim_ic, per_regime, switched, out_path: str):
    """把评估结果写成 markdown 报告到 references/。"""
    lines = []
    A = lines.append

    A("# v2 regime 信号有效性评估 (严格无 look-ahead)")
    A("")
    A(f"- **样本区间**: {rows[0]['date']} ~ {rows[-1]['date']}")
    A(f"- **样本量**: {len(rows)} 个交易日")
    A(f"- **口径**: T 日 classifier 收盘后出信号 → T+1 日 HS300 开盘进 → T+1+N 日 HS300 收盘出")
    A(f"- **规则版本**: v2 (rules_v2.py, 含 3 日冷却 + 下行放宽 + 上行放宽)")
    A("")
    A("## 1. total_score 预测力 (Spearman IC)")
    A("")
    A("| 持有 horizon | 样本 N | IC |")
    A("|---|---:|---:|")
    for h in HORIZONS:
        d = ic[h]
        A(f"| {h} 日 | {d['n']} | {d['ic']:+.4f} |")
    A("")
    A("> **解读**: IC 绝对值 < 0.05 通常认为无预测力。|IC| ≥ 0.03 且方向一致可作为弱因子。")

    A("")
    A("## 2. 单维度 5d IC")
    A("")
    A("| 维度 | IC | 备注 |")
    A("|---|---:|---|")
    desc_map = {
        "ma_position": "指数 vs 均线",
        "advance_decline": "涨跌家数",
        "sentiment_delta": "涨停减跌停",
        "sentiment_index": "情绪评分 0-100",
        "streak_height": "最高连板",
        "volume_trend": "成交量趋势",
    }
    for dim, val in sorted(dim_ic.items(), key=lambda x: abs(x[1]), reverse=True):
        A(f"| {dim} | {val:+.4f} | {desc_map.get(dim, '')} |")

    A("")
    A("## 3. 各 regime 的 5d forward return 分布")
    A("")
    A("| regime | N | 平均 | 中位 | 标准差 | 涨概率 | 方向胜率 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    order = ["强牛", "强势震荡", "中性震荡", "弱势震荡", "熊"]
    for reg in order:
        d = per_regime.get(reg, {}).get(5)
        if not d or d["n"] == 0:
            continue
        A(f"| {reg} | {d['n']} | {_pct(d['mean'])} | {_pct(d['median'])} | "
          f"{_pct(d['std'], False)} | {d['win_raw']*100:.1f}% | {d['dir_win']*100:.1f}% |")
    A("")
    A("> **方向胜率**: 多头 regime (强牛/强势震荡) 以涨为赢; 空头 regime (熊) 以跌为赢; "
      "震荡取涨跌两侧较大值 (仅作对照)。")
    A("")
    A("## 4. 不同 horizon 的 regime 平均收益")
    A("")
    A("| regime | 5d | 10d | 20d |")
    A("|---|---:|---:|---:|")
    for reg in order:
        ds = [per_regime.get(reg, {}).get(h) for h in (5, 10, 20)]
        if not any(d and d["n"] > 0 for d in ds):
            continue
        vals = [_pct(d["mean"]) if d and d["n"] > 0 else "—" for d in ds]
        A(f"| {reg} | {vals[0]} | {vals[1]} | {vals[2]} |")

    A("")
    A("## 5. 切换首日 (switched=1) 事件分析")
    A("")
    A("> 含义: T 日 regime 发生切换, T+1 开盘以新 regime 操作, 持 N 日。")
    A("")
    A("| 目标 regime | N | 5d 平均 | 5d 中位 | 5d 涨概率 |")
    A("|---|---:|---:|---:|---:|")
    for reg in order:
        d = switched.get(reg, {}).get(5)
        if not d or d["n"] == 0:
            continue
        A(f"| {reg} | {d['n']} | {_pct(d['mean'])} | {_pct(d['median'])} | {d['win_raw']*100:.1f}% |")

    A("")
    A("## 6. 结论")
    A("")
    A("(待人工根据数据填充)")
    A("")
    A("## 7. 数据来源 & 可复现")
    A("")
    A("- 数据库: `/home/rooot/agent_invest_lab/data/market.db`")
    A("- 分类表: `regime_classify_daily WHERE rules_version='v2'`")
    A("- 指数表: `index_daily WHERE ts_code='000300.SH'` (HS300)")
    A("- 评估脚本: `scripts/backfill/evaluate_signal.py`")
    A("- 重跑: `python3 scripts/backfill/evaluate_signal.py --markdown <path>`")
    A("")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"markdown 已写入: {out_path}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", default=None,
                        help="若指定, 把评估结果额外写成 markdown 报告")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    conn = connect()
    rows = load_v2_with_hs300(conn)
    if not rows:
        logger.error("regime_classify_daily v2 无数据")
        return 1
    compute_forward_returns(rows, HORIZONS)

    ic = ic_analysis(rows)
    dim_ic = six_dim_ic(rows, horizon=5)
    per_regime = per_regime_forward_return(rows)
    switched = switched_day_analysis(rows)

    print_text_report(rows, ic, dim_ic, per_regime, switched)

    if args.markdown:
        print_markdown_report(rows, ic, dim_ic, per_regime, switched, args.markdown)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
