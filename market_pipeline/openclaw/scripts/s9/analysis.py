"""S9 — regime 开关计算.

读 index_daily 的沪深300 历史, 算 close_t / MA_n - 1, > 阈值 → 进攻 regime.
"""

from __future__ import annotations

import logging
from strategy_common import get_market_db, iso_to_yyyymmdd


class IndexDataMissing(Exception):
    pass


def compute_regime(t_date_iso: str, cfg: dict) -> dict:
    """t_date_iso = 'YYYY-MM-DD'. 返回 regime 状态字典."""
    rcfg = cfg["regime"]
    ma = int(rcfg["ma_window"])
    index_code = rcfg["index_code"]
    thr = float(rcfg["offensive_threshold"])
    t_date_compact = iso_to_yyyymmdd(t_date_iso)

    conn = get_market_db()
    try:
        rows = conn.execute(
            "SELECT trade_date, close FROM index_daily "
            "WHERE ts_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT ?",
            (index_code, t_date_compact, ma + 5),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < ma + 1:
        raise IndexDataMissing(
            f"index_daily 不足 {ma+1} 行 (got {len(rows)}, index={index_code}, as_of={t_date_compact})"
        )

    # 最新行须等于 t_date_compact (否则数据未及时回填)
    latest_date = rows[0]["trade_date"]
    if latest_date != t_date_compact:
        logging.warning(
            "S9 regime 用最近可用日 %s (请求 %s)", latest_date, t_date_compact,
        )
    closes = [r["close"] for r in rows[: ma + 1]]
    close_t = closes[0]
    ma_val = sum(closes[1:]) / ma         # t-1..t-MA 的均线 (避免把 close_t 算进去)
    signal = close_t / ma_val - 1.0
    offensive = signal > thr

    return {
        "as_of": latest_date,
        "index_code": index_code,
        "ma_window": ma,
        "close_t": float(close_t),
        "ma_value": float(ma_val),
        "signal": float(signal),
        "regime": "进攻" if offensive else "防守",
        "offensive": bool(offensive),
        "sleeve": "offensive" if offensive else "defensive",
    }
