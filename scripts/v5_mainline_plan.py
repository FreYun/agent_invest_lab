#!/usr/bin/env python3
"""Deterministic v5 mainline plan used by bot101/report-mainline.

This is a repo-local reproduction of the v5 backtest source:
  - /home/rooot/.openclaw/scout/board_trend_daily
  - /home/rooot/.openclaw/scout/coarse_themes.json
  - HS300 close vs MA120 from index_daily

It intentionally does not use simworld_data sector_* tables. The output is JSON so
strategy-server can expose it as the authoritative MCP data source.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
from collections import Counter, OrderedDict
from typing import Any


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCOUT_DB = os.environ.get("SCOUT_DB", os.path.join(ROOT_DIR, "data", "market.db"))
COARSE_THEMES = os.environ.get("COARSE_THEMES", os.path.join(ROOT_DIR, "market_pipeline", "openclaw", "scout", "coarse_themes.json"))
SCOUT_DIR = os.environ.get("SCOUT_DIR", os.path.join(ROOT_DIR, "market_pipeline", "openclaw", "scout"))
START_DATE = "20250101"

K_ENTRY = 5
TOPK_EXIT = 15
CONSEC_EXIT = 2
SAT_N = 3
MAXHOLD = 5


def ymd(s: str) -> str:
    return s.replace("-", "")[:8]


def dashed(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{SCOUT_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def load_groups() -> dict[str, str]:
    with open(COARSE_THEMES, "r", encoding="utf-8") as f:
        themes = json.load(f)
    return {b["code"]: g["name"] for g in themes.get("groups", []) for b in g.get("boards", [])}


def month_starts(c: sqlite3.Connection, asof: str) -> list[str]:
    rows = c.execute(
        "SELECT DISTINCT trade_date FROM board_trend_daily "
        "WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date",
        (START_DATE, asof),
    ).fetchall()
    months: OrderedDict[str, str] = OrderedDict()
    for r in rows:
        d = r["trade_date"]
        months.setdefault(d[:6], d)
    return list(months.values())


def snapshot(c: sqlite3.Connection, trade_date: str) -> dict[str, sqlite3.Row]:
    rows = c.execute(
        "SELECT board_code, board_name, rank, above_ma60, ret60, ret20, ret5, trend_score "
        "FROM board_trend_daily WHERE trade_date=?",
        (trade_date,),
    ).fetchall()
    return {r["board_code"]: r for r in rows}


def top_boards(c: sqlite3.Connection, trade_date: str, limit: int = 15) -> list[dict[str, Any]]:
    rows = c.execute(
        "SELECT board_code, board_name, rank, above_ma60, ret60, ret20, ret5, trend_score "
        "FROM board_trend_daily WHERE trade_date=? AND rank<=? ORDER BY rank",
        (trade_date, limit),
    ).fetchall()
    b2g = load_groups()
    return [
        {
            "code": r["board_code"],
            "name": r["board_name"],
            "rank": r["rank"],
            "group": b2g.get(r["board_code"], "其他"),
            "above_ma60": bool(r["above_ma60"]),
            "ret60": r["ret60"],
            "ret20": r["ret20"],
            "ret5": r["ret5"],
            "trend_score": r["trend_score"],
        }
        for r in rows
    ]


def hs300_vs_ma120(c: sqlite3.Connection, trade_date: str) -> float | None:
    rows = c.execute(
        "SELECT close FROM index_daily WHERE ts_code='000300.SH' AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 120",
        (trade_date,),
    ).fetchall()
    if len(rows) < 120:
        return None
    closes = [float(r["close"]) for r in rows]
    return closes[0] / (sum(closes) / len(closes)) - 1


def concentration(top15: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(x["group"] for x in top15)
    dom, n = counts.most_common(1)[0] if counts else ("无", 0)
    return {
        "dominant_group": dom,
        "dominant_count": n,
        "counts": dict(counts),
    }


def regime(c: sqlite3.Connection, trade_date: str, top15: list[dict[str, Any]]) -> dict[str, Any]:
    dist = hs300_vs_ma120(c, trade_date)
    conc = concentration(top15)
    if dist is None:
        name = "数据不足"
    elif dist < -0.03:
        name = "防御·红利"
    elif conc["dominant_count"] >= 4:
        name = "抱主线·v4"
    else:
        name = "无主线·宽基"
    return {
        "name": name,
        "hs300_vs_ma120": dist,
        "concentration": conc,
    }


def run_v4_state(c: sqlite3.Connection, months: list[str]) -> list[dict[str, Any]]:
    cores: dict[str, dict[str, Any]] = {}
    prev_top5: set[str] = set()
    plan: list[dict[str, Any]] = []

    for idx, trade_date in enumerate(months):
        snap = snapshot(c, trade_date)
        ranks = {bc: r["rank"] for bc, r in snap.items()}
        top5 = {bc for bc, r in snap.items() if r["rank"] and r["rank"] <= K_ENTRY}
        top3 = [bc for bc, _ in sorted(snap.items(), key=lambda x: x[1]["rank"] or 999)[:SAT_N]]

        for bc in list(cores):
            row = snap.get(bc)
            below60 = (row["above_ma60"] != 1) if row else True
            out15 = ranks.get(bc, 999) > TOPK_EXIT
            if below60:
                del cores[bc]
            elif out15:
                cores[bc]["out"] += 1
                if cores[bc]["out"] >= CONSEC_EXIT:
                    del cores[bc]
            else:
                cores[bc]["out"] = 0

        if idx >= 1:
            for bc in top5:
                if bc in prev_top5 and snap[bc]["above_ma60"] == 1 and bc not in cores:
                    cores[bc] = {"out": 0, "since": trade_date, "name": snap[bc]["board_name"]}
        prev_top5 = top5

        satellites = [bc for bc in top3 if bc not in cores]
        holdings = []
        for bc, meta in cores.items():
            row = snap.get(bc)
            holdings.append({
                "code": bc,
                "name": meta["name"],
                "role": "核心",
                "rank": row["rank"] if row else None,
                "above_ma60": bool(row["above_ma60"]) if row else False,
                "since": meta["since"],
                "out_count": meta["out"],
            })
        for bc in satellites:
            row = snap[bc]
            holdings.append({
                "code": bc,
                "name": row["board_name"],
                "role": "卫星",
                "rank": row["rank"],
                "above_ma60": bool(row["above_ma60"]),
                "since": trade_date,
                "out_count": 0,
            })
        plan.append({"trade_date": trade_date, "holdings": holdings[:MAXHOLD]})
    return plan


def normalize_fund_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": row.get("fund"),
        "name": row.get("name"),
        "index_code": row.get("index_code"),
        "index_name": row.get("index_name"),
        "verdict": row.get("verdict"),
        "overlap": row.get("ov_overlap_ratio"),
        "overlap_weight": row.get("ov_overlap_weight"),
        "common_constituents": row.get("ov_n_common"),
        "index_constituents": row.get("ov_n_index"),
        "corr": row.get("corr"),
        "beta": row.get("beta"),
        "r2": row.get("r2"),
        "corr_n": row.get("corr_n"),
        "scale_yi": row.get("scale"),
        "pool_tier": row.get("tier"),
    }


def fund_matches(holdings: list[dict[str, Any]], trade_date: str) -> list[dict[str, Any]]:
    if SCOUT_DIR not in sys.path:
        sys.path.insert(0, SCOUT_DIR)
    import board_fund_match

    investable = {"✓纯载体", "○代理载体", "△弱代理", "▽兜底代理"}
    out: list[dict[str, Any]] = []
    for h in holdings:
        board_code = h.get("code")
        if not board_code or not str(board_code).endswith(".DC"):
            continue
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rows = board_fund_match.run(board_code, trade_date, 0.10)
        finally:
            sys.stdout = old_stdout
        selected_rows = [r for r in rows if r.get("verdict") in investable]
        selected = normalize_fund_row(selected_rows[0]) if selected_rows else None
        out.append({
            "board_code": board_code,
            "board_name": h.get("name"),
            "role": h.get("role"),
            "selected": selected,
            "candidates": [normalize_fund_row(r) for r in selected_rows[:5]],
        })
    return out


def build(asof: str, include_funds: bool = False) -> dict[str, Any]:
    asof = ymd(asof)
    c = conn()
    months = month_starts(c, asof)
    if not months:
        raise SystemExit(f"no board_trend_daily month start between {START_DATE} and {asof}")
    decision = months[-1]
    top15 = top_boards(c, decision, 15)
    states = run_v4_state(c, months)
    v4_holdings = states[-1]["holdings"]
    rg = regime(c, decision, top15)
    if rg["name"].startswith("抱主线"):
        portfolio = v4_holdings
    elif rg["name"].startswith("防御"):
        # 场外C载体（不买场内ETF）：012762=华泰柏瑞上证红利ETF联接C，即 510880 同标的联接C
        portfolio = [{"code": "012762", "name": "上证红利ETF联接C", "role": "防御"}]
    elif rg["name"].startswith("无主线"):
        # 场外C载体：007339=易方达沪深300ETF联接C（规模最大，替代 510300）
        portfolio = [{"code": "007339", "name": "沪深300ETF联接C", "role": "宽基"}]
    else:
        portfolio = []
    out = {
        "source": {
            "name": "v5_mainline_scout",
            "scout_db": SCOUT_DB,
            "coarse_themes": COARSE_THEMES,
            "fund_match_engine": f"{SCOUT_DIR}/board_fund_match.py",
            "forbidden_for_mainline_truth": ["simworld_data.sector_search", "simworld_data.sector_factor", "simworld_data.sector_market"],
        },
        "as_of_date": dashed(asof),
        "decision_trade_date": dashed(decision),
        "decision_trade_date_raw": decision,
        "regime": rg,
        "top15": top15,
        "v4_holdings": v4_holdings,
        "portfolio": portfolio,
        "state_history": states,
    }
    if include_funds and rg["name"].startswith("抱主线"):
        out["fund_matches"] = fund_matches(v4_holdings, decision)
    elif include_funds:
        out["fund_matches"] = []
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="as-of date, YYYY-MM-DD or YYYYMMDD")
    ap.add_argument("--compact", action="store_true", help="omit state_history")
    ap.add_argument("--include-funds", action="store_true", help="include v5 scout board_fund_match results")
    args = ap.parse_args()
    out = build(args.date, include_funds=args.include_funds)
    if args.compact:
        out.pop("state_history", None)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
