"""scout/margin_flow.py — 融资融券 EOD 聚合 (宽基指数 / 主线板块两视角).

设计文档: docs/superpowers/specs/2026-06-18-scout-margin-flow-design.md

数据源: tushare margin_detail (经 :18065 tushare-proxy-mcp).
  ⚠ margin_detail 不带交易所参数仅返回上交所(~1981); 必须带 `exchange`, 遍历
  ['SSE','SZSE','BSE'] union 去重才是全市场 (~4370 只).
  ⚠⚠ 参数名是 `exchange` 不是 `exchange_id`: 旧名已被上游忽略(传了等于没传, 只回
  上交所), 是 2026-06-26 起两融数据残缺只剩 1981 行的根因. run() 已加单一交易所哨兵兜底.

流程:
  1. fetch_margin_detail(trade_date)  -> 全市场融资融券明细 (网络, 可 mock)
  2. upsert_security_daily            -> 写原子快照表 margin_security_daily
  3. aggregate_indices  + write_index_daily   -> margin_index_daily (按 index_etf_map.json)
  4. aggregate_boards   + write_board_daily    -> margin_board_daily (按当日主线 × 成分)

用法:
  python3 margin_flow.py                 # 跑当日主线所在交易日 (数据未出则回退最近)
  python3 margin_flow.py --date 20260617
  python3 margin_flow.py --top 12        # 主线取前 N (默认 12)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
import _tushare_client as tushare  # noqa: E402

log = logging.getLogger("margin_flow")

MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_etf_map.json")
EXCHANGES = ("SSE", "SZSE", "BSE")
DEFAULT_TOP_N = 12


def load_index_etf_map(path: str | None = None) -> dict[str, list[str]]:
    """读 指数→ETF 映射配置. 返回 {指数名: [ts_code,...]}."""
    with open(path or MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


_FIELDS = ("rzye", "rqye", "rzrqye", "rzmre", "rzche", "rqyl", "rqmcl", "rqchl")


def fetch_margin_detail(trade_date: str) -> list[dict]:
    """遍历三交易所取全市场融资融券明细, 按 ts_code union 去重.

    ⚠ 不带交易所参数仅返回上交所; 必须带 `exchange`(旧名 `exchange_id` 已失效被忽略).
    失败抛 RuntimeError.
    """
    merged: dict[str, dict] = {}
    for ex in EXCHANGES:
        out = tushare.call("margin_detail", trade_date=trade_date, exchange=ex)
        for r in out.get("rows", []):
            code = r.get("ts_code")
            if code:
                merged[code] = r
    return list(merged.values())


def upsert_security_daily(db_path: str, trade_date: str, rows: list[dict]) -> int:
    """INSERT OR REPLACE 全市场快照. 幂等. 返回写入行数."""
    c = scout_db.conn(db_path)
    try:
        payload = [
            (trade_date, r["ts_code"], *(r.get(f) for f in _FIELDS))
            for r in rows if r.get("ts_code")
        ]
        c.executemany(
            "INSERT OR REPLACE INTO margin_security_daily"
            "(trade_date, ts_code, rzye, rqye, rzrqye, rzmre, rzche, rqyl, rqmcl, rqchl)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", payload)
        c.commit()
        return len(payload)
    finally:
        c.close()


def _prev_trade_dates(c, trade_date: str, n: int) -> list[str]:
    """margin_security_daily 内 < trade_date 的不同交易日, 降序前 n 个."""
    rows = c.execute(
        "SELECT DISTINCT trade_date FROM margin_security_daily "
        "WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?",
        (trade_date, n)).fetchall()
    return [r[0] for r in rows]


def _rzye_sum_for(c, trade_date: str, codes: list[str]) -> float | None:
    """某交易日一组 ts_code 的融资余额合计; 全缺返回 None."""
    if not codes:
        return None
    ph = ",".join("?" * len(codes))
    row = c.execute(
        f"SELECT SUM(rzye) FROM margin_security_daily "
        f"WHERE trade_date=? AND ts_code IN ({ph})",
        (trade_date, *codes)).fetchone()
    return row[0]


def aggregate_indices(db_path: str, trade_date: str, etf_map: dict) -> list[dict]:
    """按 index_etf_map 聚合各宽基指数当日融资融券 + 环比 1/5 日变化."""
    c = scout_db.conn(db_path)
    try:
        prev = _prev_trade_dates(c, trade_date, 5)
        d1 = prev[0] if len(prev) >= 1 else None
        d5 = prev[4] if len(prev) >= 5 else None
        out = []
        for index_name, codes in etf_map.items():
            if not codes:
                continue
            ph = ",".join("?" * len(codes))
            agg = c.execute(
                f"SELECT COUNT(*) n, SUM(rzye) rz, SUM(rqye) rq, "
                f"SUM(rzmre - rzche) net FROM margin_security_daily "
                f"WHERE trade_date=? AND ts_code IN ({ph})",
                (trade_date, *codes)).fetchone()
            if not agg or not agg["n"]:
                continue  # 当日无任一 ETF 快照 -> 跳过该组
            rz_now = agg["rz"] or 0.0
            chg1 = (rz_now - _rzye_sum_for(c, d1, codes)) if d1 and _rzye_sum_for(c, d1, codes) is not None else None
            chg5 = (rz_now - _rzye_sum_for(c, d5, codes)) if d5 and _rzye_sum_for(c, d5, codes) is not None else None
            top = c.execute(
                f"SELECT ts_code, rzye FROM margin_security_daily "
                f"WHERE trade_date=? AND ts_code IN ({ph}) "
                f"ORDER BY rzye DESC LIMIT 3", (trade_date, *codes)).fetchall()
            out.append({
                "index_name": index_name,
                "etf_count": agg["n"],
                "rzye_sum": rz_now,
                "rqye_sum": agg["rq"] or 0.0,
                "rz_net_buy": agg["net"] or 0.0,
                "rzye_chg1": chg1,
                "rzye_chg5": chg5,
                "top_etf": json.dumps(
                    [{"code": r["ts_code"], "rzye": r["rzye"]} for r in top],
                    ensure_ascii=False),
            })
        return out
    finally:
        c.close()


def write_index_daily(db_path: str, trade_date: str, rows: list[dict]) -> int:
    c = scout_db.conn(db_path)
    try:
        c.execute("DELETE FROM margin_index_daily WHERE trade_date=?", (trade_date,))
        c.executemany(
            "INSERT INTO margin_index_daily"
            "(trade_date, index_name, etf_count, rzye_sum, rqye_sum, rz_net_buy,"
            " rzye_chg1, rzye_chg5, top_etf) VALUES (?,?,?,?,?,?,?,?,?)",
            [(trade_date, r["index_name"], r["etf_count"], r["rzye_sum"],
              r["rqye_sum"], r["rz_net_buy"], r["rzye_chg1"], r["rzye_chg5"],
              r["top_etf"]) for r in rows])
        c.commit()
        return len(rows)
    finally:
        c.close()


def _table_exists(c, name: str) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _board_members(c, board_code: str) -> list[str]:
    """某板块最新 snapshot 的成分股 ts_code 列表."""
    rows = c.execute(
        "SELECT ts_code FROM stock_concept_map WHERE board_code=? "
        "AND snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map "
        "                   WHERE board_code=?)",
        (board_code, board_code)).fetchall()
    return [r[0] for r in rows]


def aggregate_boards(db_path: str, trade_date: str,
                     top_n: int = DEFAULT_TOP_N) -> list[dict]:
    """当日 Top-N 主线板块 (board_trend_daily 按 trend_score 降序) 的成分股融资融券聚合.

    主线取当日; 若 trade_date 当日无主线行, 用 board_trend_daily 最近一日.
    """
    c = scout_db.conn(db_path)
    try:
        bd = c.execute(
            "SELECT MAX(trade_date) FROM board_trend_daily WHERE trade_date<=?",
            (trade_date,)).fetchone()[0]
        if not bd:
            return []
        boards = c.execute(
            "SELECT board_code, board_name, rank FROM board_trend_daily "
            "WHERE trade_date=? ORDER BY trend_score DESC LIMIT ?",
            (bd, top_n)).fetchall()
        prev = _prev_trade_dates(c, trade_date, 5)
        d1 = prev[0] if len(prev) >= 1 else None
        d5 = prev[4] if len(prev) >= 5 else None
        has_db = _table_exists(c, "daily_basic")  # daily_basic 缺失则市值比降级为 None
        out = []
        for b in boards:
            codes = _board_members(c, b["board_code"])
            if not codes:
                continue
            ph = ",".join("?" * len(codes))
            agg = c.execute(
                f"SELECT COUNT(*) n, SUM(rzye) rz, SUM(rqye) rq, "
                f"SUM(rzmre - rzche) net FROM margin_security_daily "
                f"WHERE trade_date=? AND ts_code IN ({ph})",
                (trade_date, *codes)).fetchone()
            rz_now = agg["rz"] or 0.0
            chg1 = (rz_now - _rzye_sum_for(c, d1, codes)) if d1 and _rzye_sum_for(c, d1, codes) is not None else None
            chg5 = (rz_now - _rzye_sum_for(c, d5, codes)) if d5 and _rzye_sum_for(c, d5, codes) is not None else None
            # 板块总市值/流通市值: 全部成分股 daily_basic 汇总 (万元→元, ×10000), 缺数据则 None
            total_mv = circ_mv = None
            if has_db:
                mv = c.execute(
                    f"SELECT SUM(total_mv) tmv, SUM(circ_mv) cmv FROM daily_basic "
                    f"WHERE trade_date=? AND ts_code IN ({ph})",
                    (trade_date, *codes)).fetchone()
                total_mv = (mv["tmv"] * 10000) if mv and mv["tmv"] is not None else None
                circ_mv = (mv["cmv"] * 10000) if mv and mv["cmv"] is not None else None
            top = c.execute(
                f"SELECT s.ts_code, s.rzye, (s.rzmre - s.rzche) net, m.name "
                f"FROM margin_security_daily s "
                f"LEFT JOIN stock_concept_map m ON m.ts_code=s.ts_code "
                f"  AND m.board_code=? "
                f"WHERE s.trade_date=? AND s.ts_code IN ({ph}) "
                f"ORDER BY net DESC LIMIT 5",
                (b["board_code"], trade_date, *codes)).fetchall()
            out.append({
                "board_code": b["board_code"],
                "board_name": b["board_name"],
                "rank": b["rank"],
                "member_n": len(codes),
                "margin_n": agg["n"] or 0,
                "rzye_sum": rz_now,
                "rqye_sum": agg["rq"] or 0.0,
                "rz_net_buy": agg["net"] or 0.0,
                "rzye_chg1": chg1,
                "rzye_chg5": chg5,
                "total_mv": total_mv,
                "circ_mv": circ_mv,
                "top_stocks": json.dumps(
                    [{"code": r["ts_code"], "name": r["name"],
                      "net_buy": r["net"], "rzye": r["rzye"]} for r in top],
                    ensure_ascii=False),
            })
        return out
    finally:
        c.close()


def write_board_daily(db_path: str, trade_date: str, rows: list[dict]) -> int:
    c = scout_db.conn(db_path)
    try:
        c.execute("DELETE FROM margin_board_daily WHERE trade_date=?", (trade_date,))
        c.executemany(
            "INSERT INTO margin_board_daily"
            "(trade_date, board_code, board_name, rank, member_n, margin_n,"
            " rzye_sum, rqye_sum, rz_net_buy, rzye_chg1, rzye_chg5,"
            " total_mv, circ_mv, top_stocks)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(trade_date, r["board_code"], r["board_name"], r["rank"],
              r["member_n"], r["margin_n"], r["rzye_sum"], r["rqye_sum"],
              r["rz_net_buy"], r["rzye_chg1"], r["rzye_chg5"],
              r.get("total_mv"), r.get("circ_mv"), r["top_stocks"])
             for r in rows])
        c.commit()
        return len(rows)
    finally:
        c.close()


def _latest_board_trend_date(db_path: str) -> str | None:
    c = scout_db.conn(db_path)
    try:
        row = c.execute("SELECT MAX(trade_date) FROM board_trend_daily").fetchone()
        return row[0] if row else None
    finally:
        c.close()


def _prev_calendar_dates(yyyymmdd: str, back: int) -> list[str]:
    """从给定日往前回退 back 个【日历日】(跳过周末). 用于 margin 数据未出时兜底探测."""
    d = datetime.strptime(yyyymmdd, "%Y%m%d")
    out = []
    cur = d
    while len(out) < back:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:   # 跳过周六(5)/周日(6)
            out.append(cur.strftime("%Y%m%d"))
    return out


def run(db_path: str, trade_date: str | None = None,
        top_n: int = DEFAULT_TOP_N) -> str:
    """主编排: 取数→写快照→指数聚合→板块聚合. 返回实际使用的 trade_date."""
    etf_map = load_index_etf_map()
    target = trade_date or _latest_board_trend_date(db_path) \
        or datetime.now().strftime("%Y%m%d")
    rows = fetch_margin_detail(target)
    if not rows:
        # 数据未出: 按日历回退最多 7 天找最近有数据的交易日
        for cand in _prev_calendar_dates(target, 7):
            rows = fetch_margin_detail(cand)
            if rows:
                log.warning("margin %s 无数据, 回退到 %s", target, cand)
                target = cand
                break
    if not rows:
        raise RuntimeError(f"margin_detail 连续 7 日无数据 (起 {trade_date or target})")
    # 哨兵: 全市场必横跨沪(.SH)+深(.SZ); 只回单一交易所=上游 exchange 参数失效的残缺返回,
    # 拒绝写库以免半市场静默覆盖 (复刻 2026-06-26 事故: 只回上交所 1981 行被当成功写库 rc=0).
    suffixes = {r["ts_code"].rsplit(".", 1)[-1] for r in rows if r.get("ts_code")}
    if not {"SH", "SZ"} <= suffixes:
        raise RuntimeError(
            f"margin_detail {target} 疑似残缺: 仅覆盖 {sorted(suffixes)} "
            f"(全市场须含沪 SH + 深 SZ); 拒绝写库, 多半上游交易所参数失效只回单一交易所")
    n = upsert_security_daily(db_path, target, rows)
    log.info("margin_security_daily %s 写入 %d 行", target, n)
    ni = write_index_daily(db_path, target,
                           aggregate_indices(db_path, target, etf_map))
    nb = write_board_daily(db_path, target,
                           aggregate_boards(db_path, target, top_n))
    log.info("margin_index_daily=%d margin_board_daily=%d", ni, nb)
    return target


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD; 缺省=当日主线所在交易日")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="主线取前 N (默认 12)")
    ap.add_argument("--db", default=scout_db.DB_PATH, help="覆盖 DB 路径")
    a = ap.parse_args()
    used = run(a.db, a.date, a.top)
    print(f"[margin_flow] done trade_date={used}")


if __name__ == "__main__":
    main()
