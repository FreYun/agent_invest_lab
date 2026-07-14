"""
模块：资金与流动性
- 北向资金净流入 (Tushare moneyflow_hsgt)
- 10 年期国债收益率 (Tushare yc_cb 中债国债收益率曲线)

2026-04-14 迁移: 全面切换到 Tushare
- 删除 akshare 北向资金备份 (之前只做交叉验证, tushare 稳定后无必要)
- 国债收益率 akshare bond_china_yield → tushare yc_cb
"""

import logging
import time
from datetime import datetime, timedelta

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_tushare_pro


def _fetch_northbound_ts(date_str):
    """tushare 获取北向资金净流入 (沪股通 + 深股通合计)。

    north_money 字段单位为万元, 返回转为亿元。
    """
    try:
        pro = get_tushare_pro()
        date_fmt = date_str.replace("-", "")
        df = pro.moneyflow_hsgt(trade_date=date_fmt)
        time.sleep(0.15)
        if df is not None and len(df) > 0:
            r = df.iloc[0]
            north = float(r.get("north_money", 0)) / 10000  # 万 → 亿
            return {"net_flow": round(north, 2), "source": "tushare"}
        return None
    except Exception as e:
        logging.warning(f"tushare 获取北向资金失败: {e}")
        return None


def _fetch_bond_yield_ts(date_str):
    """tushare yc_cb 获取 10 年期国债收益率。

    接口返回"中债国债收益率曲线"横截面数据, 需筛 ts_code=1001.CB (国债)
    + curve_term=10.0 (10 年期)。同一天可能有多个样本点 (curve_type),
    取最后一条作为当日收盘收益率。

    返回 {'yield_10y': float} or None
    """
    try:
        pro = get_tushare_pro()
        date_fmt = date_str.replace("-", "")
        # 往前宽松几天, 避免非交易日直接命中失败
        start_dt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=7)
        df = pro.yc_cb(
            ts_code="1001.CB",
            start_date=start_dt.strftime("%Y%m%d"),
            end_date=date_fmt,
            curve_term=10.0,
        )
        time.sleep(0.15)
        if df is None or df.empty:
            return None
        # 按日期降序取最近一条
        df = df.sort_values("trade_date", ascending=False)
        row = df.iloc[0]
        yield_10y = float(row["yield"])
        return {"yield_10y": round(yield_10y, 4)}
    except Exception as e:
        logging.warning(f"tushare 获取国债收益率失败: {e}")
        return None


def fetch_capital_flow(date_str):
    """主函数：获取资金与流动性数据"""
    northbound = _fetch_northbound_ts(date_str)
    bond_yield = _fetch_bond_yield_ts(date_str)

    return {
        "northbound": northbound,
        "margin": None,
        "bond_yield": bond_yield,
        "_validation": {},  # 单数据源, 不再做交叉验证
    }
