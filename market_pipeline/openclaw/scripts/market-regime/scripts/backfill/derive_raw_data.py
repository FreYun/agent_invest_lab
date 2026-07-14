"""派生层: 从 SQLite 逐日算出 classifier 六维原始数据, 输出 CSV。

依赖:
    /home/rooot/agent_invest_lab/data/market.db (由 backfill_raw_data.py 先填充)

用法:
    python3 derive_raw_data.py                          # 全量, 输出 /tmp
    python3 derive_raw_data.py --start 20230101 --end 20260414
    python3 derive_raw_data.py --out /tmp/regime_raw.csv

输出 CSV 列 (对应 classifier 六维数据 + 派生指标):
    trade_date
    # 维度 2 涨跌家数
    total, up, down, flat, advance_decline_ratio
    # 维度 3 精确涨跌停
    limit_up_count, limit_down_count, sentiment_delta
    # 维度 4 情绪评分 (复盘 MD 口径的 0-100 复合指标)
    sentiment_index
    # 维度 5 最高连板 (本地递归计算)
    max_streak
    # 维度 6 全市场成交额
    total_amount_yi
    # 指数层 (供维度 1 & 6 额外参考)
    hs300_close, hs300_pct_chg
    csi1000_close, csi1000_pct_chg
    shanghai_close, shenzhen_close
    # 维度 6 指数成交额合成 (全市场 = 上证 + 深证 amount)
    index_amount_yi

连板递归算法:
    D[date] = set of ts_codes where close >= up_limit (真实涨停)
    for date in sorted_dates:
        for stock in D[date]:
            streak[date][stock] = streak[prev_date].get(stock, 0) + 1
            (若 prev_date 非上一个交易日, 重置为 1)
    max_streak[date] = max(streak[date].values() or [0])

情绪评分算法: 复刻 mod_sentiment._assess_sentiment 的打分逻辑, 基准 50,
根据 breadth / limit_up / limit_down / max_streak 调整, 范围 0-100。
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import DB_PATH, connect  # noqa: E402


logger = logging.getLogger("derive")


# --------------------------------------------------------------------------- #
# 情绪评分: 复刻 workspace-bot11/scripts/review/mod_sentiment._assess_sentiment
# --------------------------------------------------------------------------- #


def _assess_sentiment(total, up, limit_up, limit_down, max_streak) -> int:
    """基准 50, 根据 breadth + lu/ld + max_streak 调整到 0-100。"""
    score = 50
    if total > 0:
        up_ratio = up / total
        if up_ratio >= 0.8:
            score += 30
        elif up_ratio >= 0.6:
            score += 15
        elif up_ratio >= 0.5:
            score += 5
        elif up_ratio >= 0.4:
            score -= 5
        elif up_ratio >= 0.3:
            score -= 15
        else:
            score -= 30

    if limit_up > 0 and limit_down == 0:
        score += 15
    elif limit_up > limit_down * 3:
        score += 10
    elif limit_up > limit_down:
        score += 5
    elif limit_down > limit_up * 3:
        score -= 15
    elif limit_down > limit_up:
        score -= 5

    if max_streak >= 5:
        score += 10
    elif max_streak >= 3:
        score += 5

    return max(0, min(100, score))


# --------------------------------------------------------------------------- #
# 主派生流程
# --------------------------------------------------------------------------- #


def derive(conn, start_date: str | None, end_date: str | None) -> list[dict]:
    """从 SQLite 派生每天的六维原始数据, 返回 list of dict (按日期升序)."""
    where = []
    params = []
    if start_date:
        where.append("d.trade_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("d.trade_date <= ?")
        params.append(end_date)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # 一次查询所有日期的聚合指标 (涨跌家数 + 成交额)
    q_breadth = f"""
        SELECT
            d.trade_date,
            COUNT(*) AS total,
            SUM(CASE WHEN d.pct_chg > 0 THEN 1 ELSE 0 END) AS up,
            SUM(CASE WHEN d.pct_chg < 0 THEN 1 ELSE 0 END) AS down,
            SUM(CASE WHEN d.pct_chg = 0 THEN 1 ELSE 0 END) AS flat,
            ROUND(SUM(d.amount)/1e5, 2) AS total_amount_yi
        FROM daily d
        {where_sql}
        GROUP BY d.trade_date
        ORDER BY d.trade_date ASC
    """
    breadth_rows = conn.execute(q_breadth, params).fetchall()

    # 一次查询所有日期的精确涨停/跌停个数 (MERGE daily + stk_limit)
    q_limit = f"""
        SELECT
            d.trade_date,
            SUM(CASE WHEN d.close >= l.up_limit THEN 1 ELSE 0 END) AS lu,
            SUM(CASE WHEN d.close <= l.down_limit THEN 1 ELSE 0 END) AS ld
        FROM daily d INNER JOIN stk_limit l
            ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
        {where_sql.replace('d.trade_date', 'd.trade_date')}
        GROUP BY d.trade_date
    """
    limit_rows = {row[0]: (row[1], row[2]) for row in conn.execute(q_limit, params)}

    # 精确涨停股全集 (按日期) — 供连板递归使用
    q_lu_stocks = f"""
        SELECT d.trade_date, d.ts_code
        FROM daily d INNER JOIN stk_limit l
            ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
        WHERE d.close >= l.up_limit
        {' AND ' + ' AND '.join(where) if where else ''}
        ORDER BY d.trade_date ASC
    """
    lu_by_date: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(q_lu_stocks, params):
        lu_by_date[row[0]].add(row[1])

    # 连板递归计算
    max_streak_by_date: dict[str, int] = {}
    prev_streaks: dict[str, int] = {}
    prev_date: str | None = None
    all_dates_sorted = [r[0] for r in breadth_rows]
    for date in all_dates_sorted:
        today_lu = lu_by_date.get(date, set())
        today_streaks: dict[str, int] = {}
        for stock in today_lu:
            # 只有当前一个交易日也是连板 (或同一只股票昨天也涨停) 才累计
            today_streaks[stock] = prev_streaks.get(stock, 0) + 1
        max_streak_by_date[date] = max(today_streaks.values()) if today_streaks else 0
        prev_streaks = today_streaks
        prev_date = date

    # 指数层 (HS300 / CSI1000 / 上证综指 / 深证综指)
    q_index = """
        SELECT trade_date, ts_code, close, pct_chg, amount
        FROM index_daily
    """
    if where:
        q_index += " WHERE " + " AND ".join(w.replace("d.trade_date", "trade_date") for w in where)
    idx_by_date: dict[str, dict[str, tuple[float, float, float]]] = defaultdict(dict)
    for row in conn.execute(q_index, params):
        idx_by_date[row[0]][row[1]] = (row[2], row[3], row[4])

    # 组装结果
    results = []
    for trade_date, total, up, down, flat, total_amount_yi in breadth_rows:
        lu, ld = limit_rows.get(trade_date, (0, 0))
        max_streak = max_streak_by_date.get(trade_date, 0)
        ratio = up / total if total > 0 else None
        sentiment_delta = (lu or 0) - (ld or 0)
        sentiment_idx = _assess_sentiment(total, up, lu or 0, ld or 0, max_streak)

        idx = idx_by_date.get(trade_date, {})
        hs300 = idx.get("000300.SH", (None, None, None))
        csi1000 = idx.get("000852.SH", (None, None, None))
        sh = idx.get("000001.SH", (None, None, None))
        sz = idx.get("399106.SZ", (None, None, None))
        # 指数合成全市场成交额 (上证综指 + 深证综指 amount, 单位: 千元 → 亿元)
        index_amount_yi = None
        if sh[2] is not None and sz[2] is not None:
            index_amount_yi = round((sh[2] + sz[2]) / 1e5, 2)

        results.append({
            "trade_date": trade_date,
            "total": total,
            "up": up,
            "down": down,
            "flat": flat,
            "advance_decline_ratio": round(ratio, 4) if ratio is not None else None,
            "limit_up_count": lu or 0,
            "limit_down_count": ld or 0,
            "sentiment_delta": sentiment_delta,
            "sentiment_index": sentiment_idx,
            "max_streak": max_streak,
            "total_amount_yi": total_amount_yi,
            "hs300_close": hs300[0],
            "hs300_pct_chg": hs300[1],
            "csi1000_close": csi1000[0],
            "csi1000_pct_chg": csi1000[1],
            "shanghai_close": sh[0],
            "shenzhen_close": sz[0],
            "index_amount_yi": index_amount_yi,
        })

    return results


def write_csv(rows: list[dict], out_path: str) -> None:
    if not rows:
        logger.warning("无数据, 不生成 CSV")
        return
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"CSV 已写入: {out_path} ({len(rows)} 行)")


def main():
    parser = argparse.ArgumentParser(description="从 SQLite 派生六维原始数据 CSV")
    parser.add_argument("--start", default=None, help="起始日期 YYYYMMDD")
    parser.add_argument("--end", default=None, help="结束日期 YYYYMMDD")
    parser.add_argument("--out", default="/tmp/regime_raw_data.csv", help="输出 CSV 路径")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not os.path.exists(DB_PATH):
        logger.error(f"数据库不存在: {DB_PATH}, 请先跑 backfill_raw_data.py")
        return 1

    conn = connect()
    rows = derive(conn, args.start, args.end)
    logger.info(f"派生完成: {len(rows)} 个交易日")

    # 打印摘要
    if rows:
        first, last = rows[0], rows[-1]
        logger.info(f"日期范围: {first['trade_date']} ~ {last['trade_date']}")
        logger.info(f"最新一天: {last}")
        max_streak_ever = max(r["max_streak"] for r in rows)
        max_streak_day = next(r for r in rows if r["max_streak"] == max_streak_ever)
        logger.info(f"3 年内最高连板: {max_streak_ever} 板 @ {max_streak_day['trade_date']}")

    write_csv(rows, args.out)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
