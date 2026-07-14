"""数据解析层 — 从 DB / Tushare 抽取六维原始数据。

设计原则:
- DB 优先: 先查 market.db (index_daily / regime_raw_daily / market_amount_daily),
  有数据直接返回, 避免网络调用。
- Tushare 兜底: DB 无数据时走 Tushare API, 拿到后写入 DB, 下次命中缓存。
- MD 解析保留为最终 fallback (dims 2-5), 但正常流程不再依赖。
- 纯计算函数 (compute_index_ma / compute_volume_ratio) 接收 DataFrame
  作为输入, 便于单元测试不依赖网络

数据字段来源参见 spec §4 / handoff §2.2。

2026-04-16 改造: 数据源优先级从 CSV 缓存 / 复盘 MD 改为 market.db。
CSV 缓存和 MD 解析保留为 fallback, 不主动写 CSV。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

import tushare_client

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# DB 连接 (market.db)
# --------------------------------------------------------------------------- #

MARKET_DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))


def _get_market_db() -> sqlite3.Connection:
    conn = sqlite3.connect(MARKET_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# --------------------------------------------------------------------------- #
# 结果容器
# --------------------------------------------------------------------------- #


@dataclass
class RawMarketData:
    """六维原始数据 (解析后, 打分前)。

    任何字段都可能为 None, 表示该维度数据缺失。
    """

    date: str

    # MD 层字段
    advance_decline_ratio: Optional[float] = None
    sentiment_delta: Optional[int] = None  # 涨停数 - 跌停数
    sentiment_index: Optional[float] = None  # 情绪评分 0-100
    max_streak: Optional[int] = None  # 最高连板

    # Tushare 指数层
    hs300: Optional[dict] = None  # IndexMA: {close, ma5, ma20, ma60, ma250}
    csi1000: Optional[dict] = None
    volume_ratio_5_20: Optional[float] = None

    # 当日涨跌幅 (逃生门判断需要), 单位: 百分比, 如 -3.15 表示 -3.15%
    hs300_pct_change: Optional[float] = None
    csi1000_pct_change: Optional[float] = None

    # 诊断信息
    missing_dims: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 复盘 MD 解析
# --------------------------------------------------------------------------- #


# regex 都用宽松匹配, 允许 ** 加粗与否, 允许中英文冒号混用
# 新格式 (daily_review.py 自动产出, 单行): "上涨：**662** 家 / 下跌：**4786** 家 / 平盘：377 家"
_RE_BREADTH = re.compile(
    r"上涨[:：]\s*\*{0,2}(\d+)\*{0,2}\s*家\s*/\s*下跌[:：]\s*\*{0,2}(\d+)\*{0,2}\s*家"
    r"(?:\s*/\s*平盘[:：]\s*\*{0,2}(\d+)\*{0,2})?"
)
_RE_LU_LD = re.compile(r"涨停[:：]跌停\s*\|?\s*\*{0,2}(\d+)\*{0,2}\s*[:：]\s*\*{0,2}(\d+)\*{0,2}")
_RE_SENTIMENT_SCORE = re.compile(r"情绪评分\s*\|?\s*\*{0,2}(\d+(?:\.\d+)?)\*{0,2}\s*/\s*100")
_RE_MAX_STREAK = re.compile(r"最高连板[:：]\s*\*{0,2}(-?\d+)\*{0,2}\s*板")

# 老格式 (手工/遗留复盘 MD, 分市场多行). 例:
#   - 上涨：257 家（上证）/ 241 家（深证）
#   - 平盘：11 家（上证）/ 13 家（深证）
#   - 下跌：2075 家（上证）/ 2661 家（深证）
# 每行可能含任意市场数, 全部累加。
_RE_BREADTH_ROW = re.compile(r"^\s*[-\*]?\s*(上涨|下跌|平盘)[:：](.+?)$", re.MULTILINE)
_RE_JIA_NUM = re.compile(r"(\d+)\s*家")


def _extract_multi_market_breadth(text: str):
    """老格式 fallback: 分市场多行的涨跌家数。

    例如:
        - 上涨：257 家（上证）/ 241 家（深证）
        - 下跌：2075 家（上证）/ 2661 家（深证）
        - 平盘：11 家（上证）/ 13 家（深证）

    每一行里可能含多个 "N 家" (上证 + 深证 + 北交所 …), 全部加总。
    返回 (up, down, flat) 元组, 上涨/下跌任一为 None 则返回 None。
    """
    sums = {"上涨": None, "下跌": None, "平盘": None}
    for category, content in _RE_BREADTH_ROW.findall(text):
        if sums.get(category) is not None:
            continue  # 已经记录过, 不覆盖 (防止误匹配到表格里同名的行)
        nums = [int(n) for n in _RE_JIA_NUM.findall(content)]
        if nums:
            sums[category] = sum(nums)
    if sums["上涨"] is None or sums["下跌"] is None:
        return None
    return sums["上涨"], sums["下跌"], sums["平盘"] or 0


def parse_daily_review(md_path: str) -> dict:
    """从复盘 MD 宽松提取 4 个 MD 侧字段。

    返回 dict, 可能包含以下键 (缺失字段不出现):
        advance_count: int
        decline_count: int
        flat_count: int
        advance_decline_ratio: float    — up / (up + down + flat)
        sentiment_delta: int            — 涨停 - 跌停
        sentiment_index: float          — 情绪评分 0-100
        max_streak: int                 — 最高连板

    不抛异常 (除非文件不存在)。解析失败的字段由上游按降级处理。

    兼容两种 MD 格式:
    - 新格式 (daily_review.py 自动产出, 单行汇总)
    - 老格式 (遗留手工 MD, 分市场多行). 老格式里通常只能恢复涨跌家数,
      情绪/连板字段本身就不存在, 会进入 missing_dims。
    """
    with open(md_path, encoding="utf-8") as f:
        text = f.read()

    result: dict = {}

    # 涨跌家数 (primary 新格式 → fallback 老格式)
    up = down = flat = None
    m = _RE_BREADTH.search(text)
    if m:
        up = int(m.group(1))
        down = int(m.group(2))
        flat = int(m.group(3)) if m.group(3) else 0
    else:
        fb = _extract_multi_market_breadth(text)
        if fb is not None:
            up, down, flat = fb
            logger.debug("parse_daily_review: using multi-market fallback breadth")

    if up is not None and down is not None:
        result["advance_count"] = up
        result["decline_count"] = down
        result["flat_count"] = flat
        total = up + down + flat
        if total > 0:
            result["advance_decline_ratio"] = up / total
    else:
        logger.debug("parse_daily_review: breadth not found in %s", md_path)

    # 涨停:跌停
    limit_up_count = None  # 记下来给一致性检测用
    m = _RE_LU_LD.search(text)
    if m:
        lu = int(m.group(1))
        ld = int(m.group(2))
        limit_up_count = lu
        result["sentiment_delta"] = lu - ld
    else:
        logger.debug("parse_daily_review: 涨停:跌停 not found")

    # 情绪评分
    m = _RE_SENTIMENT_SCORE.search(text)
    if m:
        result["sentiment_index"] = float(m.group(1))
    else:
        logger.debug("parse_daily_review: 情绪评分 not found")

    # 最高连板
    m = _RE_MAX_STREAK.search(text)
    if m:
        max_streak = int(m.group(1))
        # 一致性检测: 如果有涨停但最高连板=0, 数学上不可能
        # (任何涨停股至少是 1 板 → max_streak >= 1), 说明上游数据源静默失败。
        # 典型场景: daily_review.py 的 mod_limit_up_tracking 原版用 tushare
        # limit_list_d (每小时 1 次限额), 被 rate-limit 时静默返回 [] → 0 板。
        # 这种"假零"会让 classifier 恒打 -2, 应标记为 missing 不参与打分。
        if max_streak == 0 and limit_up_count is not None and limit_up_count > 0:
            logger.warning(
                "parse_daily_review: 数据矛盾 (%s 涨停>0 但最高连板=0), "
                "疑似上游数据源失败, 将 max_streak 标为 missing",
                os.path.basename(md_path),
            )
        else:
            result["max_streak"] = max_streak
    else:
        logger.debug("parse_daily_review: 最高连板 not found")

    return result


# --------------------------------------------------------------------------- #
# Tushare 指数数据 + 缓存
# --------------------------------------------------------------------------- #


# 内部 key → Tushare ts_code
INDEX_SYMBOLS = {
    "hs300":   "000300.SH",
    "csi1000": "000852.SH",
}

# 全市场成交额通过 daily_info 接口的 ts_code 聚合:
# SH_MARKET = 沪市全部 (SH_A + SH_STAR 科创板 + 少量 B股/回购)
# SZ_MARKET = 深市全部 (SZ_MAIN 主板 + SZ_GEM 创业板)
# 合计口径 = 全市场 A 股成交额, 比老代理 (上证综指+深证成指) 完整 ~6%
MARKET_CODES = ("SH_MARKET", "SZ_MARKET")

# 拉取窗口: 覆盖 MA250 ≈ 250 交易日, 按自然日 * 1.5 = 500 天足够
_INDEX_FETCH_DAYS = 500
_MARKET_FETCH_DAYS = 500


def _today_yyyymmdd() -> str:
    from datetime import date as _date
    return _date.today().strftime("%Y%m%d")


def _days_ago_yyyymmdd(n: int) -> str:
    from datetime import date as _date, timedelta as _td
    return (_date.today() - _td(days=n)).strftime("%Y%m%d")


def _normalize_trade_date(series: pd.Series) -> pd.Series:
    """Tushare 的 trade_date 是 'YYYYMMDD', 统一到 'YYYY-MM-DD'."""
    return pd.to_datetime(series.astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")


def _read_cache(cache_path: str) -> Optional[pd.DataFrame]:
    if not os.path.exists(cache_path):
        return None
    try:
        df = pd.read_csv(cache_path)
        df["date"] = df["date"].astype(str)
        return df.sort_values("date").reset_index(drop=True)
    except Exception as e:
        logger.warning("cache read failed (%s), will refetch: %s", cache_path, e)
        return None


# --------------------------------------------------------------------------- #
# DB ↔ DataFrame 辅助
# --------------------------------------------------------------------------- #


def _load_index_from_db(
    conn: sqlite3.Connection, ts_code: str, target_date: Optional[str],
) -> Optional[pd.DataFrame]:
    """从 index_daily 取 target_date 及之前 500 行, 返回 DataFrame[date, open, high, low, close, volume].

    target_date 格式 'YYYY-MM-DD'; 如果传入且 DB 不含该日, 返回 None。
    """
    target_key = target_date.replace("-", "") if target_date else None
    if target_key:
        rows = conn.execute(
            "SELECT trade_date, open, high, low, close, vol "
            "FROM index_daily WHERE ts_code = ? AND trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT 500",
            (ts_code, target_key),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT trade_date, open, high, low, close, vol "
            "FROM index_daily WHERE ts_code = ? "
            "ORDER BY trade_date DESC LIMIT 500",
            (ts_code,),
        ).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close", "volume"])
    # YYYYMMDD → YYYY-MM-DD
    df["date"] = df["trade_date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}")
    df = df[["date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True)
    if target_date and target_date not in set(df["date"].values):
        return None
    return df


def _upsert_index_daily(ts_code: str, df: pd.DataFrame):
    """把 Tushare 返回的 index_daily DataFrame 写入 DB (INSERT OR REPLACE)。

    df 需要有 trade_date(YYYYMMDD), open, high, low, close, vol 列。
    """
    conn = _get_market_db()
    try:
        rows = [
            (r["trade_date"], ts_code, r["open"], r["high"], r["low"],
             r["close"], None, None, r["vol"], None)
            for _, r in df.iterrows()
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO index_daily "
            "(trade_date, ts_code, open, high, low, close, pre_close, pct_chg, vol, amount) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        logger.info("index_daily upserted %d rows for %s", len(rows), ts_code)
    finally:
        conn.close()


def _load_market_amount_from_db(
    conn: sqlite3.Connection, target_date: Optional[str],
) -> Optional[pd.DataFrame]:
    """从 market_amount_daily 取 target_date 及之前 500 行, 返回 DataFrame[date, volume]."""
    target_key = target_date.replace("-", "") if target_date else None
    if target_key:
        rows = conn.execute(
            "SELECT trade_date, amount_yi FROM market_amount_daily "
            "WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 500",
            (target_key,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT trade_date, amount_yi FROM market_amount_daily "
            "ORDER BY trade_date DESC LIMIT 500",
        ).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["trade_date", "volume"])
    df["date"] = df["trade_date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}")
    df = df[["date", "volume"]].sort_values("date").reset_index(drop=True)
    if target_date and target_date not in set(df["date"].values):
        return None
    return df


def _upsert_market_amount(merged: pd.DataFrame):
    """把 Tushare daily_info 合成结果写入 market_amount_daily。

    merged 需要有 trade_date(YYYYMMDD) 和 volume(亿元) 列。
    """
    conn = _get_market_db()
    try:
        rows = [(r["trade_date"], float(r["volume"])) for _, r in merged.iterrows()]
        conn.executemany(
            "INSERT OR REPLACE INTO market_amount_daily (trade_date, amount_yi) "
            "VALUES (?,?)",
            rows,
        )
        conn.commit()
        logger.info("market_amount_daily upserted %d rows", len(rows))
    finally:
        conn.close()


def load_raw_dims_from_db(date: str) -> Optional[dict]:
    """从 regime_raw_daily 读取 dims 2-5, 返回 dict 或 None。

    date 格式: 'YYYY-MM-DD' (内部转 YYYYMMDD 查表)。
    """
    date_key = date.replace("-", "")
    conn = _get_market_db()
    try:
        row = conn.execute(
            "SELECT advance_decline_ratio, sentiment_delta, sentiment_index, max_streak "
            "FROM regime_raw_daily WHERE trade_date = ?",
            (date_key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "advance_decline_ratio": row["advance_decline_ratio"],
        "sentiment_delta": row["sentiment_delta"],
        "sentiment_index": row["sentiment_index"],
        "max_streak": row["max_streak"],
    }


def fetch_index_daily(
    name: str,
    cache_dir: Optional[str] = None,
    target_date: Optional[str] = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """拉取指数日线数据, DB 优先, Tushare 兜底。

    Args:
        name:        内部指数 key ("hs300" / "csi1000"), 映射到 Tushare ts_code
        cache_dir:   已弃用, 仅保留签名兼容 (不再写 CSV)
        target_date: 目标日 'YYYY-MM-DD'; 若 DB 不含此日期则自动 Tushare 补数据
        refresh:     True 时跳过 DB 强制从 Tushare 拉取

    返回 DataFrame, 列: [date, open, high, low, close, volume] (兼容旧接口)。
    按 date 升序排列。
    """
    if name not in INDEX_SYMBOLS:
        raise KeyError(f"unknown index name {name!r}, expected one of {list(INDEX_SYMBOLS)}")
    ts_code = INDEX_SYMBOLS[name]

    # 1. DB 优先
    if not refresh:
        try:
            conn = _get_market_db()
            df = _load_index_from_db(conn, ts_code, target_date)
            conn.close()
            if df is not None:
                return df
            logger.info(
                "index_daily DB missing target_date %s for %s, fetching from Tushare",
                target_date, ts_code,
            )
        except Exception as e:
            logger.warning("DB read failed for index_daily: %s", e)

    # 2. Tushare 拉取 → 写入 DB
    raw = tushare_client.call(
        "index_daily",
        {
            "ts_code": ts_code,
            "start_date": _days_ago_yyyymmdd(_INDEX_FETCH_DAYS),
            "end_date": _today_yyyymmdd(),
        },
        fields="ts_code,trade_date,open,high,low,close,vol",
    )
    if raw.empty:
        raise ValueError(f"tushare index_daily returned 0 rows for {ts_code}")

    # 写入 DB
    try:
        _upsert_index_daily(ts_code, raw)
    except Exception as e:
        logger.warning("index_daily DB upsert failed: %s", e)

    out = pd.DataFrame({
        "date": _normalize_trade_date(raw["trade_date"]),
        "open": raw["open"].astype(float),
        "high": raw["high"].astype(float),
        "low": raw["low"].astype(float),
        "close": raw["close"].astype(float),
        "volume": raw["vol"].astype(float),
    })
    out = out.sort_values("date").reset_index(drop=True)
    return out


def fetch_full_market_volume(
    cache_dir: Optional[str] = None,
    target_date: Optional[str] = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """合成全市场成交额历史, DB 优先, Tushare 兜底。

    口径: Tushare daily_info SH_MARKET + SZ_MARKET, 沪深全 A 成交额
    (含主板+创业板+科创板)。

    Args:
        cache_dir:   已弃用, 仅保留签名兼容
        target_date: 目标日 'YYYY-MM-DD'; 若 DB 不含此日期则自动 Tushare 补
        refresh:     True 时跳过 DB 强制从 Tushare 拉取

    返回 DataFrame 列: [date, volume]
        volume 单位: 亿元。
    """
    # 1. DB 优先
    if not refresh:
        try:
            conn = _get_market_db()
            df = _load_market_amount_from_db(conn, target_date)
            conn.close()
            if df is not None:
                return df
            logger.info(
                "market_amount_daily DB missing target_date %s, fetching from Tushare",
                target_date,
            )
        except Exception as e:
            logger.warning("DB read failed for market_amount_daily: %s", e)

    # 2. Tushare 拉取 → 写入 DB
    start = _days_ago_yyyymmdd(_MARKET_FETCH_DAYS)
    end = _today_yyyymmdd()

    frames = {}
    for code in MARKET_CODES:
        df = tushare_client.call(
            "daily_info",
            {"ts_code": code, "start_date": start, "end_date": end},
            fields="trade_date,ts_code,amount",
        )
        if df.empty:
            raise ValueError(f"tushare daily_info returned 0 rows for {code}")
        frames[code] = df[["trade_date", "amount"]].rename(
            columns={"amount": f"amount_{code}"}
        )

    merged = frames[MARKET_CODES[0]]
    for code in MARKET_CODES[1:]:
        merged = pd.merge(merged, frames[code], on="trade_date", how="inner")

    amount_cols = [f"amount_{c}" for c in MARKET_CODES]
    merged["volume"] = merged[amount_cols].astype(float).sum(axis=1)

    # 写入 DB
    try:
        db_rows = merged[["trade_date", "volume"]].copy()
        _upsert_market_amount(db_rows)
    except Exception as e:
        logger.warning("market_amount_daily DB upsert failed: %s", e)

    out = pd.DataFrame({
        "date": _normalize_trade_date(merged["trade_date"]),
        "volume": merged["volume"].astype(float),
    })
    out = out.sort_values("date").reset_index(drop=True)
    return out


# --------------------------------------------------------------------------- #
# 纯计算: MA / 成交量
# --------------------------------------------------------------------------- #


def compute_index_ma(df: pd.DataFrame, target_date: str) -> dict:
    """给定指数日线 DF, 计算 target_date 当日的收盘价与 MA5/20/60/250。

    返回 IndexMA shape: {close, ma5, ma20, ma60, ma250}

    异常:
        KeyError   — target_date 不在 df 中 (非交易日 / 数据未更新)
        ValueError — 数据不足 250 个交易日, 无法算 MA250
    """
    df = df.sort_values("date").reset_index(drop=True)
    mask = df["date"] == target_date
    if not mask.any():
        raise KeyError(f"target_date {target_date!r} not in index data")
    idx = int(df.index[mask][0])
    if idx < 249:
        raise ValueError(
            f"not enough history for MA250 at {target_date}: "
            f"only {idx + 1} trading days available"
        )
    window = df.iloc[: idx + 1]
    return {
        "close": float(window.iloc[-1]["close"]),
        "ma5": float(window["close"].tail(5).mean()),
        "ma20": float(window["close"].tail(20).mean()),
        "ma60": float(window["close"].tail(60).mean()),
        "ma250": float(window["close"].tail(250).mean()),
    }


def compute_pct_change(df: pd.DataFrame, target_date: str) -> float:
    """target_date 当日相对上一交易日的收盘价涨跌幅 (%, 如 -3.15 表 -3.15%)。

    异常:
        KeyError   — target_date 不在 df 中
        ValueError — target_date 是 df 的第一条, 无前一日可比
    """
    df = df.sort_values("date").reset_index(drop=True)
    mask = df["date"] == target_date
    if not mask.any():
        raise KeyError(f"target_date {target_date!r} not in index data")
    idx = int(df.index[mask][0])
    if idx < 1:
        raise ValueError(f"no previous trading day before {target_date}")
    prev_close = float(df.iloc[idx - 1]["close"])
    today_close = float(df.iloc[idx]["close"])
    if prev_close == 0:
        raise ValueError(f"previous close is zero at {target_date}")
    return (today_close - prev_close) / prev_close * 100.0


def compute_volume_ratio(df: pd.DataFrame, target_date: str) -> float:
    """5 日均量 / 20 日均量 (含 target_date 当日)。

    DataFrame 需要 date 列 + volume 列。volume 语义不限定: 可以是指数
    成交量 (股数), 也可以是指数/全市场成交额 (元), 只要口径一致。
    2026-04-13 起编排器传入的是 `fetch_full_market_volume` 的全市场
    成交额 (上证 + 深证 amount 合计), 比原来的 HS300 volume 代理更准。

    异常:
        KeyError   — target_date 不在 df 中
        ValueError — 数据不足 20 天, 或 20 日均量为 0
    """
    df = df.sort_values("date").reset_index(drop=True)
    mask = df["date"] == target_date
    if not mask.any():
        raise KeyError(f"target_date {target_date!r} not in index data")
    idx = int(df.index[mask][0])
    if idx < 19:
        raise ValueError(
            f"not enough history for 20d volume at {target_date}: "
            f"only {idx + 1} trading days available"
        )
    window = df.iloc[: idx + 1]
    vol5 = float(window["volume"].tail(5).mean())
    vol20 = float(window["volume"].tail(20).mean())
    if vol20 <= 0:
        raise ValueError(f"20d volume average is non-positive at {target_date}: {vol20}")
    return vol5 / vol20


# --------------------------------------------------------------------------- #
# 编排器 — 组装 RawMarketData
# --------------------------------------------------------------------------- #


def build_raw_market_data(
    date: str,
    md_path: Optional[str] = None,
    cache_dir: Optional[str] = None,
    hs300_df: Optional[pd.DataFrame] = None,
    csi1000_df: Optional[pd.DataFrame] = None,
    full_market_df: Optional[pd.DataFrame] = None,
) -> RawMarketData:
    """从复盘 MD + Tushare 构建 RawMarketData。

    参数:
        date            — 目标交易日 'YYYY-MM-DD'
        md_path         — 复盘 MD 路径, 为 None 时跳过 MD 解析
        cache_dir       — Tushare 缓存目录, 为 None 时不做 Tushare 拉取
        hs300_df        — 预加载的 HS300 日线 DF (测试/离线模式用)
        csi1000_df      — 预加载的 CSI1000 日线 DF
        full_market_df  — 预加载的全市场成交额 DF (date + volume), 用于
                          volume_trend 维度; 未提供时若 cache_dir 可用,
                          自动从 Tushare daily_info 合成 (SH_MARKET + SZ_MARKET)。
                          为了向后兼容, 允许未传且 cache_dir 不可用, 此时
                          fallback 到 hs300_df 的 volume 字段。

    每个失败的维度都被记入 result.missing_dims 列表, 不抛异常。
    """
    result = RawMarketData(date=date)

    # ---- dims 2-5: DB 优先, MD fallback ----
    db_raw = load_raw_dims_from_db(date)
    if db_raw is not None:
        logger.info("dims 2-5 loaded from regime_raw_daily (DB)")
        if db_raw["advance_decline_ratio"] is not None:
            result.advance_decline_ratio = db_raw["advance_decline_ratio"]
        else:
            result.missing_dims.append("advance_decline")
        if db_raw["sentiment_delta"] is not None:
            result.sentiment_delta = db_raw["sentiment_delta"]
        else:
            result.missing_dims.append("sentiment_delta")
        if db_raw["sentiment_index"] is not None:
            result.sentiment_index = db_raw["sentiment_index"]
        else:
            result.missing_dims.append("sentiment_index")
        if db_raw["max_streak"] is not None:
            result.max_streak = db_raw["max_streak"]
        else:
            result.missing_dims.append("streak_height")
    elif md_path is not None:
        # MD fallback (老路径, DB 无数据时走这里)
        logger.info("dims 2-5: DB miss, falling back to MD parsing")
        try:
            md_data = parse_daily_review(md_path)
        except FileNotFoundError as e:
            logger.warning("review MD not found: %s", e)
            md_data = {}
            result.warnings.append(f"review_md_missing: {md_path}")

        if "advance_decline_ratio" in md_data:
            result.advance_decline_ratio = md_data["advance_decline_ratio"]
        else:
            result.missing_dims.append("advance_decline")

        if "sentiment_delta" in md_data:
            result.sentiment_delta = md_data["sentiment_delta"]
        else:
            result.missing_dims.append("sentiment_delta")

        if "sentiment_index" in md_data:
            result.sentiment_index = md_data["sentiment_index"]
        else:
            result.missing_dims.append("sentiment_index")

        if "max_streak" in md_data:
            result.max_streak = md_data["max_streak"]
        else:
            result.missing_dims.append("streak_height")
    else:
        result.missing_dims.extend(
            ["advance_decline", "sentiment_delta", "sentiment_index", "streak_height"]
        )
        result.warnings.append("dims 2-5: no DB data and no MD path")

    # ---- 指数侧 (DB 优先 + Tushare 兜底) ----
    # HS300
    hs300_data = hs300_df
    if hs300_data is None:
        try:
            hs300_data = fetch_index_daily("hs300", target_date=date)
        except Exception as e:
            logger.warning("fetch HS300 failed: %s", e)
            result.warnings.append(f"fetch_hs300_failed: {e}")

    # CSI1000
    csi1000_data = csi1000_df
    if csi1000_data is None:
        try:
            csi1000_data = fetch_index_daily("csi1000", target_date=date)
        except Exception as e:
            logger.warning("fetch CSI1000 failed: %s", e)
            result.warnings.append(f"fetch_csi1000_failed: {e}")

    # MA + 涨跌幅
    if hs300_data is not None:
        try:
            result.hs300 = compute_index_ma(hs300_data, date)
        except (KeyError, ValueError) as e:
            logger.warning("compute HS300 MA failed: %s", e)
            result.warnings.append(f"hs300_ma_failed: {e}")
        try:
            result.hs300_pct_change = compute_pct_change(hs300_data, date)
        except (KeyError, ValueError) as e:
            logger.warning("compute HS300 pct_change failed: %s", e)
            result.warnings.append(f"hs300_pct_change_failed: {e}")

    if csi1000_data is not None:
        try:
            result.csi1000 = compute_index_ma(csi1000_data, date)
        except (KeyError, ValueError) as e:
            logger.warning("compute CSI1000 MA failed: %s", e)
            result.warnings.append(f"csi1000_ma_failed: {e}")
        try:
            result.csi1000_pct_change = compute_pct_change(csi1000_data, date)
        except (KeyError, ValueError) as e:
            logger.warning("compute CSI1000 pct_change failed: %s", e)
            result.warnings.append(f"csi1000_pct_change_failed: {e}")

    if result.hs300 is None or result.csi1000 is None:
        result.missing_dims.append("ma_position")

    # 成交量 (优先用全市场成交额, fallback HS300 成交量)
    volume_source_df = full_market_df
    if volume_source_df is None:
        try:
            volume_source_df = fetch_full_market_volume(target_date=date)
        except Exception as e:
            logger.warning("fetch full market volume failed: %s", e)
            result.warnings.append(f"fetch_full_market_failed: {e}")

    if volume_source_df is None:
        # 最终 fallback: HS300 成交量 (旧代理方式)
        volume_source_df = hs300_data
        if volume_source_df is not None:
            result.warnings.append("volume_trend_using_hs300_proxy")

    if volume_source_df is not None:
        try:
            result.volume_ratio_5_20 = compute_volume_ratio(volume_source_df, date)
        except (KeyError, ValueError) as e:
            logger.warning("compute volume ratio failed: %s", e)
            result.warnings.append(f"volume_ratio_failed: {e}")
            result.missing_dims.append("volume_trend")
    else:
        result.missing_dims.append("volume_trend")

    return result
