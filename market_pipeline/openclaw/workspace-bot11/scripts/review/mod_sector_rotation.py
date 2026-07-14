"""
模块：板块轮动
- 行业板块 TOP5 涨/跌 (东财行业)
- 概念板块 TOP5 涨/跌 (同花顺概念)

2026-04-14 迁移: Tushare 主路
- 行业: akshare stock_board_industry_name_em → tushare moneyflow_ind_dc
- 概念: akshare stock_board_concept_name_em → tushare moneyflow_cnt_ths

关键限制:
- moneyflow_ind_dc / moneyflow_cnt_ths 每小时最多 2 次调用
- daily_review 一天跑一次 = 两个接口各 1 次, 刚好在限额内
- 为了应对一小时内重跑 (调试/补数据), 本模块实现 JSON 缓存:
  当天已成功拉取则直接读缓存, 避免触发限额
- akshare 作为 fallback 保留, 限额触发时自动降级
"""

import json
import logging
import os
import time
from datetime import datetime

import akshare as ak

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_tushare_pro

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, "sector_cache")


def _cache_path(kind: str, date_str: str) -> str:
    """kind: 'industry' | 'concept'"""
    return os.path.join(CACHE_DIR, f"{kind}_{date_str}.json")


def _read_cache(kind: str, date_str: str):
    path = _cache_path(kind, date_str)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"sector cache 读取失败 ({path}): {e}")
        return None


def _write_cache(kind: str, date_str: str, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(kind, date_str)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"sector cache 写入失败 ({path}): {e}")


# --------------------------------------------------------------------------- #
# 行业板块: Tushare moneyflow_ind_dc (东财行业)
# --------------------------------------------------------------------------- #


def _fetch_industry_ts(date_str: str):
    """Tushare moneyflow_ind_dc 获取东财行业板块排名。

    字段含义 (单日查询返回包含多个 content_type, 需筛 '行业'):
        pct_change: 板块涨跌幅
        net_amount: 净流入 (元)
        buy_sm_amount_stock: 领涨股 (字段名历史遗留, 实际是板块领涨)
        rank: 板块排名
    """
    try:
        pro = get_tushare_pro()
        date_fmt = date_str.replace("-", "")
        df = pro.moneyflow_ind_dc(
            trade_date=date_fmt,
            fields="trade_date,content_type,ts_code,name,pct_change,close,"
                   "net_amount,buy_sm_amount_stock,rank",
        )
        time.sleep(0.15)
        if df is None or df.empty:
            return None

        industries = df[df["content_type"] == "行业"].copy()
        if industries.empty:
            return None
        industries = industries.sort_values("pct_change", ascending=False).reset_index(drop=True)

        def extract(row):
            return {
                "name": str(row["name"]),
                "pct_chg": float(row["pct_change"]),
                "amount": 0.0,  # moneyflow_ind_dc 不直接提供板块成交额
                "leader": str(row.get("buy_sm_amount_stock", "") or ""),
                "leader_pct": 0.0,  # 领涨股涨跌幅不在此接口返回
                "up_count": 0,  # 同上, 需要另起接口
                "down_count": 0,
            }

        top5 = [extract(industries.iloc[i]) for i in range(min(5, len(industries)))]
        bottom5 = [extract(industries.iloc[-(i + 1)]) for i in range(min(5, len(industries)))]
        bottom5.reverse()

        return {
            "top": top5,
            "bottom": bottom5,
            "total": int(len(industries)),
            "source": "tushare",
        }
    except Exception as e:
        logging.warning(f"tushare 获取行业板块失败: {e}")
        return None


def _fetch_industry_ak():
    """akshare 东财行业板块 (fallback, 字段更丰富)."""
    try:
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            return None

        df = df.sort_values("涨跌幅", ascending=False).reset_index(drop=True)

        def extract(row):
            return {
                "name": row.get("板块名称", ""),
                "pct_chg": float(row.get("涨跌幅", 0)),
                "amount": float(row.get("成交额", 0)),
                "leader": row.get("领涨股票", ""),
                "leader_pct": float(
                    row.get("涨跌幅.1", row.get("领涨股票-涨跌幅", 0))
                    if "涨跌幅.1" in row.index or "领涨股票-涨跌幅" in row.index
                    else 0
                ),
                "up_count": int(row.get("上涨家数", 0)),
                "down_count": int(row.get("下跌家数", 0)),
            }

        top5 = [extract(df.iloc[i]) for i in range(min(5, len(df)))]
        bottom5 = [extract(df.iloc[-(i + 1)]) for i in range(min(5, len(df)))]
        bottom5.reverse()

        return {"top": top5, "bottom": bottom5, "total": len(df), "source": "akshare"}
    except Exception as e:
        logging.warning(f"akshare 获取行业板块失败: {e}")
        return None


def _fetch_industry_boards(date_str: str):
    """优先读当日缓存 → tushare 主路 → akshare fallback."""
    cached = _read_cache("industry", date_str)
    if cached:
        logging.info("行业板块: 读取当日缓存")
        return cached

    result = _fetch_industry_ts(date_str)
    if result is None:
        logging.info("行业板块: tushare 失败, fallback akshare")
        result = _fetch_industry_ak()

    if result is not None:
        _write_cache("industry", date_str, result)
    return result


# --------------------------------------------------------------------------- #
# 概念板块: Tushare moneyflow_cnt_ths (同花顺概念)
# --------------------------------------------------------------------------- #


def _fetch_concept_ts(date_str: str):
    """Tushare moneyflow_cnt_ths 获取同花顺概念板块排名."""
    try:
        pro = get_tushare_pro()
        date_fmt = date_str.replace("-", "")
        df = pro.moneyflow_cnt_ths(
            trade_date=date_fmt,
            fields="trade_date,ts_code,name,lead_stock,close_price,pct_change,"
                   "company_num,net_amount",
        )
        time.sleep(0.15)
        if df is None or df.empty:
            return None

        df = df.sort_values("pct_change", ascending=False).reset_index(drop=True)

        def extract(row):
            return {
                "name": str(row["name"]),
                "pct_chg": float(row["pct_change"]),
                "leader": str(row.get("lead_stock", "") or ""),
            }

        top5 = [extract(df.iloc[i]) for i in range(min(5, len(df)))]
        bottom5 = [extract(df.iloc[-(i + 1)]) for i in range(min(5, len(df)))]
        bottom5.reverse()

        return {"top": top5, "bottom": bottom5, "total": len(df), "source": "tushare"}
    except Exception as e:
        logging.warning(f"tushare 获取概念板块失败: {e}")
        return None


def _fetch_concept_ak():
    """akshare 东财概念板块 (fallback)."""
    try:
        df = ak.stock_board_concept_name_em()
        if df is None or df.empty:
            return None

        df = df.sort_values("涨跌幅", ascending=False).reset_index(drop=True)

        def extract(row):
            return {
                "name": row.get("板块名称", ""),
                "pct_chg": float(row.get("涨跌幅", 0)),
                "leader": row.get("领涨股票", ""),
            }

        top5 = [extract(df.iloc[i]) for i in range(min(5, len(df)))]
        bottom5 = [extract(df.iloc[-(i + 1)]) for i in range(min(5, len(df)))]
        bottom5.reverse()

        return {"top": top5, "bottom": bottom5, "total": len(df), "source": "akshare"}
    except Exception as e:
        logging.warning(f"akshare 获取概念板块失败: {e}")
        return None


def _fetch_concept_boards(date_str: str):
    """优先读当日缓存 → tushare 主路 → akshare fallback."""
    cached = _read_cache("concept", date_str)
    if cached:
        logging.info("概念板块: 读取当日缓存")
        return cached

    result = _fetch_concept_ts(date_str)
    if result is None:
        logging.info("概念板块: tushare 失败, fallback akshare")
        result = _fetch_concept_ak()

    if result is not None:
        _write_cache("concept", date_str, result)
    return result


# --------------------------------------------------------------------------- #
# 主函数
# --------------------------------------------------------------------------- #


def fetch_sector_rotation(date_str):
    """主函数：获取板块轮动数据"""
    industry = _fetch_industry_boards(date_str)
    concept = _fetch_concept_boards(date_str)

    return {
        "industry": industry,
        "concept": concept,
    }
