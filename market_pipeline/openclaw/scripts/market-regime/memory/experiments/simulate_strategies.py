"""按 v2 regime 的多个策略模拟器, 输出净值时序到 regime_strategy_nav 表。

模拟逻辑:
    日 PnL = 当日仓位 × (HS300_close / HS300_open - 1)
    净值 = 累乘 (1 + PnL)
    最大回撤 = 截至该日 (NAV / peak_NAV - 1) 的最低值

模拟的策略 (4 个):
    fullhold              满仓持有 HS300 (基准)
    v2_dynamic            按 v2 regime 动态调仓 (spec §5 position_limit)
    v2_entry_only         只在 v2 多头切入日加仓 90%, 其他天 50%
    v2_entry_held_5d      多头切入后持 5 天 90%, 其他天 30%

依赖 regime_classify_daily 的 v2 数据已在表里 (跑过 load_to_db.py 之后)。

用法:
    python3 simulate_strategies.py
    python3 simulate_strategies.py --strategies fullhold,v2_dynamic   # 只跑指定策略
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import connect  # noqa: E402

logger = logging.getLogger("simulate")


# spec §5 position_limit.total
POSITION_LIMIT = {
    "强牛":   0.90,
    "强势震荡": 0.70,
    "中性震荡": 0.50,
    "弱势震荡": 0.30,
    "熊":     0.20,
}


def load_v2_classify(conn) -> list[dict]:
    """从 regime_classify_daily 取 v2 全量数据, 按日期升序."""
    rows = conn.execute(
        """
        SELECT trade_date, regime_name, switched
        FROM regime_classify_daily
        WHERE rules_version = 'v2'
        ORDER BY trade_date ASC
        """
    ).fetchall()
    return [{"date": r[0], "regime": r[1], "switched": r[2]} for r in rows]


def load_hs300_open_close(conn) -> dict:
    """返回 {iso_date: (open, close)} 全量 HS300."""
    result = {}
    for d, o, c in conn.execute(
        "SELECT trade_date, open, close FROM index_daily "
        "WHERE ts_code='000300.SH' ORDER BY trade_date"
    ):
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        result[iso] = (float(o), float(c))
    return result


# --------------------------------------------------------------------------- #
# 4 个仓位策略
# --------------------------------------------------------------------------- #


def strat_fullhold(rows, idx) -> float:
    return 1.0


def strat_v2_dynamic(rows, idx) -> float:
    """用昨日 regime 决定今日仓位 (T-1 日 close 后出信号, T 日开盘调仓)."""
    if idx == 0:
        return 0.5
    regime = rows[idx - 1]["regime"]
    return POSITION_LIMIT.get(regime, 0.5)


def strat_v2_entry_only(rows, idx) -> float:
    """昨日切入强牛/强势震荡 → 今日加仓 90%, 其他天 50%。

    对齐实操: T 日 classifier 收盘后出信号, T+1 日开盘加仓。
    若 idx 是 T+1 日 → 检查 rows[idx-1] 是否 switched + 多头 regime。
    """
    if idx == 0:
        return 0.5
    prev = rows[idx - 1]
    if int(prev["switched"]) == 1 and prev["regime"] in ("强牛", "强势震荡"):
        return 0.9
    return 0.5


def strat_v2_entry_held_5d(rows, idx) -> float:
    """多头切入信号触发后, T+1 ~ T+5 共 5 天持 90%, 其他天 30%。

    对齐实操: 切入信号在 T 日收盘后产出, 实际加仓窗口是 T+1..T+5。
    检查 idx-1..idx-5 (即 T..T-4) 内是否有切入事件 → idx 落在某次切入的 T+1..T+5 区间。
    """
    for k in range(1, 6):  # k=1..5: 检查"昨天到 5 天前"是否切入
        i = idx - k
        if i < 0:
            break
        r = rows[i]
        if int(r["switched"]) == 1 and r["regime"] in ("强牛", "强势震荡"):
            return 0.9
    return 0.3


STRATEGIES = {
    "fullhold": (strat_fullhold, "满仓持有 HS300 (基准)"),
    "v2_dynamic": (strat_v2_dynamic, "v2 regime 动态调仓 (spec §5)"),
    "v2_entry_only": (strat_v2_entry_only, "仅多头切入日加仓 90%, 默认 50%"),
    "v2_entry_held_5d": (strat_v2_entry_held_5d, "多头切入后持 5 天 90%, 默认 30%"),
}


# --------------------------------------------------------------------------- #
# 模拟引擎
# --------------------------------------------------------------------------- #


def simulate(strategy_id: str, rows: list[dict], hs300: dict) -> list[dict]:
    """跑一个策略, 返回每日净值列表 [{date, position, daily_pnl_pct, nav, max_dd}, ...]"""
    fn, _desc = STRATEGIES[strategy_id]
    nav = 1.0
    peak = 1.0
    out = []

    for idx, r in enumerate(rows):
        date = r["date"]
        if date not in hs300:
            continue
        open_p, close_p = hs300[date]
        if open_p <= 0:
            continue
        position = fn(rows, idx)
        daily_ret = (close_p / open_p) - 1
        daily_pnl = position * daily_ret
        nav *= (1 + daily_pnl)
        if nav > peak:
            peak = nav
        max_dd = (nav / peak) - 1  # 负数

        out.append({
            "trade_date": date,
            "strategy_id": strategy_id,
            "position": round(position, 4),
            "daily_pnl_pct": round(daily_pnl * 100, 6),
            "cumulative_nav": round(nav, 6),
            "max_drawdown_to_date": round(max_dd, 6),
        })

    return out


def write_to_db(conn, results: list[dict]) -> int:
    if not results:
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
            r["strategy_id"],
            r["position"],
            r["daily_pnl_pct"],
            r["cumulative_nav"],
            r["max_drawdown_to_date"],
        )
        for r in results
    ]
    conn.executemany(sql, batch)
    conn.commit()
    return len(batch)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategies",
        default=",".join(STRATEGIES.keys()),
        help="逗号分隔的策略 ID 列表",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    conn = connect()
    rows = load_v2_classify(conn)
    if not rows:
        logger.error(
            "regime_classify_daily 中无 v2 数据, 请先跑 load_to_db.py 灌入"
        )
        return 1
    hs300 = load_hs300_open_close(conn)
    logger.info(f"加载 v2 classify {len(rows)} 行, HS300 {len(hs300)} 天")

    requested = [s.strip() for s in args.strategies.split(",") if s.strip()]

    summary = []
    for strategy_id in requested:
        if strategy_id not in STRATEGIES:
            logger.warning(f"未知策略 {strategy_id}, 跳过")
            continue
        _, desc = STRATEGIES[strategy_id]
        logger.info(f"--- 跑 {strategy_id}: {desc} ---")
        sim = simulate(strategy_id, rows, hs300)
        n = write_to_db(conn, sim)
        if not sim:
            continue
        last = sim[-1]
        first_nav = sim[0]["cumulative_nav"]
        total_ret = (last["cumulative_nav"] / first_nav - 1) * 100
        max_dd = min(r["max_drawdown_to_date"] for r in sim) * 100
        ratio = abs(total_ret / max_dd) if max_dd != 0 else 0
        logger.info(
            f"  {n} 行落库, 总回报 {total_ret:+.2f}%, "
            f"最大回撤 {max_dd:+.2f}%, return/maxdd={ratio:.2f}"
        )
        summary.append({
            "id": strategy_id,
            "n": n,
            "total_return": total_ret,
            "max_dd": max_dd,
            "ratio": ratio,
        })

    print()
    print("=" * 70)
    print(f"{'strategy_id':<22} {'天数':>5} {'总回报':>10} {'最大回撤':>10} {'ret/dd':>8}")
    print("-" * 70)
    for s in summary:
        print(f"  {s['id']:<20} {s['n']:>5} {s['total_return']:>+9.2f}% "
              f"{s['max_dd']:>+9.2f}% {s['ratio']:>8.2f}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
