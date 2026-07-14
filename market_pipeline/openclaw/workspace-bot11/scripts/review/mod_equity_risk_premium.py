"""
模块：股债收益比 (Equity Risk Premium)
- 沪深300 PE → 盈利收益率 (1/PE)   [tushare index_dailybasic]
- 10年期国债收益率                   [tushare yc_cb 中债国债收益率曲线]
- ERP = 盈利收益率 - 国债收益率

2026-04-14 迁移: 国债收益率从 akshare bond_china_yield 改为 tushare yc_cb。

2026-04-15 重写: 修复 percentile 口径 bug + 合并接口调用
- 修复 pre-existing bug: 原代码把 `earnings_yield` 序列当作 `erp_history`,
  分位时用 ERP 标量和 earnings_yield 序列比较, 永远返回 0.0%。
- 新设计: `_fetch_erp_series` 一次拉取 3 年 HS300 PE + 3 年 10Y 国债,
  按 trade_date 合并, 得到真正的 ERP 序列, 分位比较名实相符。
- 缓存 schema 升级: rows 含 pe_ttm / earnings_yield / bond_yield_10y / erp
- `shared_bond_yield` 参数仍保留, 但仅作为 yc_cb 历史拉取失败时的 bond
  展示兜底, 正常路径下当日值从合并序列的尾部直接提取。
"""

import logging
import time
import json
import os
from datetime import datetime, timedelta

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_tushare_pro

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(SCRIPT_DIR, "erp_cache.json")


def _load_erp_history():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"erp_cache 读取失败: {e}")
    return {}


def _save_erp_history(data):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _calc_percentile(current, history_values):
    """当前值在历史序列中的分位 (0-100, 值越大越"便宜")。"""
    if not history_values or current is None:
        return None
    below = sum(1 for v in history_values if v <= current)
    return round(below / len(history_values) * 100, 1)


def _is_new_schema(cache):
    """判断缓存是否为新 schema (rows 里每行都含 erp 字段)."""
    rows = cache.get("rows") or []
    return bool(rows) and "erp" in rows[0] and "bond_yield_10y" in rows[0]


def _fetch_erp_series(date_str, days=750):
    """一次 PE + 一次 yc_cb, 合并为 3 年 ERP 序列 + 当日值。

    缓存 schema:
        {
            "last_update": "YYYY-MM-DD",
            "rows": [
                {"trade_date": "YYYYMMDD", "pe_ttm": float,
                 "earnings_yield": float, "bond_yield_10y": float, "erp": float},
                ...
            ]
        }
    旧缓存 (只有 erp_values 列表) 视为未命中, 自动升级。

    返回: (today_dict_or_None, erp_history_list)
        today_dict: {pe_ttm, earnings_yield, bond_yield_10y, erp}
        erp_history_list: 按日期升序的 ERP 序列 (供分位计算)
    """
    cache = _load_erp_history()
    target_fmt = date_str.replace("-", "")

    # ---- 1. 缓存命中路径 ----
    if _is_new_schema(cache) and cache.get("last_update") == date_str:
        rows = cache["rows"]
        matched = [r for r in rows if r["trade_date"] == target_fmt]
        if matched:
            r = matched[0]
            today = {
                "pe_ttm": round(float(r["pe_ttm"]), 2),
                "earnings_yield": round(float(r["earnings_yield"]), 4),
                "bond_yield_10y": round(float(r["bond_yield_10y"]), 4),
                "erp": round(float(r["erp"]), 4),
            }
            history = [float(r["erp"]) for r in rows]
            logging.info("ERP 序列: 命中当日缓存")
            return today, history

    # ---- 2. 冷启动 / 跨日, 拉新数据 ----
    try:
        pro = get_tushare_pro()
        start_dt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=int(days * 1.5))
        start_fmt = start_dt.strftime("%Y%m%d")

        # 2.1 拉 3 年 HS300 PE_TTM
        logging.info(f"拉 HS300 PE 历史 {start_fmt} ~ {target_fmt}")
        pe_df = pro.index_dailybasic(
            ts_code="000300.SH",
            start_date=start_fmt,
            end_date=target_fmt,
            fields="trade_date,pe_ttm",
        )
        time.sleep(0.15)
        if pe_df is None or pe_df.empty:
            logging.warning("PE 历史为空, ERP 序列拉取终止")
            return None, []
        pe_df = pe_df[pe_df["pe_ttm"] > 0].copy()
        pe_df["earnings_yield"] = 1 / pe_df["pe_ttm"] * 100

        # 2.2 拉 3 年 10Y 国债 (curve_type=0 到期收益率, 与 daily_review 用法一致)
        logging.info(f"拉 10Y 国债历史 {start_fmt} ~ {target_fmt}")
        bond_df = pro.yc_cb(
            ts_code="1001.CB",
            start_date=start_fmt,
            end_date=target_fmt,
            curve_term=10.0,
        )
        time.sleep(0.15)
        if bond_df is None or bond_df.empty:
            logging.warning("10Y 国债历史为空, ERP 序列拉取终止")
            return None, []
        bond_df = bond_df[bond_df["curve_type"] == "0"].copy()
        bond_df = bond_df[["trade_date", "yield"]].rename(
            columns={"yield": "bond_yield_10y"}
        )

        # 2.3 按 trade_date 内连接 (只保留 PE 和 bond 都有的日期)
        df = pe_df.merge(bond_df, on="trade_date", how="inner")
        df = df.sort_values("trade_date", ascending=True).reset_index(drop=True)
        df["erp"] = df["earnings_yield"] - df["bond_yield_10y"]

        if df.empty:
            logging.warning("PE/bond 合并后为空, ERP 序列拉取终止")
            return None, []

        # 2.4 提取当日 (精确匹配 → tail 退化)
        today_df = df[df["trade_date"] == target_fmt]
        if today_df.empty:
            today_df = df.tail(1)
        r = today_df.iloc[0]
        today = {
            "pe_ttm": round(float(r["pe_ttm"]), 2),
            "earnings_yield": round(float(r["earnings_yield"]), 4),
            "bond_yield_10y": round(float(r["bond_yield_10y"]), 4),
            "erp": round(float(r["erp"]), 4),
        }
        history = df["erp"].tolist()

        # 2.5 落盘缓存 (新 schema, 清理旧字段)
        cache["rows"] = df[
            ["trade_date", "pe_ttm", "earnings_yield", "bond_yield_10y", "erp"]
        ].to_dict("records")
        cache["last_update"] = date_str
        cache.pop("erp_values", None)  # 删除旧 schema 残留
        _save_erp_history(cache)

        return today, history
    except Exception as e:
        logging.warning(f"获取 ERP 历史失败: {e}")
        return None, []


def fetch_erp(date_str, shared_bond_yield=None):
    """主函数：计算股债收益比 + 历史分位。

    Args:
        date_str: 日期 'YYYY-MM-DD'
        shared_bond_yield: 外部已拉好的 10 年期国债收益率 (%), 仅在
            `_fetch_erp_series` 彻底失败时作为 bond 展示兜底。正常路径
            下当日 bond_yield 直接从 3 年历史尾部提取, 保证与历史序列同源。
    """
    today, erp_history = _fetch_erp_series(date_str)

    if today is None:
        # 整体失败兜底: 仅保留 shared bond 作显示 (不再计算 ERP / 分位)
        return {
            "csi300_pe": None,
            "bond_yield_10y": (
                float(shared_bond_yield) if shared_bond_yield is not None else None
            ),
            "erp": None,
            "erp_percentile": None,
        }

    pe_data = {
        "pe_ttm": today["pe_ttm"],
        "earnings_yield": today["earnings_yield"],
    }
    bond_yield_10y = today["bond_yield_10y"]
    erp = today["erp"]

    # 真正的 ERP 分位: 当前 ERP 标量在历史 ERP 序列中的位置
    # (之前 bug: 用 earnings_yield 序列比较, 永远返回 0.0%)
    percentile = _calc_percentile(erp, erp_history)

    return {
        "csi300_pe": pe_data,
        "bond_yield_10y": bond_yield_10y,
        "erp": erp,
        "erp_percentile": percentile,
    }
