"""萌芽主线 — 每日每板块早期信号计算.

每个信号产出连续分数; 板块"点头"= 分数落当日 universe 有限分数前 20%.
3 个核心信号 (涨幅加速/成交占比抬升/广度扩散) 纯 concept_board_daily, 全周期可算;
资金流入加速仅 2025-05+ 且需成分聚合, 作可选确认.
"""
from __future__ import annotations
import os, sys, math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from board_trend import _recon_index  # noqa: E402
from s8_live import _is_broad  # noqa: E402
from board_trend import _is_noise  # noqa: E402


def _ma(series, n):
    return sum(series[-n:]) / n if len(series) >= n else None


def price_accel_score(pcts):
    """近5日涨速 − 上一个5日涨速; 仅当 ret5_now>0 且 刚收复 MA20 才有效, 否则 -inf.

    pcts: 板块日 pct_change(%) 序列(旧→新), 至少 ~21 个.
    收复 MA20 近似: 当前 >= MA20, 且 6 日前 < 当前 MA20 (近期才站上).
    """
    if len(pcts) < 21:
        return float("-inf")
    idx = _recon_index(pcts)
    if len(idx) < 21:
        return float("-inf")
    ret5_now = idx[-1] / idx[-6] - 1
    ret5_prev = idx[-6] / idx[-11] - 1
    ma20 = _ma(idx, 20)
    cur = idx[-1]
    reclaimed = (ma20 is not None) and (cur >= ma20) and (idx[-6] < ma20)
    if ret5_now <= 0 or not reclaimed:
        return float("-inf")
    return ret5_now - ret5_prev


def share_trend_score(shares):
    """板块成交占比序列(旧→新) 近5日均 / 近20日均 − 1. 仅当占比确在抬升(>0)才有效,
    否则 -inf(与 price_accel/breadth 的硬门控语义一致, 避免无门控噪声主导点头).
    数据不足(<6 有效点) → -inf."""
    vals = [s for s in shares if s is not None]
    if len(vals) < 6:
        return float("-inf")
    a5 = sum(vals[-5:]) / 5
    a20 = sum(vals[-20:]) / min(20, len(vals))
    if a20 <= 0:
        return float("-inf")
    r = a5 / a20 - 1
    return r if r > 0 else float("-inf")


def breadth_score(up_ratio):
    """上涨家数占比序列(旧→新). 当前 <= 0.5 → -inf(整板没起来).
    否则 = 当前 − 近(4~6日前)基准, 抓广度刚扩散."""
    vals = [r for r in up_ratio if r is not None]
    if len(vals) < 4:
        return float("-inf")
    cur = vals[-1]
    if cur <= 0.5:
        return float("-inf")
    base = sum(vals[-4:-1]) / 3  # 前3日均(不含当日)
    return cur - base


CORE_SIGNALS = ("price_accel", "share_trend", "breadth")
WINDOW = 26  # 取窗口交易日数(算 ret5 加速 + ma20 需要 >=21)


def _window_dates(conn, trade_date, n=WINDOW):
    ds = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM concept_board_daily "
        "WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?", (trade_date, n))]
    return ds[::-1]


def compute_core_signals(conn, trade_date):
    """→ {board_code: {board_code,board_name,price_accel,share_trend,breadth,fired:set,n_signals}}
    fired = 三核心信号里点头(各自横截面前20%)的集合."""
    dates = _window_dates(conn, trade_date)
    if len(dates) < 21:
        return {}
    dph = ",".join("?" * len(dates))
    mkt_amt = {d: (a or 0.0) for d, a in conn.execute(
        f"SELECT trade_date, SUM(amount) FROM daily WHERE trade_date IN ({dph}) "
        f"GROUP BY trade_date", dates)}
    rows = {}
    for bc, bn, d, pct, up, dn, tmv, tr in conn.execute(
        f"SELECT board_code,board_name,trade_date,pct_change,up_num,down_num,total_mv,turnover_rate "
        f"FROM concept_board_daily WHERE trade_date IN ({dph})", dates):
        e = rows.setdefault(bc, {"name": bn, "pct": {}, "up": {}, "dn": {}, "amt": {}})
        if bn:
            e["name"] = bn
        e["pct"][d] = pct
        e["up"][d] = up
        e["dn"][d] = dn
        e["amt"][d] = (tmv or 0.0) * (tr or 0.0) / 100.0

    raw = {}
    for bc, e in rows.items():
        bn = e["name"]
        if _is_broad(bn) or _is_noise(bn):
            continue
        pcts = [e["pct"][d] for d in dates if e["pct"].get(d) is not None]
        if len(pcts) < 21:
            continue
        shares = [(e["amt"].get(d, 0.0) / mkt_amt[d]) if mkt_amt.get(d) else None for d in dates]
        up_ratio = []
        for d in dates:
            u, dn = e["up"].get(d), e["dn"].get(d)
            up_ratio.append(u / (u + dn) if (u is not None and dn is not None and (u + dn) > 0) else None)
        raw[bc] = {
            "board_code": bc, "board_name": bn,
            "price_accel": price_accel_score(pcts),
            "share_trend": share_trend_score(shares),
            "breadth": breadth_score(up_ratio),
        }
    if not raw:
        return {}
    fired_sets = {}
    for sig in CORE_SIGNALS:
        fired_sets[sig] = fire_top_quintile({bc: r[sig] for bc, r in raw.items()})
    for bc, r in raw.items():
        fired = {sig for sig in CORE_SIGNALS if bc in fired_sets[sig]}
        r["fired"] = fired
        r["n_signals"] = len(fired)
    return raw


def fire_top_quintile(scores: dict, pct: float = 0.8) -> set:
    """scores: board_code -> float(可含 -inf). 返回有限分数中 >= pct 分位的板块集合."""
    finite = sorted(v for v in scores.values() if v != float("-inf") and not math.isnan(v))
    if not finite:
        return set()
    k = int(math.ceil(pct * len(finite) + 1e-9)) - 1
    thresh = finite[k]
    return {bc for bc, v in scores.items()
            if v != float("-inf") and not math.isnan(v) and v >= thresh}
