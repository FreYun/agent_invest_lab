"""scout/board_moneyflow.py — 主力资金(net_main)行业级净流入/流出 EOD 聚合.

设计文档: docs/superpowers/specs/2026-06-26-scout-board-moneyflow-design.md

数据源(均已在库, 无网络): moneyflow_daily(个股 net_main 万元) + sw_industry_member
(申万一级互斥行业) + stock_names(名). 严禁用概念板块汇总(双重计数).

流程:
  1. aggregate_boards  -> 各申万一级行业 当日/5日 net_main 汇总 + 行业内 top_in/top_out
  2. aggregate_market  -> 全市场当日/5日 net_main(个股去重求和) + 净流入/流出行业数
  3. write_*           -> board_moneyflow_daily / market_moneyflow_daily (DELETE+INSERT 幂等)

用法:
  python3 board_moneyflow.py                 # 跑 moneyflow_daily 最新交易日
  python3 board_moneyflow.py --date 20260625
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402

log = logging.getLogger("board_moneyflow")

TOP_STOCKS = 3   # 每行业取净流入/流出前 N 成分股


def prev_trade_dates(c, trade_date: str, n: int) -> list[str]:
    """moneyflow_daily 内 <= trade_date 的不同交易日, 降序前 n 个(含当日)."""
    rows = c.execute(
        "SELECT DISTINCT trade_date FROM moneyflow_daily "
        "WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?",
        (trade_date, n)).fetchall()
    return [r[0] for r in rows]


def _top_stocks(c, trade_date: str, l1_code: str, desc: bool) -> list[dict]:
    """行业内当日 net_main 排序前 N 成分股. desc=True 取净流入(降序), False 取净流出(升序)."""
    order = "DESC" if desc else "ASC"
    rows = c.execute(
        f"SELECT f.ts_code, sn.name, f.net_main net FROM moneyflow_daily f "
        f"JOIN sw_industry_member s ON s.ts_code=f.ts_code "
        f"LEFT JOIN stock_names sn ON sn.ts_code=f.ts_code "
        f"WHERE f.trade_date=? AND s.l1_code=? "
        f"ORDER BY f.net_main {order} LIMIT ?",
        (trade_date, l1_code, TOP_STOCKS)).fetchall()
    return [{"code": r["ts_code"], "name": r["name"], "net": r["net"]} for r in rows]


def aggregate_boards(db_path: str, trade_date: str) -> list[dict]:
    """各申万一级行业 当日/5日 net_main 汇总. 当日无 moneyflow 数据返回 []."""
    c = scout_db.conn(db_path)
    try:
        dates5 = prev_trade_dates(c, trade_date, 5)
        if not dates5 or dates5[0] != trade_date:
            return []  # 当日无数据
        ph5 = ",".join("?" * len(dates5))
        net5 = {r["l1_code"]: r["net"] for r in c.execute(
            f"SELECT s.l1_code, SUM(f.net_main) net FROM moneyflow_daily f "
            f"JOIN sw_industry_member s ON s.ts_code=f.ts_code "
            f"WHERE f.trade_date IN ({ph5}) GROUP BY s.l1_code", dates5).fetchall()}
        today = c.execute(
            "SELECT s.l1_code, s.l1_name, SUM(f.net_main) net, COUNT(*) n "
            "FROM moneyflow_daily f JOIN sw_industry_member s ON s.ts_code=f.ts_code "
            "WHERE f.trade_date=? GROUP BY s.l1_code, s.l1_name", (trade_date,)).fetchall()
        out = []
        for r in today:
            lc = r["l1_code"]
            out.append({
                "sw_l1_code": lc,
                "sw_l1_name": r["l1_name"],
                "net_1d": r["net"] or 0.0,
                "net_5d": net5.get(lc, 0.0) or 0.0,
                "member_n": r["n"],
                "top_in": _top_stocks(c, trade_date, lc, desc=True),
                "top_out": _top_stocks(c, trade_date, lc, desc=False),
            })
        return out
    finally:
        c.close()


def aggregate_market(db_path: str, trade_date: str, board_rows: list[dict]) -> dict | None:
    """全市场当日/5日 net_main(个股去重求和) + 净流入/流出行业数. 无数据返回 None."""
    if not board_rows:
        return None
    c = scout_db.conn(db_path)
    try:
        dates5 = prev_trade_dates(c, trade_date, 5)
        ph5 = ",".join("?" * len(dates5))
        net_1d = c.execute(
            "SELECT SUM(net_main) FROM moneyflow_daily WHERE trade_date=?",
            (trade_date,)).fetchone()[0]
        net_5d = c.execute(
            f"SELECT SUM(net_main) FROM moneyflow_daily WHERE trade_date IN ({ph5})",
            dates5).fetchone()[0]
        return {
            "net_1d": net_1d or 0.0,
            "net_5d": net_5d or 0.0,
            "in_n": sum(1 for r in board_rows if (r["net_1d"] or 0) > 0),
            "out_n": sum(1 for r in board_rows if (r["net_1d"] or 0) < 0),
        }
    finally:
        c.close()


def write_board_daily(db_path: str, trade_date: str, rows: list[dict]) -> int:
    c = scout_db.conn(db_path)
    try:
        c.execute("DELETE FROM board_moneyflow_daily WHERE trade_date=?", (trade_date,))
        c.executemany(
            "INSERT INTO board_moneyflow_daily"
            "(trade_date, sw_l1_code, sw_l1_name, net_1d, net_5d, member_n, top_in, top_out)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(trade_date, r["sw_l1_code"], r["sw_l1_name"], r["net_1d"], r["net_5d"],
              r["member_n"], json.dumps(r["top_in"], ensure_ascii=False),
              json.dumps(r["top_out"], ensure_ascii=False)) for r in rows])
        c.commit()
        return len(rows)
    finally:
        c.close()


def write_market_daily(db_path: str, trade_date: str, row: dict) -> int:
    c = scout_db.conn(db_path)
    try:
        c.execute("DELETE FROM market_moneyflow_daily WHERE trade_date=?", (trade_date,))
        c.execute(
            "INSERT INTO market_moneyflow_daily"
            "(trade_date, net_1d, net_5d, in_n, out_n) VALUES (?,?,?,?,?)",
            (trade_date, row["net_1d"], row["net_5d"], row["in_n"], row["out_n"]))
        c.commit()
        return 1
    finally:
        c.close()


def _latest_moneyflow_date(db_path: str) -> str | None:
    c = scout_db.conn(db_path)
    try:
        row = c.execute("SELECT MAX(trade_date) FROM moneyflow_daily").fetchone()
        return row[0] if row else None
    finally:
        c.close()


_WAN_TO_YI = 1e-4   # 万元 -> 亿元


def board_payload(c, top_n: int = 12) -> dict:
    """只读: 从两表组装前端 payload. net 字段转亿元; top_in/top_out 透传原始(万元).

    c: scout_db.conn 连接(row_factory=Row). 取 board_moneyflow_daily 最新交易日.
    """
    row = c.execute("SELECT MAX(trade_date) FROM board_moneyflow_daily").fetchone()
    date = row[0] if row else None
    if not date:
        return {"date": None, "market": None, "inflow_top": [], "outflow_top": []}

    def _board(order: str):
        rows = c.execute(
            f"SELECT sw_l1_name, net_1d, net_5d, member_n, top_in, top_out "
            f"FROM board_moneyflow_daily WHERE trade_date=? "
            f"ORDER BY net_1d {order} LIMIT ?", (date, top_n)).fetchall()
        return [{
            "name": r["sw_l1_name"],
            "net_1d": (r["net_1d"] or 0.0) * _WAN_TO_YI,
            "net_5d": (r["net_5d"] or 0.0) * _WAN_TO_YI,
            "member_n": r["member_n"],
            "top_in": json.loads(r["top_in"] or "[]"),
            "top_out": json.loads(r["top_out"] or "[]"),
        } for r in rows]

    m = c.execute(
        "SELECT net_1d, net_5d, in_n, out_n FROM market_moneyflow_daily "
        "WHERE trade_date=?", (date,)).fetchone()
    trend = c.execute(
        "SELECT net_1d FROM market_moneyflow_daily WHERE trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 5", (date,)).fetchall()
    market = None
    if m:
        market = {
            "net_1d": (m["net_1d"] or 0.0) * _WAN_TO_YI,
            "net_5d": (m["net_5d"] or 0.0) * _WAN_TO_YI,
            "in_n": m["in_n"], "out_n": m["out_n"],
            "trend5": [(t["net_1d"] or 0.0) * _WAN_TO_YI for t in reversed(trend)],
        }
    return {
        "date": date,
        "market": market,
        "inflow_top": _board("DESC"),
        "outflow_top": _board("ASC"),
    }


def run(db_path: str, trade_date: str | None = None) -> str:
    """主编排: 行业聚合 → 全市场聚合 → 写两表. 返回实际 trade_date."""
    target = trade_date or _latest_moneyflow_date(db_path)
    if not target:
        raise RuntimeError("moneyflow_daily 无任何数据")
    boards = aggregate_boards(db_path, target)
    if not boards:
        raise RuntimeError(f"moneyflow_daily {target} 当日无数据, 不写空表")
    mkt = aggregate_market(db_path, target, boards)
    nb = write_board_daily(db_path, target, boards)
    write_market_daily(db_path, target, mkt)
    log.info("board_moneyflow %s: 行业=%d 全市场净流入=%.0f万",
             target, nb, mkt["net_1d"])
    return target


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD; 缺省=moneyflow_daily 最新交易日")
    ap.add_argument("--db", default=scout_db.DB_PATH, help="覆盖 DB 路径")
    a = ap.parse_args()
    scout_db.init_schema(a.db)
    used = run(a.db, a.date)
    print(f"[board_moneyflow] done trade_date={used}")


if __name__ == "__main__":
    main()
