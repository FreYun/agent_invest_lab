"""scout/board_etf.py — 板块对标基金 EOD 聚合 (纯本地 SQL, 零网络)

设计文档: docs/superpowers/specs/2026-05-28-scout-board-etf-design.md

底表 = fund_top_holdings (正向: 基金→前十大重仓, fund_holdings_sync.py 季度刷新),
用 etf_meta 的分类位拆两个产出:
  - board_etf_daily        ← is_strict_etf=1   (对标场内 ETF)
  - board_active_fund_daily ← is_active_equity=1 (对标主动基金)

流程:
  0. radar_board_codes(server) — 盘面雷达四面板板块并集(趋势主线/资金拥挤/潜在主线/上涨中继)
  1. compute_codes = radar ∪ 全白名单
  2. compute_board_etf_rows / compute_board_active_fund_rows — 纯 SQL, 板块全成分 ∩ 持仓, 每板 Top3
  3. 各自单事务 DELETE+INSERT 落库

(原"按股票反查基金"反向链路已于 2026-06 退役, 改由正向表统一供数; 详见 fund_holdings_sync.py)

用法:
  python3 board_etf.py              # 跑最新交易日
  python3 board_etf.py --date 20260528
  python3 board_etf.py --limit 1    # 只跑前 1 个雷达板块 (调试用)
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402

log = logging.getLogger("board_etf")


def compute_board_etf_rows(db_path: str, trade_date: str, board_codes) -> list[dict]:
    """纯 SQL 联表: 对盘面雷达板块集里每个板块, 用 fund_top_holdings 聚合候选场内 ETF,
    按 hit_weight 降序取 Top3.

    板块集 board_codes = 盘面雷达四面板并集(趋势主线/资金拥挤/潜在主线/上涨中继),
    不再只限 Top10 趋势主线.
    口径: 用板块【全部成分股】(stock_concept_map 最新快照) ∩ ETF 前十大重仓算命中权重.
    hit_count = 命中的成分股数. 数据源 = fund_top_holdings (正向, 季度刷新).

    返回每行包含 board_code/rank/fund_code/fund_name/hit_weight/hit_count/
    report_date/fund_scale 的 dict 列表 (已按 board_code, rank 排序).
    """
    codes = list(board_codes)
    if not codes:
        return []
    bph = ",".join("?" * len(codes))
    c = scout_db.conn(db_path)
    try:
        sql = f"""
        WITH agg AS (
            SELECT bt.board_code,
                   fth.fund_code,
                   em.fund_name,
                   SUM(fth.weight)            AS hit_weight,
                   COUNT(*)                    AS hit_count,
                   MAX(fth.report_date)        AS report_date,
                   em.fund_scale,
                   ROW_NUMBER() OVER (
                       PARTITION BY bt.board_code
                       ORDER BY SUM(fth.weight) DESC, fth.fund_code
                   ) AS rk
            FROM board_trend_daily bt
            JOIN stock_concept_map scm
              ON scm.board_code = bt.board_code
             AND scm.snapshot_date = (SELECT MAX(snapshot_date) FROM stock_concept_map)
            JOIN fund_top_holdings fth
              ON fth.stock_code = substr(scm.ts_code, 1, 6)
            JOIN etf_meta em
              ON em.fund_code = fth.fund_code
            WHERE bt.trade_date = ?
              AND bt.board_code IN ({bph})
              AND em.is_strict_etf = 1
            GROUP BY bt.board_code, fth.fund_code
        )
        SELECT board_code, rk AS rank, fund_code, fund_name,
               hit_weight, hit_count, report_date, fund_scale
        FROM agg
        WHERE rk <= 3
        ORDER BY board_code, rk
        """
        return [dict(r) for r in c.execute(sql, (trade_date, *codes))]
    finally:
        c.close()


_SHARE_SUFFIX_RE = re.compile(r"[ABCDEHIOR]$")


def _base_name(name: str) -> str:
    """剥掉基金名尾部份额后缀字母(A/C/E…), 用于主份额归并."""
    return _SHARE_SUFFIX_RE.sub("", name or "")


def _main_share_key(row: dict) -> tuple:
    """同 (company, base_name) 组内主份额优先级: 无后缀 > 'A' 后缀 > 最小 fund_code."""
    name = row.get("fund_name") or ""
    suffixless = 0 if _base_name(name) == name else 1
    not_a = 0 if name.endswith("A") else 1
    return (suffixless, not_a, row["fund_code"])


def compute_board_active_fund_rows(db_path: str, trade_date: str, board_codes,
                                   top_n: int = 3, min_hits: int = 2) -> list[dict]:
    """对盘面雷达板块集里每个板块, 用 fund_top_holdings 聚合主动权益基金:
    命中成分股数 >= min_hits, 主份额去重, 按 hit_weight 降序每板取 top_n.
    板块集 board_codes = 盘面雷达四面板并集(趋势主线/资金拥挤/潜在主线/上涨中继).
    口径: 用板块【全部成分股】(stock_concept_map 最新快照) ∩ 基金前十大重仓.
    数据源 = fund_top_holdings (fund_holdings_sync.py 正向全量, 季度刷新), 零 PIT 调用.
    注: 改用正向表后覆盖白酒/食品饮料等不上雷达的防御板块 (反查表漏抓其龙头)."""
    codes = list(board_codes)
    if not codes:
        return []
    bph = ",".join("?" * len(codes))
    c = scout_db.conn(db_path)
    try:
        sql = f"""
        SELECT bt.board_code,
               fth.fund_code,
               em.fund_name,
               em.company,
               SUM(fth.weight)      AS hit_weight,
               COUNT(*)             AS hit_count,
               MAX(fth.report_date) AS report_date,
               em.fund_scale
        FROM board_trend_daily bt
        JOIN stock_concept_map scm
          ON scm.board_code = bt.board_code
         AND scm.snapshot_date = (SELECT MAX(snapshot_date) FROM stock_concept_map)
        JOIN fund_top_holdings fth
          ON fth.stock_code = substr(scm.ts_code, 1, 6)
        JOIN etf_meta em
          ON em.fund_code = fth.fund_code
        WHERE bt.trade_date = ?
          AND bt.board_code IN ({bph})
          AND em.is_active_equity = 1
        GROUP BY bt.board_code, fth.fund_code
        HAVING COUNT(*) >= ?
        """
        raw = [dict(r) for r in c.execute(sql, (trade_date, *codes, min_hits))]
    finally:
        c.close()

    by_board: dict[str, list[dict]] = defaultdict(list)
    for r in raw:
        by_board[r["board_code"]].append(r)

    out: list[dict] = []
    for board_code, brows in by_board.items():
        groups: dict[tuple, dict] = {}
        for r in brows:
            key = (r.get("company") or "", _base_name(r.get("fund_name") or ""))
            cur = groups.get(key)
            if cur is None or _main_share_key(r) < _main_share_key(cur):
                groups[key] = r
        deduped = sorted(groups.values(), key=lambda r: -r["hit_weight"])
        for rank, r in enumerate(deduped[:top_n], 1):
            out.append({
                "board_code": board_code, "rank": rank,
                "fund_code": r["fund_code"], "fund_name": r["fund_name"],
                "hit_weight": round(r["hit_weight"], 1), "hit_count": r["hit_count"],
                "report_date": r["report_date"], "fund_scale": r["fund_scale"],
            })
    out.sort(key=lambda r: (r["board_code"], r["rank"]))
    return out


def match_funds_for_stocks(db_path: str, stock_codes, threshold: float = 80.0,
                           top_n: int = 3) -> dict:
    """组合选基金/ETF 核心: 给定一个【股票池】(裸 6 位), 算每只基金/ETF 在池上的
    持仓权重合计 (hit_weight, 占其净值 %), 主动/ETF 各成一组.

    每组返回规则: hit_weight > threshold 的【全部】; 若一只都不到, 返回该组 Top_n.
    份额去重: 同 (公司, 去后缀名) 组只留主份额 (复用 _main_share_key).

    返回 {pool_size, active:[...], etf:[...]}, 每项含
      fund_code/fund_name/company/hit_weight/hit_count/report_date/fund_scale/over_threshold.
    """
    pool = sorted({str(s).split(".")[0] for s in stock_codes if s})
    result = {"pool_size": len(pool), "active": [], "etf": []}
    if not pool:
        return result
    sph = ",".join("?" * len(pool))
    c = scout_db.conn(db_path)
    try:
        sql = f"""
        SELECT fth.fund_code, em.fund_name, em.company, em.fund_scale,
               em.is_active_equity, em.is_strict_etf,
               SUM(fth.weight)      AS hit_weight,
               COUNT(*)             AS hit_count,
               MAX(fth.report_date) AS report_date
        FROM fund_top_holdings fth
        JOIN etf_meta em ON em.fund_code = fth.fund_code
        WHERE fth.stock_code IN ({sph})
          AND (em.is_active_equity = 1 OR em.is_strict_etf = 1)
        GROUP BY fth.fund_code
        """
        raw = [dict(r) for r in c.execute(sql, pool)]
    finally:
        c.close()

    def _finalize(rows: list[dict]) -> list[dict]:
        # 份额去重: 同 (公司, base_name) 组取主份额
        groups: dict[tuple, dict] = {}
        for r in rows:
            key = (r.get("company") or "", _base_name(r.get("fund_name") or ""))
            cur = groups.get(key)
            if cur is None or _main_share_key(r) < _main_share_key(cur):
                groups[key] = r
        deduped = sorted(groups.values(), key=lambda r: -r["hit_weight"])
        over = [r for r in deduped if r["hit_weight"] > threshold]
        chosen = over if over else deduped[:top_n]
        return [{
            "fund_code": r["fund_code"], "fund_name": r["fund_name"],
            "company": r["company"], "hit_weight": round(r["hit_weight"], 1),
            "hit_count": r["hit_count"], "report_date": r["report_date"],
            "fund_scale": r["fund_scale"], "over_threshold": r["hit_weight"] > threshold,
        } for r in chosen]

    result["active"] = _finalize([r for r in raw if r["is_active_equity"] == 1])
    result["etf"] = _finalize([r for r in raw if r["is_strict_etf"] == 1])
    return result


def _board_stock_pool(c, board_codes, scope: str) -> list[str]:
    """按 scope 取板块股票池 (裸 6 位 DISTINCT 并集):
      'all'     → 板块全成分 (stock_concept_map 最新快照)
      'leader1' → 板块龙头 (board_leader_daily 最新日 rank<=1)
      'leader3' → 龙头+接力 (board_leader_daily 最新日 rank<=3)
    """
    if not board_codes:
        return []
    bph = ",".join("?" * len(board_codes))
    if scope in ("leader1", "leader3"):
        rmax = 1 if scope == "leader1" else 3
        rows = c.execute(
            f"SELECT DISTINCT substr(ts_code, 1, 6) FROM board_leader_daily "
            f"WHERE trade_date=(SELECT MAX(trade_date) FROM board_leader_daily) "
            f"AND board_code IN ({bph}) AND rank <= ?", [*board_codes, rmax])
    else:
        rows = c.execute(
            f"SELECT DISTINCT substr(ts_code, 1, 6) FROM stock_concept_map "
            f"WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map) "
            f"AND board_code IN ({bph})", list(board_codes))
    return [r[0] for r in rows]


def match_boards_funds(db_path: str, board_codes, threshold: float = 80.0,
                       top_n: int = 3, scope: str = "all") -> dict:
    """多板块组合选基金/ETF: 按 scope 取板块股票池 (全成分/龙头top1/龙头+接力top3),
    再交 match_funds_for_stocks 匹配. 并集去重 (同股同属多板块只算一次).
    返回 {pool_size, active, etf, scope}."""
    codes = list(board_codes)
    if not codes:
        return {"pool_size": 0, "active": [], "etf": [], "scope": scope}
    c = scout_db.conn(db_path)
    try:
        pool = _board_stock_pool(c, codes, scope)
    finally:
        c.close()
    out = match_funds_for_stocks(db_path, pool, threshold, top_n)
    out["scope"] = scope
    return out


def _research_board_codes() -> set:
    """研究映射板块: 读 fund.db industry_board_map 的 board_code 去重集合。只读。
    fund.db 不可达时抛异常, 由调用方 best-effort 兜底。"""
    import sqlite3
    import server
    conn = sqlite3.connect("file:" + server.RESEARCH_DB + "?mode=ro", uri=True, timeout=30)
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT board_code FROM industry_board_map")}
    finally:
        conn.close()


def _write_board_table(db_path: str, table: str, trade_date: str,
                       rows: list[dict]) -> None:
    """单事务幂等落库: 先删当日, 再批量插. board_etf_daily / board_active_fund_daily
    列结构一致, 共用此 helper."""
    c = scout_db.conn(db_path)
    try:
        c.execute("BEGIN")
        c.execute(f"DELETE FROM {table} WHERE trade_date=?", (trade_date,))
        if rows:
            c.executemany(
                f"INSERT INTO {table}"
                "(trade_date, board_code, rank, fund_code, fund_name, hit_weight, "
                "hit_count, report_date, fund_scale) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(trade_date, r["board_code"], r["rank"], r["fund_code"],
                  r["fund_name"], r["hit_weight"], r["hit_count"],
                  r["report_date"], r["fund_scale"]) for r in rows])
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def run(db_path: str, trade_date: str, limit: int | None = None) -> int:
    """从正向表 fund_top_holdings 聚合两张对标表 (board_etf_daily 场内ETF +
    board_active_fund_daily 主动基金). 纯本地 SQL, 零网络; 持仓由
    fund_holdings_sync.py 季度刷新. 返回 rc (0=正常)."""
    scout_db.init_schema(db_path)
    start = time.time()

    # 盘面雷达四面板板块并集(趋势主线/资金拥挤/潜在主线/上涨中继), 与看板展示口径一致
    import server  # noqa: E402  延迟导入, 避免模块级耦合
    rc = scout_db.conn(db_path)
    try:
        radar_codes = server.radar_board_codes(rc, trade_date)
    finally:
        rc.close()
    if not radar_codes:
        log.warning("没有盘面雷达板块 (trade_date=%s) — 跳过", trade_date)
        return 0

    # 聚合扩到全白名单 (成分股跨板块重叠, 白名单板块都能从正向表出对标基金/ETF)
    server.WHITELIST.load_if_changed()
    compute_codes = (set(radar_codes) if limit
                     else set(radar_codes) | server.WHITELIST.codes())
    if limit:
        compute_codes = set(sorted(compute_codes)[:limit])
    if not limit:
        try:
            research_codes = _research_board_codes()
            compute_codes |= research_codes
            log.info("并入研究映射板块 %d 个", len(research_codes))
        except Exception as e:
            log.warning("读 industry_board_map 失败, 跳过研究板块扩集: %s", e)
    log.info("聚合板块 %d 个 (radar %d + 白名单)", len(compute_codes), len(radar_codes))

    rows = compute_board_etf_rows(db_path, trade_date, compute_codes)
    log.info("对标ETF聚合得 %d 行 (%d 板块)", len(rows), len({r["board_code"] for r in rows}))
    _write_board_table(db_path, "board_etf_daily", trade_date, rows)

    af_rows = compute_board_active_fund_rows(db_path, trade_date, compute_codes)
    log.info("主动基金聚合得 %d 行 (%d 板块)",
             len(af_rows), len({r["board_code"] for r in af_rows}))
    _write_board_table(db_path, "board_active_fund_daily", trade_date, af_rows)

    log.info("完成: 对标ETF %d 行 + 主动基金 %d 行, 耗时 %.1fs",
             len(rows), len(af_rows), time.time() - start)
    return 0


def _latest_trade_date(db_path: str) -> str | None:
    c = scout_db.conn(db_path)
    try:
        r = c.execute(
            "SELECT MAX(trade_date) FROM board_leader_daily").fetchone()
        return r[0]
    finally:
        c.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD, 默认最新 board_leader_daily 日期")
    ap.add_argument("--limit", type=int, help="只处理前 N 个雷达板块 (调试用)")
    ap.add_argument("--db", default=scout_db.DB_PATH)
    args = ap.parse_args()
    date = args.date or _latest_trade_date(args.db)
    if not date:
        log.error("board_leader_daily 无数据, 退出")
        sys.exit(1)
    sys.exit(run(args.db, date, args.limit))


if __name__ == "__main__":
    main()
