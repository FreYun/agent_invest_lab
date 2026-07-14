"""
模块：情绪温度计
- 赚钱效应评估
- 涨停/跌停比
- 情绪综合评分 (0-100)
- PCR 数据（暂缺，预留接口）
"""

import logging
import akshare as ak


def _assess_sentiment(breadth, limit_data):
    """综合评估市场情绪"""
    score = 50  # 基准分

    if breadth:
        total = breadth.get("total", 1)
        up = breadth.get("up", 0)
        up_ratio = up / total if total > 0 else 0.5

        # 涨跌家数得分 (0-40分)
        if up_ratio >= 0.8:
            score += 30
        elif up_ratio >= 0.6:
            score += 15
        elif up_ratio >= 0.5:
            score += 5
        elif up_ratio >= 0.4:
            score -= 5
        elif up_ratio >= 0.3:
            score -= 15
        else:
            score -= 30

        # 涨停跌停比得分 (0-20分)
        lu = breadth.get("limit_up", 0)
        ld = breadth.get("limit_down", 0)
        if lu > 0 and ld == 0:
            score += 15
        elif lu > ld * 3:
            score += 10
        elif lu > ld:
            score += 5
        elif ld > lu * 3:
            score -= 15
        elif ld > lu:
            score -= 5

    if limit_data:
        # 连板高度得分 (0-10分)
        max_c = limit_data.get("max_consecutive", 0)
        if max_c >= 5:
            score += 10
        elif max_c >= 3:
            score += 5

    # 限制在 0-100
    score = max(0, min(100, score))
    return score


def _sentiment_label(score):
    """情绪评分转文字"""
    if score >= 80:
        return "极度亢奋"
    elif score >= 65:
        return "偏多活跃"
    elif score >= 50:
        return "中性偏暖"
    elif score >= 35:
        return "中性偏冷"
    elif score >= 20:
        return "偏空低迷"
    else:
        return "极度恐慌"


def _profit_effect(breadth):
    """赚钱效应判断"""
    if not breadth:
        return "未知"
    total = breadth.get("total", 1)
    up = breadth.get("up", 0)
    ratio = up / total if total > 0 else 0.5

    if ratio >= 0.7:
        return "好"
    elif ratio >= 0.5:
        return "一般"
    else:
        return "差"


def fetch_sentiment(date_str, shared_breadth=None):
    """主函数：获取情绪温度计数据

    Args:
        date_str: 日期字符串
        shared_breadth: 从 market_overview 传入的共享涨跌家数数据，避免重复拉取
    """
    breadth = shared_breadth
    limit_data = None

    # 如果没有共享数据，自行获取（tushare 优先，akshare 仅当天兜底）
    if breadth is None:
        try:
            import sys, os, time
            from datetime import datetime
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from config import get_tushare_pro
            pro = get_tushare_pro()
            date_fmt = date_str.replace("-", "")
            df = pro.daily(trade_date=date_fmt)
            time.sleep(0.15)
            if df is not None and not df.empty:
                total = len(df)
                up = len(df[df["pct_chg"] > 0])
                down = len(df[df["pct_chg"] < 0])
                limit_up = 0
                limit_down = 0
                for _, row in df.iterrows():
                    code = str(row["ts_code"])
                    pct = row["pct_chg"]
                    if code.startswith(("30", "68")):
                        th = 19.8
                    elif code.startswith(("8", "4")):
                        th = 29.8
                    else:
                        th = 9.8
                    if pct >= th:
                        limit_up += 1
                    elif pct <= -th:
                        limit_down += 1
                breadth = {
                    "total": total, "up": up, "down": down,
                    "limit_up": limit_up, "limit_down": limit_down,
                }
        except Exception as e:
            logging.warning(f"情绪模块 tushare 获取行情失败: {e}")

    # tushare 失败且当天，尝试 akshare 实时
    if breadth is None:
        try:
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")
            if date_str == today:
                df = ak.stock_zh_a_spot_em()
                if df is not None and not df.empty:
                    total = len(df)
                    up = len(df[df["涨跌幅"] > 0])
                    down = len(df[df["涨跌幅"] < 0])
                    limit_up = 0
                    limit_down = 0
                    for _, row in df.iterrows():
                        code = str(row["代码"])
                        pct = row["涨跌幅"]
                        if row["最新价"] == 0:
                            continue
                        if code.startswith(("30", "68")):
                            th = 19.8
                        elif code.startswith(("8", "4")):
                            th = 29.8
                        else:
                            th = 9.8
                        if pct >= th:
                            limit_up += 1
                        elif pct <= -th:
                            limit_down += 1
                    breadth = {
                        "total": total, "up": up, "down": down,
                        "limit_up": limit_up, "limit_down": limit_down,
                    }
        except Exception as e2:
            logging.warning(f"情绪模块 akshare 获取行情也失败: {e2}")

    # 简化连板数据（仅用于评分）
    limit_data = {"max_consecutive": 0}

    # 计算情绪评分
    score = _assess_sentiment(breadth, limit_data)
    label = _sentiment_label(score)
    profit = _profit_effect(breadth)

    return {
        "score": score,
        "label": label,
        "profit_effect": profit,
        "up_ratio": round(breadth["up"] / breadth["total"] * 100, 1) if breadth and breadth.get("total", 0) > 0 else None,
        "limit_up_down_ratio": f'{breadth["limit_up"]}:{breadth["limit_down"]}' if breadth else None,
        "pcr": None,  # 暂缺，后续迭代
        "implied_vol": None,  # 暂缺，后续迭代
    }
