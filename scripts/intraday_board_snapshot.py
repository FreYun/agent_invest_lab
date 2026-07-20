#!/usr/bin/env python3
"""盘中板块实时快照（方案 B+，仅 48080 看板日度主线卡用）。

给两张日度主线卡（market_mainline_daily / mainline_rotation_daily）叠加「今日盘中实时」
视角：取当日全部板块的实时行情，按盘中动量重排 top15，与 T-1 的确定性状态机骨架
（核心/卫星）并列对比、高亮今日异动板块。

**只显示、不喂引擎**：不改写 board_trend_daily、不动 mainline_daily_plan.py 的状态机记忆，
因此没有盘中/盘后口径漂移的风险，也没有未来函数（realtime 只读当日、不入库）。

数据源：
  - scout market.db  board_trend_daily：板块全集 + T-1 rank（排名参照系）
  - fund.db          market_reports(market_mainline_daily)：T-1 核心/卫星骨架
  - ttjj 实时接口     market_realtime_quote(90.BKxxxx)：当日板块实时行情（批量 ≤20，串行）

用法：
  intraday_board_snapshot.py --date YYYY-MM-DD [--fund-db PATH] [--scout-db PATH] [--top 15]

输出：单个 JSON（applicable=false 时带 reason）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# 板块实时接口单次上限实测 ~20；必须串行（上游会 reset，并发是伪失败）。
BATCH = 20
BATCH_SLEEP = 0.3
ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FUND_DB = "/home/rooot/agent_invest_lab/data/fund.db"
DEFAULT_SCOUT_DB = os.environ.get("SCOUT_DB", str(ROOT_DIR / "data" / "market.db"))


def _today_iso() -> str:
    n = _dt.datetime.now()
    return f"{n.year:04d}-{n.month:02d}-{n.day:02d}"


def _num(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().rstrip("%")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _board_secid(board_code: str) -> str:
    # "BK1326.DC" -> "90.BK1326"
    return "90." + board_code.split(".")[0]


def _load_board_universe(scout_db: str, date_dash: str) -> tuple[str, dict[str, dict]]:
    """返回 (prev_date_dash, {board_code: {name, rank}})，取 ≤date 的最新交易日。"""
    ymd = date_dash.replace("-", "")
    con = sqlite3.connect(f"file:{scout_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT MAX(trade_date) AS d FROM board_trend_daily WHERE trade_date <= ?", (ymd,)
    ).fetchone()
    prev = row["d"] if row else None
    if not prev:
        return "", {}
    rows = con.execute(
        "SELECT board_code, board_name, rank FROM board_trend_daily WHERE trade_date = ?", (prev,)
    ).fetchall()
    con.close()
    uni = {r["board_code"]: {"name": r["board_name"], "rank": r["rank"]} for r in rows}
    return f"{prev[:4]}-{prev[4:6]}-{prev[6:]}", uni


def _load_skeleton(fund_db: str, date_dash: str) -> dict | None:
    """取 ≤date 最新一期 market_mainline_daily 的结构化骨架。"""
    con = sqlite3.connect(f"file:{fund_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT as_of_date, structured_json FROM market_reports "
        "WHERE report_type='market_mainline_daily' AND scope='global' AND as_of_date <= ? "
        "ORDER BY as_of_date DESC LIMIT 1",
        (date_dash,),
    ).fetchone()
    con.close()
    if not row or not row["structured_json"]:
        return None
    try:
        j = json.loads(row["structured_json"])
    except (ValueError, TypeError):
        return None
    j["_as_of"] = row["as_of_date"]
    return j


def _fetch_quotes(date_dash: str, secids: list[str]) -> tuple[dict[str, dict], str]:
    """串行分批取实时行情，返回 ({secid: item}, quote_time)。"""
    import os

    # ttjj_data_pit_mcp 在 repo 根，而脚本从 scripts/ 启动时 sys.path[0]=scripts/ → 手动补 repo 根
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import ttjj_data_pit_mcp as m  # 延迟导入：无行情需求时不加载

    out: dict[str, dict] = {}
    qtime = ""
    for i in range(0, len(secids), BATCH):
        chunk = secids[i : i + BATCH]
        r = m.market_realtime_quote(date_dash, chunk, include=["quote"], raw=False, timeout=15)
        if not r.get("success"):
            continue
        for it in r.get("items") or []:
            sid = it.get("secid") or it.get("输入代码")
            if sid:
                out[str(sid)] = it
                if not qtime and it.get("行情时间"):
                    qtime = str(it.get("行情时间"))
        time.sleep(BATCH_SLEEP)
    return out, qtime


def build_snapshot(date_dash: str, fund_db: str, scout_db: str, top: int) -> dict:
    if date_dash != _today_iso():
        return {"applicable": False, "reason": "not_today", "date": date_dash}

    prev_date, universe = _load_board_universe(scout_db, date_dash)
    if not universe:
        return {"applicable": False, "reason": "no_board_universe", "date": date_dash}

    skeleton = _load_skeleton(fund_db, date_dash)
    holdings = (skeleton or {}).get("holdings") or []
    skel_codes = {h.get("code") for h in holdings}

    secid_to_code = {_board_secid(c): c for c in universe}
    quotes, qtime = _fetch_quotes(date_dash, list(secid_to_code.keys()))

    # 汇总每个板块的盘中行情
    boards: list[dict] = []
    for code, meta in universe.items():
        q = quotes.get(_board_secid(code))
        if not q:
            continue
        boards.append(
            {
                "code": code,
                "name": meta["name"],
                "prev_rank": meta["rank"],
                "chg_pct": _num(q.get("涨跌幅")),
                "chg5": _num(q.get("5日涨跌幅")),
                "chg20": _num(q.get("20日涨跌幅")),
                "vol_ratio": _num(q.get("量比")),
            }
        )

    # 盘中排名 = 按当日涨跌幅降序（盘中动量口径，非引擎 trend_score）
    ranked = sorted(boards, key=lambda b: (b["chg_pct"] is None, -(b["chg_pct"] or -1e9)))
    for i, b in enumerate(ranked, 1):
        b["intraday_rank"] = i
    by_code = {b["code"]: b for b in ranked}

    # 骨架板块的盘中表现（保持骨架顺序：核心在前）
    role_order = {"核心": 0, "卫星": 1}
    skel_view = []
    for h in sorted(holdings, key=lambda h: role_order.get(h.get("role"), 9)):
        b = by_code.get(h.get("code"))
        if not b:
            continue
        skel_view.append(
            {
                "code": h.get("code"),
                "name": h.get("name") or b["name"],
                "role": h.get("role"),
                "prev_rank": h.get("rank", b["prev_rank"]),
                "chg_pct": b["chg_pct"],
                "chg5": b["chg5"],
                "chg20": b["chg20"],
                "vol_ratio": b["vol_ratio"],
                "intraday_rank": b["intraday_rank"],
            }
        )

    # 今日盘中异动 = 盘中 top-N 里、不在骨架的板块
    movers = [
        {
            "code": b["code"],
            "name": b["name"],
            "prev_rank": b["prev_rank"],
            "chg_pct": b["chg_pct"],
            "chg5": b["chg5"],
            "intraday_rank": b["intraday_rank"],
        }
        for b in ranked[:top]
        if b["code"] not in skel_codes
    ]

    return {
        "applicable": True,
        "date": date_dash,
        "prev_date": prev_date,
        "skeleton_as_of": (skeleton or {}).get("_as_of", prev_date),
        "quote_time": qtime,
        "regime": (skeleton or {}).get("regime"),
        "regime_days": (skeleton or {}).get("regime_days"),
        "mainline_theme": (skeleton or {}).get("mainline_theme"),
        "coverage": f"{len(boards)}/{len(universe)}",
        "top": top,
        "note": "盘中动量口径（当日涨跌幅排名），非引擎 trend_score；仅提示今日板块异动，不改变状态机骨架与可投池。",
        "skeleton": skel_view,
        "new_movers": movers,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")  # 空 = 服务端今日（盘中快照只对「今天」有意义）
    ap.add_argument("--fund-db", default=DEFAULT_FUND_DB)
    ap.add_argument("--scout-db", default=DEFAULT_SCOUT_DB)
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    date = a.date or _today_iso()
    try:
        snap = build_snapshot(date, a.fund_db, a.scout_db, a.top)
    except Exception as e:  # noqa: BLE001 — 看板兜底：任何异常都回 applicable=false，不让卡片报错
        snap = {"applicable": False, "reason": f"{type(e).__name__}: {e}", "date": date}
    json.dump(snap, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
