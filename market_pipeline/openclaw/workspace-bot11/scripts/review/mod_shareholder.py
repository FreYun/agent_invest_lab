"""
模块：股东增减持
- 拟增持/减持公告数
- tushare stk_holdertrade 接口
"""

import logging
import time
from collections import Counter

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_tushare_pro


def fetch_shareholder_activity(date_str):
    """主函数：获取股东增减持数据"""
    try:
        pro = get_tushare_pro()
        date_fmt = date_str.replace("-", "")

        df = pro.stk_holdertrade(ann_date=date_fmt)
        time.sleep(0.15)

        if df is None or df.empty:
            return {
                "increase_count": 0,
                "decrease_count": 0,
                "increase_list": [],
                "decrease_list": [],
                "level": "无数据",
            }

        # in_de 字段: IN=增持, DE=减持
        increases = df[df["in_de"] == "IN"] if "in_de" in df.columns else df.head(0)
        decreases = df[df["in_de"] == "DE"] if "in_de" in df.columns else df.head(0)

        inc_count = len(increases["ts_code"].unique()) if not increases.empty else 0
        dec_count = len(decreases["ts_code"].unique()) if not decreases.empty else 0

        # 减持区间判断
        if dec_count <= 5:
            level = "偏少"
        elif dec_count <= 15:
            level = "中等"
        else:
            level = "偏多"

        # 增持明细（前5）
        inc_list = []
        if not increases.empty:
            for ts_code in increases["ts_code"].unique()[:5]:
                rows = increases[increases["ts_code"] == ts_code]
                name = rows.iloc[0].get("holder_name", "")
                vol = rows["change_vol"].sum() if "change_vol" in rows.columns else 0
                inc_list.append({
                    "ts_code": ts_code,
                    "holder": name[:10],
                    "volume": float(vol),
                })

        # 减持明细（前5）
        dec_list = []
        if not decreases.empty:
            for ts_code in decreases["ts_code"].unique()[:5]:
                rows = decreases[decreases["ts_code"] == ts_code]
                name = rows.iloc[0].get("holder_name", "")
                vol = rows["change_vol"].sum() if "change_vol" in rows.columns else 0
                dec_list.append({
                    "ts_code": ts_code,
                    "holder": name[:10],
                    "volume": float(vol),
                })

        return {
            "increase_count": inc_count,
            "decrease_count": dec_count,
            "increase_list": inc_list,
            "decrease_list": dec_list,
            "level": level,
        }
    except Exception as e:
        logging.warning(f"获取股东增减持失败: {e}")
        return {"error": str(e)}
