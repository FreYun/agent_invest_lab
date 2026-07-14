"""采集层: 从 Tushare 拉 3 年历史数据入 SQLite。

用法:
    # 默认回填 3 年 (从 today - 1100 天到 today)
    python3 backfill_raw_data.py

    # 自定义区间
    python3 backfill_raw_data.py --start 20230101 --end 20260414

    # 仅跑一个月验证流程
    python3 backfill_raw_data.py --start 20260301 --end 20260414

    # 跳过已有日期 (增量模式, 默认开启)
    python3 backfill_raw_data.py --resume

    # 强制重拉所有日期 (小心使用)
    python3 backfill_raw_data.py --force

流水线:
1. 一次性拉 4 个指数 3 年日线 → index_daily 表 (4 次 API 调用)
2. 交易日历过滤 (tushare trade_cal) → 得到有效日期列表
3. 按日循环: daily(trade_date=X) + stk_limit(trade_date=X) → daily / stk_limit 表
4. 每 50 天打一次进度 + ETA

数据库: /home/rooot/agent_invest_lab/data/market.db (不进 git)
Token: 从 workspace-bot11/scripts/config.py 读
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta

# 把 workspace-bot11/scripts 加入 path, 复用 config.get_tushare_pro
BOT11_SCRIPTS = "/home/rooot/agent_invest_lab/market_pipeline/openclaw/workspace-bot11/scripts"
if os.path.isdir(BOT11_SCRIPTS):
    sys.path.insert(0, BOT11_SCRIPTS)
from config import get_tushare_pro  # noqa: E402

# 本地模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import (  # noqa: E402
    connect,
    date_exists,
    date_range,
    row_counts,
    set_progress,
)


logger = logging.getLogger("backfill")


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #


INDICES = {
    "HS300": "000300.SH",
    "CSI1000": "000852.SH",
    "上证综指": "000001.SH",
    "深证综指": "399106.SZ",
}

# 一次 daily/stk_limit 调用的温和间隔 (tushare 无限额, 但别打爆)
SLEEP_BETWEEN_CALLS = 0.15


# --------------------------------------------------------------------------- #
# Phase 1: 指数日线 (一次性 4 次调用)
# --------------------------------------------------------------------------- #


def backfill_index_daily(conn, pro, start_date: str, end_date: str, force: bool = False) -> int:
    """拉 4 个指数日线, 插入 index_daily 表。

    增量逻辑: 检查表内对该指数的 [min, max] 日期是否已经覆盖请求区间,
    完全覆盖才跳过; 否则一次拉完整请求区间, 用 INSERT OR REPLACE 去重。
    指数日线一次调用很快 (4 次 × ~250ms), 没必要做更精细的增量。
    """
    inserted = 0
    for name, ts_code in INDICES.items():
        if not force:
            row = conn.execute(
                "SELECT MIN(trade_date), MAX(trade_date) FROM index_daily WHERE ts_code=?",
                (ts_code,),
            ).fetchone()
            existing_min, existing_max = row[0], row[1]
            if existing_min and existing_max and existing_min <= start_date and existing_max >= end_date:
                logger.info(
                    f"  [skip] {name} ({ts_code}): 已覆盖 {existing_min}~{existing_max}"
                )
                continue

        logger.info(f"  拉 {name} ({ts_code}) {start_date}~{end_date}")
        df = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        time.sleep(SLEEP_BETWEEN_CALLS)
        if df is None or df.empty:
            logger.warning(f"  {name} 返回空")
            continue

        rows = [
            (
                r["trade_date"], r["ts_code"],
                float(r.get("open", 0) or 0),
                float(r.get("high", 0) or 0),
                float(r.get("low", 0) or 0),
                float(r.get("close", 0) or 0),
                float(r.get("pre_close", 0) or 0),
                float(r.get("pct_chg", 0) or 0),
                float(r.get("vol", 0) or 0),
                float(r.get("amount", 0) or 0),
            )
            for _, r in df.iterrows()
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO index_daily "
            "(trade_date, ts_code, open, high, low, close, pre_close, pct_chg, vol, amount) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        inserted += len(rows)
        logger.info(f"  [{name}] +{len(rows)} 行")
    return inserted


# --------------------------------------------------------------------------- #
# Phase 2: 交易日历
# --------------------------------------------------------------------------- #


def fetch_trading_days(pro, start_date: str, end_date: str) -> list[str]:
    """从 tushare trade_cal 获取 SSE 有效交易日列表, 升序返回 YYYYMMDD."""
    cal = pro.trade_cal(exchange="SSE", start_date=start_date, end_date=end_date)
    time.sleep(SLEEP_BETWEEN_CALLS)
    open_days = cal[cal["is_open"] == 1]["cal_date"].tolist()
    return sorted(open_days)


# --------------------------------------------------------------------------- #
# Phase 3: 全市场 daily + stk_limit 按日循环
# --------------------------------------------------------------------------- #


def _insert_daily_rows(conn, df):
    rows = [
        (
            r["trade_date"], r["ts_code"],
            float(r.get("open", 0) or 0),
            float(r.get("high", 0) or 0),
            float(r.get("low", 0) or 0),
            float(r.get("close", 0) or 0),
            float(r.get("pre_close", 0) or 0),
            float(r.get("pct_chg", 0) or 0),
            float(r.get("vol", 0) or 0),
            float(r.get("amount", 0) or 0),
        )
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO daily "
        "(trade_date, ts_code, open, high, low, close, pre_close, pct_chg, vol, amount) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _insert_stk_limit_rows(conn, df):
    rows = [
        (
            r["trade_date"], r["ts_code"],
            float(r.get("up_limit", 0) or 0),
            float(r.get("down_limit", 0) or 0),
        )
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO stk_limit "
        "(trade_date, ts_code, up_limit, down_limit) VALUES (?,?,?,?)",
        rows,
    )
    return len(rows)


def backfill_daily_and_limit(conn, pro, trading_days: list[str], force: bool = False) -> dict:
    """按日循环拉 daily + stk_limit。

    Returns:
        dict: {'daily_rows': int, 'limit_rows': int, 'skipped': int, 'failed': int}
    """
    stats = {"daily_rows": 0, "limit_rows": 0, "skipped": 0, "failed": 0}
    n_total = len(trading_days)
    t0 = time.time()

    for idx, date in enumerate(trading_days):
        # 增量: 如果两张表都已有该日期数据, 跳过
        if not force:
            has_daily = date_exists(conn, "daily", date)
            has_limit = date_exists(conn, "stk_limit", date)
            if has_daily and has_limit:
                stats["skipped"] += 1
                continue

        try:
            # daily
            if force or not date_exists(conn, "daily", date):
                df_daily = pro.daily(trade_date=date)
                time.sleep(SLEEP_BETWEEN_CALLS)
                if df_daily is not None and not df_daily.empty:
                    stats["daily_rows"] += _insert_daily_rows(conn, df_daily)

            # stk_limit
            if force or not date_exists(conn, "stk_limit", date):
                df_limit = pro.stk_limit(trade_date=date)
                time.sleep(SLEEP_BETWEEN_CALLS)
                if df_limit is not None and not df_limit.empty:
                    stats["limit_rows"] += _insert_stk_limit_rows(conn, df_limit)

            conn.commit()
            set_progress(conn, "daily_stk_limit", date)
        except Exception as e:
            stats["failed"] += 1
            logger.warning(f"  [fail] {date}: {e}")
            continue

        # 进度 + ETA
        done = idx + 1
        if done % 20 == 0 or done == n_total:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta_sec = (n_total - done) / rate if rate > 0 else 0
            logger.info(
                f"  进度 {done}/{n_total} ({done/n_total*100:.1f}%) "
                f"已用 {elapsed:.0f}s, ETA {eta_sec:.0f}s, "
                f"速率 {rate:.1f} 天/秒"
            )

    return stats


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(description="market-regime-classifier 数据回填")
    parser.add_argument("--start", default=None, help="起始日期 YYYYMMDD, 默认 today-1100")
    parser.add_argument("--end", default=None, help="结束日期 YYYYMMDD, 默认 today")
    parser.add_argument("--force", action="store_true", help="强制重拉所有日期")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    today = datetime.now()
    if args.end is None:
        args.end = today.strftime("%Y%m%d")
    if args.start is None:
        args.start = (today - timedelta(days=1100)).strftime("%Y%m%d")

    logger.info(f"===== 回填区间: {args.start} ~ {args.end} =====")
    logger.info(f"数据库: /home/rooot/agent_invest_lab/data/market.db")

    pro = get_tushare_pro()
    conn = connect()

    # Phase 1: 指数日线
    logger.info("--- Phase 1: 指数日线 ---")
    backfill_index_daily(conn, pro, args.start, args.end, force=args.force)

    # Phase 2: 交易日历
    logger.info("--- Phase 2: 交易日历 ---")
    trading_days = fetch_trading_days(pro, args.start, args.end)
    logger.info(f"  共 {len(trading_days)} 个交易日")

    # Phase 3: daily + stk_limit 逐日
    logger.info("--- Phase 3: daily + stk_limit 逐日回填 ---")
    stats = backfill_daily_and_limit(conn, pro, trading_days, force=args.force)

    # 总结
    logger.info("===== 完成 =====")
    logger.info(f"  daily 插入行数:    {stats['daily_rows']}")
    logger.info(f"  stk_limit 插入行数: {stats['limit_rows']}")
    logger.info(f"  跳过 (已存在):     {stats['skipped']}")
    logger.info(f"  失败:             {stats['failed']}")

    counts = row_counts(conn)
    logger.info(f"  当前库总行数: {counts}")
    for t in ("daily", "stk_limit", "index_daily"):
        lo, hi = date_range(conn, t)
        logger.info(f"  {t:<12} 日期范围: {lo} ~ {hi}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
