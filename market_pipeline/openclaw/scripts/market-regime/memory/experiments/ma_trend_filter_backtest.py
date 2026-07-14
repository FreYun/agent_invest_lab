"""MA 趋势过滤器回测 — 单目标: 控回撤.

规则 (以 MA200 为例):
    T 日 HS300 收盘 >= MA200(T) → T+1 日持仓 100%
    T 日 HS300 收盘 <  MA200(T) → T+1 日持仓 0%

MA 用"T 日及之前 N 天的 close 均值" (包含 T 日), 符合实盘可用的信号。
信号 T 日收盘后触发, T+1 日开盘执行, 无 look-ahead。

支持两种 PnL 引擎, 用 --engine 切换:
    --engine cc       (默认, 推荐) close-to-close: daily_ret = close[T]/close[T-1] - 1
                      真实买入持有口径, 捕获过夜 gap, 能反映 HS300 真实走势
    --engine intraday 日内 only: daily_ret = close[T]/open[T] - 1
                      和 simulate_strategies.py 同口径, 但该口径会严重虚高净值 (丢失过夜 gap),
                      只有在需要和老 regime_strategy_nav 可比时才用

对 MA 过滤器, 信号在 T 日收盘触发, T+1 日起按信号持仓:
    cc 引擎: position[T+1..T+k] × (close[T+1..T+k] / close[T..T+k-1] - 1)
    intraday 引擎: position[T+1..T+k] × (close[T+1..T+k] / open[T+1..T+k] - 1)

同时跑 MA60 / MA120 / MA200 三个窗口做敏感性对照。

用法:
    python3 ma_trend_filter_backtest.py                          # 默认跑 60/120/200, 落库 + 打印
    python3 ma_trend_filter_backtest.py --start 2020-01-02       # 指定起始日
    python3 ma_trend_filter_backtest.py --markdown report.md     # 同时写 markdown
    python3 ma_trend_filter_backtest.py --no-db                  # 不落库 (只打印 + 可选 md)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import connect  # noqa: E402

logger = logging.getLogger("ma_filter")

DEFAULT_START = "2020-01-02"
DEFAULT_END = "2026-04-14"
DEFAULT_WINDOWS = [60, 120, 200]


def load_hs300_ordered(conn) -> list[dict]:
    """返回 [{date, open, close}, ...] 按日期升序 (iso 格式 YYYY-MM-DD)."""
    rows = conn.execute(
        "SELECT trade_date, open, close FROM index_daily "
        "WHERE ts_code='000300.SH' ORDER BY trade_date ASC"
    ).fetchall()
    out = []
    for d, o, c in rows:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        out.append({"date": iso, "open": float(o), "close": float(c)})
    return out


def compute_ma(values: list[float], window: int) -> list[float | None]:
    """窗口移动平均; 前 window-1 项为 None. 含当前值."""
    out: list[float | None] = []
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= window:
            running -= values[i - window]
        if i >= window - 1:
            out.append(running / window)
        else:
            out.append(None)
    return out


# --------------------------------------------------------------------------- #
# 策略定义
# --------------------------------------------------------------------------- #


def build_ma_signals(series: list[dict], window: int) -> list[dict]:
    """给每行加 ma / above_ma 字段."""
    closes = [r["close"] for r in series]
    ma = compute_ma(closes, window)
    for i, r in enumerate(series):
        r[f"ma{window}"] = ma[i]
        r[f"above_ma{window}"] = (ma[i] is not None) and (r["close"] >= ma[i])
    return series


def _daily_ret(series: list[dict], idx: int, engine: str) -> float:
    """计算第 idx 天的 daily return (不考虑持仓变化, 只用于无 trade 日)."""
    r = series[idx]
    if engine == "intraday":
        if r["open"] <= 0:
            return 0.0
        return r["close"] / r["open"] - 1
    elif engine == "cc":
        if idx == 0:
            return 0.0
        prev_close = series[idx - 1]["close"]
        if prev_close <= 0:
            return 0.0
        return r["close"] / prev_close - 1
    else:
        raise ValueError(f"unknown engine: {engine}")


def _daily_pnl_with_execution(
    series: list[dict],
    idx: int,
    old_position: float,
    new_position: float,
) -> float:
    """严格 T+1 open 建仓 / 离场的日 PnL 计算.

    T 日的 P&L 分两段:
      隔夜段 (close(T-1) → open(T)): 拿旧仓位 (old_position)
      日内段 (open(T) → close(T)):  拿新仓位 (new_position, 即 T-1 信号)

    若 old == new (无交易日): 等同 close-to-close 全段持仓.
    若 old != new (交易日):   体现真实执行损耗.
    """
    r = series[idx]
    if idx == 0 or r["open"] <= 0 or r["close"] <= 0:
        return 0.0
    prev_close = series[idx - 1]["close"]
    if prev_close <= 0:
        return 0.0
    overnight_ret = r["open"] / prev_close - 1   # T-1 close → T open
    intraday_ret = r["close"] / r["open"] - 1    # T open → T close
    return old_position * overnight_ret + new_position * intraday_ret


def simulate_filter(
    series: list[dict],
    window: int,
    start_idx: int,
    end_idx: int,
    engine: str = "cc",
    execution: str = "next_open",
) -> dict:
    """跑 MA{window} 过滤策略.

    execution 选项:
      "next_open" (推荐, 默认) — T-1 信号, T 日 open 交易. 隔夜段用旧仓位, 日内段用新仓位.
                                  无交易日退化为 close-to-close.
      "same_close"              — T-1 信号即刻以 T-1 close 建仓 (look-ahead 旧版本, 保留做对比).
    """
    field = f"above_ma{window}"
    nav = 1.0
    peak = 1.0
    out = []

    last_signal = False
    trade_count = 0
    days_in = 0
    days_out = 0

    for idx in range(start_idx, end_idx + 1):
        r = series[idx]
        if r["open"] <= 0 or r["close"] <= 0:
            continue

        # new_position: T-1 日收盘信号 → T 日仓位
        prev = series[idx - 1] if idx > 0 else None
        new_position = 1.0 if (prev and prev[field]) else 0.0

        # old_position: T-2 日收盘信号 → T-1 日仓位 = 隔夜还在持的仓位
        prev2 = series[idx - 2] if idx >= 2 else None
        old_position = 1.0 if (prev2 and prev2[field]) else 0.0

        if prev is not None and bool(prev[field]) != last_signal:
            trade_count += 1
            last_signal = bool(prev[field])

        if new_position > 0:
            days_in += 1
        else:
            days_out += 1

        if execution == "same_close":
            daily_pnl = new_position * _daily_ret(series, idx, engine)
        elif execution == "next_open":
            daily_pnl = _daily_pnl_with_execution(series, idx, old_position, new_position)
        else:
            raise ValueError(f"unknown execution: {execution}")

        nav *= (1 + daily_pnl)
        if nav > peak:
            peak = nav
        max_dd = (nav / peak) - 1

        out.append({
            "trade_date": r["date"],
            "position": new_position,
            "daily_pnl_pct": daily_pnl * 100,
            "cumulative_nav": nav,
            "max_drawdown_to_date": max_dd,
        })

    return {
        "rows": out,
        "trade_count": trade_count,
        "days_in": days_in,
        "days_out": days_out,
    }


def simulate_fullhold(
    series: list[dict],
    start_idx: int,
    end_idx: int,
    engine: str = "cc",
    execution: str = "next_open",
) -> dict:
    """fullhold baseline: 每日满仓. (execution 参数其实用不着, 但保持接口一致)"""
    nav = 1.0
    peak = 1.0
    out = []

    for idx in range(start_idx, end_idx + 1):
        r = series[idx]
        if r["open"] <= 0 or r["close"] <= 0:
            continue
        # fullhold old=new=1, 两种 execution 结果等价
        if execution == "next_open":
            daily_pnl = _daily_pnl_with_execution(series, idx, 1.0, 1.0)
        else:
            daily_pnl = _daily_ret(series, idx, engine)
        nav *= (1 + daily_pnl)
        if nav > peak:
            peak = nav
        max_dd = (nav / peak) - 1
        out.append({
            "trade_date": r["date"],
            "position": 1.0,
            "daily_pnl_pct": daily_pnl * 100,
            "cumulative_nav": nav,
            "max_drawdown_to_date": max_dd,
        })

    return {"rows": out, "trade_count": 0, "days_in": len(out), "days_out": 0}


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #


def compute_metrics(sim: dict, label: str) -> dict:
    rows = sim["rows"]
    if not rows:
        return {"label": label, "n": 0}
    total_ret = (rows[-1]["cumulative_nav"] - 1) * 100
    max_dd = min(r["max_drawdown_to_date"] for r in rows) * 100
    ret_dd = abs(total_ret / max_dd) if max_dd != 0 else 0

    # 年化收益 (以 244 交易日)
    n_days = len(rows)
    years = n_days / 244
    ann_ret = ((1 + total_ret / 100) ** (1 / years) - 1) * 100 if years > 0 else 0

    # 年化波动 (日波动 × sqrt(244))
    pnls = [r["daily_pnl_pct"] / 100 for r in rows]
    mean_pnl = sum(pnls) / len(pnls)
    var = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
    ann_vol = (var ** 0.5) * (244 ** 0.5) * 100

    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0

    # 找最大回撤发生的日期和持续时长
    peak_nav = 1.0
    peak_date = rows[0]["trade_date"]
    worst_dd = 0.0
    worst_dd_date = rows[0]["trade_date"]
    worst_peak_date = rows[0]["trade_date"]
    for r in rows:
        if r["cumulative_nav"] > peak_nav:
            peak_nav = r["cumulative_nav"]
            peak_date = r["trade_date"]
        dd = r["cumulative_nav"] / peak_nav - 1
        if dd < worst_dd:
            worst_dd = dd
            worst_dd_date = r["trade_date"]
            worst_peak_date = peak_date

    return {
        "label": label,
        "n": n_days,
        "total_return_pct": total_ret,
        "ann_return_pct": ann_ret,
        "max_dd_pct": max_dd,
        "ann_vol_pct": ann_vol,
        "sharpe": sharpe,
        "ret_dd": ret_dd,
        "worst_dd_from": worst_peak_date,
        "worst_dd_to": worst_dd_date,
        "trade_count": sim["trade_count"],
        "days_in": sim["days_in"],
        "days_out": sim["days_out"],
        "in_market_pct": sim["days_in"] / (sim["days_in"] + sim["days_out"]) * 100
            if (sim["days_in"] + sim["days_out"]) > 0 else 0,
    }


def analyze_crash_windows(series: list[dict], sims: dict, windows: dict) -> dict:
    """对已知崩盘窗口分别算 fullhold / 过滤器的回撤.

    A 股已知的系统性崩盘窗口 (经验):
        2015 股灾:              2015-06-12 ~ 2015-09-30  (HS300 -45%, CSI1000 -55%)
        2016 熔断:              2016-01-04 ~ 2016-02-29  (双向熔断, 指数 -25%)
        2018 全年熊:            2018-01-25 ~ 2018-12-28  (流动性/贸易战, -32%)
        2020 COVID:            2020-02-01 ~ 2020-04-30
        2022 三杀 (俄乌+疫情+地产): 2021-12-01 ~ 2022-10-31
        2024-Q1 微盘股:         2024-01-15 ~ 2024-02-29
    """
    crashes = [
        ("2015 股灾", "2015-06-12", "2015-09-30"),
        ("2016 熔断", "2016-01-04", "2016-02-29"),
        ("2018 全年熊", "2018-01-25", "2018-12-28"),
        ("2020 COVID", "2020-02-01", "2020-04-30"),
        ("2022 三杀", "2021-12-01", "2022-10-31"),
        ("2024-Q1", "2024-01-15", "2024-02-29"),
    ]
    result = []
    for name, start, end in crashes:
        row_dict = {"crash": name, "range": f"{start} ~ {end}"}
        for label, sim in sims.items():
            rows = [r for r in sim["rows"] if start <= r["trade_date"] <= end]
            if not rows:
                row_dict[label] = None
                continue
            nav_start = rows[0]["cumulative_nav"]
            nav_min = min(r["cumulative_nav"] for r in rows)
            nav_end = rows[-1]["cumulative_nav"]
            dd = (nav_min / nav_start - 1) * 100
            ret = (nav_end / nav_start - 1) * 100
            row_dict[label] = {"dd": dd, "ret": ret}
        result.append(row_dict)
    return result


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #


def write_to_db(conn, strategy_id: str, sim: dict) -> int:
    rows = sim["rows"]
    if not rows:
        return 0
    sql = """
        INSERT OR REPLACE INTO regime_strategy_nav
        (trade_date, strategy_id, position, daily_pnl_pct,
         cumulative_nav, max_drawdown_to_date)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    batch = [
        (
            r["trade_date"],
            strategy_id,
            round(r["position"], 4),
            round(r["daily_pnl_pct"], 6),
            round(r["cumulative_nav"], 6),
            round(r["max_drawdown_to_date"], 6),
        )
        for r in rows
    ]
    conn.executemany(sql, batch)
    conn.commit()
    return len(batch)


# --------------------------------------------------------------------------- #
# Markdown 输出
# --------------------------------------------------------------------------- #


def render_markdown(
    metrics: list[dict],
    crashes: list[dict],
    start: str,
    end: str,
    windows: list[int],
    engine: str = "cc",
) -> str:
    lines = []
    lines.append("# MA 趋势过滤器回测 — 单目标控回撤")
    lines.append("")
    lines.append(f"- **样本**: HS300, {start} ~ {end}, {metrics[0]['n']} 个交易日")
    engine_desc = {
        "cc": "close-to-close (真实买入持有口径, 含过夜 gap)",
        "intraday": "日内 only (close/open - 1), 会虚高净值, 不推荐做长期回测",
    }[engine]
    lines.append(f"- **PnL 引擎**: {engine_desc}")
    lines.append(f"- **信号**: T 日收盘计算 above_MA, T+1 日按信号调仓 (0 or 100%)")
    lines.append("")
    lines.append("## 1. 总体对比")
    lines.append("")
    lines.append("| 策略 | 总回报 | 年化收益 | 最大回撤 | 年化波动 | Sharpe | ret/dd | 在场比例 | 交易次数 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for m in metrics:
        lines.append(
            f"| {m['label']} | {m['total_return_pct']:+.2f}% | {m['ann_return_pct']:+.2f}% | "
            f"{m['max_dd_pct']:+.2f}% | {m['ann_vol_pct']:.2f}% | {m['sharpe']:.2f} | "
            f"{m['ret_dd']:.2f} | {m['in_market_pct']:.0f}% | {m['trade_count']} |"
        )
    lines.append("")

    # 最深回撤期
    lines.append("## 2. 最深回撤区间")
    lines.append("")
    lines.append("| 策略 | 峰值日期 | 谷底日期 | 回撤 |")
    lines.append("|---|---|---|---:|")
    for m in metrics:
        lines.append(
            f"| {m['label']} | {m['worst_dd_from']} | {m['worst_dd_to']} | {m['max_dd_pct']:+.2f}% |"
        )
    lines.append("")

    # 崩盘窗口对比
    lines.append("## 3. 已知崩盘窗口表现")
    lines.append("")
    header = "| 崩盘 | 区间 |"
    sep = "|---|---|"
    for m in metrics:
        header += f" {m['label']} 回撤 | {m['label']} 收益 |"
        sep += "---:|---:|"
    lines.append(header)
    lines.append(sep)
    for c in crashes:
        row = f"| {c['crash']} | {c['range']} |"
        for m in metrics:
            label = m['label']
            cell = c.get(label)
            if cell is None:
                row += " — | — |"
            else:
                row += f" {cell['dd']:+.2f}% | {cell['ret']:+.2f}% |"
        lines.append(row)
    lines.append("")

    # 结论模板
    lines.append("## 4. 诚实解读 (自动生成)")
    lines.append("")
    fh = next((m for m in metrics if m['label'] == 'fullhold'), None)
    if fh:
        for m in metrics:
            if m['label'] == 'fullhold':
                continue
            dd_delta = m['max_dd_pct'] - fh['max_dd_pct']  # 负数表示更少回撤
            ret_delta = m['total_return_pct'] - fh['total_return_pct']  # 负数表示代价
            lines.append(
                f"- **{m['label']}**: 最大回撤 {m['max_dd_pct']:+.2f}% (比 fullhold {dd_delta:+.2f} pp), "
                f"总回报 {m['total_return_pct']:+.2f}% (比 fullhold {ret_delta:+.2f} pp), "
                f"ret/dd {m['ret_dd']:.2f} (vs fullhold {fh['ret_dd']:.2f})"
            )
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
    parser.add_argument(
        "--windows",
        default=",".join(str(w) for w in DEFAULT_WINDOWS),
        help="逗号分隔的 MA 窗口, e.g. 60,120,200",
    )
    parser.add_argument(
        "--engine",
        default="cc",
        choices=["cc", "intraday"],
        help="PnL 引擎: cc (close-to-close, 真实口径, 默认) 或 intraday (日内 only, 旧口径)",
    )
    parser.add_argument(
        "--execution",
        default="next_open",
        choices=["next_open", "same_close"],
        help="执行模型: next_open (T-1 信号 T 日 open 交易, 默认, 推荐) 或 "
             "same_close (T-1 信号即 T-1 close 建仓, 有 look-ahead, 仅用于对比)",
    )
    parser.add_argument("--no-db", action="store_true", help="不落库")
    parser.add_argument("--markdown", default=None, help="同时写 markdown 报告")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    conn = connect()
    series = load_hs300_ordered(conn)
    logger.info(f"加载 HS300 {len(series)} 天, 范围 {series[0]['date']} ~ {series[-1]['date']}")

    windows = [int(w.strip()) for w in args.windows.split(",") if w.strip()]
    for w in windows:
        build_ma_signals(series, w)

    # 定位起止下标
    date_to_idx = {r["date"]: i for i, r in enumerate(series)}
    if args.start not in date_to_idx:
        # 找 >= args.start 的第一个日期
        candidates = [r for r in series if r["date"] >= args.start]
        if not candidates:
            logger.error(f"找不到 >= {args.start} 的日期")
            return 1
        start_idx = date_to_idx[candidates[0]["date"]]
    else:
        start_idx = date_to_idx[args.start]

    if args.end not in date_to_idx:
        candidates = [r for r in series if r["date"] <= args.end]
        if not candidates:
            logger.error(f"找不到 <= {args.end} 的日期")
            return 1
        end_idx = date_to_idx[candidates[-1]["date"]]
    else:
        end_idx = date_to_idx[args.end]

    logger.info(
        f"回测区间 [{series[start_idx]['date']} ~ {series[end_idx]['date']}], "
        f"共 {end_idx - start_idx + 1} 天"
    )

    logger.info(f"PnL 引擎: {args.engine}, 执行模型: {args.execution}")

    # 跑 fullhold
    sims = {}
    sims["fullhold"] = simulate_fullhold(series, start_idx, end_idx, args.engine, args.execution)

    # 跑每个 MA 窗口
    for w in windows:
        sims[f"ma{w}_filter"] = simulate_filter(
            series, w, start_idx, end_idx, args.engine, args.execution
        )

    # 算指标
    metrics = []
    for label, sim in sims.items():
        m = compute_metrics(sim, label)
        metrics.append(m)

    # 崩盘窗口分析
    crashes = analyze_crash_windows(series, sims, windows)

    # 落库
    if not args.no_db:
        for label, sim in sims.items():
            if label == "fullhold":
                continue  # fullhold 已由 simulate_strategies.py 落过, 避免覆盖
            n = write_to_db(conn, label, sim)
            logger.info(f"{label}: {n} 行落库")

    # 打印汇总
    print()
    print("=" * 90)
    print(
        f"{'策略':<16}{'总回报':>10}{'年化':>10}{'最大回撤':>10}"
        f"{'年化波动':>10}{'Sharpe':>8}{'ret/dd':>8}{'在场':>8}"
    )
    print("-" * 90)
    for m in metrics:
        print(
            f"{m['label']:<16}{m['total_return_pct']:>+9.2f}%{m['ann_return_pct']:>+9.2f}%"
            f"{m['max_dd_pct']:>+9.2f}%{m['ann_vol_pct']:>9.2f}%"
            f"{m['sharpe']:>8.2f}{m['ret_dd']:>8.2f}{m['in_market_pct']:>7.0f}%"
        )
    print()

    print("崩盘窗口对比:")
    print("-" * 90)
    for c in crashes:
        print(f"  [{c['crash']}] {c['range']}")
        for m in metrics:
            cell = c.get(m['label'])
            if cell is None:
                continue
            print(f"     {m['label']:<16} 回撤 {cell['dd']:+6.2f}%  收益 {cell['ret']:+6.2f}%")
        print()

    # Markdown 报告
    if args.markdown:
        md = render_markdown(metrics, crashes, args.start, args.end, windows, args.engine)
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(md)
        logger.info(f"Markdown 报告已写入 {args.markdown}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
