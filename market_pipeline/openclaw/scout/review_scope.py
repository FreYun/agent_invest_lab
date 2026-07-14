"""重点池收集器 — 给 bot11 盘中点评挑选个股, 输出 JSON.

重点池 = 已触发候选(triggered=1) + S8 实时在池(still_candidate=1) + 趋势主线龙头(top-N),
按 code 去重(候选源优先于 trend), 硬上限 cap 控 token. 复用 candidate_logic 的题材/纪要做研究起点.

用法:
  python3 review_scope.py            # 今日重点池 JSON 到 stdout
  python3 review_scope.py --cap 20   # 调上限
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402


def triggered(c, today):
    return c.execute(
        "SELECT strategy, code, ts_code, name FROM intraday_trigger_log "
        "WHERE trade_date=? AND triggered=1", (today,)).fetchall()


def s8_live(c, today):
    return c.execute(
        "SELECT code, ts_code, name FROM intraday_trigger_log "
        "WHERE trade_date=? AND strategy='s8' AND still_candidate=1", (today,)).fetchall()


def trend_leads(c, top_n=8):
    d = c.execute("SELECT MAX(trade_date) FROM board_trend_daily").fetchone()[0]
    if not d:
        return []
    return c.execute(
        "SELECT lead_code, lead_name FROM board_trend_daily "
        "WHERE trade_date=? AND rank<=? AND lead_code IS NOT NULL ORDER BY rank",
        (d, top_n)).fetchall()


def board_live(c):
    """最新板块快照 board_code -> live_pct."""
    snap = c.execute("SELECT MAX(snapshot_time) FROM intraday_board").fetchone()[0]
    out = {}
    if not snap:
        return out
    for r in c.execute(
        "SELECT board_code, live_pct FROM intraday_board WHERE snapshot_time=?", (snap,)):
        out[r["board_code"]] = r["live_pct"]
    return out


def board_leads(c, board_codes, top=5):
    """board_code -> 今日领涨 top [{name,pct}], 取自 intraday_board_members.lead_json."""
    if not board_codes:
        return {}
    ph = ",".join("?" * len(board_codes))
    out = {}
    for bc, lj in c.execute(
        f"SELECT board_code, lead_json FROM intraday_board_members "
        f"WHERE board_code IN ({ph})", list(board_codes)):
        arr = json.loads(lj or "[]")[:top]
        out[bc] = [{"name": s.get("n"), "pct": s.get("pct")} for s in arr]
    return out


def build_board_scope(c, today, top_n=10):
    """趋势主线 top-N 板块上下文 [dict], 供小奶龙深度点评. 无 board_trend_daily -> []."""
    d = c.execute("SELECT MAX(trade_date) FROM board_trend_daily").fetchone()[0]
    if not d:
        return []
    rows = [dict(r) for r in c.execute(
        "SELECT board_code,board_name,trend_score,ret20,ret60,vol_ratio,amt_share,"
        "ma_aligned,lead_code,lead_name,rank FROM board_trend_daily "
        "WHERE trade_date=? ORDER BY rank LIMIT ?", (d, top_n))]
    live = board_live(c)
    leads = board_leads(c, [r["board_code"] for r in rows])
    for r in rows:
        r["live_pct"] = live.get(r["board_code"])
        r["lead_stocks"] = leads.get(r["board_code"], [])
    return rows


def live_context(c):
    """最新快照的 code -> {price,pct,signal,top_theme,top_theme_pct}.

    strategy='hold' 行是用户持仓的现价展示伪策略，不纳入点评上下文。
    """
    snap = c.execute("SELECT MAX(snapshot_time) FROM intraday_candidate_live").fetchone()[0]
    out = {}
    if not snap:
        return out
    for r in c.execute(
        "SELECT code, price, pct, signal, top_theme, top_theme_pct "
        "FROM intraday_candidate_live WHERE snapshot_time=? AND strategy != 'hold'", (snap,)):
        out[scout_db.bare(r["code"])] = {
            "price": r["price"], "pct": r["pct"], "signal": r["signal"],
            "top_theme": r["top_theme"], "top_theme_pct": r["top_theme_pct"]}
    return out


def logic_context(c):
    """code -> {themes, zsxq}, 同 code 多策略行时优先非空的一行."""
    out = {}
    for r in c.execute("SELECT code, themes_json, zsxq_json FROM candidate_logic"):
        code = scout_db.bare(r["code"])
        themes = json.loads(r["themes_json"] or "[]")
        zsxq = json.loads(r["zsxq_json"] or "[]")
        cur = out.get(code)
        if cur is None or (not cur["themes"] and not cur["zsxq"] and (themes or zsxq)):
            out[code] = {"themes": themes, "zsxq": zsxq}
    return out


# 盘前候选 = 日级策略 S1-S7 + S8(锚定昨收).
# 2026-06-02 改: S8 收盘后日线版(s8/select.py)在合适市场结构里出 ~15 条/天,接进 premarket。
# 盘中 s8_live.py 仍走 intraday_trigger_log (still_candidate=1) 不受这里影响,两条并行。
# S8 验证: s8/select.py 收盘后口径回测 buy_divergence 路径 net +0.91%/笔,见 final_report §3.1。
PREMARKET_STRATEGIES = tuple(scout_db.STRATEGIES)


def premarket_scope(c, anchor, cap=30):
    """盘前重点池 [dict] — 锚定昨收的 S1-S7 全量去重, 注入买点计划 + 题材纪要.

    输出每只:
      {code, name, ts_code, sources:[strategy,...],
       t_close, entry_zone_low, entry_zone_high, stop_loss_price,
       position_pct, signal_score, themes, zsxq}

    不注入实时上下文(price/pct/signal) — 盘前快照都是昨日,意义不大.
    """
    if not anchor:
        return []
    order, rec = [], {}
    # ── 技术策略 S1-S8: 带买点列(entry_zone/stop_loss/signal_score) ──
    # S9 是因子选股, 无日内买点列, 单独处理(见下), 不进此循环。
    for s in PREMARKET_STRATEGIES:
        if s == "s9":
            continue
        # s8_candidates schema 用资金/技术字段, 无 t_close 列, 用 NULL 兜底
        tclose_expr = "NULL" if s == "s8" else "t_close"
        try:
            rows = c.execute(
                f"SELECT code, name, {tclose_expr} AS t_close, "
                f"entry_zone_low, entry_zone_high, "
                f"stop_loss_price, position_pct, signal_score "
                f"FROM {s}_candidates WHERE date=?", (anchor,)).fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            code = scout_db.bare(r["code"])
            if code not in rec:
                rec[code] = {
                    "code": code,
                    "name": r["name"],
                    "ts_code": scout_db.to_suffix(code),
                    "sources": [],
                    "t_close": r["t_close"],
                    "entry_zone_low": r["entry_zone_low"],
                    "entry_zone_high": r["entry_zone_high"],
                    "stop_loss_price": r["stop_loss_price"],
                    "position_pct": r["position_pct"],
                    "signal_score": r["signal_score"],
                }
                order.append(code)
            if r["name"] and not rec[code].get("name"):
                rec[code]["name"] = r["name"]
            if s not in rec[code]["sources"]:
                rec[code]["sources"].append(s)
    tech_codes = order[:cap]  # 技术票按 cap 截断

    # ── S9 因子票: 无买点列(全 NULL), 注入因子元数据(s9_factor)。
    # 不占技术 cap 名额 → top20 因子票全部追加, 去重保留技术买点。 ──
    s9_order = []
    try:
        s9_rows = c.execute(
            "SELECT code, name, sleeve_label, factor_name, factor_value, "
            "regime, total_mv_yi, pe_ttm, reason "
            "FROM s9_candidates WHERE date=? ORDER BY rank", (anchor,)).fetchall()
    except sqlite3.OperationalError:
        s9_rows = []
    for r in s9_rows:
        code = scout_db.bare(r["code"])
        if code not in rec:
            rec[code] = {
                "code": code,
                "name": r["name"],
                "ts_code": scout_db.to_suffix(code),
                "sources": [],
                "t_close": None, "entry_zone_low": None, "entry_zone_high": None,
                "stop_loss_price": None, "position_pct": None, "signal_score": None,
            }
            s9_order.append(code)
        if r["name"] and not rec[code].get("name"):
            rec[code]["name"] = r["name"]
        if "s9" not in rec[code]["sources"]:
            rec[code]["sources"].append("s9")
        # 因子元数据(技术票没有这些字段; 月级因子选股, bot 据此评因子逻辑/位置, 不评日内买点)
        rec[code]["s9_factor"] = {
            "sleeve": r["sleeve_label"], "factor_name": r["factor_name"],
            "factor_value": r["factor_value"], "regime": r["regime"],
            "total_mv_yi": r["total_mv_yi"], "pe_ttm": r["pe_ttm"],
            "reason": r["reason"],
        }

    codes = tech_codes + [x for x in s9_order if x not in tech_codes]
    lg = logic_context(c)
    out = []
    for code in codes:
        d = rec[code]
        g = lg.get(code, {})
        d["themes"] = g.get("themes", [])
        d["zsxq"] = g.get("zsxq", [])
        out.append(d)
    return out


def build_scope(c, today, cap=30, top_n=8):
    """重点池 [dict] — 去重 + 上限 + 注入行情/题材上下文."""
    order, rec = [], {}

    def add(code, name, ts_code, src):
        code = scout_db.bare(code)
        if code not in rec:
            rec[code] = {"code": code, "name": name,
                         "ts_code": ts_code or scout_db.to_suffix(code), "sources": []}
            order.append(code)
        if name and not rec[code].get("name"):
            rec[code]["name"] = name
        if src not in rec[code]["sources"]:
            rec[code]["sources"].append(src)

    for r in triggered(c, today):
        add(r["code"], r["name"], r["ts_code"], r["strategy"])
    for r in s8_live(c, today):
        add(r["code"], r["name"], r["ts_code"], "s8")
    for r in trend_leads(c, top_n):
        add(r["lead_code"], r["lead_name"], None, "trend")

    codes = order[:cap]
    live, lg = live_context(c), logic_context(c)
    out = []
    for code in codes:
        d = rec[code]
        d.update(live.get(code, {"price": None, "pct": None, "signal": None,
                                 "top_theme": None, "top_theme_pct": None}))
        g = lg.get(code, {})
        d["themes"] = g.get("themes", [])
        d["zsxq"] = g.get("zsxq", [])
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("premarket", "intraday"), default="intraday",
                    help="premarket=盘前 S1-S7 锚定全量; intraday=盘中实时(默认)")
    ap.add_argument("--cap", type=int, default=30, help="重点池硬上限")
    ap.add_argument("--top-n", type=int, default=8, help="趋势主线取前 N 板龙头(仅 intraday)")
    ap.add_argument("--board-top-n", type=int, default=10,
                    help="板块深度点评取前 N 板(仅 intraday)")
    ap.add_argument("--date", help="交易日 YYYY-MM-DD(默认今日)")
    a = ap.parse_args()
    today = a.date or datetime.now().strftime("%Y-%m-%d")
    scout_db.init_schema()
    c = scout_db.conn()
    if a.mode == "premarket":
        anchor = scout_db.anchor_date(c, today)
        items = premarket_scope(c, anchor, cap=a.cap)
        c.close()
        print(json.dumps({"trade_date": today, "mode": "premarket",
                          "anchor": anchor, "count": len(items),
                          "items": items},
                         ensure_ascii=False, indent=2))
    else:
        items = build_scope(c, today, cap=a.cap, top_n=a.top_n)
        boards = build_board_scope(c, today, top_n=a.board_top_n)
        c.close()
        print(json.dumps({"trade_date": today, "mode": "intraday",
                          "count": len(items), "items": items, "boards": boards},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
