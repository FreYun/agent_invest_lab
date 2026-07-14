"""
每日复盘数据自动化采集 — 入口脚本

用法:
    python daily_review.py              # 获取今日数据
    python daily_review.py 2026-03-14   # 获取指定日期数据

输出:
    落库到 market.db.regime_raw_daily + index_daily
    日志: memory/review-output/logs/review_YYYY-MM-DD.log
"""

import sys
import os
import logging
import traceback
from datetime import datetime

# 项目路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(SCRIPT_DIR)
WORKSPACE_DIR = os.path.dirname(SCRIPTS_DIR)
OUTPUT_DIR = os.path.join(WORKSPACE_DIR, "memory", "review-output")
LOG_DIR = os.path.join(OUTPUT_DIR, "logs")

sys.path.insert(0, SCRIPTS_DIR)
from config import get_tushare_pro

# 确保输出目录存在
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


def setup_logging(date_str):
    """配置日志"""
    log_file = os.path.join(LOG_DIR, f"review_{date_str}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def is_trading_day(date_str):
    """检查是否为交易日"""
    try:
        pro = get_tushare_pro()
        date_fmt = date_str.replace("-", "")
        cal = pro.trade_cal(
            exchange="SSE", start_date=date_fmt, end_date=date_fmt
        )
        if len(cal) > 0 and cal.iloc[0]["is_open"] == 1:
            return True
        return False
    except Exception as e:
        logging.warning(f"交易日历查询失败: {e}，默认继续执行")
        # 周末直接跳过
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.weekday() < 5


def run_module(name, func, date_str):
    """安全执行单个模块"""
    try:
        logging.info(f"[开始] {name}")
        result = func(date_str)
        logging.info(f"[完成] {name}")
        return result
    except Exception as e:
        logging.error(f"[失败] {name}: {e}\n{traceback.format_exc()}")
        return {"error": str(e)}


def main():
    # 解析日期参数
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")

    setup_logging(date_str)
    logging.info(f"===== 每日复盘数据采集 {date_str} =====")

    # 交易日检查
    if not is_trading_day(date_str):
        logging.info(f"{date_str} 不是交易日，跳过")
        print(f"{date_str} 不是交易日，跳过")
        return

    # 延迟导入各模块（避免启动时全部加载）
    from review.mod_market_overview import fetch_market_overview
    from review.mod_intraday_profile import fetch_intraday_profile
    from review.mod_sentiment import fetch_sentiment
    from review.mod_sector_rotation import fetch_sector_rotation
    from review.mod_limit_up_tracking import fetch_limit_up_data
    from review.mod_capital_flow import fetch_capital_flow
    from review.mod_shareholder import fetch_shareholder_activity
    from review.mod_equity_risk_premium import fetch_erp

    # 按顺序执行各模块
    results = {}

    # 1. 市场全景（先跑，提取 breadth 供下游复用）
    results["market_overview"] = run_module("市场全景", fetch_market_overview, date_str)
    shared_breadth = None
    mo = results.get("market_overview", {})
    if isinstance(mo, dict) and "error" not in mo:
        shared_breadth = mo.get("breadth")

    # 2. 其余模块（情绪温度计使用共享 breadth）
    # 股债收益比依赖资金与流动性的 bond_yield, 必须在其之后运行, 单独穿透
    modules = [
        ("日内画像", fetch_intraday_profile),
        ("情绪温度计", lambda d: fetch_sentiment(d, shared_breadth=shared_breadth)),
        ("板块轮动", fetch_sector_rotation),
        ("连板前瞻", fetch_limit_up_data),
        ("资金与流动性", fetch_capital_flow),
        ("股东行为", fetch_shareholder_activity),
    ]

    for name, func in modules:
        # 从 func name 提取 key，lambda 没有好名字则用模块名映射
        if hasattr(func, '__name__') and func.__name__ != '<lambda>':
            key = func.__name__.replace("fetch_", "")
        else:
            key = {"情绪温度计": "sentiment"}.get(name, name)
        results[key] = run_module(name, func, date_str)

    # 股债收益比: 从 capital_flow 穿透 bond_yield, 避免 yc_cb 重复调用
    cf = results.get("capital_flow", {})
    shared_bond = None
    if isinstance(cf, dict) and "error" not in cf:
        by = cf.get("bond_yield")
        if by and isinstance(by, dict):
            shared_bond = by.get("yield_10y")
    results["erp"] = run_module(
        "股债收益比",
        lambda d: fetch_erp(d, shared_bond_yield=shared_bond),
        date_str,
    )

    # 落库到 regime_raw_daily + index_daily
    try:
        _write_raw_to_db(date_str, results)
    except Exception as e:
        logging.warning(f"落库失败: {e}")

    # 统计成功/失败
    ok = sum(1 for v in results.values() if "error" not in v)
    fail = sum(1 for v in results.values() if "error" in v)
    logging.info(f"===== 完成: {ok} 成功, {fail} 失败 =====")

    # 注: 2026-04-17 起 classify.py 已退役, regime 分类由 cron
    # (daily-regime-pipeline.sh) 跑 backfill/replay.py 直接写入
    # market.db.regime_classify_daily. 本脚本只负责采集+落库, 不再触发分类.


def _write_raw_to_db(date_str: str, results: dict):
    """把复盘数据写入 market.db, 涨跌停用 close>=up_limit 精确判定。

    写入:
    - daily + stk_limit (当日全市场个股行情 + 涨跌停价)
    - regime_raw_daily (六维原始数据, 涨跌停从 daily×stk_limit 精确计算)
    - index_daily (当日指数行情)
    """
    import sqlite3
    import time

    DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))
    date_key = date_str.replace("-", "")  # YYYYMMDD

    # ---- 从 Tushare 拉 daily + stk_limit, 落库 ----
    try:
        sys.path.insert(0, os.path.join(SCRIPTS_DIR))
        from config import get_tushare_pro
        pro = get_tushare_pro()

        df_daily = pro.daily(trade_date=date_key)
        time.sleep(0.15)
        df_limit = pro.stk_limit(trade_date=date_key)
        time.sleep(0.15)
    except Exception as e:
        logging.warning(f"Tushare daily/stk_limit 拉取失败: {e}, 降级用模块数据")
        df_daily = None
        df_limit = None

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        # ---- 写 daily + stk_limit 到 DB ----
        if df_daily is not None and not df_daily.empty:
            rows_d = [
                (r["trade_date"], r["ts_code"], r["open"], r["high"], r["low"],
                 r["close"], r["pre_close"], r["pct_chg"], r["vol"], r["amount"])
                for _, r in df_daily.iterrows()
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO daily "
                "(trade_date, ts_code, open, high, low, close, pre_close, pct_chg, vol, amount) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows_d,
            )
            logging.info(f"daily 写入: {date_key} ({len(rows_d)} stocks)")

        if df_limit is not None and not df_limit.empty:
            rows_l = [
                (r["trade_date"], r["ts_code"], r["up_limit"], r["down_limit"])
                for _, r in df_limit.iterrows()
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO stk_limit "
                "(trade_date, ts_code, up_limit, down_limit) "
                "VALUES (?,?,?,?)", rows_l,
            )
            logging.info(f"stk_limit 写入: {date_key} ({len(rows_l)} stocks)")

        # ---- regime_raw_daily: 精确计算涨跌停 ----
        mo = results.get("market_overview", {})
        breadth = mo.get("breadth") if isinstance(mo, dict) and "error" not in mo else None
        sentiment = results.get("sentiment", {})
        limit_up_data = results.get("limit_up_data", {})

        if breadth is not None:
            total = breadth.get("total", 0)
            up = breadth.get("up", 0)
            down = breadth.get("down", 0)
            flat = breadth.get("flat", 0)
            total_amount = breadth.get("total_amount")
            adr = round(up / total, 4) if total > 0 else None

            # 涨跌停: 用 close >= up_limit 精确判定 (包含 ST)
            if df_daily is not None and df_limit is not None and not df_daily.empty and not df_limit.empty:
                merged = df_daily.merge(df_limit, on=["trade_date", "ts_code"], how="inner")
                lu = int((merged["close"] >= merged["up_limit"]).sum())
                ld = int((merged["close"] <= merged["down_limit"]).sum())
                logging.info(f"涨跌停精确计算: 涨停={lu}, 跌停={ld} (close vs up/down_limit)")
            else:
                # 降级: 用模块的数据
                lu = breadth.get("limit_up", 0)
                ld = breadth.get("limit_down", 0)
                logging.info(f"涨跌停降级用模块数据: 涨停={lu}, 跌停={ld}")

            score = sentiment.get("score") if isinstance(sentiment, dict) and "error" not in sentiment else None
            max_c = limit_up_data.get("max_consecutive", 0) if isinstance(limit_up_data, dict) and "error" not in limit_up_data else 0

            indices = mo.get("indices", {})
            hs300 = indices.get("沪深300", {})
            csi1000 = indices.get("中证1000", {})

            conn.execute(
                """INSERT OR REPLACE INTO regime_raw_daily (
                    trade_date, total, up, down, flat, advance_decline_ratio,
                    limit_up_count, limit_down_count, sentiment_delta,
                    sentiment_index, max_streak, total_amount_yi,
                    hs300_close, hs300_pct_chg, csi1000_close, csi1000_pct_chg
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    date_key, total, up, down, flat, adr,
                    lu, ld, lu - ld,
                    score, max_c, total_amount,
                    hs300.get("close"), hs300.get("pct_chg"),
                    csi1000.get("close"), csi1000.get("pct_chg"),
                ),
            )
            logging.info(f"regime_raw_daily 写入: {date_key}")

        # ---- index_daily: 今日指数行情 ----
        # 10 大宽基与 mod_market_overview.INDICES 对齐；缺一只 res14/scout 大盘技术面就会
        # 落后一天。upstream 缺数据的宽基 skip，避免写空行。
        indices = mo.get("indices", {}) if isinstance(mo, dict) and "error" not in mo else {}
        INDEX_MAP = {
            "上证指数": "000001.SH",
            "深证成指": "399001.SZ",
            "深证综指": "399106.SZ",
            "创业板指": "399006.SZ",
            "沪深300": "000300.SH",
            "中证1000": "000852.SH",
            "科创50":   "000688.SH",
            "中证500":  "000905.SH",
            "中证2000": "932000.CSI",
            "北证50":   "899050.BJ",
        }
        written = 0
        missing = []
        for name, ts_code in INDEX_MAP.items():
            idx = indices.get(name, {})
            if idx and idx.get("close"):
                conn.execute(
                    """INSERT OR REPLACE INTO index_daily
                    (trade_date, ts_code, open, high, low, close, pre_close, pct_chg, vol, amount)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        date_key, ts_code,
                        idx.get("open"), idx.get("high"), idx.get("low"), idx.get("close"),
                        idx.get("pre_close"), idx.get("pct_chg"),
                        idx.get("vol"), idx.get("amount"),
                    ),
                )
                written += 1
            else:
                missing.append(f"{name}({ts_code})")
        logging.info(f"index_daily 写入: {date_key} ({written}/{len(INDEX_MAP)} indices)")
        if missing:
            logging.warning(f"index_daily 缺失: {date_key} → {', '.join(missing)}")

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
