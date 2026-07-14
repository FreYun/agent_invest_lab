"""
模块：日内行情画像
- 使用 5 分钟 K 线刻画日内走势
- 开盘跳空、上午/下午涨跌、尾盘走向、量能分布
- 自动生成日内叙述文字
"""

import logging
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

# 指数代码映射 (akshare index_zh_a_hist_min_em 使用纯数字代码)
INDICES = {
    "上证指数": "000001",
    "深证成指": "399001",
    "创业板指": "399006",
    "沪深300": "000300",
}

# 时间分界
MORNING_START = "09:30"
MORNING_END = "11:30"
AFTERNOON_START = "13:00"
AFTERNOON_END = "15:00"
TAIL_START = "14:30"


def _get_prev_close(symbol, date_str):
    """获取前一交易日收盘价"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        # 往前取 10 天的日线以确保包含前一交易日
        start = (dt - timedelta(days=10)).strftime("%Y-%m-%d")
        end = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        df = ak.index_zh_a_hist_min_em(
            symbol=symbol, period="5",
            start_date=f"{start} 09:30:00",
            end_date=f"{end} 15:00:00",
        )
        if df is not None and not df.empty:
            return float(df.iloc[-1]["收盘"])
    except Exception as e:
        logging.warning(f"获取 {symbol} 前收失败: {e}")
    return None


def _fetch_intraday_bars(symbol, date_str):
    """获取指定日期的 5 分钟 K 线"""
    try:
        df = ak.index_zh_a_hist_min_em(
            symbol=symbol, period="5",
            start_date=f"{date_str} 09:30:00",
            end_date=f"{date_str} 15:00:00",
        )
        if df is None or df.empty:
            return None

        # 统一列名
        df.columns = [c.strip() for c in df.columns]
        df["时间"] = pd.to_datetime(df["时间"])
        df["time_str"] = df["时间"].dt.strftime("%H:%M")
        return df
    except Exception as e:
        logging.warning(f"获取 {symbol} 分钟线失败: {e}")
        return None


def _analyze_index(name, symbol, date_str):
    """分析单个指数的日内画像"""
    df = _fetch_intraday_bars(symbol, date_str)
    if df is None or len(df) < 5:
        return None

    prev_close = _get_prev_close(symbol, date_str)
    first_bar = df.iloc[0]
    last_bar = df.iloc[-1]

    open_price = float(first_bar["开盘"])
    close_price = float(last_bar["收盘"])
    day_high = float(df["最高"].max())
    day_low = float(df["最低"].min())

    # 日内高/低点时间
    high_idx = df["最高"].idxmax()
    low_idx = df["最低"].idxmin()
    high_time = df.loc[high_idx, "time_str"]
    low_time = df.loc[low_idx, "time_str"]

    # 开盘跳空
    open_gap_pct = None
    if prev_close and prev_close > 0:
        open_gap_pct = round((open_price / prev_close - 1) * 100, 2)

    # 振幅
    base = prev_close if prev_close and prev_close > 0 else open_price
    amplitude = round((day_high - day_low) / base * 100, 2) if base > 0 else 0

    # 上午/下午/尾盘分段
    morning = df[df["time_str"] <= MORNING_END]
    afternoon = df[df["time_str"] >= AFTERNOON_START]
    tail = df[df["time_str"] >= TAIL_START]

    morning_open = float(morning.iloc[0]["开盘"]) if len(morning) > 0 else open_price
    morning_close = float(morning.iloc[-1]["收盘"]) if len(morning) > 0 else open_price
    morning_pct = round((morning_close / morning_open - 1) * 100, 2) if morning_open > 0 else 0

    afternoon_open = float(afternoon.iloc[0]["开盘"]) if len(afternoon) > 0 else morning_close
    afternoon_close = float(afternoon.iloc[-1]["收盘"]) if len(afternoon) > 0 else morning_close
    afternoon_pct = round((afternoon_close / afternoon_open - 1) * 100, 2) if afternoon_open > 0 else 0

    tail_open = float(tail.iloc[0]["开盘"]) if len(tail) > 0 else afternoon_close
    tail_close = float(tail.iloc[-1]["收盘"]) if len(tail) > 0 else afternoon_close
    last30_pct = round((tail_close / tail_open - 1) * 100, 2) if tail_open > 0 else 0

    # 量能分布
    morning_vol = float(morning["成交量"].sum()) if len(morning) > 0 else 0
    afternoon_vol = float(afternoon["成交量"].sum()) if len(afternoon) > 0 else 0
    total_vol = morning_vol + afternoon_vol
    morning_vol_pct = round(morning_vol / total_vol * 100, 1) if total_vol > 0 else 50.0

    # 上午振幅（用于判断冲高回落等形态）
    morning_high = float(morning["最高"].max()) if len(morning) > 0 else open_price
    morning_low = float(morning["最低"].min()) if len(morning) > 0 else open_price

    return {
        "open_gap_pct": open_gap_pct,
        "morning_pct": morning_pct,
        "afternoon_pct": afternoon_pct,
        "last30_pct": last30_pct,
        "high_time": high_time,
        "low_time": low_time,
        "amplitude": amplitude,
        "morning_vol_pct": morning_vol_pct,
        "morning_high": morning_high,
        "morning_low": morning_low,
        "morning_open": morning_open,
    }


def _describe_open(gap):
    """描述开盘情况"""
    if gap is None:
        return "平开"
    if gap >= 0.3:
        return f"高开 {gap:+.2f}%"
    elif gap <= -0.3:
        return f"低开 {gap:+.2f}%"
    else:
        return "平开"


def _describe_morning(data):
    """描述上午走势"""
    pct = data["morning_pct"]
    morning_high = data["morning_high"]
    morning_low = data["morning_low"]
    morning_open = data["morning_open"]

    # 判断是否冲高回落：先涨后跌
    if morning_open > 0:
        high_pct = (morning_high / morning_open - 1) * 100
        low_pct = (morning_low / morning_open - 1) * 100
    else:
        high_pct = 0
        low_pct = 0

    if high_pct > 0.3 and pct < -0.1:
        return "冲高回落"
    elif low_pct < -0.3 and pct > 0.1:
        return "探底回升"
    elif pct > 0.3:
        return "震荡走强"
    elif pct < -0.3:
        return "震荡走弱"
    else:
        return "窄幅震荡"


def _describe_afternoon(data):
    """描述下午走势"""
    pct = data["afternoon_pct"]
    morning_pct = data["morning_pct"]

    if pct > 0.3:
        return "午后拉升"
    elif pct < -0.3:
        return "午后走低"
    elif abs(pct) <= 0.15:
        return "横盘整理"
    elif (pct > 0 and morning_pct > 0) or (pct < 0 and morning_pct < 0):
        return "延续上午趋势"
    else:
        return "午后分化"


def _describe_tail(pct):
    """描述尾盘"""
    if pct > 0.15:
        return "尾盘翘尾"
    elif pct < -0.15:
        return "尾盘跳水"
    else:
        return "尾盘平稳"


def _generate_narrative(sh_data):
    """基于上证指数生成日内叙述"""
    if sh_data is None:
        return ""

    parts = []

    # 开盘
    open_desc = _describe_open(sh_data["open_gap_pct"])

    # 总体走势关键词
    gap = sh_data.get("open_gap_pct") or 0
    morning_pct = sh_data["morning_pct"]
    afternoon_pct = sh_data["afternoon_pct"]
    total_trend = morning_pct + afternoon_pct

    if gap >= 0.3 and total_trend < -0.2:
        trend_word = "高开低走"
    elif gap <= -0.3 and total_trend > 0.2:
        trend_word = "低开高走"
    elif total_trend > 0.3:
        trend_word = "震荡上行"
    elif total_trend < -0.3:
        trend_word = "震荡下行"
    else:
        trend_word = "窄幅震荡"

    parts.append(f"今日{trend_word}。")

    # 上午
    morning_desc = _describe_morning(sh_data)
    parts.append(f"早盘{open_desc}，上午{morning_desc}（{sh_data['morning_pct']:+.2f}%）。")

    # 下午
    afternoon_desc = _describe_afternoon(sh_data)
    parts.append(f"{afternoon_desc}（{sh_data['afternoon_pct']:+.2f}%），")

    # 尾盘
    tail_desc = _describe_tail(sh_data["last30_pct"])
    parts.append(f"{tail_desc}（{sh_data['last30_pct']:+.2f}%）。")

    # 量能
    vol_pct = sh_data["morning_vol_pct"]
    if vol_pct >= 55:
        vol_desc = "量能前高后低"
    elif vol_pct <= 45:
        vol_desc = "量能后半场放大"
    else:
        vol_desc = "量能分布均匀"
    parts.append(f"上午成交占比 {vol_pct:.0f}%，{vol_desc}。")

    return "".join(parts)


def fetch_intraday_profile(date_str):
    """主函数：获取日内行情画像"""
    indices_data = {}

    for name, symbol in INDICES.items():
        logging.info(f"获取 {name} 日内分钟线...")
        data = _analyze_index(name, symbol, date_str)
        if data:
            indices_data[name] = data

    if not indices_data:
        return {"error": "无法获取任何指数的分钟线数据"}

    # 基于上证指数生成叙述
    sh_data = indices_data.get("上证指数")
    narrative = _generate_narrative(sh_data)

    return {
        "indices": indices_data,
        "narrative": narrative,
    }
