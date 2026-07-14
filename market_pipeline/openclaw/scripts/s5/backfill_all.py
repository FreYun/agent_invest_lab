#!/usr/bin/env python3
"""S5 全量历史回填: 从 DB 自建涨停池 + K 线, 跑 select → verify(backtest).

不依赖 akshare/research-mcp, 纯 SQLite 数据驱动.

用法:
    python3 backfill_all.py --start 20250101 --end 20260417
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_all")


# ------------------------------------------------------------------ #
# 行业映射 (tushare stock_basic, 拉一次缓存)
# ------------------------------------------------------------------ #

_INDUSTRY_CACHE_PATH = os.path.join(SCRIPT_DIR, ".industry_cache.json")


def _load_industry_map() -> dict[str, str]:
    if os.path.exists(_INDUSTRY_CACHE_PATH):
        age = time.time() - os.path.getmtime(_INDUSTRY_CACHE_PATH)
        if age < 7 * 86400:
            with open(_INDUSTRY_CACHE_PATH) as f:
                return json.load(f)

    logger.info("从 tushare 拉取行业映射 (stock_basic)...")
    sys.path.insert(0, "/home/rooot/agent_invest_lab/market_pipeline/openclaw/workspace-bot11/scripts")
    from config import get_tushare_pro
    pro = get_tushare_pro()
    df = pro.stock_basic(fields="ts_code,industry")
    mapping = {}
    for _, r in df.iterrows():
        mapping[r["ts_code"]] = r["industry"] or "未知"
    with open(_INDUSTRY_CACHE_PATH, "w") as f:
        json.dump(mapping, f, ensure_ascii=False)
    logger.info(f"  缓存 {len(mapping)} 只股票行业映射")
    return mapping


# ------------------------------------------------------------------ #
# Phase 0: 预计算全量涨停连板 (跟 derive_raw_data.py 同逻辑)
# ------------------------------------------------------------------ #


def _compute_all_streaks(conn: sqlite3.Connection,
                         start: str, end: str,
                         all_dates: list[str]) -> dict[tuple[str, str], int]:
    """返回 {(ts_code, trade_date): streak} — 只有涨停日才有条目."""
    logger.info("计算全量涨停连板...")

    rows = conn.execute("""
        SELECT d.trade_date, d.ts_code
        FROM daily d
        INNER JOIN stk_limit l ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
        WHERE d.close >= l.up_limit
        ORDER BY d.trade_date ASC
    """).fetchall()

    lu_by_date: dict[str, set[str]] = defaultdict(set)
    for td, ts in rows:
        lu_by_date[td].add(ts)

    result: dict[tuple[str, str], int] = {}
    prev_streaks: dict[str, int] = {}

    for td in all_dates:
        today_lu = lu_by_date.get(td, set())
        today_streaks: dict[str, int] = {}
        for stock in today_lu:
            today_streaks[stock] = prev_streaks.get(stock, 0) + 1
            result[(stock, td)] = today_streaks[stock]
        prev_streaks = today_streaks

    logger.info(f"  涨停日条目: {len(result)}")
    return result


# ------------------------------------------------------------------ #
# Phase 1: 构建 prewarm 数据
# ------------------------------------------------------------------ #


def _build_prewarm_from_db(conn: sqlite3.Connection, trade_date: str,
                           industry_map: dict[str, str],
                           all_dates: list[str],
                           streak_map: dict[tuple[str, str], int]) -> dict:
    iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"

    # 1. 涨停池
    lu_rows = conn.execute("""
        SELECT d.ts_code, d.close, d.pct_chg, d.amount
        FROM daily d
        INNER JOIN stk_limit l ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
        WHERE d.trade_date = ? AND d.close >= l.up_limit
    """, (trade_date,)).fetchall()

    if not lu_rows:
        return {"limit_up_count": 0}

    conn.execute("DELETE FROM limit_up_pool WHERE date = ?", (iso,))
    for ts_code, close, pct_chg, amount in lu_rows:
        code6 = ts_code.split(".")[0]
        industry = industry_map.get(ts_code, "未知")
        streak = streak_map.get((ts_code, trade_date), 1)
        conn.execute("""
            INSERT OR REPLACE INTO limit_up_pool
            (date, code, name, industry, pct_chg, streak, close, amount,
             market_cap, turnover_rate, seal_amount, first_seal_time, last_seal_time, blast_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (iso, code6, "", industry, pct_chg or 0, streak, close or 0, amount or 0,
              0, 0, 0, "", "", 0))

    # 2. 热门行业 top 3
    industry_counts: dict[str, int] = defaultdict(int)
    for ts_code, *_ in lu_rows:
        industry_counts[industry_map.get(ts_code, "未知")] += 1
    hot = sorted(industry_counts.items(), key=lambda x: -x[1])[:3]

    conn.execute("DELETE FROM hot_industries_daily WHERE date = ?", (iso,))
    for rank, (ind_name, cnt) in enumerate(hot, 1):
        conn.execute(
            "INSERT INTO hot_industries_daily (date, rank, industry, limit_count) VALUES (?,?,?,?)",
            (iso, rank, ind_name, cnt))

    # 3. Universe: 只用涨停池 (不加全行业成分，避免 universe 过大)
    universe_codes: dict[str, str] = {}
    for ts_code, *_ in lu_rows:
        code6 = ts_code.split(".")[0]
        universe_codes.setdefault(code6, industry_map.get(ts_code, "未知"))

    # 额外: 近 30 天内涨停过的股票也纳入 (捕捉"龙回头"回调期的股票)
    idx = all_dates.index(trade_date) if trade_date in all_dates else -1
    if idx >= 0:
        lookback_start = max(0, idx - 30)
        for past_td in all_dates[lookback_start:idx]:
            past_lu = conn.execute("""
                SELECT d.ts_code FROM daily d
                INNER JOIN stk_limit l ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
                WHERE d.trade_date = ? AND d.close >= l.up_limit
            """, (past_td,)).fetchall()
            for (ts,) in past_lu:
                code6 = ts.split(".")[0]
                universe_codes.setdefault(code6, industry_map.get(ts, "未知"))

    conn.execute("DELETE FROM s5_daily_universe WHERE date = ?", (iso,))
    for code6, ind in universe_codes.items():
        conn.execute("INSERT INTO s5_daily_universe (date, code, industry) VALUES (?,?,?)",
                     (iso, code6, ind))

    # 4. K 线缓存 (T-35 ~ T+12, 覆盖 select 回看 + verify 多日持仓)
    if idx >= 0:
        start_idx = max(0, idx - 35)
        end_idx = min(len(all_dates) - 1, idx + 12)
        start_d, end_d = all_dates[start_idx], all_dates[end_idx]
    else:
        start_d = end_d = trade_date

    code6_to_ts = {}
    for ts_code in industry_map:
        code6_to_ts[ts_code.split(".")[0]] = ts_code

    ts_list = [code6_to_ts[c] for c in universe_codes if c in code6_to_ts]

    if ts_list:
        placeholders = ",".join("?" * len(ts_list))

        kline_rows = conn.execute(f"""
            SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close, d.pct_chg, d.vol, d.amount,
                   CASE WHEN d.close >= l.up_limit THEN 1 ELSE 0 END AS is_lu,
                   CASE WHEN d.close <= l.down_limit THEN 1 ELSE 0 END AS is_ld
            FROM daily d
            LEFT JOIN stk_limit l ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
            WHERE d.trade_date >= ? AND d.trade_date <= ?
              AND d.ts_code IN ({placeholders})
        """, [start_d, end_d] + ts_list).fetchall()

        n_bars = 0
        for ts_code, td, op, hi, lo, cl, pct, vol, amt, is_lu, is_ld in kline_rows:
            code6 = ts_code.split(".")[0]
            kiso = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
            lu_streak = streak_map.get((ts_code, td), 0)
            conn.execute("""
                INSERT OR REPLACE INTO klines_cache
                (code, date, open, high, low, close, pct_chg, volume, amount,
                 is_limit_up, is_limit_down, limit_up_streak, limit_down_streak)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (code6, kiso, op, hi, lo, cl, pct, vol, amt, is_lu or 0, is_ld or 0, lu_streak, 0))
            n_bars += 1
    else:
        n_bars = 0

    conn.commit()
    return {
        "limit_up_count": len(lu_rows),
        "hot": [{"name": h[0], "count": h[1]} for h in hot],
        "universe_size": len(universe_codes),
        "klines_bars": n_bars,
    }


# ------------------------------------------------------------------ #
# 主流程
# ------------------------------------------------------------------ #


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20250101")
    ap.add_argument("--end", default="20260417")
    args = ap.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    industry_map = _load_industry_map()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-50000")

    all_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily ORDER BY trade_date"
    ).fetchall()]

    target_dates = [d for d in all_dates if args.start <= d <= args.end]
    logger.info(f"回填 {len(target_dates)} 个交易日 ({target_dates[0]} ~ {target_dates[-1]})")

    # Phase 0: 预计算全量连板
    streak_map = _compute_all_streaks(conn, args.start, args.end, all_dates)

    # Phase 1: 构建 prewarm 数据
    logger.info("===== Phase 1: 构建 prewarm 数据 =====")
    prewarm_ok_dates = []
    for i, td in enumerate(target_dates):
        if (i + 1) % 50 == 0:
            logger.info(f"  prewarm [{i+1}/{len(target_dates)}]")
        result = _build_prewarm_from_db(conn, td, industry_map, all_dates, streak_map)
        if result["limit_up_count"] > 0:
            prewarm_ok_dates.append(td)

    conn.close()
    logger.info(f"  prewarm 完成: {len(prewarm_ok_dates)}/{len(target_dates)} 天有涨停数据")

    # Phase 2: 跑 select (数据全部落 market.db)
    logger.info("===== Phase 2: 批量 select =====")
    import subprocess
    from db_writer import read_select_run
    n_candidates_total = 0
    dates_with_candidates = []

    for i, td in enumerate(prewarm_ok_dates):
        iso = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
        if (i + 1) % 50 == 0:
            logger.info(f"  select [{i+1}/{len(prewarm_ok_dates)}]")

        subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "select.py"), "--date", iso],
            capture_output=True, text=True, timeout=60,
        )

        payload = read_select_run(iso)
        if payload and payload.get("candidates"):
            cands = payload["candidates"]
            n_candidates_total += len(cands)
            dates_with_candidates.append((td, iso, len(cands)))
            logger.info(f"  ★ {iso}: {len(cands)} 只候选")

    logger.info(f"  select 完成: {len(dates_with_candidates)} 天有候选, 共 {n_candidates_total} 只")

    # Phase 3: 跑 verify (backtest)
    logger.info("===== Phase 3: 批量 verify (backtest) =====")
    n_verify = 0
    for td, iso, n_cands in dates_with_candidates:
        idx = all_dates.index(td) if td in all_dates else -1
        if idx < 0 or idx + 1 >= len(all_dates):
            continue
        t1_td = all_dates[idx + 1]
        t1_iso = f"{t1_td[:4]}-{t1_td[4:6]}-{t1_td[6:8]}"

        r = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "verify.py"),
             "--date", t1_iso, "--mode", "backtest"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            n_verify += 1
            logger.info(f"  ✓ {iso} ({n_cands}只) → verify {t1_iso}")
        else:
            logger.warning(f"  ✗ verify {t1_iso} 失败: {r.stderr[-200:]}")

    logger.info("===== 回填完成 =====")
    logger.info(f"  有涨停的交易日: {len(prewarm_ok_dates)}")
    logger.info(f"  有候选的交易日: {len(dates_with_candidates)}")
    logger.info(f"  总候选: {n_candidates_total}")
    logger.info(f"  verify 成功: {n_verify}")

    if dates_with_candidates:
        logger.info("  候选日期:")
        for td, iso, n in dates_with_candidates:
            logger.info(f"    {iso}: {n} 只")


if __name__ == "__main__":
    main()
