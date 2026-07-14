"""S5 数据读取层 — 纯 market.db read + 纯函数 helpers.

2026-04-17 精简后: 不再拉取外部数据. 所有缓存表 (limit_up_pool / hot_industries_daily /
s5_daily_universe / klines_cache) 由 cron `daily-regime-pipeline.sh` step 3
`scripts/s5-prewarm.py` 预先填好. 本模块只负责:

  1. 从 DB 读, miss → 抛清晰异常 (提示跑 prewarm)
  2. 纯函数 helpers (连板提取 / 日历位移)
  3. fetch_intraday: T+1 盘中分时 (实时数据不走 cache, verify.py 唯一入口)

如果你要回测/重跑历史某天, 先:
    python3 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5-prewarm.py --date YYYY-MM-DD
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

# 共享 DB (lazy)
_db_module = None

_STRATEGY_LIB_DIR = "/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5/_lib"


def _db():
    global _db_module
    if _db_module is None:
        if _STRATEGY_LIB_DIR not in sys.path:
            sys.path.insert(0, _STRATEGY_LIB_DIR)
        import db as _d
        _db_module = _d
        _d.init_market_db()
    return _db_module


# akshare lazy (仅 fetch_intraday + shift_trading_days 用)
_ak_module = None


def _ak():
    global _ak_module
    if _ak_module is None:
        import akshare as ak
        _ak_module = ak
    return _ak_module


class DataNotPrewarmed(RuntimeError):
    """cache miss, 需要先跑 scripts/s5-prewarm.py."""


# --------------------------------------------------------------------------- #
# 1. T 日涨停池 (读 limit_up_pool)
# --------------------------------------------------------------------------- #


def get_zt_pool(date_str: str):
    """返回 DataFrame (与旧 fetch_zt_pool 列名一致, 保留下游依赖)."""
    import pandas as pd
    conn = _db().get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT code AS 代码, name AS 名称, industry AS 所属行业,
                   pct_chg AS 涨跌幅, streak AS 连板数,
                   close AS 最新价, amount AS 成交额,
                   market_cap AS 流通市值, turnover_rate AS 换手率,
                   seal_amount AS 封板资金, first_seal_time AS 首次封板时间,
                   last_seal_time AS 最后封板时间, blast_count AS 炸板次数
            FROM limit_up_pool WHERE date = ?
            ORDER BY streak DESC, code
            """,
            (date_str,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    return pd.DataFrame([dict(r) for r in rows])


# --------------------------------------------------------------------------- #
# 2. T 日 S5 universe + 热门行业 (读 s5_daily_universe + hot_industries_daily)
# --------------------------------------------------------------------------- #


def get_universe(date_str: str) -> tuple:
    """返回 (codes: list, hot_industries: list, code_to_industry: dict).

    hot_industries: [{"name": str, "limit_count": int}], rank 升序
    raise DataNotPrewarmed 如果该日没有 universe
    """
    conn = _db().get_market_db()
    try:
        univ_rows = conn.execute(
            "SELECT code, industry FROM s5_daily_universe WHERE date = ? ORDER BY code",
            (date_str,),
        ).fetchall()
        hot_rows = conn.execute(
            "SELECT industry, limit_count FROM hot_industries_daily WHERE date = ? ORDER BY rank",
            (date_str,),
        ).fetchall()
    finally:
        conn.close()

    if not univ_rows:
        raise DataNotPrewarmed(
            f"s5_daily_universe 无 {date_str} 数据. 请先跑: "
            f"python3 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5-prewarm.py --date {date_str}"
        )

    codes = [r["code"] for r in univ_rows]
    code_to_industry = {r["code"]: r["industry"] for r in univ_rows}
    hot_industries = [
        {"name": r["industry"], "limit_count": r["limit_count"]} for r in hot_rows
    ]
    return codes, hot_industries, code_to_industry


# --------------------------------------------------------------------------- #
# 3. 批量 K 线 (读 klines_cache)
# --------------------------------------------------------------------------- #


def get_klines_batch(codes: list, start_yyyymmdd: str, end_yyyymmdd: str) -> dict:
    """从 klines_cache 读 {code: [bar]}. 没覆盖的 code 不在返回 dict 中."""
    if not codes:
        return {}
    start_iso = f"{start_yyyymmdd[:4]}-{start_yyyymmdd[4:6]}-{start_yyyymmdd[6:8]}"
    end_iso = f"{end_yyyymmdd[:4]}-{end_yyyymmdd[4:6]}-{end_yyyymmdd[6:8]}"

    codes = sorted(set(codes))
    conn = _db().get_market_db()
    try:
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"""
            SELECT code, date, open, high, low, close, pct_chg, volume, amount,
                   is_limit_up, is_limit_down, limit_up_streak, limit_down_streak
            FROM klines_cache
            WHERE code IN ({placeholders}) AND date >= ? AND date <= ?
            ORDER BY code, date
            """,
            (*codes, start_iso, end_iso),
        ).fetchall()
    finally:
        conn.close()

    result: dict = {}
    for r in rows:
        code = r["code"]
        bar = {
            "date": r["date"],
            "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "pct_chg": r["pct_chg"],
            "volume": r["volume"], "amount": r["amount"],
            "is_limit_up": bool(r["is_limit_up"]),
            "is_limit_down": bool(r["is_limit_down"]),
            "limit_up_streak": r["limit_up_streak"] or 0,
            "limit_down_streak": r["limit_down_streak"] or 0,
        }
        result.setdefault(code, []).append(bar)
    return result


# --------------------------------------------------------------------------- #
# 4. 从 K 线提取历史连板 (纯函数)
# --------------------------------------------------------------------------- #


def extract_streaks_from_klines(klines: list, end_date: str) -> list:
    """扫 K 线, 每段连板取峰值.

    Args:
        klines: 时序从老到新
        end_date: T 日 'YYYY-MM-DD', 只看严格在 T 之前

    Returns:
        [{"date", "max_streak", "close"}]
    """
    streaks = []
    in_streak = False
    cur_max = 0
    cur_peak_idx = -1

    for i, bar in enumerate(klines):
        if bar["date"] >= end_date:
            break
        s = bar["limit_up_streak"]
        if s > 0:
            if not in_streak:
                in_streak = True
                cur_max = s
                cur_peak_idx = i
            elif s > cur_max:
                cur_max = s
                cur_peak_idx = i
        else:
            if in_streak:
                peak = klines[cur_peak_idx]
                streaks.append(
                    {"date": peak["date"], "max_streak": cur_max, "close": peak["close"]}
                )
                in_streak = False
                cur_max = 0
                cur_peak_idx = -1

    if in_streak and cur_peak_idx >= 0:
        peak = klines[cur_peak_idx]
        streaks.append(
            {"date": peak["date"], "max_streak": cur_max, "close": peak["close"]}
        )
    return streaks


# --------------------------------------------------------------------------- #
# 5. T+1 盘中分时 (realtime, verify.py 专用)
# --------------------------------------------------------------------------- #


def fetch_intraday(code: str):
    """T+1 盘中分时 (akshare stock_intraday_em). 实时数据不 cache. 失败返 None."""
    try:
        df = _ak().stock_intraday_em(symbol=code)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logging.warning(f"分时数据拉取失败 ({code}): {e}")
        return None


# --------------------------------------------------------------------------- #
# 6. 交易日历助手
# --------------------------------------------------------------------------- #


def shift_trading_days(date_str: str, n_days: int) -> str:
    """date_str ± n 交易日. akshare 日历, 失败退化为日历日估算."""
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

    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=int(n_days * 1.4))).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 向后兼容: 保留旧名 (内部转发), 逐步迁移
# --------------------------------------------------------------------------- #

def fetch_zt_pool(date_str: str, use_cache: bool = True):
    """兼容旧 API. 现在 == get_zt_pool."""
    return get_zt_pool(date_str)


def fetch_klines_batch(codes: list, start_date: str, end_date: str,
                       batch_size: int = 200, use_cache: bool = True) -> dict:
    """兼容旧 API. 现在 == get_klines_batch, 读 DB miss 不再走网络."""
    return get_klines_batch(codes, start_date, end_date)
