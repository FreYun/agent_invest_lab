"""earnings_resonance — 业绩共振日频筛选 (轻量版, 市场反应代理).

设计: 详见 docs/superpowers/specs/2026-07-06-earnings-resonance-tracker-design.md

判定入口 (A 档, 需同时满足):
- gap_pct  >= 3.0   开盘跳空
- day_ret  >= 5.0   当日涨幅
- vol_ratio >= 1.5  量比

个股需同时命中当日 board_trend_daily 排名 <= BOARD_TREND_TOP_N 的主线板块
才入 earnings_resonance_daily.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402

GAP_MIN = 3.0
DAY_RET_MIN = 5.0
VOL_RATIO_MIN = 1.5
BOARD_TREND_TOP_N = 30
OTHER_BOARDS_LIMIT = 3
EVENT_WINDOW_TRADING_DAYS = 60


def _is_20cm(ts_code: str, name: str | None = None) -> bool:
    """判是否 20% 涨跌停范围: 科创板/创业板/北交所/ST 除外.

    ST 走 10cm(不到 5%)本函数不用管, 这里只区分 10cm vs 20cm.
    """
    if not ts_code:
        return False
    prefix2 = ts_code[:2]
    prefix1 = ts_code[:1]
    return prefix2 in ("68", "30") or prefix1 in ("8", "4")


def compute_shape_tag(row: dict) -> str:
    """从日 K 开高低收 + 昨收 推形态标签; 可能多个用逗号分隔.

    row 需含 ts_code / pre_close / open / high / low / close.
    """
    pre = float(row.get("pre_close") or 0.0)
    o = float(row.get("open") or 0.0)
    h = float(row.get("high") or 0.0)
    lo = float(row.get("low") or 0.0)
    c = float(row.get("close") or 0.0)
    if pre <= 0 or o <= 0:
        return ""

    gap_pct = (o / pre - 1) * 100
    day_ret = (c / pre - 1) * 100
    is20 = _is_20cm(row.get("ts_code") or "")

    tags: list[str] = []

    # LIMIT_UP: 收盘触涨停(允许些许浮点误差)
    limit_thresh = 19.8 if is20 else 9.8
    if day_ret >= limit_thresh:
        tags.append("LIMIT_UP")

    # T_SHAPE: 盘中触涨停线但未封, 收盘 <=+5%
    touch_thresh = 19.5 if is20 else 9.5
    if (h / pre - 1) * 100 >= touch_thresh and day_ret <= 5.0:
        tags.append("T_SHAPE")

    # HIGH_STRONG: 高开(>=3%) 且 收盘几乎没回落(close/open >= 0.995)
    if gap_pct >= 3.0 and o > 0 and c / o >= 0.995:
        tags.append("HIGH_STRONG")

    # HIGH_FADE_REBOUND: 高开 + 盘中 low <= open×0.97 但 close 从 low 反包 >=3%
    if gap_pct >= 3.0 and lo > 0 and lo / o <= 0.97 and c / lo >= 1.03:
        tags.append("HIGH_FADE_REBOUND")

    # HIGH_FADE: 高开 + close/open <= 0.97, 无反包(REBOUND 已上就不加这个)
    if (gap_pct >= 3.0 and c / o <= 0.97
            and "HIGH_FADE_REBOUND" not in tags):
        tags.append("HIGH_FADE")

    # LOW_STRONG: 平/低开(gap<=1%) 但当日 >=+5%
    if gap_pct <= 1.0 and day_ret >= 5.0:
        tags.append("LOW_STRONG")

    return ",".join(tags)


def pick_primary_board(c: sqlite3.Connection, ts_code: str, trade_date: str,
                       top_n: int = BOARD_TREND_TOP_N) -> Optional[tuple]:
    """给一只票, 从 stock_concept_map 找它的所有板块, JOIN 当日 board_trend_daily,
    过滤 rank<=top_n, 按 rank 升序; 返回 (code, name, rank, score, other_boards_csv).

    other_boards_csv: 除 primary 外命中的板块名(按 rank 升序), 最多 OTHER_BOARDS_LIMIT 个.
    若无命中返回 None.
    """
    rows = c.execute(
        "SELECT btd.board_code, btd.board_name, btd.rank, btd.trend_score "
        "FROM stock_concept_map scm "
        "JOIN board_trend_daily btd "
        "  ON btd.board_code = scm.board_code AND btd.trade_date = ? "
        "WHERE scm.ts_code = ? AND btd.rank <= ? "
        "ORDER BY btd.rank ASC",
        (trade_date, ts_code, top_n),
    ).fetchall()
    if not rows:
        return None
    primary = rows[0]
    others = [r["board_name"] for r in rows[1:1 + OTHER_BOARDS_LIMIT] if r["board_name"]]
    return (
        primary["board_code"],
        primary["board_name"],
        int(primary["rank"]),
        float(primary["trend_score"] or 0.0),
        ",".join(others),
    )


def compute_resonance(c: sqlite3.Connection, trade_date: str,
                      top_n: int = BOARD_TREND_TOP_N) -> list[dict]:
    """扫全市场 daily+daily_basic, 过硬门槛后 JOIN 主线板块; 返回入库行 dict 列表."""
    # 1) 硬门槛在 SQL 里过, 减少 python 层遍历
    #    day_ret 直接用 daily.pct_chg (tushare 原生, 未复权)
    sql = (
        "SELECT d.ts_code, d.open, d.high, d.low, d.close, d.pre_close, "
        "       d.pct_chg AS day_ret, db.volume_ratio AS vol_ratio "
        "FROM daily d "
        "JOIN daily_basic db ON db.trade_date=d.trade_date AND db.ts_code=d.ts_code "
        "WHERE d.trade_date=? "
        "  AND d.pre_close > 0 AND d.open > 0 "
        "  AND (d.open/d.pre_close - 1)*100 >= ? "
        "  AND d.pct_chg >= ? "
        "  AND db.volume_ratio >= ? "
    )
    cur = c.execute(sql, (trade_date, GAP_MIN, DAY_RET_MIN, VOL_RATIO_MIN))
    rows_in = [dict(r) for r in cur.fetchall()]
    if not rows_in:
        return []

    # 2) 拉一次 stock_names 做名字 (缺就用 stock_concept_map.name 兜底, 再缺置空)
    name_map: dict[str, str] = {}
    try:
        for r in c.execute("SELECT ts_code, name FROM stock_names").fetchall():
            name_map[r["ts_code"]] = r["name"]
    except sqlite3.OperationalError:
        pass  # 表可能不存在(单测), 走 fallback

    out: list[dict] = []
    for r in rows_in:
        ts_code = r["ts_code"]
        pb = pick_primary_board(c, ts_code, trade_date, top_n=top_n)
        if not pb:
            continue
        pb_code, pb_name, pb_rank, pb_score, others_csv = pb
        row_for_tag = {
            "ts_code": ts_code,
            "pre_close": r["pre_close"], "open": r["open"], "high": r["high"],
            "low": r["low"], "close": r["close"],
        }
        gap_pct = (r["open"] / r["pre_close"] - 1) * 100 if r["pre_close"] else 0.0
        out.append({
            "trade_date": trade_date,
            "ts_code": ts_code,
            "name": name_map.get(ts_code) or "",
            "gap_pct": round(gap_pct, 3),
            "day_ret": round(float(r["day_ret"] or 0.0), 3),
            "vol_ratio": round(float(r["vol_ratio"] or 0.0), 3),
            "open_px": r["open"], "high_px": r["high"],
            "low_px": r["low"], "close_px": r["close"], "pre_close": r["pre_close"],
            "shape_tag": compute_shape_tag(row_for_tag),
            "primary_board_code": pb_code,
            "primary_board_name": pb_name,
            "primary_board_rank": pb_rank,
            "primary_board_trend_score": pb_score,
            "other_boards": others_csv,
        })
    # 排序: 主线 rank ASC, 同板块内 day_ret DESC
    out.sort(key=lambda x: (x["primary_board_rank"], -x["day_ret"]))
    return out


def write_db(c: sqlite3.Connection, trade_date: str, rows: list[dict],
             dry_run: bool = False) -> int:
    """删同日 + 批量插入; dry_run=True 时不落库只返回条数."""
    if dry_run:
        return len(rows)
    c.execute("DELETE FROM earnings_resonance_daily WHERE trade_date=?", (trade_date,))
    if rows:
        c.executemany(
            "INSERT INTO earnings_resonance_daily "
            "(trade_date, ts_code, name, gap_pct, day_ret, vol_ratio, "
            " open_px, high_px, low_px, close_px, pre_close, shape_tag, "
            " primary_board_code, primary_board_name, primary_board_rank, "
            " primary_board_trend_score, other_boards) "
            "VALUES (:trade_date,:ts_code,:name,:gap_pct,:day_ret,:vol_ratio,"
            " :open_px,:high_px,:low_px,:close_px,:pre_close,:shape_tag,"
            " :primary_board_code,:primary_board_name,:primary_board_rank,"
            " :primary_board_trend_score,:other_boards)",
            rows,
        )
    c.commit()
    return len(rows)


def _latest_trade_date(c: sqlite3.Connection) -> str | None:
    r = c.execute("SELECT MAX(trade_date) FROM daily").fetchone()
    return r[0] if r and r[0] else None


def main() -> int:
    p = argparse.ArgumentParser(description="业绩共振日频筛选 (轻量版)")
    p.add_argument("--date", help="YYYYMMDD; 缺省=daily 表 MAX(trade_date)")
    p.add_argument("--top-n", type=int, default=BOARD_TREND_TOP_N,
                   help=f"主线板块 rank 过滤门槛 (默认 {BOARD_TREND_TOP_N})")
    p.add_argument("--dry-run", action="store_true", help="只打印, 不落库")
    args = p.parse_args()

    c = scout_db.conn()
    try:
        td = args.date or _latest_trade_date(c)
        if not td:
            print("[earnings_resonance] daily 表空, 退出", file=sys.stderr)
            return 1
        rows = compute_resonance_v2(c, td)
        n = write_db_v2(c, td, rows, dry_run=args.dry_run)
        tag = "[dry-run] " if args.dry_run else ""
        print(f"[earnings_resonance v2] {tag}{td}: 写入 {n} 行 (事件驱动)")
        for r in rows[:20]:
            rank_s = f"#{r['primary_board_rank']:>2}" if r['primary_board_rank'] else "—"
            print(f"  {rank_s} {r['primary_board_name'] or '—':<8} "
                  f"{r['ts_code']} {r['name']:<8} "
                  f"[{r['latest_event_type'] or 'express'}] "
                  f"+{(r['latest_p_change_min'] or 0):.0f}% cum={r['cum_ret_since_event']!s:>6}")
        return 0
    finally:
        c.close()


# =====================================================================
# v2 事件驱动 (spec 2026-07-07): 保留上面的 v1 函数不删, 但 main 改调 v2 链
# =====================================================================

def trading_days_before(c: sqlite3.Connection, td: str, n: int) -> list[str]:
    """从 daily 表拿 <= td 的最近 n 个交易日, 升序返回."""
    rows = c.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?", (td, n),
    ).fetchall()
    return [r[0] for r in reversed(rows)]  # 升序


def latest_reporting_period(c: sqlite3.Connection, window_start: str, td: str) -> str | None:
    """全局最新报告期: 窗口内所有 is_hard_hit=1 事件里 MAX(end_date).

    卡硬标是为了避开偶发的未来/异常 end_date 单条记录 (如某票预告写了 20261231).
    返回 YYYYMMDD 或 None.
    """
    r = c.execute(
        "SELECT MAX(end_date) FROM earnings_events "
        "WHERE ann_date >= ? AND ann_date <= ? AND is_hard_hit = 1 AND end_date IS NOT NULL",
        (window_start, td),
    ).fetchone()
    return r[0] if r and r[0] else None


def latest_event_per_ts_code(c: sqlite3.Connection, window_start: str, td: str) -> dict[str, dict]:
    """窗口内每票的最新硬标事件, 只保留全市场最新一期报告 (end_date == MAX).

    两阶段 + end_date 过滤:
    1) 计算 latest_period = MAX(end_date) over is_hard_hit=1 事件
    2) 每票取 MAX(ann_date), 限制 end_date == latest_period
    3) 同 (ts_code, ann_date) 有 forecast + express 时, forecast 优先
    """
    latest_period = latest_reporting_period(c, window_start, td)
    if not latest_period:
        return {}
    # 阶段 1: 每票在最新期内的 MAX(ann_date)
    latest_map = {
        r[0]: r[1] for r in c.execute(
            "SELECT ts_code, MAX(ann_date) FROM earnings_events "
            "WHERE ann_date >= ? AND ann_date <= ? AND is_hard_hit = 1 "
            "AND end_date = ? "
            "GROUP BY ts_code",
            (window_start, td, latest_period),
        ).fetchall()
    }
    if not latest_map:
        return {}
    # 阶段 2: 每票拿一条 event 详情 (forecast 优先)
    out: dict[str, dict] = {}
    for ts_code, ann_date in latest_map.items():
        r = c.execute(
            "SELECT ts_code, ann_date, source, end_date, event_type, "
            "       p_change_min, p_change_max, summary "
            "FROM earnings_events "
            "WHERE ts_code=? AND ann_date=? AND end_date=? AND is_hard_hit=1 "
            "ORDER BY CASE source WHEN 'forecast' THEN 0 ELSE 1 END LIMIT 1",
            (ts_code, ann_date, latest_period),
        ).fetchone()
        if r:
            out[ts_code] = dict(r)
    return out


def compute_event_returns(c: sqlite3.Connection, ts_code: str, ann_date: str,
                          td: str) -> dict:
    """算事件后走势: t1_open_ret / t1_close_ret / cum_ret_since_event.

    基准 = event_close = daily(ts_code, ann_date').close, ann_date' = <=ann_date 最近有 daily 的日.
    T+1 = trading_calendar 里 > ann_date' 的第一个交易日.
    当日披露 (ann_date == td): t1_* 全 NULL, cum_ret = 0.
    停牌无 daily: 相关字段 NULL.
    """
    out = {"t1_open_ret": None, "t1_close_ret": None, "cum_ret_since_event": None}
    # 找事件基准: ann_date 或往前第一个 daily 有值的日
    r = c.execute(
        "SELECT trade_date, close FROM daily "
        "WHERE ts_code=? AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
        (ts_code, ann_date),
    ).fetchone()
    if not r or r[1] in (None, 0):
        return out
    event_trade_date, event_close = r[0], float(r[1])

    # 若事件公告日 > 当前快照日 td (即"今晚公告 + 明天开始交易"), 走势字段全 NULL,
    # 等下一交易日 daily 入库. 例: ann_date=20260707, td=20260706. 注意区分:
    #   - 事件在过去的周末回退到上周五交易日: ann_date < td, 正常算, 不进这个分支
    #   - 事件今晚新披露, ann_date > td, 全 NULL
    if ann_date > td:
        return out

    # cum_ret_since_event
    r_td = c.execute(
        "SELECT close FROM daily WHERE ts_code=? AND trade_date=?",
        (ts_code, td),
    ).fetchone()
    if r_td and r_td[0] not in (None, 0):
        out["cum_ret_since_event"] = round((float(r_td[0]) / event_close - 1) * 100, 3)

    # t1: > event_trade_date 的第一个 daily 行
    r_t1 = c.execute(
        "SELECT open, close FROM daily "
        "WHERE ts_code=? AND trade_date > ? ORDER BY trade_date ASC LIMIT 1",
        (ts_code, event_trade_date),
    ).fetchone()
    if r_t1:
        o, cl = r_t1
        if o not in (None, 0):
            out["t1_open_ret"] = round((float(o) / event_close - 1) * 100, 3)
        if cl not in (None, 0):
            out["t1_close_ret"] = round((float(cl) / event_close - 1) * 100, 3)
    return out


def pick_primary_board_no_filter(c: sqlite3.Connection, ts_code: str,
                                 trade_date: str) -> tuple | None:
    """v2: 命中 board_trend_daily 就填(不限 top_n); 未命中返 None.

    多板块命中按 rank 升序取第一; 其他板块名进 other_boards 前 OTHER_BOARDS_LIMIT 个.
    """
    rows = c.execute(
        "SELECT btd.board_code, btd.board_name, btd.rank, btd.trend_score "
        "FROM stock_concept_map scm "
        "JOIN board_trend_daily btd "
        "  ON btd.board_code = scm.board_code AND btd.trade_date = ? "
        "WHERE scm.ts_code = ? "
        "ORDER BY btd.rank ASC",
        (trade_date, ts_code),
    ).fetchall()
    if not rows:
        return None
    primary = rows[0]
    others = [r["board_name"] for r in rows[1:1 + OTHER_BOARDS_LIMIT] if r["board_name"]]
    return (
        primary["board_code"], primary["board_name"], int(primary["rank"]),
        float(primary["trend_score"] or 0.0), ",".join(others),
    )


def _today_yyyymmdd() -> str:
    """系统时间 today, YYYYMMDD. 独立函数便于测试 monkeypatch."""
    import datetime
    return datetime.date.today().strftime("%Y%m%d")


def compute_resonance_v2(c: sqlite3.Connection, trade_date: str,
                         event_end: str | None = None) -> list[dict]:
    """v2 主逻辑: 事件驱动. 参 spec 2026-07-07 §计算逻辑.

    trade_date: 快照日 = daily.MAX(trade_date), 决定"当日走势"字段和板块归属
    event_end:  事件窗口上限, 默认 today(). 与 trade_date 分离是因为业绩预告
                是盘后公告, 当日盘中就有 20260707 的预告, 但 daily 20260707
                要等今晚 15:00 后 stock-select-daily 落库. 卡在 daily MAX
                会让当日新披露漏进 UI.
    """
    if event_end is None:
        event_end = _today_yyyymmdd()
    # 60 交易日窗口 (仍从 trade_date 往前数, 保证窗口锚定在有 daily 的日期)
    calendar = trading_days_before(c, trade_date, EVENT_WINDOW_TRADING_DAYS)
    if not calendar:
        return []
    window_start = calendar[0]

    # 每票最新硬标事件 (事件公告日 <= event_end, 可以晚于 trade_date)
    events = latest_event_per_ts_code(c, window_start, event_end)
    if not events:
        return []

    # stock_names for name lookup
    name_map: dict[str, str] = {}
    try:
        for r in c.execute("SELECT ts_code, name FROM stock_names").fetchall():
            name_map[r["ts_code"]] = r["name"]
    except sqlite3.OperationalError:
        pass

    out: list[dict] = []
    for ts_code, ev in events.items():
        # 当日 daily / daily_basic (辅助列, 若无也不阻止入表)
        r_d = c.execute(
            "SELECT open, high, low, close, pre_close, pct_chg "
            "FROM daily WHERE ts_code=? AND trade_date=?",
            (ts_code, trade_date),
        ).fetchone()
        r_db = c.execute(
            "SELECT volume_ratio FROM daily_basic WHERE ts_code=? AND trade_date=?",
            (ts_code, trade_date),
        ).fetchone()

        # 事件后走势
        er = compute_event_returns(c, ts_code, ev["ann_date"], trade_date)

        # 板块归属
        pb = pick_primary_board_no_filter(c, ts_code, trade_date)
        pb_code, pb_name, pb_rank, pb_score, others_csv = pb if pb else (None, None, None, None, "")

        # days_since_event: ann_date > trade_date (今日刚披露) 记 0, 否则按交易日算差
        if ev["ann_date"] > trade_date:
            days_since = 0
        elif ev["ann_date"] in calendar:
            i_ann = calendar.index(ev["ann_date"])
            days_since = len(calendar) - 1 - i_ann
        else:
            days_since = None

        # 当日辅助列 (v1 保留字段)
        if r_d:
            o, h, l, cl, pre, pct = (r_d[k] for k in ("open","high","low","close","pre_close","pct_chg"))
            gap = ((o / pre - 1) * 100) if (pre and o) else None
            row_for_tag = {"ts_code": ts_code, "pre_close": pre, "open": o, "high": h, "low": l, "close": cl}
            shape = compute_shape_tag(row_for_tag)
        else:
            o = h = l = cl = pre = pct = gap = None
            shape = ""
        vr = r_db["volume_ratio"] if r_db else None

        out.append({
            "trade_date":       trade_date,
            "ts_code":          ts_code,
            "name":             name_map.get(ts_code) or "",
            # v1 辅助列 (可 NULL)
            "gap_pct":          round(gap, 3) if gap is not None else None,
            "day_ret":          round(float(pct), 3) if pct is not None else None,
            "vol_ratio":        round(float(vr), 3) if vr is not None else None,
            "open_px": o, "high_px": h, "low_px": l, "close_px": cl, "pre_close": pre,
            "shape_tag":        shape,
            "primary_board_code": pb_code, "primary_board_name": pb_name,
            "primary_board_rank": pb_rank, "primary_board_trend_score": pb_score,
            "other_boards":     others_csv,
            # v2 事件字段
            "latest_event_ann_date": ev["ann_date"],
            "latest_event_source":   ev["source"],
            "latest_event_type":     ev.get("event_type"),
            "latest_p_change_min":   ev.get("p_change_min"),
            "latest_p_change_max":   ev.get("p_change_max"),
            "latest_summary":        ev.get("summary"),
            "days_since_event":      days_since,
            "t1_open_ret":           er["t1_open_ret"],
            "t1_close_ret":          er["t1_close_ret"],
            "cum_ret_since_event":   er["cum_ret_since_event"],
        })
    # 排序: 有 rank 的按 rank ASC, 无 rank 的排后; 同 rank 按 cum_ret DESC
    out.sort(key=lambda x: (
        1 if x["primary_board_rank"] is None else 0,
        x["primary_board_rank"] if x["primary_board_rank"] is not None else 0,
        -(x["cum_ret_since_event"] or 0),
    ))
    return out


def write_db_v2(c: sqlite3.Connection, trade_date: str, rows: list[dict],
                dry_run: bool = False) -> int:
    """v2 写库: DELETE 同日 + INSERT 全字段. 字段列表比 v1 多 10 个 v2 列."""
    if dry_run:
        return len(rows)
    c.execute("DELETE FROM earnings_resonance_daily WHERE trade_date=?", (trade_date,))
    if rows:
        c.executemany(
            "INSERT INTO earnings_resonance_daily "
            "(trade_date, ts_code, name, gap_pct, day_ret, vol_ratio, "
            " open_px, high_px, low_px, close_px, pre_close, shape_tag, "
            " primary_board_code, primary_board_name, primary_board_rank, "
            " primary_board_trend_score, other_boards, "
            " latest_event_ann_date, latest_event_source, latest_event_type, "
            " latest_p_change_min, latest_p_change_max, latest_summary, "
            " days_since_event, t1_open_ret, t1_close_ret, cum_ret_since_event) "
            "VALUES (:trade_date,:ts_code,:name,:gap_pct,:day_ret,:vol_ratio,"
            " :open_px,:high_px,:low_px,:close_px,:pre_close,:shape_tag,"
            " :primary_board_code,:primary_board_name,:primary_board_rank,"
            " :primary_board_trend_score,:other_boards,"
            " :latest_event_ann_date,:latest_event_source,:latest_event_type,"
            " :latest_p_change_min,:latest_p_change_max,:latest_summary,"
            " :days_since_event,:t1_open_ret,:t1_close_ret,:cum_ret_since_event)",
            rows,
        )
    c.commit()
    return len(rows)


if __name__ == "__main__":
    sys.exit(main())
