#!/usr/bin/env python3
"""S5 战法所需外部数据的 cron 预热脚本 (self-contained).

职责:
  把 S5 select.py 会用到的"所有"外部数据灌进 market.db, 让 skill 运行时纯 DB read.

预热写入的表:
  1. limit_up_pool         — T 日涨停池 (akshare)
  2. hot_industries_daily  — top 3 热门行业 (从涨停池派生)
  3. s5_daily_universe     — S5 选股 universe (top 3 行业成分 + 涨停池)
  4. klines_cache          — universe 近 35 日 K 线 (本地 daily + stk_limit JOIN)

K 线数据源:
  daily / stk_limit 由 daily_review.py (tushare) 在 21:00 写入.
  prewarm 在 daily 写入完成后从本地 JOIN 出 K 线 + 算 streak, 不依赖任何远程 MCP.

用法:
  python3 s5-prewarm.py                 # 今天 (本地时区)
  python3 s5-prewarm.py --date 2026-04-16

cron 集成: daily-regime-pipeline.sh Step 3.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta

# 共享 DB 模块 (lazy)
_STRATEGY_LIB_DIR = "/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5/_lib"
if _STRATEGY_LIB_DIR not in sys.path:
    sys.path.insert(0, _STRATEGY_LIB_DIR)
import db as market_db  # noqa: E402


# --------------------------------------------------------------------------- #
# akshare lazy import (仅用于 limit_up_pool / 行业成分)
# --------------------------------------------------------------------------- #

_ak_module = None


def _ak():
    global _ak_module
    if _ak_module is None:
        import akshare as ak
        _ak_module = ak
    return _ak_module


# --------------------------------------------------------------------------- #
# Step 1: limit_up_pool
# --------------------------------------------------------------------------- #


def write_limit_up_pool(date_str: str, df) -> int:
    """T 日涨停池 → market.db.limit_up_pool."""
    conn = market_db.get_market_db()
    try:
        n = 0
        for _, row in df.iterrows():
            conn.execute(
                """
                INSERT OR REPLACE INTO limit_up_pool (
                    date, code, name, industry, pct_chg, streak,
                    close, amount, market_cap, turnover_rate,
                    seal_amount, first_seal_time, last_seal_time, blast_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    date_str,
                    str(row.get("代码", "")).zfill(6),
                    row.get("名称"),
                    row.get("所属行业"),
                    float(row.get("涨跌幅", 0) or 0),
                    int(row.get("连板数", 0) or 0),
                    float(row.get("最新价", 0) or 0),
                    float(row.get("成交额", 0) or 0),
                    float(row.get("流通市值", 0) or 0),
                    float(row.get("换手率", 0) or 0),
                    float(row.get("封板资金", 0) or 0),
                    str(row.get("首次封板时间", "")),
                    str(row.get("最后封板时间", "")),
                    int(row.get("炸板次数", 0) or 0),
                ),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Step 2: hot_industries_daily
# --------------------------------------------------------------------------- #


def derive_hot_industries(df, top_n: int = 3) -> list:
    """从涨停池派生 top N 热门行业."""
    if df is None or df.empty:
        return []
    counts = df["所属行业"].fillna("未知").value_counts()
    return [
        {"name": name, "limit_count": int(cnt)}
        for name, cnt in counts.head(top_n).items()
    ]


def write_hot_industries(date_str: str, hot: list):
    """热门行业 → hot_industries_daily."""
    conn = market_db.get_market_db()
    try:
        conn.execute("DELETE FROM hot_industries_daily WHERE date = ?", (date_str,))
        for rank, h in enumerate(hot, start=1):
            conn.execute(
                """
                INSERT INTO hot_industries_daily (date, rank, industry, limit_count)
                VALUES (?, ?, ?, ?)
                """,
                (date_str, rank, h["name"], h["limit_count"]),
            )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Step 3: s5_daily_universe (top 3 行业成分 + 涨停池股)
# --------------------------------------------------------------------------- #


def fetch_industry_constituents(industry_name: str) -> list:
    try:
        df = _ak().stock_board_industry_cons_em(symbol=industry_name)
        if df is None or df.empty:
            return []
        return df["代码"].astype(str).str.zfill(6).tolist()
    except Exception as e:
        logging.warning(f"行业 {industry_name} 成分股拉取失败: {e}")
        return []


def write_universe(date_str: str, hot: list, zt_df) -> int:
    """计算 universe 并写 s5_daily_universe. 返回去重后的 code 数."""
    code_to_industry = {}

    # 热门行业成分
    for h in hot:
        cons = fetch_industry_constituents(h["name"])
        for c in cons:
            code_to_industry.setdefault(c, h["name"])

    # 涨停池本身
    for _, row in zt_df.iterrows():
        c = str(row["代码"]).zfill(6)
        code_to_industry.setdefault(c, row.get("所属行业", "未知"))

    conn = market_db.get_market_db()
    try:
        conn.execute("DELETE FROM s5_daily_universe WHERE date = ?", (date_str,))
        for code, industry in code_to_industry.items():
            conn.execute(
                "INSERT INTO s5_daily_universe (date, code, industry) VALUES (?, ?, ?)",
                (date_str, code, industry),
            )
        conn.commit()
    finally:
        conn.close()
    return len(code_to_industry)


# --------------------------------------------------------------------------- #
# Step 4: klines_cache
# --------------------------------------------------------------------------- #


def write_klines_cache(klines_map: dict) -> int:
    conn = market_db.get_market_db()
    try:
        n = 0
        for code, bars in klines_map.items():
            for b in bars:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO klines_cache (
                        code, date, open, high, low, close, pct_chg, volume, amount,
                        is_limit_up, is_limit_down, limit_up_streak, limit_down_streak
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        code, b["date"],
                        b["open"], b["high"], b["low"], b["close"],
                        b["pct_chg"], b["volume"], b["amount"],
                        1 if b["is_limit_up"] else 0,
                        1 if b["is_limit_down"] else 0,
                        b["limit_up_streak"], b["limit_down_streak"],
                    ),
                )
                n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def fetch_klines_batch(codes: list, start_yyyymmdd: str, end_yyyymmdd: str, batch_size: int = 200) -> dict:
    """从本地 daily + stk_limit JOIN 出 K 线, 含 limit_up_streak. 返回 {code: [bar]}.

    单位:
      - daily.vol  (手) → klines_cache.volume (股)  : ×100
      - daily.amount (千元) → klines_cache.amount (元) : ×1000
    streak 计算:
      为正确性, 涨停回看从 end-90 自然日开始, 覆盖任何可能的连板起点.
    """
    codes = sorted(set(codes))
    if not codes:
        return {}

    conn = market_db.get_market_db()
    try:
        # 1. code6 → ts_code 反查
        placeholders = ",".join("?" * len(codes))
        ts_rows = conn.execute(
            f"SELECT DISTINCT ts_code FROM daily WHERE substr(ts_code, 1, 6) IN ({placeholders})",
            codes,
        ).fetchall()
        if not ts_rows:
            return {}
        ts_to_code6 = {r[0]: r[0][:6] for r in ts_rows}
        ts_codes = list(ts_to_code6.keys())
        ts_ph = ",".join("?" * len(ts_codes))

        # 2. streak: 拿 [end-90, end] 内的涨停日, 按交易日累加
        streak_lookback_start = (
            datetime.strptime(end_yyyymmdd, "%Y%m%d") - timedelta(days=90)
        ).strftime("%Y%m%d")

        all_dates = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM daily WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
                (streak_lookback_start, end_yyyymmdd),
            ).fetchall()
        ]

        lu_rows = conn.execute(
            f"""
            SELECT d.trade_date, d.ts_code
            FROM daily d
            INNER JOIN stk_limit l ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
            WHERE d.close >= l.up_limit
              AND d.trade_date >= ? AND d.trade_date <= ?
              AND d.ts_code IN ({ts_ph})
            """,
            [streak_lookback_start, end_yyyymmdd] + ts_codes,
        ).fetchall()

        lu_by_date: dict[str, set] = defaultdict(set)
        for td, ts in lu_rows:
            lu_by_date[td].add(ts)

        streak_map: dict[tuple, int] = {}
        prev_streaks: dict[str, int] = {}
        for td in all_dates:
            today_lu = lu_by_date.get(td, set())
            today_streaks = {ts: prev_streaks.get(ts, 0) + 1 for ts in today_lu}
            for ts, s in today_streaks.items():
                streak_map[(ts, td)] = s
            prev_streaks = today_streaks

        # 3. 拉 K 线
        kline_rows = conn.execute(
            f"""
            SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
                   d.pct_chg, d.vol, d.amount,
                   CASE WHEN l.up_limit IS NOT NULL AND d.close >= l.up_limit THEN 1 ELSE 0 END AS is_lu,
                   CASE WHEN l.down_limit IS NOT NULL AND d.close <= l.down_limit THEN 1 ELSE 0 END AS is_ld
            FROM daily d
            LEFT JOIN stk_limit l ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
            WHERE d.trade_date >= ? AND d.trade_date <= ?
              AND d.ts_code IN ({ts_ph})
            """,
            [start_yyyymmdd, end_yyyymmdd] + ts_codes,
        ).fetchall()

        result: dict[str, list] = defaultdict(list)
        for ts_code, td, op, hi, lo, cl, pct, vol, amt, is_lu, is_ld in kline_rows:
            code6 = ts_to_code6[ts_code]
            kiso = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
            result[code6].append({
                "date": kiso,
                "open": float(op or 0),
                "high": float(hi or 0),
                "low": float(lo or 0),
                "close": float(cl or 0),
                "pct_chg": float(pct or 0),
                "volume": float(vol or 0) * 100,    # 手 → 股
                "amount": float(amt or 0) * 1000,   # 千元 → 元
                "is_limit_up": bool(is_lu),
                "is_limit_down": bool(is_ld),
                "limit_up_streak": streak_map.get((ts_code, td), 0),
                "limit_down_streak": 0,
            })

        for code6 in result:
            result[code6].sort(key=lambda b: b["date"])
        return dict(result)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 交易日历
# --------------------------------------------------------------------------- #


def shift_trading_days(date_str: str, n_days: int) -> str:
    """date_str ± n 交易日. akshare 日历."""
    from datetime import datetime, timedelta

    try:
        cal_df = _ak().tool_trade_date_hist_sina()
        cal = sorted(d.strftime("%Y-%m-%d") for d in cal_df["trade_date"])
        try:
            idx = cal.index(date_str)
        except ValueError:
            idx = 0
            for i, d in enumerate(cal):
                if d >= date_str:
                    idx = i
                    break
        target = idx + n_days
        if 0 <= target < len(cal):
            return cal[target]
    except Exception as e:
        logging.warning(f"交易日历失败 ({date_str} {n_days:+d}): {e}, 用日历日估算")

    # fallback
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=int(n_days * 1.4))).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def prewarm(date_str: str) -> int:
    market_db.init_market_db()

    logging.info(f"===== S5 prewarm t={date_str} =====")

    # 1. 涨停池 (akshare)
    try:
        df = _ak().stock_zt_pool_em(date=date_str.replace("-", ""))
    except Exception as e:
        logging.warning(f"涨停池拉取失败 ({date_str}): {e}")
        df = None
    if df is None or df.empty:
        logging.warning("T 日涨停池为空, prewarm 提前结束 (非交易日/极端市场)")
        return 0
    n = write_limit_up_pool(date_str, df)
    logging.info(f"[1/4] limit_up_pool: {n} 行")

    # 2. 派生热门行业
    hot = derive_hot_industries(df, top_n=3)
    if not hot:
        logging.warning("热门行业为空, 跳过 universe/K线 预热")
        return 0
    write_hot_industries(date_str, hot)
    logging.info(f"[2/4] hot_industries_daily: {[h['name'] for h in hot]}")

    # 3. 计算 + 写 universe
    universe_size = write_universe(date_str, hot, df)
    logging.info(f"[3/4] s5_daily_universe: {universe_size} 只")

    # 4. K 线批量
    start = shift_trading_days(date_str, -35).replace("-", "")
    end = date_str.replace("-", "")
    codes = [r["code"] for r in market_db.get_market_db().execute(
        "SELECT code FROM s5_daily_universe WHERE date = ?", (date_str,)
    ).fetchall()]
    logging.info(f"[4/4] K 线 {start}~{end}: {len(codes)} 只 ← daily/stk_limit (本地)")
    klines_map = fetch_klines_batch(codes, start, end)
    n_bars = write_klines_cache(klines_map)
    missing = len(codes) - len(klines_map)
    logging.info(f"  写 cache: {len(klines_map)} 只, 共 {n_bars} 行 (缺失 {missing} 只)")

    # T 日 K 线缺失率告警 (cron 退出码非 0 让日志可见)
    t_day_iso = date_str
    t_day_have = sum(1 for bars in klines_map.values() if any(b["date"] == t_day_iso for b in bars))
    t_day_miss_rate = 1 - (t_day_have / len(codes)) if codes else 0
    if t_day_miss_rate > 0.2:
        logging.error(
            f"T 日 ({t_day_iso}) K 线缺失率 {t_day_miss_rate:.0%} "
            f"({len(codes) - t_day_have}/{len(codes)} 只), 检查 daily 表是否就绪"
        )
        logging.info("===== prewarm 完成 (有告警) =====")
        return 1

    logging.info("===== prewarm 完成 =====")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--date",
        default=date_cls.today().strftime("%Y-%m-%d"),
        help="T 日 YYYY-MM-DD, 默认本地今日",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    return prewarm(args.date)


if __name__ == "__main__":
    sys.exit(main())
