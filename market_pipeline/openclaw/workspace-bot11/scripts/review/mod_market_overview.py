"""
模块：市场全景
- 三大指数 + 沪深300 行情 (Tushare `index_daily` 为主, akshare 为历史 OHLC 备份)
- 全市场成交额
- 涨跌家数 (Tushare `daily` 全市场日线为主, akshare 实时快照仅当天兜底)
- 涨停跌停数 (东财 `stock_zt_pool_em` — Tushare limit_list_d 有 1/天限额弃用)
"""

import logging
import time
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_tushare_pro

# 关注的指数：10 大宽基（res14/scout 大盘技术面都吃这份，缺一只当日就落后一天）。
# ak 为空表示 akshare 无对应编码，只走 tushare 主源，不做备份/交叉验证（见 _fetch_indices_ak）。
INDICES = {
    "上证指数": {"ak": "sh000001", "ts": "000001.SH"},
    "深证成指": {"ak": "sz399001", "ts": "399001.SZ"},
    "深证综指": {"ak": "sz399106", "ts": "399106.SZ"},
    "创业板指": {"ak": "sz399006", "ts": "399006.SZ"},
    "沪深300": {"ak": "sh000300", "ts": "000300.SH"},
    "中证1000": {"ak": "sh000852", "ts": "000852.SH"},
    "科创50":   {"ak": "sh000688", "ts": "000688.SH"},
    "中证500":  {"ak": "sh000905", "ts": "000905.SH"},
    "中证2000": {"ak": None,       "ts": "932000.CSI"},
    "北证50":   {"ak": None,       "ts": "899050.BJ"},
}


def _fetch_indices_ak(date_str):
    """akshare 获取指数行情。ak=None 的指数跳过（akshare 无对应编码，只信 tushare 主源）。"""
    results = {}
    for name, codes in INDICES.items():
        if not codes.get("ak"):
            continue
        try:
            df = ak.stock_zh_index_daily_em(symbol=codes["ak"])
            if df is None or df.empty:
                continue
            # 列名可能是中文或英文，统一处理
            df.columns = [c.strip() for c in df.columns]
            # 按日期筛选
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                row = df[df["date"] == date_str]
            elif "日期" in df.columns:
                df["日期"] = pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d")
                row = df[df["日期"] == date_str]
            else:
                # 尝试用最后一行
                row = df.tail(1)

            if row.empty:
                logging.warning(f"akshare: {name} 无 {date_str} 数据")
                continue

            r = row.iloc[0]
            # 尝试多种列名
            results[name] = {
                "open": float(r.get("open", r.get("开盘", 0))),
                "close": float(r.get("close", r.get("收盘", 0))),
                "high": float(r.get("high", r.get("最高", 0))),
                "low": float(r.get("low", r.get("最低", 0))),
                "volume": float(r.get("volume", r.get("成交量", 0))),
                "amount": float(r.get("amount", r.get("成交额", 0))),
                "pct_chg": 0,  # 需要计算
            }
            # 计算涨跌幅
            close = results[name]["close"]
            open_ = results[name]["open"]
            # 获取前一日收盘价来计算涨跌幅
            idx = row.index[0]
            if idx > 0:
                prev_close = float(df.iloc[df.index.get_loc(idx) - 1].get(
                    "close", df.iloc[df.index.get_loc(idx) - 1].get("收盘", close)
                ))
                if prev_close > 0:
                    results[name]["pct_chg"] = round((close / prev_close - 1) * 100, 2)
        except Exception as e:
            logging.warning(f"akshare 获取 {name} 失败: {e}")
    return results


def _fetch_indices_ts(date_str):
    """tushare 获取指数行情 (主数据源)。

    返回 dict 格式和 akshare 路径兼容, 含 OHLC + volume + amount + pct_chg。
    Tushare amount 单位为千元, 转换为元以保持和 akshare 一致。
    """
    results = {}
    try:
        pro = get_tushare_pro()
        date_fmt = date_str.replace("-", "")
        for name, codes in INDICES.items():
            try:
                df = pro.index_daily(
                    ts_code=codes["ts"],
                    start_date=date_fmt,
                    end_date=date_fmt,
                )
                if df is not None and len(df) > 0:
                    r = df.iloc[0]
                    results[name] = {
                        "open": float(r["open"]),
                        "close": float(r["close"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "volume": float(r.get("vol", 0)),
                        # tushare amount 单位千元 → 元 (对齐 akshare 口径)
                        "amount": float(r.get("amount", 0)) * 1000,
                        "pct_chg": float(r["pct_chg"]),
                    }
                time.sleep(0.15)
            except Exception as e:
                logging.warning(f"tushare 获取 {name} 失败: {e}")
    except Exception as e:
        logging.warning(f"tushare 初始化失败: {e}")
    return results


def _fetch_limit_counts_zt_pool(date_str):
    """从东方财富涨停/跌停板池拉真实涨停数 / 跌停数。

    2026-04-13 加入, 替换原来的 "pct_chg >= 9.8 粗筛" 逻辑。原逻辑问题:
    - ST 股 5% 涨停被漏 (硬编码 9.8 阈值)
    - 股价收于 9.85% 但未真正封板的个股被误算
    - 跟 mod_limit_up_tracking 的数据源不一致, 导致复盘 MD 跨章节矛盾

    东财涨停板池是真实封板判定, 无限额, 只保留近 2-3 周数据。失败时返回
    (None, None) 由上游保留 pct_chg 粗筛结果作 fallback。
    """
    try:
        date_fmt = date_str.replace("-", "")
        zt = ak.stock_zt_pool_em(date=date_fmt)
        dt = ak.stock_zt_pool_dtgc_em(date=date_fmt)
        limit_up = len(zt) if zt is not None and not zt.empty else 0
        limit_down = len(dt) if dt is not None and not dt.empty else 0
        return limit_up, limit_down
    except Exception as e:
        logging.warning(f"东财涨跌停板池拉取失败 ({date_str}): {e}")
        return None, None


def _fetch_breadth_ak(date_str=None):
    """akshare 获取涨跌家数（使用实时行情计算）。

    total/up/down/flat/total_amount 用 stock_zh_a_spot_em 的全量快照;
    limit_up/limit_down 优先用东财涨停板池 (需要 date_str), 失败时 fallback
    到 pct_chg 粗筛。
    """
    try:
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None

        total = len(df)
        up = len(df[df["涨跌幅"] > 0])
        down = len(df[df["涨跌幅"] < 0])
        flat = total - up - down

        # 优先从东财涨停池拿精确的 limit_up / limit_down
        limit_up = None
        limit_down = None
        if date_str:
            limit_up, limit_down = _fetch_limit_counts_zt_pool(date_str)

        # Fallback: pct_chg 粗筛
        if limit_up is None or limit_down is None:
            logging.info("breadth: 东财板池不可用, fallback 到 pct_chg 粗筛")
            limit_up = 0
            limit_down = 0
            for _, row in df.iterrows():
                code = str(row["代码"])
                pct = row["涨跌幅"]
                price = row["最新价"]
                if price == 0:
                    continue
                if code.startswith(("30", "68")):
                    threshold = 19.8
                elif code.startswith(("8", "4")):
                    threshold = 29.8
                else:
                    threshold = 9.8
                if pct >= threshold:
                    limit_up += 1
                elif pct <= -threshold:
                    limit_down += 1

        # 总成交额（亿元）
        total_amount = df["成交额"].sum() / 1e8

        return {
            "total": total,
            "up": up,
            "down": down,
            "flat": flat,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "total_amount": round(total_amount, 0),
        }
    except Exception as e:
        logging.warning(f"akshare 获取涨跌家数失败: {e}")
        return None


def _cross_validate(ak_data, ts_data):
    """交叉验证指数数据"""
    validation = {}
    for name in ak_data:
        if name not in ts_data:
            continue
        ak_close = ak_data[name].get("close", 0)
        ts_close = ts_data[name].get("close", 0)
        if ak_close > 0 and ts_close > 0:
            diff_pct = abs(ak_close - ts_close) / ts_close * 100
            validation[name] = {
                "akshare": ak_close,
                "tushare": ts_close,
                "diff_pct": round(diff_pct, 4),
                "ok": diff_pct < 0.5,
            }
    return validation


def _fetch_breadth_ts(date_str):
    """tushare 获取涨跌家数（历史日期用, 通过全市场日线数据计算）。

    total/up/down/flat/total_amount 来自 pro.daily 的全量行情;
    limit_up/limit_down 优先从东财涨停板池拿, 失败 fallback 到 pct_chg 粗筛。
    """
    try:
        pro = get_tushare_pro()
        date_fmt = date_str.replace("-", "")
        df = pro.daily(trade_date=date_fmt)
        time.sleep(0.15)

        if df is None or df.empty:
            return None

        total = len(df)
        up = len(df[df["pct_chg"] > 0])
        down = len(df[df["pct_chg"] < 0])
        flat = total - up - down

        # 优先从东财涨停池拿精确值
        limit_up, limit_down = _fetch_limit_counts_zt_pool(date_str)

        # Fallback: tushare pct_chg 粗筛
        if limit_up is None or limit_down is None:
            logging.info("breadth (ts): 东财板池不可用, fallback 到 tushare pct_chg 粗筛")
            limit_up = 0
            limit_down = 0
            for _, row in df.iterrows():
                code = str(row["ts_code"])
                pct = row["pct_chg"]
                if code.startswith(("30", "68")):
                    threshold = 19.8
                elif code.startswith(("8", "4")):
                    threshold = 29.8
                else:
                    threshold = 9.8
                if pct >= threshold:
                    limit_up += 1
                elif pct <= -threshold:
                    limit_down += 1

        # 总成交额: tushare amount 单位为千元
        total_amount = df["amount"].sum() / 1e5  # 千元 → 亿元

        return {
            "total": total,
            "up": up,
            "down": down,
            "flat": flat,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "total_amount": round(total_amount, 0),
        }
    except Exception as e:
        logging.warning(f"tushare 获取涨跌家数失败: {e}")
        return None


def fetch_market_overview(date_str):
    """主函数：获取市场全景数据。

    2026-04-14 翻转优先级: Tushare `index_daily` 为主, akshare 仅做历史验证/备份。
    Tushare 稳定性优于 akshare 东财接口, 且覆盖 OHLC + volume + amount 完整字段。
    """
    # 1. 指数行情 (tushare 主)
    indices_ts = _fetch_indices_ts(date_str)

    # 2. akshare 指数行情 (备份 / 交叉验证)
    indices_ak = _fetch_indices_ak(date_str)

    # 以 tushare 为基准, akshare 补缺 (某些历史日期 tushare 偶尔缺数据)
    indices = dict(indices_ts)
    for name, data in indices_ak.items():
        if name not in indices or indices[name].get("close", 0) == 0:
            indices[name] = data

    # 3. 涨跌家数
    # akshare stock_zh_a_spot_em 是实时快照，仅当天有效；历史日期必须用 tushare
    # limit_up/limit_down 子字段会被两个路径优先从东财涨停板池拿, date_str 是为此需要
    today = datetime.now().strftime("%Y-%m-%d")
    if date_str == today:
        # 当天优先 akshare 实时快照 (tushare daily 日终才更新)
        breadth = _fetch_breadth_ak(date_str)
        if breadth is None:
            breadth = _fetch_breadth_ts(date_str)
    else:
        breadth = _fetch_breadth_ts(date_str)
        if breadth is None:
            breadth = _fetch_breadth_ak(date_str)  # 最后兜底

    # 4. 交叉验证 (akshare vs tushare close 差异)
    validation = _cross_validate(indices_ak, indices_ts)

    return {
        "indices": indices,
        "breadth": breadth,
        "_validation": validation,
    }
