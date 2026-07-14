"""scout/research_overlay.py — res 行业研究 → scout 个股/板块 下游贴标桥.

把 lab fund.db 的 research_note(行业粒度) 经 industry_board_map + market.db 的
stock_concept_map 映射到 scout 候选个股/板块, 产出"行业研究立场"overlay, 供
board 点评 / pick 深研 cron 在 prompt 里确定性引用(软提示+降权, 不硬过滤).

只读两库. lab fund.db 走 ?mode=ro 保护主库. 任一库不可用 -> fail-soft 返回空覆盖.

环境变量(单测/部署覆盖用):
- SCOUT_DB_PATH:          覆盖 market.db 路径
- SCOUT_RESEARCH_DB_PATH: 覆盖 lab fund.db 路径
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402

LAB_DB_DEFAULT = "/home/rooot/agent_invest_lab/data/fund.db"
_RATING_RE = re.compile(r"^(看多|中性|看空)")


def market_db() -> str:
    return os.environ.get("SCOUT_DB_PATH", scout_db.DB_PATH)


def lab_db() -> str:
    return os.environ.get("SCOUT_RESEARCH_DB_PATH", LAB_DB_DEFAULT)


def _stance(rating: str) -> str:
    m = _RATING_RE.match((rating or "").strip())
    return m.group(1) if m else "中性"


def _semantics(note: dict) -> tuple[int, bool]:
    """软语义: 看空/过期/qc-fail -> (-1, True); 其余 (0, False). 绝不给正向."""
    bad = (note["stance"] == "看空" or note["expired"] or note["qc_status"] == "fail")
    return (-1 if bad else 0, bool(bad))


def _load_research(now_str: str):
    """读 lab fund.db. 返回 (notes_by_ind, board_to_inds, board_name) 或 None(fail-soft).

    notes_by_ind: {industry_id: {industry_id, industry, stance, rating_raw,
                                 valid_until, expired, qc_status, snippet}}
    board_to_inds: {board_code: [(industry_id, weight, source)] 按 weight 降序/manual 优先}
    board_name:   {board_code: board_name}
    """
    try:
        c = sqlite3.connect("file:" + lab_db() + "?mode=ro", uri=True, timeout=30)
    except Exception:
        return None
    try:
        c.row_factory = sqlite3.Row
        notes = {}
        for r in c.execute(
            "SELECT n.industry_id AS iid, i.name AS industry, n.rating, n.valid_until, "
            "n.qc_status, n.conclusion "
            "FROM research_note n JOIN industry i ON i.id=n.industry_id "
            "WHERE n.id IN (SELECT MAX(id) FROM research_note GROUP BY industry_id)"
        ):
            vu = r["valid_until"]
            vu_cmp = (str(vu) + " 23:59:59") if vu and len(str(vu)) == 10 else str(vu)
            notes[r["iid"]] = {
                "industry_id": r["iid"], "industry": r["industry"],
                "stance": _stance(r["rating"]), "rating_raw": r["rating"] or "",
                "valid_until": vu, "expired": bool(vu) and vu_cmp < now_str,
                "qc_status": r["qc_status"], "snippet": (r["conclusion"] or "")[:40],
            }
        board_to_inds, board_name = {}, {}
        for r in c.execute(
            "SELECT industry_id, board_code, board_name, weight, source FROM industry_board_map"
        ):
            board_to_inds.setdefault(r["board_code"], []).append(
                (r["industry_id"], r["weight"] or 0.0, r["source"] or ""))
            board_name[r["board_code"]] = r["board_name"]
        for bc in board_to_inds:
            board_to_inds[bc].sort(key=lambda t: (-(t[1]), t[2] != "manual", t[0]))
        return notes, board_to_inds, board_name
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()


def _entry(key, name, hit, notes):
    """hit: [(industry_id, weight, source)] (已按 weight 排). 第一个有 note 的=主行业."""
    primary, secondary = None, []
    for iid, _w, _src in hit:
        n = notes.get(iid)
        if not n:
            continue
        if primary is None:
            primary = n
        elif len(secondary) < 2:
            secondary.append({"industry": n["industry"], "stance": n["stance"]})
    if primary is None:
        return {"key": key, "name": name, "covered": False, "tier_delta": 0,
                "risk_flag": False, "note": "无行业研究覆盖"}
    td, rf = _semantics(primary)
    return {
        "key": key, "name": name, "covered": True,
        "industry": primary["industry"], "industry_id": primary["industry_id"],
        "stance": primary["stance"], "rating_raw": primary["rating_raw"],
        "valid_until": primary["valid_until"], "expired": primary["expired"],
        "qc_status": primary["qc_status"], "snippet": primary["snippet"],
        "secondary": secondary, "tier_delta": td, "risk_flag": rf, "note": "",
    }


def _nocover(key, name, note):
    return {"key": key, "name": name, "covered": False, "tier_delta": 0,
            "risk_flag": False, "note": note}


def _compute_codes(codes, notes, board_to_inds):
    """code(裸6位/带后缀) -> stock_concept_map 最新快照板块 -> 聚合命中行业."""
    try:
        mc = sqlite3.connect("file:" + market_db() + "?mode=ro", uri=True, timeout=30)
        mc.row_factory = sqlite3.Row
    except Exception:
        return [_nocover(c, None, "行情库不可用") for c in codes]
    try:
        try:
            snap = mc.execute("SELECT MAX(snapshot_date) FROM stock_concept_map").fetchone()[0]
        except sqlite3.OperationalError:
            snap = None
        out = []
        for c in codes:
            bare = scout_db.bare(c)
            ts = scout_db.to_suffix(c)
            name = None
            try:
                row = mc.execute("SELECT name FROM stock_names WHERE code=?", (bare,)).fetchone()
                if row:
                    name = row[0]
            except sqlite3.OperationalError:
                pass
            hit = {}
            if snap:
                for r in mc.execute(
                    "SELECT board_code FROM stock_concept_map WHERE ts_code=? AND snapshot_date=?",
                    (ts, snap)
                ):
                    for iid, w, src in board_to_inds.get(r["board_code"], []):
                        if iid not in hit or w > hit[iid][1]:
                            hit[iid] = (iid, w, src)
            hit_sorted = sorted(hit.values(), key=lambda t: (-(t[1]), t[2] != "manual", t[0]))
            out.append(_entry(c, name, hit_sorted, notes))
        return out
    finally:
        mc.close()


def compute(codes=None, boards=None, now_str=None):
    """核心桥. codes/boards 字符串列表. 返回 entry 列表(codes 在前 boards 在后)."""
    now_str = now_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    codes = list(codes or [])
    boards = list(boards or [])
    research = _load_research(now_str)
    if research is None:
        return ([_nocover(c, None, "行业研究库不可用") for c in codes]
                + [_nocover(b, None, "行业研究库不可用") for b in boards])
    notes, board_to_inds, board_name = research
    out = []
    if codes:
        out += _compute_codes(codes, notes, board_to_inds)
    for b in boards:
        out.append(_entry(b, board_name.get(b), board_to_inds.get(b, []), notes))
    return out


def from_reviews(reviewer, now_str=None):
    """读 market.db intraday_review 今日该 reviewer top-6 code, 贴标. 表缺/库坏 -> []."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        mc = sqlite3.connect("file:" + market_db() + "?mode=ro", uri=True, timeout=30)
    except Exception:
        return []
    try:
        rows = mc.execute(
            "SELECT code FROM intraday_review WHERE trade_date=? AND reviewer=? "
            "ORDER BY (COALESCE(logic_stars,0)+COALESCE(action_stars,0)) DESC LIMIT 6",
            (today, reviewer)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        mc.close()
    return compute(codes=[r[0] for r in rows], now_str=now_str)


def mainlines(n=10, now_str=None):
    """读 market.db board_trend_daily 最新交易日 top-N 板块, 贴标. 表缺/库坏 -> []."""
    try:
        mc = sqlite3.connect("file:" + market_db() + "?mode=ro", uri=True, timeout=30)
    except Exception:
        return []
    try:
        td = mc.execute("SELECT MAX(trade_date) FROM board_trend_daily").fetchone()[0]
        rows = mc.execute(
            "SELECT board_code FROM board_trend_daily WHERE trade_date=? ORDER BY rank LIMIT ?",
            (td, n)).fetchall() if td else []
    except sqlite3.OperationalError:
        rows = []
    finally:
        mc.close()
    return compute(boards=[r[0] for r in rows], now_str=now_str)


def render_prompt_block(entries, title):
    """渲染人读速查块. entries 为空 -> ''."""
    lines = []
    for e in entries:
        nm = e.get("name") or ""
        if not e.get("covered"):
            lines.append(f"- {e['key']} {nm} | {e.get('note') or '无行业研究覆盖'}")
            continue
        flag = " ⚠建议压一档+标风险" if e.get("risk_flag") else ""
        vu = e.get("valid_until") or "—"
        exp = "(已过期)" if e.get("expired") else ""
        qc = e.get("qc_status") or "—"
        sec = ""
        if e.get("secondary"):
            sec = " 〔兼:" + "/".join(f"{s['industry']}{s['stance']}" for s in e["secondary"]) + "〕"
        lines.append(
            f"- {e['key']} {nm} | {e['industry']} {e['stance']} | 有效至{vu}{exp} "
            f"| qc:{qc}{flag}{sec} | {e['snippet']}")
    return (title + "\n" + "\n".join(lines)) if lines else ""


_PICK_TITLE = "📚 行业研究立场速查(res 研究室, 仅供旁证; 看空/过期/qc未过=建议压一档并标注风险):"
_BOARD_TITLE = "📚 板块对应行业研究立场(res 研究室, 仅供旁证; 看空/过期/qc未过=压持续性星级并标注):"


def overlay_for_pick_prompt(reviewer):
    return render_prompt_block(from_reviews(reviewer), _PICK_TITLE)


def overlay_for_board_prompt(n=10):
    return render_prompt_block(mainlines(n), _BOARD_TITLE)


def main():
    ap = argparse.ArgumentParser(description="res 行业研究 -> scout 个股/板块 贴标 overlay")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", help="逗号分隔的代码(裸6位或带后缀)")
    g.add_argument("--boards", help="逗号分隔的板块码 BKxxxx.DC")
    g.add_argument("--mainlines", type=int, nargs="?", const=10, help="趋势主线 top-N(默认10)")
    g.add_argument("--from-reviews", help="某 reviewer 今日 intraday_review top-6")
    ap.add_argument("--format", choices=("json", "prompt"), default="json")
    a = ap.parse_args()
    if a.codes is not None:
        entries, title = compute(codes=[x for x in a.codes.split(",") if x]), _PICK_TITLE
    elif a.boards is not None:
        entries, title = compute(boards=[x for x in a.boards.split(",") if x]), _BOARD_TITLE
    elif a.mainlines is not None:
        entries, title = mainlines(a.mainlines), _BOARD_TITLE
    else:
        entries, title = from_reviews(a.from_reviews), _PICK_TITLE
    if a.format == "json":
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    else:
        print(render_prompt_block(entries, title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
