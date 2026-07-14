"""
模块：连板 & 题材前瞻
- 今日涨停股列表
- 连板高度统计
- 按题材/行业分组

数据源 (2026-04-13 重构):
  akshare stock_zt_pool_em / stock_zt_pool_dtgc_em (东方财富涨停板池)

原来用 tushare limit_list_d, 但该接口每小时最多 1 次调用, 在历史回放
场景下绝大部分日子会被静默 rate-limit 成 "返回空列表 → 今日涨停 0 家",
导致复盘 MD 数据长期不准。东财涨停板池的优点:
  - 无调用限额
  - 涨停/跌停分两个接口独立拉
  - 连板数直接作为字段返回 (`连板数`), 不用另算
  - 所属行业已在同一个接口里 (`所属行业`), 省掉 stock_basic 二次调用
  - 真实"封板" 判定, 不是用 pct_chg >= 9.8 粗筛

已知限制: 东财仅保留最近 2-3 周数据, 更早的日期会返回空。对日常每天
跑的 daily_review 完全够用, 但历史回放会受限。
"""

import logging
import akshare as ak
import pandas as pd
from collections import Counter


def _code_to_ts(code: str) -> str:
    """6 位 A 股代码 → tushare 风格 'NNNNNN.SH/SZ/BJ'。

    仅用于显示兼容 (renderer 用 ts_code 字段)。
    """
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return code


def _fetch_zt_pool(date_str: str):
    """akshare 涨停板池, 返回 DataFrame。失败或空返回 None。

    date_str 格式: 'YYYY-MM-DD', 内部转换为 'YYYYMMDD'。
    """
    try:
        date_fmt = date_str.replace("-", "")
        df = ak.stock_zt_pool_em(date=date_fmt)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logging.warning(f"akshare 涨停板池拉取失败 ({date_str}): {e}")
        return None


def _fetch_dt_pool(date_str: str):
    """akshare 跌停板池, 返回 DataFrame。失败或空返回 None。"""
    try:
        date_fmt = date_str.replace("-", "")
        df = ak.stock_zt_pool_dtgc_em(date=date_fmt)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logging.warning(f"akshare 跌停板池拉取失败 ({date_str}): {e}")
        return None


def _zt_pool_to_items(zt_df: pd.DataFrame) -> list:
    """把涨停池 DF 转成 renderer 兼容的 item 列表, 按连板数降序。

    输入字段 (akshare stock_zt_pool_em):
        代码 / 名称 / 涨跌幅 / 最新价 / 成交额 / 流通市值 / 总市值 /
        换手率 / 封板资金 / 首次封板时间 / 最后封板时间 / 炸板次数 /
        涨停统计 / 连板数 / 所属行业
    输出字段 (匹配原 tushare 版本的 renderer 用法):
        ts_code / name / close / pct_chg / fd_amount / first_time /
        last_time / open_times / consecutive / up_stat / industry
    """
    items = []
    for _, row in zt_df.iterrows():
        code = str(row.get("代码", "")).zfill(6)
        items.append({
            "ts_code": _code_to_ts(code),
            "name": str(row.get("名称", "")),
            "close": float(row.get("最新价", 0) or 0),
            "pct_chg": float(row.get("涨跌幅", 0) or 0),
            "fd_amount": float(row.get("封板资金", 0) or 0),  # 元
            "first_time": str(row.get("首次封板时间", "")),
            "last_time": str(row.get("最后封板时间", "")),
            "open_times": int(row.get("炸板次数", 0) or 0),
            "consecutive": int(row.get("连板数", 1) or 1),
            "up_stat": str(row.get("涨停统计", "")),
            "industry": str(row.get("所属行业", "未知")),
        })
    items.sort(key=lambda x: x["consecutive"], reverse=True)
    return items


def fetch_limit_up_data(date_str: str) -> dict:
    """主函数：获取连板 & 题材前瞻。

    返回 dict 结构与原版兼容 (renderer 消费的字段):
        limit_up_count_ak         东财涨停池行数 (主口径)
        limit_up_count_ts         保留为 0 (不再用 tushare, 仅保留字段名)
        limit_down_count          东财跌停池行数
        max_consecutive           最高连板数
        consecutive_dist          {板数: 只数}
        theme_dist                {行业: 涨停只数} top 10
        top_consecutive           连板数降序的前 10 只
        total_limit_up            主口径=limit_up_count_ak
    """
    zt_df = _fetch_zt_pool(date_str)
    dt_df = _fetch_dt_pool(date_str)

    limit_ups = _zt_pool_to_items(zt_df) if zt_df is not None else []

    # 连板统计
    max_consecutive = 0
    consecutive_dist: dict = {}
    for item in limit_ups:
        c = item["consecutive"]
        if c > max_consecutive:
            max_consecutive = c
        consecutive_dist[c] = consecutive_dist.get(c, 0) + 1

    # 题材分布 (直接从 `所属行业` 字段统计, 无需二次调用)
    theme_dist: dict = {}
    if limit_ups:
        theme_counter = Counter(item["industry"] for item in limit_ups)
        theme_dist = dict(theme_counter.most_common(10))

    # 跌停数
    limit_down_count = int(len(dt_df)) if dt_df is not None else 0

    return {
        "limit_up_count_ak": len(limit_ups),
        "limit_up_count_ts": 0,  # 遗留字段, 已弃用 tushare, 保留 key 避免破坏下游
        "limit_down_count": limit_down_count,
        "max_consecutive": max_consecutive,
        "consecutive_dist": consecutive_dist,
        "theme_dist": theme_dist,
        "top_consecutive": limit_ups[:10],
        "total_limit_up": len(limit_ups),
        "data_source": "akshare_east_money_zt_pool",
    }
