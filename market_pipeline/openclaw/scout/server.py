"""盘中实时选股看板 — web 服务 (:18901, stdlib, 零新依赖).

只读 market.db 盘中表, 拼 JSON 给前端轮询. 与采集/富集解耦: 采集挂了仍显示上一帧.
(18900 被一个无关 node 服务占用, 故默认 18901)

路由:
  GET /                -> scout.html
  GET /api/scout       -> {snapshot, boards, candidates, meta}
  GET /api/regime_history?days=20 -> {rows(每行含csi1000中证1000收盘+up/dn/flat/tot涨跌家数), change}; days上限9999(全部)
  GET /api/earnings-resonance?date=YYYYMMDD -> 业绩共振个股 (trade_date, stats, rows[])
  GET /health          -> ok

用法:
  python3 server.py            # 监听 0.0.0.0:18901
  python3 server.py --port N
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _ttjj_client
import board_etf
import lof_arb
import mainline_forward_daily
import perf
import pick_perf
import scout_auth
import scout_db
import since924
import valuation
import valuation_admin
import board_moneyflow
from board_trend import _recon_index
from whitelist import Whitelist

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))
ZSXQ_DB = "/home/rooot/database/zsxq.db"
# ---- 研究展示: 只读 fund.db (lab 主库 + 研究引擎写入点, 绝不可写) ----
RESEARCH_DB = "/home/rooot/agent_invest_lab/data/fund.db"
RES_CATEGORY = {
    "res6": "硬科技", "res7": "能源材料", "res8": "医药生物",
    "res9": "大消费", "res10": "金融地产", "res11": "周期资源", "res12": "高端制造",
}
RES_CATEGORY_ORDER = ["res6", "res7", "res8", "res9", "res10", "res11", "res12"]
MACRO_TYPES = [
    ("market_strategy", "市场环境", "res1"),
    ("policy_analysis", "政策分析", "res2"),
    ("intl_relations", "国际关系", "res4"),
    ("cross_market_linkage", "跨市场联动", "res5"),
]
_RATING_ORDER = {"看多": 0, "中性": 1, "看空": 2}
_CONF_ORDER = {"高": 0, "中": 1, "低": 2}
WHITELIST_PATH = os.path.join(HERE, "coarse_themes.json")
WHITELIST = Whitelist(WHITELIST_PATH)

# 汉字拼音首字母 (pypinyin 离线生成, U+4E00–U+9FFF), 供个股搜索拼音首字母检索.
# 主表每字一个首字母; poly 补多音字其余首字母, 匹配时按位取并集 (长→{c,z}, 重→{c,z,t}).
_PY_FL_BASE = 0x4E00
try:
    with open(os.path.join(HERE, "pinyin_fl.txt"), encoding="utf-8") as _pyf:
        _PY_FL_TABLE = _pyf.read().strip()
except OSError:
    _PY_FL_TABLE = ""
try:
    with open(os.path.join(HERE, "pinyin_poly.json"), encoding="utf-8") as _pyf:
        _PY_FL_POLY = json.load(_pyf)
except (OSError, ValueError):
    _PY_FL_POLY = {}


def _py_sets(name: str):
    """逐字可能的首字母集合 (生僻/非汉字跳过, ASCII 原样小写)."""
    sets = []
    for ch in name or "":
        cp = ord(ch)
        if _PY_FL_BASE <= cp <= 0x9FFF:
            base = _PY_FL_TABLE[cp - _PY_FL_BASE] if _PY_FL_TABLE else "_"
            st = set()
            if base != "_":
                st.add(base)
            st.update(_PY_FL_POLY.get(ch, ""))
            if st:
                sets.append(st)
        elif cp < 128:
            sets.append({ch.lower()})
    return sets


def py_match(name: str, q: str) -> bool:
    """q (纯拼音首字母,已小写) 是否为 name 首字母串的连续子串 (逐字多音取并集)."""
    if not q or not _PY_FL_TABLE:
        return False
    sets = _py_sets(name)
    n, m = len(sets), len(q)
    if m > n:
        return False
    for i in range(n - m + 1):
        if all(q[k] in sets[i + k] for k in range(m)):
            return True
    return False

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
GROUPS = {}  # group_id -> name, 懒加载

CROWDING_MIN_HISTORY = 200
LARGE_BOARD_MEMBERS = 80

# 高位滞涨预警阈值 (可调; 定义见 docs/superpowers/specs/2026-06-01-scout-high-stall-alert-design.md)
STALL_RET60_MIN = 30.0     # 高位: 60日涨幅下限
STALL_RET20_MIN = 20.0     # 高位: 20日涨幅下限 (与 ret60 二选一)
STALL_RET5_ABS_MAX = 3.0   # 滞涨: 近5日绝对涨幅上限 % (再衰减, 绝对涨幅还高就不算滞涨, 防抛物线误报)
STALL_DECEL_RATIO = 0.4    # 滞涨: 近5日涨速 / 中期5日均速 的上限 (动量衰减, 越低衰减越狠)
STALL_VOL_HOT = 1.2        # 放量分界 (vol_ratio)
STALL_PEAK_HOT = 0.95      # 天量度分界 (amt_peak_ratio)
STALL_VOL_COLD = 0.9       # 缩量分界 (vol_ratio)
STALL_SHARE_DROP = -0.05   # 占大盘比掉头分界 (amt_share_trend)

# 上涨中继发现阈值 (可调; 见 docs/superpowers/specs/2026-06-01-scout-uptrend-continuation-design.md)
CONT_RET60_MIN = 20.0      # 趋势: 60日涨幅下限
CONT_DIST_MA20_HI = 6.0    # 回踩: 距MA20上限 % (高于此还没回够, 仍是主升)
CONT_DIST_MA20_LO = -3.0   # 回踩: 距MA20下限 % (低于此回踩过深/破位)
CONT_RET5_MAX = 1.0        # 回踩: 近5日涨幅上限 % (没在冲)
CONT_VOL_MAX = 1.0         # 不放量分界 (vol_ratio ≤ 此; 镜像高位出货的放量≥1.2)

# K线 tag 的 regime 出现门槛 (回测 docs/superpowers/specs/2026-06-04 验证: 只在有效 regime 才显示)
# 高位出货仅在强牛有效(其它 regime 触发后仍续涨/反向); 上涨中继在强牛+中性震荡干净(强势震荡抛硬币/弱市无效)
TAG_REGIME_STALL = ("STRONG_BULL",)
TAG_REGIME_CONT = ("STRONG_BULL", "NEUTRAL_RANGE")


def _alert_rank(level: str | None) -> int:
    return {"red": 4, "orange": 3, "yellow": 2, "watch": 1}.get(level or "", 0)


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def clean_full(text: str) -> str:
    """zsxq 富文本 -> 可读全文: <br>/</p> 转换行, 剥标签, 解实体, 保留段落."""
    if not text:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    t = re.sub(r"</p>", "\n", t, flags=re.I)
    t = _TAG.sub("", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _research_conn():
    """只读连 fund.db。任何写操作都会抛错，保护 lab 主库。"""
    return sqlite3.connect("file:" + RESEARCH_DB + "?mode=ro", uri=True, timeout=30)


def _parse_rating(s):
    """'看多(置信度中)' -> ('看多','中'); 兼容简写 '看多(中)'; 无法解析 -> (原串, None)。"""
    s = (s or "").strip()
    m = re.match(r"^(看多|中性|看空)\((?:置信度)?(高|中|低)\)$", s)
    if m:
        return m.group(1), m.group(2)
    return s, None


def _qc_map(conn):
    """{(target_type, target_id): (status, reason)} —— 每个目标最新一条 qc_verdict 的质控结论。
    res13 质控室落库;qc_verdict 表不存在(库未迁移)则返回空, 研究页照常无徽标。"""
    out = {}
    try:
        for r in conn.execute(
            "SELECT target_type, target_id, status, reasons_md FROM qc_verdict "
            "WHERE id IN (SELECT MAX(id) FROM qc_verdict GROUP BY target_type, target_id)"):
            out[(r["target_type"], r["target_id"])] = (r["status"], r["reasons_md"] or "")
    except sqlite3.OperationalError:
        pass
    return out


def build_research_payload():
    """研究 tab 主 payload: 宏观 4 类各最新一期(摘要) + 行业研究按 7 大类分组(摘要)。只读。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _research_conn()
    conn.row_factory = sqlite3.Row
    try:
        qc = _qc_map(conn)
        macro = []
        for rtype, label, res in MACRO_TYPES:
            r = conn.execute(
                "SELECT id, as_of_date, content_md, structured_json FROM res_reports "
                "WHERE report_type=? ORDER BY as_of_date DESC, id DESC LIMIT 1", (rtype,)
            ).fetchone()
            if not r:
                macro.append({"type": rtype, "label": label, "res": res, "id": None,
                              "as_of_date": None, "headline": "", "stance": "",
                              "key_points": [], "tags": []})
                continue
            try:
                sj = json.loads(r["structured_json"] or "{}")
            except Exception:
                sj = {}
            headline = sj.get("headline") or (
                (r["content_md"] or "").strip().split("\n", 1)[0].lstrip("# ").strip())
            qcst, qcrsn = qc.get(("res_report", r["id"]), (None, ""))
            macro.append({
                "type": rtype, "label": label, "res": res, "id": r["id"],
                "as_of_date": r["as_of_date"], "headline": headline,
                "stance": sj.get("stance", "") or "",
                "key_points": sj.get("key_points") or [], "tags": sj.get("tags") or [],
                "qc_status": qcst, "qc_reason": qcrsn})

        rows = conn.execute(
            "SELECT n.id AS note_id, n.bot_id, n.created_at, n.cycle_position, "
            "       n.supply_demand, n.rating, n.conclusion, n.evidence_json, n.valid_until, "
            "       i.name AS industry, "
            "       (SELECT COUNT(*) FROM research_report rr WHERE rr.note_id=n.id) AS rep_cnt "
            "FROM research_note n JOIN industry i ON i.id=n.industry_id "
            "WHERE n.id IN (SELECT MAX(id) FROM research_note GROUP BY industry_id)"
        ).fetchall()

        buckets = {res: [] for res in RES_CATEGORY_ORDER}
        expired_count = 0
        skipped = 0
        for r in rows:
            res = r["bot_id"]
            if res not in buckets:  # 非 res6-res12 的历史 bot_id, 不归入 7 大类
                skipped += 1
                continue
            rating, conf = _parse_rating(r["rating"])
            vu = r["valid_until"]
            expired = bool(vu) and str(vu) < now
            if expired:
                expired_count += 1
            try:
                ev = json.loads(r["evidence_json"] or "{}")
                if not isinstance(ev, dict):
                    ev = {}
            except Exception:
                ev = {}
            qcst, qcrsn = qc.get(("research_note", r["note_id"]), (None, ""))
            buckets[res].append({
                "note_id": r["note_id"], "industry": r["industry"], "rating": rating,
                "confidence": conf, "cycle_position": r["cycle_position"] or "",
                "supply_demand": r["supply_demand"] or "", "conclusion": r["conclusion"] or "",
                "evidence": ev, "created_at": r["created_at"], "valid_until": vu,
                "expired": expired, "has_report": (r["rep_cnt"] or 0) > 0,
                "qc_status": qcst, "qc_reason": qcrsn})

        groups = []
        note_count = 0
        for res in RES_CATEGORY_ORDER:
            inds = buckets[res]
            inds.sort(key=lambda x: x["created_at"] or "", reverse=True)
            inds.sort(key=lambda x: (_RATING_ORDER.get(x["rating"], 9),
                                     _CONF_ORDER.get(x["confidence"], 9)))
            note_count += len(inds)
            groups.append({"cat": RES_CATEGORY[res], "res": res, "industries": inds})

        return {"macro": macro, "groups": groups,
                "meta": {"generated_at": now, "note_count": note_count,
                         "expired_count": expired_count, "skipped": skipped}}
    finally:
        conn.close()


def _note_to_md(n):
    """无长文 report 时, 用 note 的结构化结论拼一段 markdown。"""
    parts = []
    if n["rating"]:
        parts.append("**评级**：" + str(n["rating"]))
    if n["cycle_position"]:
        parts.append("**周期位置**：" + str(n["cycle_position"]))
    if n["supply_demand"]:
        parts.append("**供需**：" + str(n["supply_demand"]))
    md = "\n\n".join(parts)
    if n["conclusion"]:
        md += ("\n\n" if md else "") + "## 结论\n\n" + str(n["conclusion"])
    try:
        ev = json.loads(n["evidence_json"] or "{}")
    except Exception:
        ev = {}
    if isinstance(ev, dict) and ev:
        md += "\n\n## 关键数据\n\n" + "\n".join(f"- {k}：{v}" for k, v in ev.items())
    md += "\n\n*（本条暂无长文研报，以上为结构化研究结论）*"
    return md


def research_note_detail(note_id):
    """单条行业研究全文: 优先取 research_report 全文; 无则回退 note 结构化结论。只读。"""
    try:
        nid = int(note_id)
    except (TypeError, ValueError):
        return {"error": "bad id"}
    conn = _research_conn()
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT rr.title, rr.content_md, rr.created_at, i.name AS industry "
            "FROM research_report rr JOIN industry i ON i.id=rr.industry_id "
            "WHERE rr.note_id=? ORDER BY rr.id DESC LIMIT 1", (nid,)).fetchone()
        if r:
            return {"note_id": nid, "industry": r["industry"], "title": r["title"] or "",
                    "content_md": r["content_md"] or "", "created_at": r["created_at"]}
        # 无长文 report → 回退用 note 自己的结构化结论
        n = conn.execute(
            "SELECT n.created_at, n.cycle_position, n.supply_demand, n.conclusion, "
            "n.evidence_json, n.rating, i.name AS industry "
            "FROM research_note n JOIN industry i ON i.id=n.industry_id WHERE n.id=?", (nid,)).fetchone()
        if not n:
            return {"error": "note not found"}
        return {"note_id": nid, "industry": n["industry"], "title": "",
                "content_md": _note_to_md(n), "created_at": n["created_at"], "from_note": True}
    finally:
        conn.close()


def research_macro_detail(rid):
    """单条宏观日报全文: res_reports.content_md。只读。"""
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return {"error": "bad id"}
    conn = _research_conn()
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT report_type, as_of_date, content_md FROM res_reports WHERE id=?",
            (rid,)).fetchone()
        if not r:
            return {"error": "report not found"}
        label = next((l for t, l, _ in MACRO_TYPES if t == r["report_type"]), r["report_type"])
        return {"id": rid, "label": label, "as_of_date": r["as_of_date"],
                "content_md": r["content_md"] or ""}
    finally:
        conn.close()


def _pick_stats():
    """从 research_pick_daily 统计 5/20 日胜率. 返回 {'resonance':..., 'ambush':...}."""
    conn = sqlite3.connect("file:" + RESEARCH_DB + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        out = {}
        for kind in ("resonance", "ambush"):
            r = conn.execute("""
                SELECT
                  COUNT(*)                                    AS total,
                  SUM(CASE WHEN ret5  IS NOT NULL THEN 1 END) AS n5,
                  SUM(CASE WHEN ret5  > 0            THEN 1 END) AS w5,
                  SUM(CASE WHEN ret20 IS NOT NULL THEN 1 END) AS n20,
                  SUM(CASE WHEN ret20 > 0            THEN 1 END) AS w20
                FROM research_pick_daily WHERE kind = ?
            """, (kind,)).fetchone()
            total = int(r["total"] or 0)
            n5 = int(r["n5"] or 0); w5 = int(r["w5"] or 0)
            n20 = int(r["n20"] or 0); w20 = int(r["w20"] or 0)
            out[kind] = {
                "total": total,
                "n5": n5, "w5": w5, "win5": (round(w5 / n5, 4) if n5 else None),
                "n20": n20, "w20": w20, "win20": (round(w20 / n20, 4) if n20 else None),
            }
        return out
    finally:
        conn.close()


def build_research_signals():
    """基本面看多 × 趋势位置 双向信号 + 历史胜率 stats. 只读 fund.db + market.db."""
    from scripts.research_pick_engine import compute_picks_for_date
    r = compute_picks_for_date(research_db=RESEARCH_DB, market_db=DB_PATH)
    try:
        stats = _pick_stats()
    except Exception:
        stats = {"resonance": None, "ambush": None}
    return {"resonance": r["resonance"], "ambush": r["ambush"], "stats": stats}


def zsxq_topic(topic_id: str) -> dict:
    zc = sqlite3.connect(ZSXQ_DB, timeout=30)
    zc.row_factory = sqlite3.Row
    try:
        if not GROUPS:
            try:
                for g in zc.execute("SELECT group_id, name FROM groups"):
                    GROUPS[g["group_id"]] = g["name"]
            except Exception:
                pass
        r = zc.execute(
            "SELECT topic_id, group_id, create_date, author_name, title, text, article_html "
            "FROM topics WHERE topic_id=CAST(? AS INTEGER)", (topic_id,)).fetchone()
        if not r:
            return {"error": "topic not found"}
        body = clean_full(r["text"]) or clean_full(r["article_html"])
        return {
            "topic_id": r["topic_id"], "date": r["create_date"],
            "group": GROUPS.get(r["group_id"], str(r["group_id"])),
            "author": r["author_name"], "title": r["title"], "text": body,
        }
    finally:
        zc.close()


def _snippet(text: str, kw: str, win: int = 60) -> str:
    """命中 kw 的上下文窗口(剥标签后); 命中不到取开头. 与 logic.snippet_around 一致."""
    t = _TAG.sub("", text or "")
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
    t = _WS.sub(" ", t).strip()
    i = t.find(kw)
    if i < 0:
        return t[:win * 2]
    a = max(0, i - win // 3)
    return ("…" if a > 0 else "") + t[a:a + win] + ("…" if a + win < len(t) else "")


def zsxq_search(names, days: int = 90, limit: int = 100) -> dict:
    """多关键词(板块名 + 龙头 + 成分股) OR 命中近 days 天 zsxq 纪要, 每条标注命中的关键词(matched), 倒序.
    单关键词时等价于旧的按股名搜索. 供板块级'星球纪要'聚合下钻."""
    names = [n for n in (names or []) if n]
    if not names:
        return {"name": "", "names": [], "days": days, "count": 0, "items": []}
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    zc = sqlite3.connect(ZSXQ_DB, timeout=30)
    zc.row_factory = sqlite3.Row
    try:
        if not GROUPS:
            try:
                for g in zc.execute("SELECT group_id, name FROM groups"):
                    GROUPS[g["group_id"]] = g["name"]
            except Exception:
                pass
        where_or = " OR ".join("text LIKE ?" for _ in names)
        params = [f"%{n}%" for n in names] + [since, limit]
        rows = zc.execute(
            f"SELECT topic_id, group_id, create_date, author_name, title, text FROM topics "
            f"WHERE ({where_or}) AND create_date>=? ORDER BY create_time DESC LIMIT ?",
            params).fetchall()
        items = []
        for r in rows:
            txt = r["text"] or ""
            matched = [n for n in names if n in txt]
            kw = matched[0] if matched else names[0]
            snip = _snippet(txt, kw) or _TAG.sub("", r["title"] or "")[:80]
            items.append({
                "topic_id": str(r["topic_id"]), "date": r["create_date"],
                "group": (GROUPS.get(r["group_id"], str(r["group_id"])) or "")[:12],
                "author": (r["author_name"] or "")[:16], "snippet": snip,
                "matched": matched,
            })
        return {"name": names[0], "names": names, "days": days, "count": len(items), "items": items}
    finally:
        zc.close()


def zsxq_list(name: str, days: int = 90, limit: int = 100) -> dict:
    """单关键词便捷封装(个股 chip 单点用). 见 zsxq_search."""
    return zsxq_search([name], days, limit)


def _board_member_names(c, code: str) -> list:
    """板块全成分股名清单(裸 ts_code -> 名). 主用 kpl_theme_daily, 盘中成分 JSON 补缺.
    stock_concept_map.name 列为空, 故必须经名表映射; 否则纪要按股名搜不到成分股."""
    ts = [r[0] for r in c.execute(
        "SELECT ts_code FROM stock_concept_map WHERE board_code=?", (code,))]
    if not ts:
        return []
    ph = ",".join("?" * len(ts))
    nm = {}
    for tc, sn in c.execute(
        f"SELECT ts_code, stock_name FROM kpl_theme_daily "
        f"WHERE ts_code IN ({ph}) AND stock_name IS NOT NULL AND stock_name<>''", ts):
        nm.setdefault(tc, sn)
    # 补缺: kpl_theme_daily 未覆盖的成分用本板块盘中 JSON 的裸码->名兜底(如 洁美科技/艾华集团)
    missing = {t.split(".")[0]: t for t in ts if t not in nm}
    if missing:
        for col in ("lead_json", "lag_json", "limit_up_json", "limit_down_json"):
            for cc, n in c.execute(
                f"SELECT json_extract(value,'$.c'), json_extract(value,'$.n') "
                f"FROM intraday_board_members, json_each({col}) WHERE board_code=?", (code,)):
                t = missing.get(cc)
                if t and n:
                    nm.setdefault(t, n)
    seen, out = set(), []
    for t in ts:
        n = nm.get(t)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def board_keywords(c, code: str) -> list:
    """板块级'星球纪要'聚合关键词: 板块名全部别名(含去'概念'后缀变体) + 全成分股名.
    concept_board_daily 同一 board_code 常存'被动元件'/'被动元件概念'两种写法, 一并纳入."""
    names = [r[0] for r in c.execute(
        "SELECT DISTINCT board_name FROM concept_board_daily "
        "WHERE board_code=? AND board_name IS NOT NULL AND board_name<>''", (code,))]
    seen, out = set(), []
    for k in list(names) + _board_member_names(c, code):
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def zsxq_board(code: str, days: int = 90, limit: int = 120) -> dict:
    """板块下钻: 展开为板块名 + 全成分股名后做 OR 命中搜索. 见 zsxq_search."""
    c = _conn()
    try:
        kw = board_keywords(c, code)
    finally:
        c.close()
    if not kw:
        return {"name": "", "names": [], "days": days, "count": 0, "items": []}
    return zsxq_search(kw, days, limit)


# ── 成分区间涨跌幅 (未复权, 取自 daily.close) ──────────────────────────
# 成分 all_json 里是裸码 (920799), daily.ts_code 带后缀 (920799.BJ); 需映射.
# 映射 + 交易日历按最新交易日 T0 缓存, T0 变 (每日结算) 才重建.
_RET_CACHE = {"t0": None, "code_map": {}, "calendar": []}
_RET_WINDOW_DEFAULT = 60
_RET_WINDOW_MAX = 500


def _ensure_ret_cache(c):
    """返回 (t0, 裸码->ts_code 映射, 交易日历降序). 按 T0 缓存, 缺 daily 表返回空."""
    if not _table_exists(c, "daily"):
        return None, {}, []
    row = c.execute("SELECT MAX(trade_date) FROM daily").fetchone()
    t0 = row[0] if row else None
    if not t0:
        return None, {}, []
    if _RET_CACHE["t0"] != t0 or not _RET_CACHE["code_map"]:
        cmap = {}
        for r in c.execute("SELECT ts_code FROM daily WHERE trade_date=?", (t0,)):
            tc = r[0]
            cmap[tc.split(".")[0]] = tc
        cal = [r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM daily ORDER BY trade_date DESC "
            f"LIMIT {_RET_WINDOW_MAX + 100}")]
        _RET_CACHE.update({"t0": t0, "code_map": cmap, "calendar": cal})
    return _RET_CACHE["t0"], _RET_CACHE["code_map"], _RET_CACHE["calendar"]


def member_interval_returns(c, codes, *, window=None, start=None, end=None):
    """成分裸码列表 -> 区间涨跌幅 (未复权 close).

    window 模式: end=最新交易日, start=往前 window 个交易日 (历史不足取最早).
    range 模式: start/end 为 YYYYMMDD, 各 snap 到 <= 该日的最近交易日.
    返回 {window, start_td, end_td, returns:{裸码: 涨跌幅% or None}}; 两端任一缺收盘 -> None."""
    codes = list(codes)
    out = {code: None for code in codes}
    _t0, code_map, calendar = _ensure_ret_cache(c)
    meta = {"window": window, "start_td": None, "end_td": None, "returns": out}
    if not code_map:
        return meta
    if window is not None:
        if not calendar:
            return meta
        end_td = calendar[0]
        idx = window if window < len(calendar) else len(calendar) - 1
        start_td = calendar[idx]
    else:
        row_e = c.execute("SELECT MAX(trade_date) FROM daily WHERE trade_date<=?",
                          (end,)).fetchone()
        row_s = c.execute("SELECT MAX(trade_date) FROM daily WHERE trade_date<=?",
                          (start,)).fetchone()
        end_td = row_e[0] if row_e else None
        start_td = row_s[0] if row_s else None
    meta["start_td"], meta["end_td"] = start_td, end_td
    if not start_td or not end_td or start_td >= end_td:
        return meta
    # 一次拉两日全市场收盘, 按日建 dict (比逐成分查快, 最大板块也只 2 日扫描)
    cs, ce = {}, {}
    for r in c.execute(
            "SELECT ts_code, trade_date, close FROM daily WHERE trade_date IN (?,?)",
            (start_td, end_td)):
        ts, td, close = r[0], r[1], r[2]
        if close is None:
            continue
        (cs if td == start_td else ce)[ts] = close
    for code in codes:
        ts = code_map.get(code)
        if not ts:
            continue
        a, b = cs.get(ts), ce.get(ts)
        if a and b and a > 0:
            out[code] = round((b / a - 1.0) * 100.0, 2)
    return meta


def _board_member_codes(c, code):
    """板块全部成分清单的裸码并集 (all ∪ 涨停/跌停/领涨/领跌), 供区间涨跌幅冷加载用."""
    r = c.execute(
        "SELECT limit_up_json, limit_down_json, lead_json, lag_json, all_json "
        "FROM intraday_board_members WHERE board_code=?", (code,)).fetchone()
    if not r:
        return set()
    codes = set()
    for col in r:
        for m in json.loads(col or "[]"):
            cc = m.get("c")
            if cc:
                codes.add(cc)
    return codes


def board_members(code: str) -> dict:
    """单个板块成分个股下钻: 当前帧的涨停/跌停/领涨/领跌清单(由 collect.compute_boards 落库).

    追加 etfs: 该板块对标 ETF 列表 (取自 board_etf_daily 最近一日).
    非 Top10 主线板块查不到 ETF, etfs 返回 []."""
    c = _conn()
    try:
        r = c.execute(
            "SELECT snapshot_time, limit_up_json, limit_down_json, lead_json, lag_json, all_json "
            "FROM intraday_board_members WHERE board_code=?", (code,)).fetchone()
        base = {"code": code, "snapshot_time": None, "all": [],
                "limit_up": [], "limit_down": [], "lead": [], "lag": [], "etfs": [], "active_funds": []}
        if r:
            base.update({
                "snapshot_time": r["snapshot_time"],
                "limit_up": json.loads(r["limit_up_json"] or "[]"),
                "limit_down": json.loads(r["limit_down_json"] or "[]"),
                "lead": json.loads(r["lead_json"] or "[]"),
                "lag": json.loads(r["lag_json"] or "[]"),
                "all": json.loads(r["all_json"] or "[]"),
            })
        # ETF: 取该板块最近一日的对标 ETF
        etfs = c.execute(
            "SELECT rank, fund_code, fund_name, hit_weight, hit_count, report_date "
            "FROM board_etf_daily WHERE board_code=? "
            "AND trade_date=(SELECT MAX(trade_date) FROM board_etf_daily WHERE board_code=?) "
            "ORDER BY rank", (code, code)).fetchall()
        base["etfs"] = [dict(e) for e in etfs]
        af = c.execute(
            "SELECT rank, fund_code, fund_name, hit_weight, hit_count, report_date "
            "FROM board_active_fund_daily WHERE board_code=? "
            "AND trade_date=(SELECT MAX(trade_date) FROM board_active_fund_daily WHERE board_code=?) "
            "ORDER BY rank", (code, code)).fetchall()
        base["active_funds"] = [dict(x) for x in af]
        # 默认 60 日区间涨跌幅: 随手算掉 (实测 <30ms), 其它窗口前端冷加载.
        # 取所有成分清单的裸码并集, 把 ret 写回每个 member dict.
        member_lists = [base["all"], base["limit_up"], base["limit_down"],
                        base["lead"], base["lag"]]
        codes = {m.get("c") for lst in member_lists for m in lst if m.get("c")}
        rj = member_interval_returns(c, codes, window=_RET_WINDOW_DEFAULT)
        rmap = rj["returns"]
        for lst in member_lists:
            for m in lst:
                m["ret"] = rmap.get(m.get("c"))
        base["ret_window"] = _RET_WINDOW_DEFAULT
        base["ret_start_td"] = rj["start_td"]
        base["ret_end_td"] = rj["end_td"]
        return base
    finally:
        c.close()


_BOARD_TYPE_MAP = {"concept": "概念板块", "industry": "行业板块", "region": "地域板块"}


def board_search(c, q: str, types: str = "concept,industry", wl_codes=None, limit: int = 30) -> dict:
    """白名单管理搜索框: 在 concept_board_daily 最新交易日按名/代码模糊搜板块.
    默认排除地域板块; 每条标 in_whitelist. wl_codes 缺省取 WHITELIST(便于测试注入)."""
    q = (q or "").strip()
    if not q:
        return {"boards": []}
    if wl_codes is None:
        WHITELIST.load_if_changed()
        wl_codes = WHITELIST.codes()
    want = [_BOARD_TYPE_MAP[t] for t in (types or "").split(",") if t in _BOARD_TYPE_MAP]
    if not want:
        want = ["概念板块", "行业板块"]
    md = c.execute("SELECT MAX(trade_date) FROM concept_board_daily").fetchone()[0]
    if not md:
        return {"boards": []}
    like = f"%{q}%"
    tph = ",".join("?" * len(want))
    rows = c.execute(
        f"SELECT board_code, board_name, idx_type FROM concept_board_daily "
        f"WHERE trade_date=? AND idx_type IN ({tph}) "
        f"AND (board_name LIKE ? OR board_code LIKE ?) "
        f"ORDER BY board_name LIMIT ?",
        [md] + want + [like, like, limit]).fetchall()
    boards = [{"code": r["board_code"], "name": r["board_name"], "idx_type": r["idx_type"],
               "in_whitelist": r["board_code"] in wl_codes} for r in rows]
    return {"boards": boards}


_BOARD_CODE_RE = re.compile(r"^BK\d+\.DC$")


def _atomic_write_json(path: str, data) -> None:
    """临时文件 + os.replace 原子落盘, 杜绝半截 JSON 打挂主线系统."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _whitelist_add(c, body, path: str = WHITELIST_PATH):
    """加板块进白名单 JSON. 返回 (http_code, body_dict). 不触发回填(由 handler 负责)."""
    code = str((body or {}).get("code", "")).strip().upper()
    group = str((body or {}).get("group", "")).strip()
    name = str((body or {}).get("name", "")).strip()
    if not _BOARD_CODE_RE.match(code):
        return 400, {"error": "板块代码格式非法(应形如 BK1137.DC)"}
    if not group:
        return 400, {"error": "请选择或新建题材组"}
    row = c.execute(
        "SELECT board_name FROM concept_board_daily WHERE board_code=? "
        "ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
    if not row:
        return 400, {"error": "板块不存在(不在行情库)"}
    if not name:
        name = row[0]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    groups = data.setdefault("groups", [])
    for g in groups:
        for b in g.get("boards", []):
            if b.get("code") == code:
                return 400, {"error": "板块已在白名单"}
    members = c.execute(
        "SELECT COUNT(*) FROM stock_concept_map WHERE board_code=? "
        "AND snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map)", (code,)).fetchone()[0]
    target = next((g for g in groups if g.get("name") == group), None)
    if target is None:
        target = {"name": group, "boards": []}
        groups.append(target)
    entry = {"name": name, "code": code}
    if members:
        entry["members"] = members
    target.setdefault("boards", []).append(entry)
    data["updated"] = datetime.now().strftime("%Y-%m-%d")
    _atomic_write_json(path, data)
    return 200, {"ok": True, "code": code, "name": name, "group": group}


def _whitelist_remove(body, path: str = WHITELIST_PATH):
    """从白名单 JSON 移除板块. 返回 (http_code, body_dict). 空组保留."""
    code = str((body or {}).get("code", "")).strip().upper()
    if not _BOARD_CODE_RE.match(code):
        return 400, {"error": "板块代码格式非法"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    found = False
    for g in data.get("groups", []):
        boards = g.get("boards", [])
        kept = [b for b in boards if b.get("code") != code]
        if len(kept) != len(boards):
            found = True
        g["boards"] = kept
    if not found:
        return 400, {"error": "板块不在白名单"}
    data["updated"] = datetime.now().strftime("%Y-%m-%d")
    _atomic_write_json(path, data)
    return 200, {"ok": True, "code": code}


def _spawn_whitelist_backfill(code: str) -> None:
    """后台 detached 子进程对该板块按日重算(不联网). flock 串行排队, BLAS 钳单线程, 不阻塞请求.
    best-effort: 失败不影响 add 已成功; 兜底有下次主线 pipeline 自然纳入."""
    env = dict(os.environ)
    env.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"})
    script = os.path.join(HERE, "backfill_board_history.py")
    # flock(阻塞,不加 -n)→ 多次添加排队串行; start_new_session→ 脱离请求进程存活
    cmd = ["flock", "/tmp/scout-whitelist-backfill.lock",
           "/usr/bin/python3.12", script, "--recompute-code", code]
    try:
        logf = open("/tmp/scout-whitelist-backfill.log", "a")
        proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=logf,
                                stderr=subprocess.STDOUT, start_new_session=True)
        logf.close()  # 父进程立即释放 fd; 子进程通过继承的 fd 继续写
    except Exception:
        pass  # 回填是增强, 失败静默; add 本身已成功


def board_list() -> dict:
    """供前端"组合选基"多选器: 白名单板块按题材组归类.
    返回 {groups:[{name, boards:[{code,name}]}]} (组按 group_order, 板块按名排)."""
    WHITELIST.load_if_changed()
    buckets: dict[str, list] = {g: [] for g in WHITELIST.group_order()}
    for code in WHITELIST.codes():
        g = WHITELIST.code_to_group(code) or "其他"
        buckets.setdefault(g, []).append(
            {"code": code, "name": WHITELIST.code_to_name(code) or code})
    groups = []
    for g in WHITELIST.group_order():
        bs = sorted(buckets.get(g, []), key=lambda b: b["name"])
        if bs:
            groups.append({"name": g, "boards": bs})
    return {"groups": groups}


def _board_names(codes) -> dict:
    """board_code -> 名字. 先 WHITELIST, 再 concept_board_daily 兜底 (含非白名单板块)."""
    WHITELIST.load_if_changed()
    out = {}
    miss = []
    for bc in codes:
        nm = WHITELIST.code_to_name(bc)
        if nm:
            out[bc] = nm
        else:
            miss.append(bc)
    if miss:
        c = _conn()
        try:
            ph = ",".join("?" * len(miss))
            for bc, nm in c.execute(
                f"SELECT board_code, board_name FROM concept_board_daily "
                f"WHERE board_code IN ({ph}) GROUP BY board_code", miss):
                out[bc] = nm or bc
        finally:
            c.close()
    return {bc: out.get(bc, bc) for bc in codes}


def board_fund_match(board_codes, threshold: float = 80.0, top: int = 3,
                     scope: str = "all") -> dict:
    """多板块组合选基金/ETF. scope: all=全成分 / leader1=龙头 / leader3=龙头+接力.
    详见 board_etf.match_boards_funds. 回显所选板块名."""
    out = board_etf.match_boards_funds(DB_PATH, board_codes, threshold, top, scope)
    out["threshold"] = threshold
    nm = _board_names(board_codes)
    out["boards"] = [{"code": bc, "name": nm[bc]} for bc in board_codes]
    return out


def margin_overview() -> dict:
    """融资融券面板数据: 最近交易日的宽基指数聚合 + 主线板块聚合.
    indices 按融资净买入降序; boards 按主线 rank 升序. top_etf/top_stocks 已解析为数组."""
    c = _conn()
    try:
        md = c.execute("SELECT MAX(trade_date) FROM margin_index_daily").fetchone()[0]
        indices, boards = [], []
        if md:
            for r in c.execute(
                    "SELECT * FROM margin_index_daily WHERE trade_date=? "
                    "ORDER BY rz_net_buy DESC", (md,)):
                d = dict(r)
                d["top_etf"] = json.loads(d.get("top_etf") or "[]")
                indices.append(d)
            for r in c.execute(
                    "SELECT * FROM margin_board_daily WHERE trade_date=? "
                    "ORDER BY rank ASC", (md,)):
                d = dict(r)
                d["top_stocks"] = json.loads(d.get("top_stocks") or "[]")
                boards.append(d)
        return {"trade_date": md, "indices": indices, "boards": boards}
    finally:
        c.close()


def stock_fund_match(stock_codes, threshold: float = 80.0, top: int = 3) -> dict:
    """个股选基: 找重仓所选个股的基金/ETF (单只占比>阈值否则Top). 回显股票名."""
    out = board_etf.match_funds_for_stocks(DB_PATH, stock_codes, threshold, top)
    out["threshold"] = threshold
    bare = [str(s).split(".")[0] for s in stock_codes]
    nm = {}
    if bare:
        c = _conn()
        try:
            ph = ",".join("?" * len(bare))
            for sc, snm in c.execute(
                f"SELECT stock_code, MAX(stock_name) FROM fund_top_holdings "
                f"WHERE stock_code IN ({ph}) GROUP BY stock_code", bare):
                nm[sc] = snm
        finally:
            c.close()
    out["stocks"] = [{"code": s, "name": nm.get(s) or s} for s in bare]
    return out


def preset_boards(kind: str) -> dict:
    """预设板块集 (供前端预填): kind=top10 (当日趋势主线Top10) / emerging (当日潜在主线)."""
    c = _conn()
    try:
        if kind == "emerging":
            codes = [r[0] for r in c.execute(
                "SELECT board_code FROM emerging_signal_daily "
                "WHERE trade_date=(SELECT MAX(trade_date) FROM emerging_signal_daily)")]
        else:  # top10
            codes = [r[0] for r in c.execute(
                "SELECT board_code FROM board_trend_daily "
                "WHERE trade_date=(SELECT MAX(trade_date) FROM board_trend_daily) "
                "AND rank<=10 ORDER BY rank")]
    finally:
        c.close()
    nm = _board_names(codes)
    return {"kind": kind, "boards": [{"code": bc, "name": nm[bc]} for bc in codes]}


def stock_search(q: str, limit: int = 20) -> dict:
    """个股搜索 (供个股选基自动补全): 在 fund_top_holdings 的股票里按名/代码模糊匹配,
    按持有基金数降序 (相关性)."""
    q = (q or "").strip()
    if not q:
        return {"stocks": []}
    like = f"%{q}%"
    c = _conn()
    try:
        rows = c.execute(
            "SELECT stock_code, MAX(stock_name) AS nm, COUNT(DISTINCT fund_code) AS h "
            "FROM fund_top_holdings WHERE stock_name LIKE ? OR stock_code LIKE ? "
            "GROUP BY stock_code ORDER BY h DESC LIMIT ?", (like, like, limit)).fetchall()
        res = {r[0]: {"code": r[0], "name": r[1], "holders": r[2]} for r in rows}
        # 拼音首字母补充: 纯字母查询(如 ndsd)时, 在全量候选里按首字母匹配, 合并去重
        ql = q.lower()
        if _PY_FL_TABLE and ql.isascii() and ql.isalpha() and len(res) < limit:
            cand = c.execute(
                "SELECT stock_code, MAX(stock_name) AS nm, COUNT(DISTINCT fund_code) AS h "
                "FROM fund_top_holdings GROUP BY stock_code ORDER BY h DESC").fetchall()
            for code, nm, h in cand:
                if code in res:
                    continue
                if py_match(nm or "", ql):
                    res[code] = {"code": code, "name": nm, "holders": h}
                    if len(res) >= limit:
                        break
    finally:
        c.close()
    stocks = sorted(res.values(), key=lambda x: -(x["holders"] or 0))[:limit]
    return {"stocks": stocks}


def hot_stocks(limit: int = 20) -> dict:
    """个股选基的热门股快捷推荐, 4 组:
      抱团核心 = 被最多基金重仓的股票 (机构共识最强);
      高仓押注 = 被≥30只基金持有 且 平均仓位最重 (机构既广持又下重注的核心);
      题材龙头 = 当日 board_leader_daily 板块龙头(rank1) ∩ 被持有 (当下热点旗手);
      接力龙头 = 当日板块 rank2-3 接力位 ∩ 被持有 (轮动接力候选).
    """
    leader_sql = (
        "SELECT substr(l.ts_code,1,6) sc, MAX(h.stock_name) nm, "
        "       COUNT(DISTINCT h.fund_code) hh "
        "FROM board_leader_daily l "
        "JOIN fund_top_holdings h ON h.stock_code = substr(l.ts_code,1,6) "
        "WHERE l.trade_date=(SELECT MAX(trade_date) FROM board_leader_daily) "
        "  AND l.rank {rankcond} "
        "GROUP BY sc ORDER BY hh DESC LIMIT ?")
    c = _conn()
    try:
        core = c.execute(
            "SELECT stock_code, MAX(stock_name) nm, COUNT(DISTINCT fund_code) h "
            "FROM fund_top_holdings GROUP BY stock_code ORDER BY h DESC LIMIT ?",
            (limit,)).fetchall()
        heavy = c.execute(
            "SELECT stock_code, MAX(stock_name) nm, COUNT(DISTINCT fund_code) h "
            "FROM fund_top_holdings GROUP BY stock_code HAVING h >= 30 "
            "ORDER BY AVG(weight) DESC LIMIT ?", (limit,)).fetchall()
        leader1 = c.execute(leader_sql.format(rankcond="= 1"), (limit,)).fetchall()
        leader23 = c.execute(leader_sql.format(rankcond="IN (2,3)"), (limit,)).fetchall()
    finally:
        c.close()
    fmt = lambda rows: [{"code": r[0], "name": r[1], "holders": r[2]} for r in rows]
    return {"groups": [
        {"name": "抱团核心", "stocks": fmt(core)},
        {"name": "高仓押注", "stocks": fmt(heavy)},
        {"name": "题材龙头", "stocks": fmt(leader1)},
        {"name": "接力龙头", "stocks": fmt(leader23)},
    ]}


_PERF_PERIODS = ("近一月", "近三月", "近六月", "近一年", "近两年", "近三年", "今年来", "成立来")


def _fund_performance(fund_code: str):
    """ttjj fund_performance 现拉 (不入库). 失败返回 None (降级, 不阻塞持仓)."""
    try:
        resp = _ttjj_client.call("fund_performance", fund_codes=[fund_code])
    except RuntimeError:
        return None
    items = resp.get("items") or []
    if not items or not items[0].get("是否可用", True):
        return None
    recs = items[0].get("绩效记录") or []
    out = []
    for r in recs:
        pn = r.get("周期名称")
        if pn not in _PERF_PERIODS:
            continue
        out.append({"period": pn, "ret": r.get("收益率"),
                    "ret_rank_pct": r.get("收益率排名百分比"),
                    "maxdd": r.get("最大回撤"), "sharpe": r.get("夏普比率"),
                    "vol": r.get("波动率")})
    out.sort(key=lambda x: _PERF_PERIODS.index(x["period"]))
    return out


def fund_detail(fund_code: str) -> dict:
    """基金下钻详情: 前十大持仓(本地 fund_top_holdings)+ 每只个股所属【白名单板块】
    (stock_concept_map ∩ WHITELIST)+ 业绩(ttjj 现拉). 业绩失败降级 perf=None."""
    WHITELIST.load_if_changed()
    c = _conn()
    try:
        fn = c.execute("SELECT fund_name FROM etf_meta WHERE fund_code=?",
                       (fund_code,)).fetchone()
        fund_name = fn[0] if fn else fund_code
        hrows = c.execute(
            "SELECT stock_code, stock_name, weight, holding_rank, report_date "
            "FROM fund_top_holdings WHERE fund_code=? ORDER BY holding_rank, weight DESC",
            (fund_code,)).fetchall()
        report_date = hrows[0][4] if hrows else None
        # 个股 → 白名单板块 (一次批量反查, 内存归组)
        stock_boards = {}
        codes = [h[0] for h in hrows]
        if codes:
            wl = WHITELIST.codes()
            ph = ",".join("?" * len(codes))
            snap = c.execute("SELECT MAX(snapshot_date) FROM stock_concept_map").fetchone()[0]
            for sc, bc in c.execute(
                f"SELECT substr(ts_code,1,6), board_code FROM stock_concept_map "
                f"WHERE snapshot_date=? AND substr(ts_code,1,6) IN ({ph})", [snap, *codes]):
                if bc in wl:
                    stock_boards.setdefault(sc, []).append(WHITELIST.code_to_name(bc) or bc)
    finally:
        c.close()
    holdings = [{"code": h[0], "name": h[1], "weight": h[2], "rank": h[3],
                 "boards": sorted(set(stock_boards.get(h[0], [])))} for h in hrows]
    return {"fund_code": fund_code, "fund_name": fund_name, "report_date": report_date,
            "holdings": holdings, "perf": _fund_performance(fund_code)}


_MKT_AMT_CACHE = {}  # {"_day": newest_date, (dates_tuple): {date: amount亿}} — EOD 一天只变一次, 跨板块复用
_BREADTH_CACHE = {}  # {(min_yyyymmdd, max_yyyymmdd): {yyyymmdd: (up, dn, flat, tot)}} — regime modal 涨跌家数, 全部档 ~6s 首次后命中


def _market_amount(c, dates):
    """date -> 全市场成交额(亿) = SUM(daily.amount千元)/1e5."""
    dph = ",".join("?" * len(dates))
    return {d: (a or 0.0) for d, a in c.execute(
        f"SELECT trade_date, SUM(amount)/1e5 FROM daily WHERE trade_date IN ({dph}) "
        f"GROUP BY trade_date", list(dates))}


def _regime_history(c, days: int = 20) -> dict:
    """从 regime_classify_daily 取最近 days 个交易日 (rules_version='v2'),
    返回 {rows: [...倒序...], change: {...最近两行对比...} | None}.
    days <= 0 时 LIMIT 0, 返回空. 每行带 csi1000=中证1000(000852.SH)当日收盘, 缺数据日 None."""
    days = max(0, min(9999, int(days)))
    rs = c.execute(
        "SELECT trade_date, regime_code, regime_name, total_score, "
        "score_ma_position, score_advance_decline, score_sentiment_delta, "
        "score_sentiment_index, score_streak_height, score_volume_trend, "
        "last_regime_code, switched, confidence "
        "FROM regime_classify_daily "
        "WHERE rules_version='v2' "
        "ORDER BY trade_date DESC LIMIT ?",
        (days,),
    ).fetchall()
    rows = [{
        "trade_date": r["trade_date"],
        "regime_code": r["regime_code"],
        "regime_name": r["regime_name"],
        "total_score": r["total_score"],
        "switched": int(r["switched"] or 0),
        "confidence": r["confidence"],
        "breakdown": {
            "ma":  r["score_ma_position"],
            "adv": r["score_advance_decline"],
            "sd":  r["score_sentiment_delta"],
            "si":  r["score_sentiment_index"],
            "sk":  r["score_streak_height"],
            "vol": r["score_volume_trend"],
        },
    } for r in rs]
    # 中证1000 收盘对齐: regime 日期 YYYY-MM-DD ↔ index_daily YYYYMMDD.
    # 用 BETWEEN 日期范围而非 IN(全部档 ~2900 日, IN 会撞绑定变量上限).
    if rows:
        keys = [r["trade_date"].replace("-", "") for r in rows]
        cmap = {d: v for d, v in c.execute(
            "SELECT trade_date, close FROM index_daily "
            "WHERE ts_code='000852.SH' AND trade_date BETWEEN ? AND ?",
            (min(keys), max(keys)))}
        for r in rows:
            v = cmap.get(r["trade_date"].replace("-", ""))
            r["csi1000"] = round(v, 2) if v is not None else None
        # 每日涨/跌/平/总家数: daily 表聚合(全部档 ~6s, 进程内缓存 key=(min,max)).
        # 测试桩可能没 daily 表(test_regime_history_empty_table), 失败时所有行给 None.
        try:
            bkey = (min(keys), max(keys))
            bmap = _BREADTH_CACHE.get(bkey)
            if bmap is None:
                bmap = {d: (u, dn, fl, tot) for d, u, dn, fl, tot in c.execute(
                    "SELECT trade_date, "
                    "SUM(CASE WHEN pct_chg>0 THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN pct_chg<0 THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN pct_chg=0 THEN 1 ELSE 0 END), "
                    "COUNT(*) FROM daily WHERE trade_date BETWEEN ? AND ? "
                    "GROUP BY trade_date", bkey)}
                _BREADTH_CACHE[bkey] = bmap
            for r in rows:
                tup = bmap.get(r["trade_date"].replace("-", ""))
                if tup is None:
                    r["up"] = r["dn"] = r["flat"] = r["tot"] = None
                else:
                    r["up"], r["dn"], r["flat"], r["tot"] = tup
        except sqlite3.OperationalError:
            for r in rows:
                r["up"] = r["dn"] = r["flat"] = r["tot"] = None
    change = None
    if len(rows) >= 2:
        curr, prev = rows[0], rows[1]
        delta = (curr["total_score"] or 0) - (prev["total_score"] or 0)
        direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
        change = {
            "prev_date": prev["trade_date"],
            "curr_date": curr["trade_date"],
            "prev_score": prev["total_score"],
            "curr_score": curr["total_score"],
            "delta": delta,
            "direction": direction,
            "prev_regime": prev["regime_name"],
            "curr_regime": curr["regime_name"],
            "switched": bool(curr["switched"]),
        }
    return {"rows": rows, "change": change}


def regime_history(days: int = 20) -> dict:
    c = _conn()
    try:
        return _regime_history(c, days)
    finally:
        c.close()


# ── K线历史 tag 回标: 复用 _classify_stall / _is_continuation / _compute_crowding_alerts ──
_BK_CROWD_CACHE = {"snap": None, "by_date": {}}   # 按 stock_concept_map 快照日缓存, 跨点击复用


def _cont_reasons(row):
    """上涨中继的 hover 理由(无现成 reasons, 就地拼)."""
    r = []
    ret60, dm, vol = row.get("ret60"), row.get("dist_ma20"), row.get("vol_ratio")
    if ret60 is not None:
        r.append(f"ret60 {ret60:+.0f}%")
    if dm is not None:
        r.append(f"距MA20 {dm:+.1f}%")
    if vol is not None:
        r.append(f"量能×{vol:.2f}")
    return r


def _crowding_by_code(c, d):
    """该交易日 board_code -> 拥挤标记 dict. 按成分股快照日缓存(K线点击间复用, 避免逐日全表扫重算)."""
    try:
        snap = c.execute("SELECT MAX(snapshot_date) FROM stock_concept_map").fetchone()[0]
    except sqlite3.Error:
        snap = None
    if _BK_CROWD_CACHE["snap"] != snap:
        _BK_CROWD_CACHE.update(snap=snap, by_date={})
    cache = _BK_CROWD_CACHE["by_date"]
    if d not in cache:
        cache[d] = _compute_crowding_alerts(c, d).get("by_code", {})
    return cache[d]


def _regime_map(c, dates):
    """{YYYYMMDD: regime_code}, 取自 regime_classify_daily(其 trade_date 为 YYYY-MM-DD)."""
    if not dates:
        return {}
    dashed = {f"{d[:4]}-{d[4:6]}-{d[6:8]}": d for d in dates}
    ph = ",".join("?" * len(dashed))
    try:
        return {dashed[td]: code for td, code in c.execute(
            f"SELECT trade_date, regime_code FROM regime_classify_daily "
            f"WHERE trade_date IN ({ph})", list(dashed.keys())) if td in dashed}
    except sqlite3.Error:
        return {}


def _stall_shown(stall, regime_code):
    """stall 显示口径(K线弹窗与盘面雷达共用, 单一真相): 量价背离已下线;
    高位出货仅在 STRONG_BULL regime 显示. 返回应显示的 stall dict, 否则 None."""
    if stall and stall.get("kind") == "高位出货" and regime_code in TAG_REGIME_STALL:
        return stall
    return None


def _cont_shown(regime_code):
    """上涨中继显示口径: 仅 STRONG_BULL + NEUTRAL_RANGE regime."""
    return regime_code in TAG_REGIME_CONT


def _classify_board_tag(row, regime_code, crowding_alert):
    """单板块当日 K线信号(单一真相, 卡片徽章与 K线图三角共用).

    高位出货(放量, level red, 仅 STRONG_BULL) 优先 → 上涨中继(缩量回踩, level cont,
    STRONG_BULL+NEUTRAL_RANGE). 两者互斥. 量价背离已下线(_stall_shown 卡掉).
    返回 {"kind","level","label","reasons"} 或 None. 改阈值/regime 一处生效.
    """
    if not row:
        return None
    shown = _stall_shown(_classify_stall(row, crowding_alert), regime_code)
    if shown:
        return {"kind": "高位出货", "level": shown["level"],
                "label": "高位出货", "reasons": shown.get("reasons", [])}
    if _is_continuation(row) and _cont_shown(regime_code):
        return {"kind": "上涨中继", "level": "cont",
                "label": "上涨中继", "reasons": _cont_reasons(row)}
    return None


def _board_tags(c, code, dates):
    """对 dates 每个交易日重算 高位出货/上涨中继, 返回 [{i,kind,level,reasons}].

    按触发日的大盘 regime 卡显示(回测 2026-06-04 验证):
      - 高位出货: 仅 STRONG_BULL 强牛 (其它 regime 触发后仍续涨/反向, 看跌无效);
      - 上涨中继: STRONG_BULL + NEUTRAL_RANGE (强势震荡抛硬币/弱市无效);
      - 量价背离: 已彻底下线(任何 regime 看跌无效), _classify_stall 返回它时忽略.
    高位出货(若命中)与中继互斥(放量 vs 缩量), 不会同日双标. 不落库, 阈值/regime 改了自动跟着变.
    性能: 拥挤计算贵, 先用廉价前置门槛(above_ma20 且高位)过滤, 命中才算拥挤; 再叠跨点击缓存.
    """
    if not dates:
        return []
    dates = list(dates)
    dph = ",".join("?" * len(dates))
    rows = {r["trade_date"]: dict(r) for r in c.execute(
        f"SELECT * FROM board_trend_daily WHERE board_code=? AND trade_date IN ({dph})",
        [code] + dates)}
    regime = _regime_map(c, dates)
    out = []
    for i, d in enumerate(dates):
        row = rows.get(d)
        if not row:
            continue
        reg = regime.get(d)
        ret60, ret20 = row.get("ret60"), row.get("ret20")
        high = (ret60 is not None and ret60 >= STALL_RET60_MIN) or \
               (ret20 is not None and ret20 >= STALL_RET20_MIN)
        # 廉价前置门槛: 仅高位才值得算昂贵的拥挤; 否则 crowd=None(高位出货自然不成立, 落中继)
        crowd = _crowding_by_code(c, d).get(code) if (row.get("above_ma20") and high) else None
        tag = _classify_board_tag(row, reg, crowd)
        if tag:
            out.append({"i": i, "kind": tag["kind"], "level": tag["level"],
                        "reasons": tag["reasons"]})
    return out


def _emerging_tags(c, code, dates):
    """萌芽主线标记: emerging_signal_daily 命中日 → [{i, kind:"潜在主线", actionable, regime, reasons}].
    与 K线弹窗 上涨中继/高位出货 同机制(按日期索引 i 标), 但独立来源(萌芽探测器 emerging.py),
    含撞门非白名单板块. 不落库依赖 board_trend_daily, 故非白名单板块也能标."""
    if not dates:
        return []
    dates = list(dates)
    dph = ",".join("?" * len(dates))
    SIGN = {"price_accel": "涨速", "share_trend": "占比", "breadth": "广度"}
    rows = {}
    for d, act, reg, fired, pdays, iswl in c.execute(
        f"SELECT trade_date, actionable, regime_code, fired, persist_days, is_whitelist "
        f"FROM emerging_signal_daily WHERE board_code=? AND trade_date IN ({dph})",
        [code] + dates):
        rows[d] = (act, reg, fired, pdays, iswl)
    out = []
    for i, d in enumerate(dates):
        if d not in rows:
            continue
        act, reg, fired, pdays, iswl = rows[d]
        try:
            sigs = json.loads(fired) if fired else []
        except Exception:
            sigs = []
        rs = ["+".join(SIGN.get(s, s) for s in sigs), f"持{pdays}日"]
        if not iswl:
            rs.append("撞门")
        rs.append("强牛可操作" if act else f"{reg or '?'}·仅观察")
        out.append({"i": i, "kind": "潜在主线", "actionable": int(act or 0),
                    "regime": reg, "reasons": [x for x in rs if x]})
    return out


def _table_exists(c, name: str) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _board_kline(c, code, n=60, mkt_cache=None):
    """单板块近 n 交易日: 走势线(pct_change 复利还原) + 每日成交额(亿) + 占大盘比(%)
    + 融资余额(亿)柱 + 融资余额/板块总市值(%)线."""
    dates = [r[0] for r in c.execute(
        "SELECT trade_date FROM concept_board_daily WHERE board_code=? "
        "ORDER BY trade_date DESC LIMIT ?", (code, n))][::-1]
    if not dates:
        return {"board_code": code, "board_name": None,
                "dates": [], "idx": [], "amount": [], "share": []}
    bn = c.execute("SELECT board_name FROM concept_board_daily WHERE board_code=? "
                   "ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
    board_name = bn[0] if bn else None
    dph = ",".join("?" * len(dates))
    pmap = {d: p for d, p in c.execute(
        f"SELECT trade_date, pct_change FROM concept_board_daily "
        f"WHERE board_code=? AND trade_date IN ({dph})", [code] + dates)}
    idx = _recon_index([pmap.get(d) for d in dates])
    bamt = {d: (a or 0.0) for d, a in c.execute(
        f"SELECT dd.trade_date, SUM(dd.amount)/1e5 "
        f"FROM stock_concept_map scm JOIN daily dd ON scm.ts_code=dd.ts_code "
        f"WHERE scm.board_code=? "
        f"AND scm.snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map) "
        f"AND dd.trade_date IN ({dph}) GROUP BY dd.trade_date", [code] + dates)}
    if mkt_cache is not None:
        if mkt_cache.get("_day") != dates[-1]:
            mkt_cache.clear()
            mkt_cache["_day"] = dates[-1]
        key = tuple(dates)
        mamt = mkt_cache.get(key)
        if mamt is None:
            mamt = _market_amount(c, dates)
            mkt_cache[key] = mamt
    else:
        mamt = _market_amount(c, dates)
    amount = [round(bamt[d], 3) if d in bamt else None for d in dates]
    share = []
    for d in dates:
        b, m = bamt.get(d), mamt.get(d)
        share.append(round(b / m * 100, 3) if (b is not None and m) else None)
    # trend_score 历史序列: 与 dates 一一对应, 无数据日给 None (前端断笔)
    smap = {d: s for d, s in c.execute(
        f"SELECT trade_date, trend_score FROM board_trend_daily "
        f"WHERE board_code=? AND trade_date IN ({dph})", [code] + dates)}
    score = [round(smap[d], 1) if d in smap else None for d in dates]
    # 融资余额(元)逐日: 成分股 margin_security_daily.rzye 合计 (仿 amount, 即时聚合)
    rzmap = {d: (v or 0.0) for d, v in c.execute(
        f"SELECT msd.trade_date, SUM(msd.rzye) "
        f"FROM stock_concept_map scm JOIN margin_security_daily msd ON scm.ts_code=msd.ts_code "
        f"WHERE scm.board_code=? "
        f"AND scm.snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map) "
        f"AND msd.trade_date IN ({dph}) GROUP BY msd.trade_date", [code] + dates)}
    # 板块总市值(万元)逐日: 成分股 daily_basic.total_mv 合计 (融资/总市值 分母)
    mvmap = {d: (v or 0.0) for d, v in c.execute(
        f"SELECT db.trade_date, SUM(db.total_mv) "
        f"FROM stock_concept_map scm JOIN daily_basic db ON scm.ts_code=db.ts_code "
        f"WHERE scm.board_code=? "
        f"AND scm.snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map) "
        f"AND db.trade_date IN ({dph}) GROUP BY db.trade_date", [code] + dates)} \
        if _table_exists(c, "daily_basic") else {}
    rzye = [round(rzmap[d] / 1e8, 3) if d in rzmap else None for d in dates]
    rz_mv_pct = []
    for d in dates:
        rz, mv = rzmap.get(d), mvmap.get(d)
        rz_mv_pct.append(round(rz / (mv * 10000) * 100, 3)
                         if (rz is not None and mv) else None)
    tags = _board_tags(c, code, dates) + _emerging_tags(c, code, dates)
    return {"board_code": code, "board_name": board_name, "dates": dates,
            "idx": [round(v, 4) for v in idx], "amount": amount, "share": share,
            "score": score, "rzye": rzye, "rz_mv_pct": rz_mv_pct, "tags": tags}


def board_kline(code, n=60):
    c = _conn()
    try:
        return _board_kline(c, code, n, mkt_cache=_MKT_AMT_CACHE)
    finally:
        c.close()


def _lead_stocks(c, board_codes, top: int = 5):
    """board_code -> 今日领涨 top N [{code,name,pct}], 取自 intraday_board_members.lead_json.
    盘后/无帧 → 该板块缺省 []."""
    if not board_codes:
        return {}
    ph = ",".join("?" * len(board_codes))
    out = {}
    for bc, lj in c.execute(
        f"SELECT board_code, lead_json FROM intraday_board_members "
        f"WHERE board_code IN ({ph})", list(board_codes)):
        arr = json.loads(lj or "[]")[:top]
        out[bc] = [{"code": s.get("c"), "name": s.get("n"), "pct": s.get("pct")} for s in arr]
    return out


def in_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


_PERF_SETTLED = {"key": None, "data": None}   # 已结算段缓存, 按 latest_t_date 失效
_PERF_CACHE = {"ts": 0.0, "data": None}
_PERF_TTL = 60   # 秒; 盘中今日点随盘刷新(只跑廉价 overlay), 已结算段(含复权全表扫描)按结算日缓存
_PERF_LOCK = threading.Lock()   # 保护已结算段重建: 用户请求与后台保活撞车时只扫一次


def cached_perf() -> dict:
    now = time.time()
    if _PERF_CACHE["data"] is not None and now - _PERF_CACHE["ts"] < _PERF_TTL:
        return _PERF_CACHE["data"]
    c = _conn()
    try:
        key = perf.latest_t_date(c)   # 已结算段仅随结算日推进(每日一次)重算, 避免 60s 轮询反复全表扫描
        with _PERF_LOCK:              # 锁内重建: 并发触发时第二个等锁后见 key 已匹配直接跳过, 绝不重复全表扫
            if _PERF_SETTLED["data"] is None or _PERF_SETTLED["key"] != key:
                _PERF_SETTLED.update(key=key, data=perf.compute_settled(c))
            settled = _PERF_SETTLED["data"]
        data = perf.overlay_live(c, settled)   # 廉价 overlay 放锁外, 并发请求不互相阻塞
    finally:
        c.close()
    _PERF_CACHE.update(ts=now, data=data)
    return data


def _keep_warm(interval: int = 300, warmup_delay: int = 20):
    """后台预热 + 保活: 把 ~10s 复权全表扫挪到后台, 用户与 dashboard 启动都不等。

    warmup_delay: 启动后先等一会再预热, 避让 dashboard 启动风暴, 不与其他服务抢 CPU。
    之后每 interval 探一次 latest_t_date: 没变只跑廉价 overlay(几十 ms); 变了(每日结算)才在后台重算。
    休眠线程 OS 不调度, 稳态 CPU ≈ 0。
    """
    time.sleep(warmup_delay)
    while True:
        try:
            cached_perf()
        except Exception:
            pass
        # 预热 since924 缓存，避免首位用户等全表扫描
        try:
            _c = _conn()
            try:
                since924.stocks_payload(_c)
                since924.concept_payload(_c)
            finally:
                _c.close()
        except Exception:
            pass
        time.sleep(interval)


def pick_perf_payload(reviewer=None, since=None) -> dict:
    """推票战绩 payload: 读 daily_pick 逐笔回测 + 聚合。只读。"""
    c = _conn()
    try:
        return pick_perf.evaluate_all(c, reviewer=reviewer, since=since)
    finally:
        c.close()


def _is_continuation(row):
    """上涨中继: 中长期趋势在 + 缩量回踩 MA20. 纯函数(EOD), 返回 True/False.

    需 row 含 above_ma60/ret60/dist_ma20/ret5/vol_ratio.
    高位出货(放量)的镜像: 这里要求"不放量"(vol_ratio ≤ 1.0).
    """
    if not row:
        return False
    if not row.get("above_ma60"):                       # 长期趋势破 → 不是中继
        return False
    ret60 = row.get("ret60")
    if ret60 is None or ret60 < CONT_RET60_MIN:         # 中期没涨够 → 低位, 不是中继
        return False
    dm = row.get("dist_ma20")
    if dm is None or dm < CONT_DIST_MA20_LO or dm > CONT_DIST_MA20_HI:  # 没回到 MA20 一带
        return False
    ret5 = row.get("ret5")
    if ret5 is None or ret5 > CONT_RET5_MAX:            # 近5日还在冲 → 没回踩
        return False
    vol = row.get("vol_ratio")
    if vol is None or vol > CONT_VOL_MAX:               # 放量 → 可能出货, 不是健康回调
        return False
    return True


def _compute_emerging(c, trade_date: str, window: int = 5, top_n: int = 10) -> list:
    """[DEPRECATED 2026-06 由 emerging.py + emerging_signal_daily 取代, 不再被 build_payload 调用] 潜在主线发现: 看最近 window 个交易日里, 板块按 trend_score 排名上升幅度.

    口径: 每个交易日的 board_trend_daily 按 trend_score 降序排 rank (1=最强).
          rank_old (window 日前) - rank_now > 0 即排名上升, 数字越大上升越猛.
    新进榜的板块 (window 日前没数据) 给 rank_old = MAX_RANK 兜底, 仍能识别"突然杀进"的主线.

    返回 list[dict]: board_code, board_name, score_now, rank_now, rank_old, rank_delta,
                     score_old, score_delta. 按 rank_delta 降序 (上升最猛在前).
    """
    # 取最近 window+1 个有 board_trend_daily 数据的交易日
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM board_trend_daily "
        "WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
        (trade_date, window + 1))]
    if len(dates) < 2:
        return []
    date_now, date_old = dates[0], dates[-1]
    # 直接读 board_trend_daily.rank (写入时已按 score 降序 enumerate)
    rows_now = c.execute(
        "SELECT board_code, board_name, trend_score, rank FROM board_trend_daily "
        "WHERE trade_date=? ORDER BY rank", (date_now,)).fetchall()
    rows_old = c.execute(
        "SELECT board_code, trend_score, rank FROM board_trend_daily "
        "WHERE trade_date=?", (date_old,)).fetchall()
    rank_old = {r[0]: (r[2], r[1]) for r in rows_old}
    max_old_rank = max((r[2] for r in rows_old), default=0) + 1  # 新进榜兜底
    out = []
    for bc, bn, score_now, rank_now in rows_now:
        ro, score_old = rank_old.get(bc, (max_old_rank, None))
        rank_delta = ro - rank_now  # 正数表示上升, 数字越大上升越猛
        if rank_delta <= 0:
            continue
        # 突然杀进榜的板块 score_old 为 None: 按 0 算 score_delta
        score_delta = (score_now - (score_old or 0)) if score_old is not None else score_now
        out.append({
            "board_code": bc, "board_name": bn,
            "score_now": round(score_now, 1), "rank_now": rank_now,
            "rank_old": ro, "rank_delta": rank_delta,
            "score_old": round(score_old, 1) if score_old is not None else None,
            "score_delta": round(score_delta, 1),
            "is_new": score_old is None,  # 5 日前不在白名单/数据里
        })
    # 优先看 rank 上升幅度, 次比 score 上升幅度 (rank 同位时 score 增长更猛在前)
    out.sort(key=lambda r: (-r["rank_delta"], -r["score_delta"]))
    return out[:top_n]


def _compute_continuation(c, trade_date, top_n=10):
    """上涨中继扫描: 该日全量 board_trend_daily 行 → 筛 _is_continuation → 按 ret60 降序 → top_n.
    (镜像 _compute_emerging 的扫全量写法; 中继板块排名会掉出 Top10, 故不能只看 Top10.)
    """
    rows = [dict(r) for r in c.execute(
        "SELECT board_code,board_name,trend_score,ret20,ret60,ret5,dist_ma20,vol_ratio,"
        "amt_share,amt_share_trend,ma_aligned,above_ma20,above_ma60,lead_code,lead_name,rank "
        "FROM board_trend_daily WHERE trade_date=?", (trade_date,))]
    hits = [r for r in rows if _is_continuation(r)]
    hits.sort(key=lambda r: -(r.get("ret60") or 0))
    return hits[:top_n]


def _classify_stall(row, crowding_alert):
    """高位滞涨结构预警 (EOD, 只吃 board_trend_daily 字段 + 拥挤标记).

    输入: 一行 board_trend_daily dict(需含 above_ma20/ret60/ret20/ret5/vol_ratio/
          amt_peak_ratio/amt_share_trend) + 该板块拥挤标记 dict|None.
    返回: None 或 {"kind","level","label","reasons"}.

    高位 = above_ma20 且 (ret60>=30% 或 ret20>=20%) 且 有拥挤标记
    滞涨 = 动量衰减: 近5日涨速 < 中期5日均速×0.4, 且近5日涨幅 <= +3%
    放量(高位出货, red) 优先于 缩量(量价背离, orange).
    """
    if not row:
        return None
    if not row.get("above_ma20"):                  # 破位 → 退潮管, 不报
        return None
    ret60, ret20 = row.get("ret60"), row.get("ret20")
    high = (ret60 is not None and ret60 >= STALL_RET60_MIN) or \
           (ret20 is not None and ret20 >= STALL_RET20_MIN)
    if not high:
        return None
    if not crowding_alert:                          # 不拥挤 → 不是热门高位
        return None
    ret5 = row.get("ret5")
    # 滞涨 = 动量衰减: 近5日涨速明显低于该板块中期(20日)涨速, 且近5日本身没怎么涨.
    # 中期5日均速 base5 = ret20/4 (20日≈4个5日窗口), 缺则退 ret60/12.
    base5 = (ret20 / 4.0) if (ret20 is not None and ret20 > 0) else \
            ((ret60 / 12.0) if (ret60 is not None and ret60 > 0) else None)
    if base5 is None or base5 <= 0:                 # 中期没涨 → 不该到这(高位门槛已挡)
        return None
    decel = (ret5 is not None) and (ret5 < STALL_DECEL_RATIO * base5)
    if ret5 is None or ret5 > STALL_RET5_ABS_MAX or not decel:  # 还在猛涨 或 没衰减 → 不算滞涨
        return None

    vol = row.get("vol_ratio")
    peak = row.get("amt_peak_ratio")
    share_tr = row.get("amt_share_trend")
    hot = (vol is not None and vol >= STALL_VOL_HOT) or \
          (peak is not None and peak >= STALL_PEAK_HOT)
    hot_money_ok = (share_tr is None) or (share_tr >= 0)   # 钱没撤
    cold = (vol is not None and vol <= STALL_VOL_COLD) or \
           (share_tr is not None and share_tr < STALL_SHARE_DROP)

    reasons = []
    if ret60 is not None:
        reasons.append(f"ret60 {ret60:+.0f}%")
    if vol is not None:
        reasons.append(f"量能×{vol:.2f}")
    if ret5 is not None:
        reasons.append(f"近5日 {ret5:+.1f}%")

    if hot and hot_money_ok:
        return {"kind": "高位出货", "level": "red", "label": "高位出货", "reasons": reasons}
    if cold:
        return {"kind": "量价背离", "level": "orange", "label": "量价背离", "reasons": reasons}
    return None


def _compute_crowding_alerts(c, trade_date: str) -> dict:
    """日度资金拥挤/虹吸预警.

    数据只取 EOD 的 board_trend_daily. 大板块用绝对成交占比识别市场虹吸;
    中小板块用自身历史分位识别相对拥挤. 历史不足时不做相对分位预警,
    这样后续 board_trend_daily 积累够长后会自然生效。
    """
    if not trade_date:
        return {"trade_date": None, "min_history": CROWDING_MIN_HISTORY,
                "large_siphon": [], "relative": [], "by_code": {}}
    try:
        member_count = {r["board_code"]: r["n"] for r in c.execute(
            "SELECT board_code, COUNT(*) AS n FROM stock_concept_map "
            "WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map) "
            "GROUP BY board_code")}
    except sqlite3.Error:
        member_count = {}

    rows = [dict(r) for r in c.execute(
        "SELECT trade_date,board_code,board_name,trend_score,ret20,ret60,ret5,amt_peak_ratio,"
        "amt_share,amt_share_trend,vol_ratio,ma_aligned,above_ma20,above_ma60,dist_ma20,"
        "lead_code,lead_name,rank "
        "FROM board_trend_daily "
        "WHERE trade_date<=? AND amt_share IS NOT NULL "
        "ORDER BY board_code, trade_date", (trade_date,))]
    hist = defaultdict(list)
    for r in rows:
        hist[r["board_code"]].append(r)
    current = [rs[-1] for rs in hist.values() if rs and rs[-1]["trade_date"] == trade_date]

    large_siphon, relative, by_code = [], [], {}
    skipped_relative = 0
    for r in current:
        bc = r["board_code"]
        h = hist.get(bc, [])
        share = r.get("amt_share") or 0.0
        mcount = member_count.get(bc, 0)
        bucket = "large" if mcount > LARGE_BOARD_MEMBERS else ("mid" if mcount > 30 else "small")
        is_top10 = (r.get("rank") or 9999) <= 10
        history_days = len(h)
        own_pct = None
        if history_days >= CROWDING_MIN_HISTORY:
            vals = [x.get("amt_share") for x in h if x.get("amt_share") is not None]
            own_pct = sum(1 for v in vals if v <= share) / len(vals) if vals else None
        elif bucket != "large":
            skipped_relative += 1

        base = {
            "trade_date": r["trade_date"], "board_code": bc, "board_name": r.get("board_name"),
            "rank": r.get("rank"), "is_top10": is_top10,
            "member_count": mcount, "bucket": bucket,
            "amt_share": round(share, 5),
            "amt_share_pct": round(share * 100, 1),
            "amt_share_trend": r.get("amt_share_trend"),
            "ret20": r.get("ret20"), "ret60": r.get("ret60"),
            "trend_score": r.get("trend_score"),
            "vol_ratio": r.get("vol_ratio"), "ma_aligned": r.get("ma_aligned"),
            "ret5": r.get("ret5"), "amt_peak_ratio": r.get("amt_peak_ratio"),
            "above_ma20": r.get("above_ma20"),
            "above_ma60": r.get("above_ma60"), "dist_ma20": r.get("dist_ma20"),
            "lead_code": r.get("lead_code"), "lead_name": r.get("lead_name"),
            "history_days": history_days,
            "own_pctile": round(own_pct * 100, 1) if own_pct is not None else None,
        }

        alerts = []
        if bucket == "large" and share >= 0.07:
            if share >= 0.10:
                level, label, short = "red", "极度虹吸", "极虹吸"
            elif share >= 0.094:
                level, label, short = "orange", "强虹吸", "强虹吸"
            elif share >= 0.085:
                level, label, short = "yellow", "中度虹吸", "中虹吸"
            else:
                level, label, short = "watch", "轻度虹吸", "轻虹吸"
            days_094 = 0
            days_100 = 0
            for x in reversed(h):
                sx = x.get("amt_share") or 0.0
                if sx >= 0.094:
                    days_094 += 1
                else:
                    break
            for x in reversed(h):
                sx = x.get("amt_share") or 0.0
                if sx >= 0.10:
                    days_100 += 1
                else:
                    break
            a = {**base, "kind": "siphon", "level": level, "label": label,
                 "short_label": short, "days_ge_094": days_094,
                 "days_ge_100": days_100}
            if not is_top10:  # Top10 已在趋势主线展示, 本面板不重复
                large_siphon.append(a)
            alerts.append(a)

        if own_pct is not None and own_pct >= 0.90:
            if own_pct >= 0.98:
                level, label, short = "red", "极度拥挤", "P98拥挤"
            elif own_pct >= 0.95:
                level, label, short = "orange", "高度拥挤", "P95拥挤"
            else:
                level, label, short = "yellow", "轻度拥挤", "P90拥挤"
            a = {**base, "kind": "relative", "level": level, "label": label,
                 "short_label": short}
            if bucket != "large" and not is_top10:  # Top10 已在趋势主线展示, 本面板不重复
                relative.append(a)
            alerts.append(a)

        if alerts:
            by_code[bc] = max(alerts, key=lambda x: _alert_rank(x.get("level")))

    def sort_key(a):
        top_penalty = 1 if a.get("is_top10") else 0
        return (-_alert_rank(a.get("level")), top_penalty,
                -(a.get("own_pctile") or 0), -(a.get("amt_share") or 0), a.get("rank") or 9999)

    large_siphon.sort(key=lambda a: (-_alert_rank(a.get("level")), -(a.get("amt_share") or 0), a.get("rank") or 9999))
    relative.sort(key=sort_key)
    return {
        "trade_date": trade_date,
        "min_history": CROWDING_MIN_HISTORY,
        "large_member_min": LARGE_BOARD_MEMBERS + 1,
        "skipped_relative": skipped_relative,
        "large_siphon": large_siphon[:8],
        "relative": relative[:10],
        "by_code": by_code,
    }


def radar_board_codes(c, trade_date: str) -> set:
    """盘面雷达四面板板块码并集: 趋势主线Top10 + 资金拥挤 + 潜在主线 + 上涨中继.

    与 build_payload 渲染口径一致, 供 board_etf 决定"给哪些板块算基金匹配".
    各子项 best-effort: 任一失败(数据不足/表缺)只影响该面板, 不致命.
    """
    codes: set = set()
    try:
        codes |= {r[0] for r in c.execute(
            "SELECT board_code FROM board_trend_daily WHERE trade_date=? AND rank<=10",
            (trade_date,))}
    except Exception:
        pass
    try:
        ca = _compute_crowding_alerts(c, trade_date)
        codes |= {a["board_code"] for a in
                  ca.get("large_siphon", []) + ca.get("relative", [])}
    except Exception:
        pass
    try:
        # 潜在主线改读萌芽探测器输出(emerging.py 写, 在 board_etf 之前跑); 含撞门非白名单板块
        codes |= {r[0] for r in c.execute(
            "SELECT board_code FROM emerging_signal_daily WHERE trade_date=?",
            (trade_date,))}
    except Exception:
        pass
    try:
        codes |= {r["board_code"] for r in
                  _compute_continuation(c, trade_date, top_n=10)}
    except Exception:
        pass
    return codes


def _synth_from_trigger(t: dict) -> dict:
    """已掉出当前快照但已触发的候选 → 合成一行(用 trigger_log 定格的买点+滚动浮盈)."""
    return {
        "snapshot_time": t.get("last_time"), "strategy": t["strategy"], "code": t["code"],
        "ts_code": t.get("ts_code"), "name": t.get("name"), "industry": t.get("industry"),
        "cand_date": t.get("trade_date"), "price": t.get("last_price"),
        "pct": None, "vol_ratio": None, "turnover_rate": None, "amount": None,
        "speed_5min": None, "net_main": None,
        "entry_low": t.get("entry_low"), "entry_high": t.get("entry_high"),
        "stop_loss": t.get("stop_loss"), "target_1": t.get("target_1"),
        "target_2": t.get("target_2"), "position_pct": t.get("position_pct"),
        "dist_to_entry": None, "signal": "持有跟踪",
        "top_theme": t.get("mainline"), "top_theme_pct": None,
        "logic": {"themes": [], "zsxq": [], "summary": None, "refresh_time": None},
        "trig": t, "dropped": True,
    }


def load_reviews(c, today: str) -> dict:
    """今日 intraday_review -> {code: {reviewer: {logic_stars, action_stars, summary, reviewed_at}}}.
    表尚未建(全新库) -> 兜底空 dict."""
    out = {}
    try:
        cur = c.execute(
            "SELECT code, reviewer, logic_stars, action_stars, summary, reviewed_at "
            "FROM intraday_review WHERE trade_date=?", (today,))
    except sqlite3.OperationalError:
        return out
    for r in cur:
        out.setdefault(r["code"], {})[r["reviewer"]] = {
            "logic_stars": r["logic_stars"], "action_stars": r["action_stars"],
            "summary": r["summary"], "reviewed_at": r["reviewed_at"]}
    return out


def load_board_reviews(c, today: str) -> dict:
    """今日 intraday_board_review -> {board_code: {reviewer: {summary, logic_stars, continuation_stars, reviewed_at}}}.
    logic_stars 是 5-27 加的列, 旧数据 NULL — 前端容错为不渲染逻辑星. 表尚未建(全新库) -> 兜底空 dict."""
    out = {}
    try:
        cur = c.execute(
            "SELECT board_code, reviewer, summary, logic_stars, continuation_stars, reviewed_at "
            "FROM intraday_board_review WHERE trade_date=?", (today,))
    except sqlite3.OperationalError:
        return out
    for r in cur:
        out.setdefault(r["board_code"], {})[r["reviewer"]] = {
            "summary": r["summary"], "logic_stars": r["logic_stars"],
            "continuation_stars": r["continuation_stars"],
            "reviewed_at": r["reviewed_at"]}
    return out


def load_picks(c, today: str) -> dict:
    """今日 daily_pick: 取最新 slot, 按 reviewer 分组返回.

    slot 优先级 intraday_pm > intraday_am > premarket. 表尚未建 -> 兜底空 dict.
    返回 {"slot": <最新 slot 或 None>, "by_reviewer": {bot_id: [pick_row...]}}.
    deep_research_json 字段会被解析成 deep_research 子 dict (前端友好).
    """
    try:
        latest = c.execute(
            "SELECT slot FROM daily_pick WHERE trade_date=? "
            "ORDER BY CASE slot "
            "  WHEN 'intraday_pm' THEN 3 "
            "  WHEN 'intraday_am' THEN 2 "
            "  ELSE 1 END DESC LIMIT 1", (today,)).fetchone()
    except sqlite3.OperationalError:
        return {"slot": None, "by_reviewer": {}}
    if not latest:
        return {"slot": None, "by_reviewer": {}}
    slot = latest["slot"]
    rows = c.execute(
        "SELECT * FROM daily_pick WHERE trade_date=? AND slot=? "
        "ORDER BY reviewer, rank", (today, slot)).fetchall()
    by_reviewer: dict = {}
    for r in rows:
        d = dict(r)
        d["deep_research"] = json.loads(d.pop("deep_research_json") or "{}")
        verdict = (d["deep_research"].get("verdict") or {}) if isinstance(d["deep_research"], dict) else {}
        d["target_price"] = verdict.get("target_price")
        by_reviewer.setdefault(r["reviewer"], []).append(d)
    return {"slot": slot, "by_reviewer": by_reviewer}


def _validate_holding(code, cost_price):
    """校验持仓输入. 返回 (ok: bool, err: str|None)."""
    if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
        return False, "代码必须是 6 位数字"
    try:
        cp = float(cost_price)
    except (TypeError, ValueError):
        return False, "成本价必须是数字"
    if cp <= 0:
        return False, "成本价必须为正数"
    return True, None


def insert_holding(c, code, name, cost_price, added_at, owner_id):
    """加一笔持仓(每次跟买独立成笔, 同 code 可多笔), 归属 owner_id. 返回新 lot id."""
    cur = c.execute(
        "INSERT INTO follow_holding(code, name, cost_price, added_at, owner_id) VALUES (?,?,?,?,?)",
        (code, name, float(cost_price), added_at, owner_id))
    c.commit()
    return cur.lastrowid


def update_holding_cost(c, holding_id, cost_price, owner_id):
    """改某笔持仓成本价(按 id+owner, 不动 added_at). 返回是否命中(归属不符=未命中)."""
    cur = c.execute(
        "UPDATE follow_holding SET cost_price=? WHERE id=? AND owner_id=?",
        (float(cost_price), holding_id, owner_id))
    c.commit()
    return cur.rowcount > 0


def delete_holding(c, holding_id, owner_id):
    c.execute("DELETE FROM follow_holding WHERE id=? AND owner_id=?", (holding_id, owner_id))
    c.commit()


def _holding_price(c, code):
    """持仓现价解析: ① 最新 intraday_candidate_live 任意策略行 → ② daily 表最新收盘价.
    都查不到返回 None."""
    r = c.execute(
        "SELECT price FROM intraday_candidate_live WHERE code=? AND price IS NOT NULL "
        "ORDER BY snapshot_time DESC LIMIT 1", (code,)).fetchone()
    if r and r["price"] is not None:
        return r["price"]
    # daily 兜底: ts_code 带后缀, trade_date YYYYMMDD
    r = c.execute(
        "SELECT close FROM daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
        (scout_db.to_suffix(code),)).fetchone()
    return r["close"] if r and r["close"] is not None else None


def _holding_rec_meta(c, code):
    """该 code 的历史推荐元信息: 首次推荐日 / 推荐交易日数(去重) / 最新一条计划.
    daily_pick 未建表 -> 全兜底(pick_days=0, 其余 None). 计划取 picked_at 最新行."""
    meta = {"first_pick": None, "pick_days": 0, "entry_low": None,
            "entry_high": None, "stop_loss": None, "target_price": None}
    try:
        agg = c.execute(
            "SELECT MIN(trade_date) AS first_pick, COUNT(DISTINCT trade_date) AS pick_days "
            "FROM daily_pick WHERE code=?", (code,)).fetchone()
    except sqlite3.OperationalError:
        return meta
    if not agg or not agg["pick_days"]:
        return meta
    meta["first_pick"] = agg["first_pick"]
    meta["pick_days"] = agg["pick_days"]
    latest = c.execute(
        "SELECT entry_low, entry_high, stop_loss, deep_research_json FROM daily_pick "
        "WHERE code=? ORDER BY picked_at DESC LIMIT 1", (code,)).fetchone()
    if latest:
        meta["entry_low"] = latest["entry_low"]
        meta["entry_high"] = latest["entry_high"]
        meta["stop_loss"] = latest["stop_loss"]
        try:
            dr = json.loads(latest["deep_research_json"] or "{}")
            verdict = (dr.get("verdict") or {}) if isinstance(dr, dict) else {}
            meta["target_price"] = verdict.get("target_price")
        except (ValueError, TypeError):
            pass
    return meta


def load_holdings(c, owner_id):
    """全部持仓 + 实时现价 + 相对成本盈亏% + 历史推荐元信息. 表未建 → 兜底空 list.
    返回 [{code, name, cost_price, price, pl_pct,
           first_pick, pick_days, entry_low, entry_high, stop_loss, target_price}],
    按 added_at 倒序(新加在前). 推荐字段来自 daily_pick 历史, 无记录则兜底为 None/0."""
    try:
        rows = c.execute(
            "SELECT id, code, name, cost_price, added_at FROM follow_holding "
            "WHERE owner_id=? ORDER BY added_at DESC, id DESC", (owner_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        price = _holding_price(c, r["code"])
        cost = r["cost_price"]
        pl_pct = round((price - cost) / cost * 100, 2) if (price is not None and cost) else None
        row = {"id": r["id"], "code": r["code"], "name": r["name"], "cost_price": cost,
               "price": price, "pl_pct": pl_pct}
        row.update(_holding_rec_meta(c, r["code"]))
        out.append(row)
    return out


def load_pick_history(c):
    """daily_pick 里所有历史推荐过的 (code, name) 去重, 取每个 code 最近一次的 name.
    供前端「添加历史持仓」搜索. 表未建 → 兜底空 list."""
    try:
        # SQLite bare-column + MAX(picked_at): name 取 picked_at 最大那行的值
        rows = c.execute(
            "SELECT code, name, MAX(picked_at) FROM daily_pick "
            "GROUP BY code ORDER BY code").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"code": r["code"], "name": r["name"]} for r in rows]


def _validate_holding_id(raw):
    """解析持仓 lot id. 返回 (id|None, err|None)."""
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, "id 必须是整数"


def _validate_sell(sell_price):
    """校验卖出价. 返回 (ok: bool, err: str|None)."""
    try:
        sp = float(sell_price)
    except (TypeError, ValueError):
        return False, "卖出价必须是数字"
    if sp <= 0:
        return False, "卖出价必须为正数"
    return True, None


def sell_holding(c, holding_id, sell_price, sold_at, owner_id):
    """卖出: 把该 id+owner 的一笔搬到 follow_trade_closed(同 owner). 归属不符/无该 id → False."""
    row = c.execute(
        "SELECT code, name, cost_price, added_at FROM follow_holding WHERE id=? AND owner_id=?",
        (holding_id, owner_id)).fetchone()
    if row is None:
        return False
    c.execute(
        "INSERT INTO follow_trade_closed(code, name, cost_price, sell_price, added_at, sold_at, owner_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (row["code"], row["name"], row["cost_price"], float(sell_price), row["added_at"],
         sold_at, owner_id))
    c.execute("DELETE FROM follow_holding WHERE id=? AND owner_id=?", (holding_id, owner_id))
    c.commit()
    return True


def load_closed_trades(c, owner_id):
    """已清仓流水 + 单笔实现收益%. 按卖出时间倒序(最新在前). 表未建 → 兜底空 list.
    返回 [{id, code, name, cost_price, sell_price, realized_pct, sold_at}]."""
    try:
        rows = c.execute(
            "SELECT id, code, name, cost_price, sell_price, sold_at FROM follow_trade_closed "
            "WHERE owner_id=? ORDER BY sold_at DESC, id DESC", (owner_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        cost, sell = r["cost_price"], r["sell_price"]
        realized = round((sell - cost) / cost * 100, 2) if cost else None
        out.append({"id": r["id"], "code": r["code"], "name": r["name"],
                    "cost_price": cost, "sell_price": sell,
                    "realized_pct": realized, "sold_at": r["sold_at"]})
    return out


def compute_winrate(closed):
    """据 load_closed_trades 返回算胜率. 空 → None.
    胜=realized_pct>0; 持平(=0)计入 losses. 返回 {total, wins, losses, win_rate, avg_return}."""
    if not closed:
        return None
    total = len(closed)
    rets = [t["realized_pct"] for t in closed if t["realized_pct"] is not None]
    wins = sum(1 for r in rets if r > 0)
    avg_return = round(sum(rets) / len(rets), 2) if rets else None
    return {"total": total, "wins": wins, "losses": total - wins,
            "win_rate": round(wins / total * 100, 1), "avg_return": avg_return}


def build_payload(user_id=None) -> dict:
    c = _conn()
    try:
        snap = c.execute(
            "SELECT * FROM intraday_snapshot ORDER BY snapshot_time DESC LIMIT 1").fetchone()
        snap = dict(snap) if snap else None
        st = snap["snapshot_time"] if snap else None

        boards = []
        if st:
            boards = [dict(r) for r in c.execute(
                "SELECT board_code, board_name, live_pct, member_count, up_count, "
                "limit_up_count, limit_down_count, lead_code, lead_name, lead_pct, lead_price "
                "FROM intraday_board WHERE snapshot_time=? ORDER BY live_pct DESC", (st,))]
            # 接入粗粒度白名单 (mtime 热更, 空白名单时原样返回)
            WHITELIST.load_if_changed()
            boards = WHITELIST.filter_boards(boards)

        cands = []
        picks_live = []   # 推荐池 dp 行情, 独立字段不污染候选盯盘
        logic = {}
        if st:
            cands = [dict(r) for r in c.execute(
                "SELECT icl.*, ROUND(db.circ_mv/10000.0, 1) AS circ_mv "
                "FROM intraday_candidate_live icl "
                "LEFT JOIN daily_basic db ON icl.ts_code=db.ts_code "
                "AND REPLACE(icl.cand_date,'-','')=db.trade_date "
                "WHERE icl.snapshot_time=? AND icl.strategy NOT IN ('dp', 'hold') "
                "ORDER BY icl.strategy, icl.code", (st,))]
            picks_live = [dict(r) for r in c.execute(
                "SELECT code, name, price, pct, vol_ratio, turnover_rate, amount, "
                "net_main, signal, dist_to_entry, top_theme, top_theme_pct "
                "FROM intraday_candidate_live WHERE snapshot_time=? AND strategy='dp'",
                (st,))]
            for r in c.execute("SELECT * FROM candidate_logic"):
                d = dict(r)
                logic[(d["strategy"], d["code"])] = {
                    "themes": json.loads(d["themes_json"] or "[]"),
                    "zsxq": [{**z, "topic_id": str(z.get("topic_id") or "")}
                             for z in json.loads(d["zsxq_json"] or "[]")],
                    "summary": d["summary"],
                    "refresh_time": d["refresh_time"],
                }
        # 触发记录 + 滚动浮盈 (S1~S8 通用), 按今日交易日取
        trig = {}
        if st:
            td = st[:10]
            for r in c.execute("SELECT * FROM intraday_trigger_log WHERE trade_date=?", (td,)):
                d = dict(r)
                trig[(d["strategy"], d["code"])] = d

        cur_keys = set()
        for r in cands:
            key = (r["strategy"], r["code"])
            cur_keys.add(key)
            lg = logic.get(key)
            r["logic"] = lg or {"themes": [], "zsxq": [], "summary": None, "refresh_time": None}
            r["trig"] = trig.get(key)
            r["dropped"] = False

        # 保留已触发但本帧掉出池的候选(主要 S8): 不让买过的标的从看板消失
        for key, t in trig.items():
            if key in cur_keys or not t.get("triggered"):
                continue
            cands.append(_synth_from_trigger(t))

        # bot11 点评 + 双星评级 (按 code, 候选行 + 趋势龙头共用同一条)
        # 评级跟随所显示快照帧的交易日: 收盘后/次晨看的是上一帧, 候选锚在那天,
        # 评级也应取那天 (用日历 today 会在跨午夜后让昨日评级凭空消失).
        # 例外: 盘前 08:00-08:35 cron 已经把今日 review/board/pick 写完, 但 collect.py
        # 要 09:15 才开始写 intraday_snapshot, 此时 st 仍指向上一交易日.
        # 为了让早上 09:00 左右刷新能看到当日盘前推荐, 在 "DB 里今日确实有 review 数据"
        # 时把 review_date 切到日历今日; 否则(凌晨/周末/节假日)仍用 st[:10] 保留昨日数据.
        _today = datetime.now().strftime("%Y-%m-%d")
        if st and st[:10] < _today:
            try:
                _has_today = c.execute(
                    "SELECT 1 FROM intraday_review WHERE trade_date=? LIMIT 1",
                    (_today,)).fetchone() is not None
            except sqlite3.OperationalError:
                _has_today = False
            review_date = _today if _has_today else st[:10]
        else:
            review_date = st[:10] if st else _today
        reviews = load_reviews(c, review_date)
        board_reviews = load_board_reviews(c, review_date)
        picks = load_picks(c, review_date)
        # 持仓/清仓/胜率按当前登录用户隔离; 未登录(user_id=None) → 空, 仅看板共享.
        holdings = load_holdings(c, user_id) if user_id else []
        pick_history = load_pick_history(c)   # 搜索源, 全局共享
        closed_trades = load_closed_trades(c, user_id) if user_id else []
        winrate = compute_winrate(closed_trades)
        for r in cands:
            r["reviews"] = reviews.get(scout_db.bare(r["code"]), {})

        # 趋势主线(日线为底 + 今日盘中叠加). 表可能尚未生成 → try 兜底.
        mainlines = []
        btd = None
        crowding_alerts = {"trade_date": None, "min_history": CROWDING_MIN_HISTORY,
                           "large_siphon": [], "relative": [], "by_code": {}}
        try:
            btd = c.execute("SELECT MAX(trade_date) FROM board_trend_daily").fetchone()[0]
        except sqlite3.Error:
            btd = None
        btd_regime = _regime_map(c, [btd]).get(btd) if btd else None  # 盘面雷达 tag 的 regime 门槛
        if btd:
            try:
                crowding_alerts = _compute_crowding_alerts(c, btd)
            except sqlite3.Error:
                crowding_alerts = {"trade_date": btd, "min_history": CROWDING_MIN_HISTORY,
                                   "large_siphon": [], "relative": [], "by_code": {}}
            crowding_by_code = crowding_alerts.get("by_code", {})
            live_amt = snap["total_amount"] if snap else None
            live_board = {}
            if st:
                for r in c.execute(
                    "SELECT board_code, live_pct, amount_yi FROM intraday_board "
                    "WHERE snapshot_time=?", (st,)):
                    live_board[r["board_code"]] = (r["live_pct"], r["amount_yi"])
            btd_rows = [dict(r) for r in c.execute(
                "SELECT board_code,board_name,trend_score,ret20,ret60,ret5,amt_peak_ratio,"
                "vol_ratio,amt_share,amt_share_trend,ma_aligned,above_ma20,above_ma60,dist_ma20,"
                "lead_code,lead_name,rank "
                "FROM board_trend_daily WHERE trade_date=? ORDER BY rank LIMIT 10", (btd,))]
            lstk = _lead_stocks(c, [d["board_code"] for d in btd_rows])
            # 板块 Top3 龙头 (来自 board_leader_daily): {board_code: [(rank, ts_code, name, score), ...]}
            leaders_map = {}
            try:
                for r in c.execute(
                    "SELECT board_code, rank, ts_code, name, leader_score "
                    "FROM board_leader_daily WHERE trade_date=? AND rank<=3 "
                    "ORDER BY board_code, rank", (btd,)):
                    leaders_map.setdefault(r["board_code"], []).append({
                        "rank": r["rank"], "ts_code": r["ts_code"],
                        "name": r["name"], "score": r["leader_score"]})
            except sqlite3.Error:
                pass
            # 上一交易日各板块 rank, 用于卡片显示"昨#N"
            prev_rank = {}
            try:
                prev_date = c.execute(
                    "SELECT MAX(trade_date) FROM board_trend_daily WHERE trade_date<?",
                    (btd,)).fetchone()[0]
                if prev_date:
                    for r in c.execute(
                        "SELECT board_code, rank FROM board_trend_daily WHERE trade_date=?",
                        (prev_date,)):
                        prev_rank[r["board_code"]] = r["rank"]
            except sqlite3.Error:
                pass
            for d in btd_rows:
                lp, amt_yi = live_board.get(d["board_code"], (None, None))
                d["live_pct"] = lp
                d["prev_rank"] = prev_rank.get(d["board_code"])
                d["live_share"] = round(amt_yi / live_amt, 4) if (amt_yi and live_amt) else None
                d["lead_stocks"] = lstk.get(d["board_code"], [])
                d["leaders_top3"] = leaders_map.get(d["board_code"], [])
                d["lead_reviews"] = reviews.get(scout_db.bare(d["lead_code"]), {}) if d.get("lead_code") else {}
                d["board_reviews"] = board_reviews.get(d["board_code"], {})
                d["crowding_alert"] = crowding_by_code.get(d["board_code"])
                d["kline_tag"] = _classify_board_tag(d, btd_regime, d["crowding_alert"])
                if lp is None:
                    d["badge"] = None
                elif lp >= 2.0:
                    d["badge"] = "加速"
                elif lp <= -1.0:
                    d["badge"] = "退潮"
                else:
                    d["badge"] = "走平"
                mainlines.append(d)

            # 给拥挤面板板块补齐与主线一致的展示字段 (龙头/成分涨跌/今日涨幅/badge; 不含 bot 点评)
            crowd_list = (crowding_alerts.get("large_siphon", [])
                          + crowding_alerts.get("relative", []))
            crowd_lstk = _lead_stocks(c, [a["board_code"] for a in crowd_list])
            for a in crowd_list:
                bc = a["board_code"]
                lp, _amt = live_board.get(bc, (None, None))
                a["live_pct"] = lp
                a["prev_rank"] = prev_rank.get(bc)
                a["lead_stocks"] = crowd_lstk.get(bc, [])
                a["leaders_top3"] = leaders_map.get(bc, [])
                a["kline_tag"] = _classify_board_tag(a, btd_regime, a)   # 拥挤面板: 行自身即拥挤标记
                if lp is None:
                    a["badge"] = None
                elif lp >= 2.0:
                    a["badge"] = "加速"
                elif lp <= -1.0:
                    a["badge"] = "退潮"
                else:
                    a["badge"] = "走平"

        # 潜在主线(萌芽探测器 emerging.py EOD 写 emerging_signal_daily): N选K信号+持续门,
        # regime闸标 actionable(强牛可操作). 替代旧 _compute_emerging(已废弃保留函数体).
        emerging = []
        if btd:
            try:
                emerging = [dict(r) for r in c.execute(
                    "SELECT board_code,board_name,n_signals,fired,persist_days,is_whitelist,"
                    "regime_code,actionable,reason FROM emerging_signal_daily WHERE trade_date=? "
                    "ORDER BY actionable DESC, n_signals DESC, persist_days DESC", (btd,))]
            except sqlite3.Error:
                emerging = []
            if emerging:
                ecodes = [e["board_code"] for e in emerging]
                eph = ",".join("?" * len(ecodes))
                btd_emap = {}
                try:
                    for r in c.execute(
                        f"SELECT * FROM board_trend_daily "
                        f"WHERE trade_date=? AND board_code IN ({eph})", [btd] + ecodes):
                        btd_emap[r["board_code"]] = dict(r)
                except sqlite3.Error:
                    pass
                e_lstk = _lead_stocks(c, ecodes)
                for e in emerging:
                    bc = e["board_code"]
                    row = btd_emap.get(bc, {})
                    lp, _amt = live_board.get(bc, (None, None))
                    e["fired"] = json.loads(e["fired"]) if e.get("fired") else []
                    e["rank"] = row.get("rank")
                    e["trend_score"] = row.get("trend_score")
                    e["ret20"] = row.get("ret20"); e["ret60"] = row.get("ret60")
                    e["amt_share"] = row.get("amt_share")
                    e["amt_share_trend"] = row.get("amt_share_trend")
                    e["vol_ratio"] = row.get("vol_ratio")
                    e["dist_ma20"] = row.get("dist_ma20")
                    e["ma_aligned"] = row.get("ma_aligned")
                    e["lead_name"] = row.get("lead_name")
                    e["live_pct"] = lp
                    e["lead_stocks"] = e_lstk.get(bc, [])
                    e["leaders_top3"] = leaders_map.get(bc, [])
                    e["crowding_alert"] = crowding_by_code.get(bc)
                    e["kline_tag"] = _classify_board_tag(row, btd_regime, e["crowding_alert"]) if row else None
                    if lp is None:
                        e["badge"] = None
                    elif lp >= 2.0:
                        e["badge"] = "加速"
                    elif lp <= -1.0:
                        e["badge"] = "退潮"
                    else:
                        e["badge"] = "走平"

        # 上涨中继: 中长期趋势在 + 缩量回踩 MA20. 扫全量(中继板块排名会掉出 Top10).
        continuation = []
        if btd and _cont_shown(btd_regime):   # 上涨中继仅强牛+中性震荡 regime 显示
            try:
                continuation = _compute_continuation(c, btd, top_n=10)
            except sqlite3.Error:
                continuation = []
            c_lstk = _lead_stocks(c, [r["board_code"] for r in continuation]) if continuation else {}
            for r in continuation:
                bc = r["board_code"]
                lp, _amt = live_board.get(bc, (None, None))
                r["live_pct"] = lp
                r["prev_rank"] = prev_rank.get(bc)
                r["lead_stocks"] = c_lstk.get(bc, [])
                r["leaders_top3"] = leaders_map.get(bc, [])
                r["crowding_alert"] = crowding_by_code.get(bc)
                if lp is None:
                    r["badge"] = None
                elif lp >= 2.0:
                    r["badge"] = "加速"
                elif lp <= -1.0:
                    r["badge"] = "退潮"
                else:
                    r["badge"] = "走平"

        now = datetime.now()
        meta = {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_open": in_market_hours(now),
            "snapshot_time": st,
            "candidate_count": len(cands),
            "board_count": len(boards),
            "mainline_date": btd,
        }
        return {"snapshot": snap, "boards": boards, "candidates": cands,
                "mainlines": mainlines, "meta": meta,
                "board_groups": WHITELIST.group_order(),
                "picks": picks, "picks_live": picks_live,
                "emerging": emerging,
                "continuation": continuation,
                "crowding_alerts": crowding_alerts,
                "holdings": holdings, "pick_history": pick_history,
                "closed_trades": closed_trades, "winrate": winrate}
    finally:
        c.close()


def build_earnings_resonance_payload(trade_date: str | None = None) -> dict:
    """业绩共振 v2 (事件驱动): 返回 60 天窗口硬标命中票 + 事件后走势 + 统计小卡.

    trade_date=None 时取 MAX(trade_date). 排序: 有 rank 优先(asc), 同 rank 内
    cum_ret_since_event desc.
    """
    c = _conn()
    try:
        if trade_date is None:
            r = c.execute("SELECT MAX(trade_date) FROM earnings_resonance_daily").fetchone()
            trade_date = r[0] if r and r[0] else None
        if not trade_date:
            return {"trade_date": None,
                    "stats": {"n_stocks": 0, "n_boards": 0, "n_new_today": 0}, "rows": [],
                    "rt_snapshot_max": None}
        rows = [dict(r) for r in c.execute(
            "WITH latest_er AS ( "
            "  SELECT icl.ts_code, icl.snapshot_time, icl.price, icl.pct, "
            "         icl.low_px, icl.high_px, icl.pre_close_px "
            "  FROM intraday_candidate_live icl "
            "  WHERE icl.strategy='er' AND icl.snapshot_time = ( "
            "    SELECT MAX(snapshot_time) FROM intraday_candidate_live "
            "    WHERE strategy='er' AND ts_code = icl.ts_code "
            "  ) "
            ") "
            "SELECT erd.trade_date, erd.ts_code, erd.name, "
            "       erd.gap_pct, erd.day_ret, erd.vol_ratio, "
            "       erd.open_px, erd.high_px, erd.low_px, erd.close_px, erd.pre_close, erd.shape_tag, "
            "       erd.primary_board_code, erd.primary_board_name, erd.primary_board_rank, "
            "       erd.primary_board_trend_score, erd.other_boards, "
            "       erd.latest_event_ann_date, erd.latest_event_source, erd.latest_event_type, "
            "       erd.latest_p_change_min, erd.latest_p_change_max, erd.latest_summary, "
            "       erd.days_since_event, erd.t1_open_ret, erd.t1_close_ret, erd.cum_ret_since_event, "
            "       le.price AS rt_price, le.pct AS rt_pct, "
            "       le.low_px AS rt_low, le.high_px AS rt_high, "
            "       le.pre_close_px AS rt_pre_close, le.snapshot_time AS rt_snapshot "
            "FROM earnings_resonance_daily erd "
            "LEFT JOIN latest_er le ON le.ts_code = erd.ts_code "
            "WHERE erd.trade_date=? "
            "ORDER BY "
            "  CASE WHEN erd.primary_board_rank IS NULL THEN 1 ELSE 0 END ASC, "
            "  erd.primary_board_rank ASC, "
            "  CASE WHEN erd.cum_ret_since_event IS NULL THEN 1 ELSE 0 END ASC, "
            "  erd.cum_ret_since_event DESC",
            (trade_date,),
        )]
        # rt_low_pct = (rt_low / rt_pre_close - 1) * 100, 缺一即 None
        for r in rows:
            lo, pc = r.get("rt_low"), r.get("rt_pre_close")
            r["rt_low_pct"] = round((lo / pc - 1) * 100, 3) if (lo and pc) else None
        rt_snapshot_max = max((r["rt_snapshot"] for r in rows if r.get("rt_snapshot")),
                              default=None)
        n_stocks = len(rows)
        n_boards = len({r["primary_board_code"] for r in rows if r["primary_board_code"]})
        # "今日新披露" = latest_event_ann_date == 表内最大 ann_date (通常 = today)
        # 不直接用 trade_date, 因为业绩预告盘后发, ann_date 可以晚于 trade_date 一天.
        max_ann = max((r["latest_event_ann_date"] for r in rows
                       if r["latest_event_ann_date"]), default=None)
        n_new_today = sum(1 for r in rows if r["latest_event_ann_date"] == max_ann) if max_ann else 0
        return {
            "trade_date": trade_date,
            "stats": {"n_stocks": n_stocks, "n_boards": n_boards, "n_new_today": n_new_today},
            "rows": rows,
            "rt_snapshot_max": rt_snapshot_max,
        }
    finally:
        c.close()


def build_lof_payload() -> dict:
    """LOF 套利面板数据: 取当日最新帧 join 费率/全集, 分溢价/折价/机会三组."""
    c = _conn()
    try:
        td = c.execute(
            "SELECT MAX(trade_date) FROM lof_arbitrage_live").fetchone()[0]
        if not td:
            return {"as_of": None, "opportunities": [], "premium_list": [],
                    "discount_list": [], "params": _lof_params()}
        rows = [dict(r) for r in c.execute(
            "SELECT a.*, u.name, u.category, u.sub_theme, "
            "r.purchase_fee_pct, r.redeem_fee_pct, r.redeem_fee_source, "
            "r.max_purchase_amt, r.purchase_confirm_days, r.redeem_confirm_days, "
            "r.redeem_arrival_days, r.purchasable, r.redeemable, "
            "r.purchase_status, r.redeem_status "
            "FROM lof_arbitrage_live a "
            "JOIN lof_universe u ON u.lof_code = a.lof_code "
            "LEFT JOIN lof_rate r ON r.lof_code = a.lof_code "
            "WHERE a.trade_date = ?", (td,))]
        as_of = max((r["snapshot_time"] for r in rows), default=None)
    finally:
        c.close()
    # 机会组: actionable=="opp", 按净套利收益从高到低
    opportunities = sorted(
        [r for r in rows if r["actionable"] == "opp"],
        key=lambda r: max(r.get("net_premium_arb") or -99,
                          r.get("net_discount_arb") or -99), reverse=True)
    # 溢价观察组: 溢价 > 0
    premium_list = sorted(
        [r for r in rows if (r["premium_pct"] or 0) > 0],
        key=lambda r: r["premium_pct"], reverse=True)
    # 折价观察组: 溢价 < 0
    discount_list = sorted(
        [r for r in rows if (r["premium_pct"] or 0) < 0],
        key=lambda r: r["premium_pct"])
    return {"as_of": as_of, "opportunities": opportunities,
            "premium_list": premium_list, "discount_list": discount_list,
            "params": _lof_params()}


def _lof_params() -> dict:
    """返回 lof_arb 模块的关键阈值常量, 供前端展示说明."""
    return {"commission_pct": lof_arb.COMMISSION_PCT,
            "default_redeem_fee": lof_arb.DEFAULT_REDEEM_FEE,
            "opp": lof_arb.OPP_THRESHOLD, "watch": lof_arb.WATCH_THRESHOLD}


# ---- 大盘技术面: res14 index_technical_daily 三层数据 + 简报 md ----

_ITD_COLS = ("trade_date", "ts_code", "index_name", "close", "pct_chg",
             "ma_align", "macd_cross", "dist_ma20", "rsi14", "vol_ratio",
             "regime_rule", "tech_score_rule",
             "regime", "tech_score", "trend", "lens", "focus", "override_reason",
             "facts_at", "judged_at", "sr_levels", "boll_bbu", "boll_bbl")


def _repo_root():
    """server.py 在 <repo>/scout/ 下, 上一级即 repo 根; 不写死 /home."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def index_technical_payload(conn, date=None, repo_root=None):
    """只读 index_technical_daily(res14 产) + res14 盘前简报 md。scout 端不算任何东西。"""
    if date is None:
        row = conn.execute("SELECT MAX(trade_date) d FROM index_technical_daily").fetchone()
        date = row["d"] if row else None
    rows = []
    if date:
        cur = conn.execute(
            f"SELECT {', '.join(_ITD_COLS)} FROM index_technical_daily "
            f"WHERE trade_date=? ORDER BY tech_score IS NULL, tech_score DESC, ts_code",
            (date,))
        rows = [dict(r) for r in cur.fetchall()]
    # 简报 md: <repo>/workspace-res14/memory/YYYY-MM-DD.md
    # 优先用 judged_at 的日期, 空则用 trade_date 的日期
    brief_md = None
    facts_at = rows[0]["facts_at"] if rows else None
    judged_at = rows[0]["judged_at"] if rows else None

    if rows and judged_at:
        # judged_at 格式: "2026-07-01T08:24:10", 取前 10 字符即 "2026-07-01"
        nd = judged_at[:10]
    elif date and len(date) == 8:
        # 退回 trade_date: "20260630" -> "2026-06-30"
        nd = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    else:
        nd = None

    if nd:
        root = repo_root or _repo_root()
        p = os.path.join(root, "workspace-res14", "memory", f"{nd}.md")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    brief_md = fh.read()
            except OSError:
                brief_md = None

    return {"trade_date": date, "rows": rows, "brief_md": brief_md,
            "facts_at": facts_at, "judged_at": judged_at}


_ITI_COLS = ("ts_code", "index_name", "slot", "snapshot_time", "intraday_pct",
             "live_close", "dist_ma20", "dist_ma60", "dist_ma250", "intraday_high", "intraday_low",
             "amplitude_pct", "pullback_from_high_pct", "bounce_from_low_pct", "range_pos",
             "momentum", "level_touch", "nearest_support", "nearest_resistance", "breach_flags",
             "verify_status")


def _iti_cols_present(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(index_technical_intraday)")}
    return [c for c in _ITI_COLS if c in have]


def index_technical_intraday_payload(conn, date=None):
    """只读 index_technical_intraday(res14 盘中模式产) 的最新时段快照 + 判读记录。"""
    if date is None:
        r = conn.execute("SELECT MAX(trade_date) d FROM index_technical_intraday").fetchone()
        date = r["d"] if r else None
    rows, latest_slot, notes = [], None, []
    if date:
        sr = conn.execute("SELECT MAX(slot) s FROM index_technical_intraday WHERE trade_date=?",
                          (date,)).fetchone()
        latest_slot = sr["s"] if sr else None
        if latest_slot:
            cols = _iti_cols_present(conn)
            cur = conn.execute(
                f"SELECT {', '.join(cols)} FROM index_technical_intraday "
                f"WHERE trade_date=? AND slot=? ORDER BY ts_code", (date, latest_slot))
            rows = [dict(r) for r in cur.fetchall()]
        nc = conn.execute("SELECT slot, snapshot_time, note FROM index_technical_intraday_note "
                          "WHERE trade_date=? ORDER BY slot DESC", (date,))
        notes = [{"slot": r["slot"], "time": r["snapshot_time"], "note": r["note"]}
                 for r in nc.fetchall()]
    return {"trade_date": date, "latest_slot": latest_slot, "rows": rows, "notes": notes}


def intraday_market_payload(conn, date=None):
    """日内分钟级指数行情序列。读 intraday_snapshot 的 MAX(trade_date)。

    偶发 flock skip 会漏 1-2 分钟, 前端折线遇缺行会断; 这里在相邻快照跨度 >=2 分钟
    且 <=MAX_GAP 分钟时用 t=HH:MM 空行(值全 None)填补, 前端插值成虚线。跨午休
    (11:30->13:00)因缺口远超 MAX_GAP 天然不补。
    """
    MAX_GAP = 5  # 分钟, 超过就当真断了不填
    if date is None:
        r = conn.execute("SELECT MAX(trade_date) d FROM intraday_snapshot").fetchone()
        date = r["d"] if r else None
    series = []
    if date:
        have = {r[1] for r in conn.execute("PRAGMA table_info(intraday_snapshot)")}
        c1 = "csi1000_pct" if "csi1000_pct" in have else "NULL AS csi1000_pct"
        c2 = "csi2000_pct" if "csi2000_pct" in have else "NULL AS csi2000_pct"
        cur = conn.execute(
            "SELECT snapshot_time, sh_pct, csi300_pct, szcz_pct, gem_pct, star50_pct, "
            f"bz50_pct, {c1}, {c2}, up_count, down_count, flat_count, limit_up, limit_down, "
            "blast_count, total_amount, regime_name FROM intraday_snapshot "
            "WHERE trade_date=? ORDER BY snapshot_time", (date,))
        prev_min = None
        for r in cur.fetchall():
            st = r["snapshot_time"] or ""
            t = st[11:16]
            if prev_min is not None and t and len(t) == 5:
                try:
                    cur_min = int(t[:2]) * 60 + int(t[3:])
                    gap = cur_min - prev_min
                    if 2 <= gap <= MAX_GAP:
                        for k in range(1, gap):
                            hh, mm = divmod(prev_min + k, 60)
                            series.append({"t": f"{hh:02d}:{mm:02d}", "gap": True,
                                           "sh": None, "csi300": None, "szcz": None,
                                           "gem": None, "star50": None, "bz50": None,
                                           "csi1000": None, "csi2000": None,
                                           "up": None, "down": None, "flat": None,
                                           "limit_up": None, "limit_down": None,
                                           "blast": None, "amount": None, "regime": None})
                    prev_min = cur_min
                except ValueError:
                    prev_min = None
            elif t and len(t) == 5:
                try:
                    prev_min = int(t[:2]) * 60 + int(t[3:])
                except ValueError:
                    prev_min = None
            series.append({
                "t": t, "sh": r["sh_pct"], "csi300": r["csi300_pct"],
                "szcz": r["szcz_pct"], "gem": r["gem_pct"], "star50": r["star50_pct"],
                "bz50": r["bz50_pct"], "csi1000": r["csi1000_pct"], "csi2000": r["csi2000_pct"],
                "up": r["up_count"], "down": r["down_count"],
                "flat": r["flat_count"], "limit_up": r["limit_up"],
                "limit_down": r["limit_down"], "blast": r["blast_count"],
                "amount": r["total_amount"], "regime": r["regime_name"]})
    # latest 取最后一条 sh 非空的真实行, 避免 stat 面板卡在全 NULL 的毛刺行
    latest = None
    for r in reversed(series):
        if not r.get("gap") and r.get("sh") is not None:
            latest = r
            break
    if latest is None and series:
        latest = next((r for r in reversed(series) if not r.get("gap")), series[-1])
    return {"trade_date": date, "series": series, "latest": latest}


_SERIES_INDICES = [
    ("000001.SH", "上证综指"), ("000300.SH", "沪深300"), ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"), ("000688.SH", "科创50"), ("899050.BJ", "北证50"),
    ("000852.SH", "中证1000"), ("932000.CSI", "中证2000"),
]


def _fmt_d(yyyymmdd):
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}" if yyyymmdd and len(yyyymmdd) == 8 else yyyymmdd


def market_series_payload(conn, days=60):
    try:
        days = int(days or 60)
    except (TypeError, ValueError):
        days = 60
    days = max(1, min(days, 250))
    # 窗口 = 全市场最近 days 个交易日(以 index_daily 000300 为准)
    dates = [r["trade_date"] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM index_daily WHERE ts_code='000300.SH' "
        "ORDER BY trade_date DESC LIMIT ?", (days,)).fetchall()]
    dates = sorted(dates)
    start, end = (dates[0], dates[-1]) if dates else (None, None)
    indices = []
    for code, name in _SERIES_INDICES:
        rows = conn.execute(
            "SELECT trade_date, close FROM index_daily WHERE ts_code=? AND trade_date>=? "
            "AND trade_date<=? ORDER BY trade_date", (code, start or "", end or "")).fetchall()
        vals = [(r["trade_date"], r["close"]) for r in rows if r["close"] is not None]
        if not vals or not vals[0][1]:   # 空 / 首值 0 → 跳过(防除零)
            continue
        base = vals[0][1]
        indices.append({"ts_code": code, "index_name": name,
                        "points": [{"d": _fmt_d(d), "cum_ret": (c / base - 1) * 100}
                                   for d, c in vals]})
    breadth = [{"d": _fmt_d(r["trade_date"]), "adr": r["advance_decline_ratio"],
                "up": r["up"], "down": r["down"], "amount_yi": r["total_amount_yi"]}
               for r in conn.execute(
                   "SELECT trade_date, up, down, advance_decline_ratio, total_amount_yi "
                   "FROM regime_raw_daily WHERE trade_date>=? AND trade_date<=? "
                   "ORDER BY trade_date", (start or "", end or "")).fetchall()]
    return {"start_date": _fmt_d(start), "end_date": _fmt_d(end),
            "indices": indices, "breadth": breadth}


_STYLE_FACTOR_DEFS = {
    "value": {
        "name": "价值",
        "long_label": "低估值组",
        "short_label": "高估值组",
        "desc": "PE/PB/PS 越低分越高; 多空收益=低估值组-高估值组",
        "color": "#2f86d6",
    },
    "growth": {
        "name": "成长/预期",
        "long_label": "高成长预期组",
        "short_label": "低成长预期组",
        "desc": "使用一致预期利润改善; 数据不足时不启用, 不用高估值代理",
        "color": "#cf8a17",
    },
    "momentum": {
        "name": "动量",
        "long_label": "强动量组",
        "short_label": "弱动量组",
        "desc": "近 20/60/120 日收益越强分越高; 短历史启动期降级为可用短动量",
        "color": "#6f5ad6",
    },
}


def _is_clean_stock(code: str, name: str | None = None) -> bool:
    if not code or len(code) < 9:
        return False
    raw = code.split(".", 1)[0]
    if raw.startswith(("8", "4", "9")) or code.endswith(".BJ"):
        return False
    nm = name or ""
    return "ST" not in nm.upper() and "退" not in nm


def _rank_scores(metric, *, high_good=True, group_map=None, min_group=20):
    vals = [(k, v) for k, v in metric.items() if v is not None and v > 0]
    if len(vals) < 30:
        return {}
    groups = defaultdict(list)
    if group_map:
        for k, v in vals:
            groups[group_map.get(k) or "未分类"].append((k, v))
    else:
        groups["全市场"] = vals
    out = {}
    fallback = []
    for _g, gvals in groups.items():
        if len(gvals) < min_group:
            fallback.extend(gvals)
            continue
        gvals.sort(key=lambda x: x[1])
        den = max(1, len(gvals) - 1)
        for i, (k, _v) in enumerate(gvals):
            pct = i / den * 100.0
            out[k] = pct if high_good else 100.0 - pct
    if fallback and len(fallback) >= 30:
        fallback.sort(key=lambda x: x[1])
        den = max(1, len(fallback) - 1)
        for i, (k, _v) in enumerate(fallback):
            pct = i / den * 100.0
            out[k] = pct if high_good else 100.0 - pct
    return out


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _score_from_components(components):
    out = {}
    keys = set()
    for comp in components:
        keys.update(comp.keys())
    for k in keys:
        v = _avg([comp.get(k) for comp in components])
        if v is not None:
            out[k] = v
    return out


def _style_factor_scores(date, idx, daily_by_date, basic_by_date, consensus_by_date, dates, kind, industry_map):
    basic = basic_by_date.get(date) or {}
    if kind == "value":
        pe = {c: r.get("pe_ttm") for c, r in basic.items()}
        pb = {c: r.get("pb") for c, r in basic.items()}
        ps = {c: r.get("ps_ttm") for c, r in basic.items()}
        comps = [_rank_scores(pe, high_good=False, group_map=industry_map),
                 _rank_scores(pb, high_good=False, group_map=industry_map),
                 _rank_scores(ps, high_good=False, group_map=industry_map)]
        return _score_from_components(comps)

    if kind == "growth":
        curr_cons = consensus_by_date.get(date) or {}
        base_idx = max(0, idx - 60)
        base_cons = consensus_by_date.get(dates[base_idx]) or {}
        g = {}
        for code, curr in curr_cons.items():
            prev = base_cons.get(code)
            if curr is not None and prev is not None and prev > 0:
                g[code] = (curr - prev) / abs(prev)
        return _rank_scores(g, high_good=True, group_map=industry_map)

    if kind == "momentum":
        curr = daily_by_date.get(date) or {}
        comps = []
        for lb in (20, 60, 120):
            base_idx = idx - lb
            if base_idx < 0:
                continue
            base = daily_by_date.get(dates[base_idx]) or {}
            ret = {}
            for code, r in curr.items():
                c0 = (base.get(code) or {}).get("close")
                c1 = r.get("close")
                if c0 and c1:
                    ret[code] = c1 / c0 - 1.0
            rs = _rank_scores(ret, high_good=True, group_map=industry_map)
            if rs:
                comps.append(rs)
        if not comps and idx > 0:
            base = daily_by_date.get(dates[0]) or {}
            ret = {}
            for code, r in curr.items():
                c0 = (base.get(code) or {}).get("close")
                c1 = r.get("close")
                if c0 and c1 and date != dates[0]:
                    ret[code] = c1 / c0 - 1.0
            rs = _rank_scores(ret, high_good=True, group_map=industry_map)
            if rs:
                comps.append(rs)
        return _score_from_components(comps)
    return {}


def _style_group(items, side, q=0.2):
    if not items:
        return []
    items = sorted(items, key=lambda x: x["score"], reverse=(side == "long"))
    k = max(20, int(len(items) * q))
    return items[:min(k, len(items))]


def _group_return(group):
    vals = [x["ret"] for x in group if x.get("ret") is not None]
    return sum(vals) / len(vals) if vals else None


def _top_examples(group, limit=8):
    rows = []
    for x in group[:limit]:
        rows.append({
            "ts_code": x["code"],
            "code": x["code"].split(".", 1)[0],
            "name": x.get("name") or x["code"].split(".", 1)[0],
            "score": round(x["score"], 1),
            "ret": None if x.get("ret") is None else round(x["ret"], 2),
            "pe_ttm": x.get("pe_ttm"),
            "pb": x.get("pb"),
            "ps_ttm": x.get("ps_ttm"),
            "close": x.get("close"),
        })
    return rows


def style_factors_payload(conn, days=120):
    try:
        days = int(days or 120)
    except (TypeError, ValueError):
        days = 120
    days = max(2, min(days, 250))
    extra = 65
    dates = [r["trade_date"] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily ORDER BY trade_date DESC LIMIT ?",
        (days + extra,)).fetchall()]
    dates = sorted(dates)
    if not dates:
        return {"trade_date": None, "factors": [], "notes": ["daily 表暂无数据"]}

    dph = ",".join("?" * len(dates))
    name_map = {}
    for r in conn.execute("SELECT code, ts_code, name FROM stock_names").fetchall():
        if r["ts_code"]:
            name_map[r["ts_code"]] = r["name"]
        if r["code"]:
            name_map[r["code"]] = r["name"]
    industry_map = {r["ts_code"]: r["l1_name"] for r in conn.execute(
        "SELECT ts_code, l1_name FROM sw_industry_member").fetchall()}
    daily_by_date = defaultdict(dict)
    for r in conn.execute(
        f"SELECT trade_date, ts_code, close, pct_chg FROM daily "
        f"WHERE trade_date IN ({dph})", dates):
        nm = name_map.get(r["ts_code"]) or name_map.get(r["ts_code"].split(".", 1)[0])
        if not _is_clean_stock(r["ts_code"], nm):
            continue
        daily_by_date[r["trade_date"]][r["ts_code"]] = {
            "close": r["close"], "ret": r["pct_chg"], "name": nm}

    basic_by_date = defaultdict(dict)
    for r in conn.execute(
        f"SELECT trade_date, ts_code, pe_ttm, pb, ps_ttm, total_mv, circ_mv, close "
        f"FROM daily_basic WHERE trade_date IN ({dph})", dates):
        nm = name_map.get(r["ts_code"]) or name_map.get(r["ts_code"].split(".", 1)[0])
        if not _is_clean_stock(r["ts_code"], nm):
            continue
        basic_by_date[r["trade_date"]][r["ts_code"]] = {
            "pe_ttm": r["pe_ttm"], "pb": r["pb"], "ps_ttm": r["ps_ttm"],
            "total_mv": r["total_mv"], "circ_mv": r["circ_mv"], "close": r["close"]}

    consensus_by_date = defaultdict(dict)
    try:
        for r in conn.execute(
            f"SELECT trade_date, ts_code, profit_yi FROM consensus_profit_daily "
            f"WHERE trade_date IN ({dph})", dates):
            if r["profit_yi"] is not None:
                consensus_by_date[r["trade_date"]][r["ts_code"]] = r["profit_yi"]
    except sqlite3.OperationalError:
        pass

    latest = dates[-1]
    visible_dates = dates[-days:]
    score_cache = {}
    date_pos = {d: i for i, d in enumerate(dates)}

    def scores_for(d, kind):
        key = (d, kind)
        if key not in score_cache:
            score_cache[key] = _style_factor_scores(
                d, date_pos[d], daily_by_date, basic_by_date, consensus_by_date, dates, kind, industry_map)
        return score_cache[key]

    factors = []
    for kind, meta in _STYLE_FACTOR_DEFS.items():
        series, nav = [], 1.0
        for i in range(1, len(dates)):
            d = dates[i]
            if d not in visible_dates:
                continue
            prev = dates[i - 1]
            scores = scores_for(prev, kind)
            curr = daily_by_date.get(d) or {}
            basic = basic_by_date.get(prev) or {}
            items = []
            for code, score in scores.items():
                r = curr.get(code)
                if r is None or r.get("ret") is None:
                    continue
                br = basic.get(code) or {}
                items.append({"code": code, "score": score, "ret": r["ret"], "name": r.get("name"),
                              "pe_ttm": br.get("pe_ttm"), "pb": br.get("pb"),
                              "ps_ttm": br.get("ps_ttm"), "close": r.get("close")})
            lg = _style_group(items, "long")
            sh = _style_group(items, "short")
            lr, sr = _group_return(lg), _group_return(sh)
            spread = None if lr is None or sr is None else lr - sr
            if spread is not None:
                nav *= 1.0 + spread / 100.0
            series.append({"d": _fmt_d(d), "ret": spread,
                           "long_ret": lr, "short_ret": sr,
                           "nav": (nav - 1.0) * 100.0,
                           "n": min(len(lg), len(sh))})

        latest_scores = scores_for(latest, kind)
        latest_daily = daily_by_date.get(latest) or {}
        latest_basic = basic_by_date.get(latest) or {}
        latest_items = []
        for code, score in latest_scores.items():
            r = latest_daily.get(code) or {}
            br = latest_basic.get(code) or {}
            if code not in latest_daily and code not in latest_basic:
                continue
            latest_items.append({"code": code, "score": score, "ret": r.get("ret"),
                                 "name": r.get("name") or name_map.get(code),
                                 "pe_ttm": br.get("pe_ttm"), "pb": br.get("pb"),
                                 "ps_ttm": br.get("ps_ttm"), "close": r.get("close") or br.get("close")})
        latest_long = _style_group(latest_items, "long")
        latest_short = _style_group(latest_items, "short")
        latest_lr, latest_sr = _group_return(latest_long), _group_return(latest_short)
        spread_1d = None if latest_lr is None or latest_sr is None else latest_lr - latest_sr
        valid_series = [x for x in series if x["ret"] is not None]
        cum = valid_series[-1]["nav"] if valid_series else None
        status = "ok" if latest_items else "empty"
        if kind == "growth" and not latest_items:
            status = "insufficient"
        if kind == "momentum" and len(dates) < 20:
            status = "short_history"
        factors.append({
            "key": kind, "name": meta["name"], "desc": meta["desc"],
            "long_label": meta["long_label"], "short_label": meta["short_label"],
            "color": meta["color"], "status": status,
            "sample_n": len(latest_items), "group_n": min(len(latest_long), len(latest_short)),
            "spread_1d": spread_1d, "cum_ret": cum,
            "series": series, "long_top": _top_examples(latest_long),
            "short_top": _top_examples(latest_short),
        })

    notes = []
    if not any(consensus_by_date.values()):
        notes.append("consensus_profit_daily 暂无可比样本: 成长因子暂不启用。")
    if len(dates) < 20:
        notes.append(f"当前日线仅 {len(dates)} 个交易日: 动量窗口会使用可用短历史, 长窗稳定性不足。")
    return {"trade_date": _fmt_d(latest), "start_date": _fmt_d(visible_dates[0]),
            "end_date": _fmt_d(visible_dates[-1]), "dates_n": len(visible_dates),
            "universe_n": len(daily_by_date.get(latest) or {}),
            "neutralize": "申万一级行业内分位",
            "factors": factors, "notes": notes}


# ---- 账号系统: 请求级鉴权与 admin 处理逻辑(纯函数, 便于单测) ----

_VALID_ROLES = ("user", "admin")


def _resolve_request_user(c, auth_header):
    """从 Authorization 头解析当前用户 dict; 无/非 Bearer/失效 → None."""
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    return scout_auth.resolve_session(c, auth_header[7:].strip())


def _admin_create_user(c, body):
    """返回 (http_code, body_dict)."""
    username = str((body or {}).get("username", "")).strip()
    password = str((body or {}).get("password", ""))
    display_name = str((body or {}).get("display_name", "")).strip()
    role = (body or {}).get("role", "user")
    if not username or not password or not display_name:
        return 400, {"error": "username, password, display_name 必填"}
    if len(username) < 2 or len(username) > 50:
        return 400, {"error": "用户名长度需 2-50"}
    if len(password) < 4:
        return 400, {"error": "密码至少 4 位"}
    role = role if role in _VALID_ROLES else "user"
    if c.execute("SELECT id FROM scout_user WHERE username=?", (username,)).fetchone():
        return 409, {"error": "用户名已存在"}
    uid = scout_auth.create_user(c, username, password, display_name=display_name, role=role)
    return 200, {"ok": True, "id": uid}


def _admin_update_user(c, body):
    """改昵称/角色/状态. 返回 (http_code, body_dict)."""
    try:
        uid = int((body or {}).get("id"))
    except (TypeError, ValueError):
        return 400, {"error": "id 必须是整数"}
    display_name = (body or {}).get("display_name")
    role = (body or {}).get("role")
    status = (body or {}).get("status")
    if role is not None and role not in _VALID_ROLES:
        return 400, {"error": "role 非法"}
    if status is not None and status not in ("active", "disabled"):
        return 400, {"error": "status 非法"}
    ok, err = scout_auth.update_user(c, uid, display_name=display_name, role=role, status=status)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音访问日志
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        # gzip: /api/scout 等 JSON 体积大(~400KB), 远程访问/每 30s 轮询时压缩省 ~7x 带宽
        enc = None
        if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
            body = gzip.compress(body, 5)
            enc = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _req_user(self):
        """解析当前请求用户(自开短连接). 无效 → None."""
        c = _conn()
        try:
            return _resolve_request_user(c, self.headers.get("Authorization"))
        finally:
            c.close()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/scout.html"):
            try:
                with open(os.path.join(HERE, "scout.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "scout.html not found", "text/plain")
        elif path == "/api/scout":
            try:
                user = self._req_user()
                uid = user["id"] if user else None
                self._send(200, json.dumps(build_payload(uid), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/lof_arbitrage":
            try:
                self._send(200, json.dumps(build_lof_payload(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/board_members":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code = (qs.get("code") or [None])[0]
            if not code:
                self._send(400, json.dumps({"error": "missing code"}))
                return
            try:
                self._send(200, json.dumps(board_members(code), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/member_returns":
            # 成分区间涨跌幅冷加载: window=N (交易日窗口) 或 start=YYYYMMDD&end=YYYYMMDD
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code = (qs.get("code") or [None])[0]
            if not code:
                self._send(400, json.dumps({"error": "missing code"}))
                return
            win = (qs.get("window") or [None])[0]
            start = (qs.get("start") or [None])[0]
            end = (qs.get("end") or [None])[0]
            kwargs = {}
            if win is not None:
                if not re.fullmatch(r"\d{1,4}", win) or not (1 <= int(win) <= _RET_WINDOW_MAX):
                    self._send(400, json.dumps({"error": f"window 需为 1~{_RET_WINDOW_MAX} 的整数"}))
                    return
                kwargs["window"] = int(win)
            elif start and end:
                if not (re.fullmatch(r"\d{8}", start) and re.fullmatch(r"\d{8}", end)):
                    self._send(400, json.dumps({"error": "start/end 需为 YYYYMMDD"}))
                    return
                if start >= end:
                    self._send(400, json.dumps({"error": "start 需早于 end"}))
                    return
                kwargs["start"], kwargs["end"] = start, end
            else:
                self._send(400, json.dumps({"error": "需提供 window 或 start+end"}))
                return
            try:
                c = _conn()
                try:
                    codes = _board_member_codes(c, code)
                    self._send(200, json.dumps(
                        member_interval_returns(c, codes, **kwargs), ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/board_list":
            try:
                self._send(200, json.dumps(board_list(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/board_search":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            q = (qs.get("q") or [""])[0]
            types = (qs.get("types") or ["concept,industry"])[0]
            try:
                c = _conn()
                try:
                    self._send(200, json.dumps(board_search(c, q, types), ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/board_fund_match":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            raw = (qs.get("boards") or [""])[0]
            board_codes = [b for b in raw.split(",") if b.strip()]
            if not board_codes:
                self._send(400, json.dumps({"error": "missing boards"}))
                return
            try:
                threshold = float((qs.get("threshold") or ["80"])[0])
            except ValueError:
                threshold = 80.0
            try:
                top = int((qs.get("top") or ["3"])[0])
            except ValueError:
                top = 3
            scope = (qs.get("scope") or ["all"])[0]
            if scope not in ("all", "leader1", "leader3"):
                scope = "all"
            try:
                self._send(200, json.dumps(board_fund_match(board_codes, threshold, top, scope),
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/stock_fund_match":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            raw = (qs.get("stocks") or [""])[0]
            stock_codes = [s for s in raw.split(",") if s.strip()]
            if not stock_codes:
                self._send(400, json.dumps({"error": "missing stocks"}))
                return
            try:
                threshold = float((qs.get("threshold") or ["80"])[0])
            except ValueError:
                threshold = 80.0
            try:
                top = int((qs.get("top") or ["3"])[0])
            except ValueError:
                top = 3
            try:
                self._send(200, json.dumps(stock_fund_match(stock_codes, threshold, top),
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/preset_boards":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            kind = (qs.get("kind") or ["top10"])[0]
            try:
                self._send(200, json.dumps(preset_boards(kind), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/stock_search":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            q = (qs.get("q") or [""])[0]
            try:
                self._send(200, json.dumps(stock_search(q), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/hot_stocks":
            try:
                self._send(200, json.dumps(hot_stocks(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/fund_detail":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code = (qs.get("code") or [None])[0]
            if not code:
                self._send(400, json.dumps({"error": "missing code"}))
                return
            try:
                self._send(200, json.dumps(fund_detail(code), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/board_kline":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code = (qs.get("code") or [None])[0]
            if not code:
                self._send(400, json.dumps({"error": "missing code"}))
                return
            try:
                n = int((qs.get("n") or ["60"])[0])
            except ValueError:
                n = 60
            try:
                self._send(200, json.dumps(board_kline(code, n), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/regime_history":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                days = int((qs.get("days") or ["20"])[0])
            except ValueError:
                days = 20
            try:
                self._send(200, json.dumps(regime_history(days), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/zsxq":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            tid = (qs.get("topic_id") or [None])[0]
            if not tid:
                self._send(400, json.dumps({"error": "missing topic_id"}))
                return
            try:
                self._send(200, json.dumps(zsxq_topic(tid), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/zsxq_list":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            board = (qs.get("board") or [None])[0]
            names = [n for n in (qs.get("name") or []) if n]
            if not board and not names:
                self._send(400, json.dumps({"error": "missing name or board"}))
                return
            try:
                result = zsxq_board(board) if board else zsxq_search(names)
                self._send(200, json.dumps(result, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/research":
            try:
                payload = build_research_payload()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
                return
            try:
                sig = build_research_signals()
                payload["resonance"] = sig["resonance"]
                payload["ambush"] = sig["ambush"]
                payload["stats"] = sig.get("stats") or {}
            except Exception:
                payload["resonance"] = []
                payload["ambush"] = []
                payload["stats"] = {}
            self._send(200, json.dumps(payload, ensure_ascii=False))
        elif path == "/api/research/note":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            nid = (qs.get("id") or [None])[0]
            if not nid:
                self._send(400, json.dumps({"error": "missing id"}))
                return
            try:
                self._send(200, json.dumps(research_note_detail(nid), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/research/macro":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            rid = (qs.get("id") or [None])[0]
            if not rid:
                self._send(400, json.dumps({"error": "missing id"}))
                return
            try:
                self._send(200, json.dumps(research_macro_detail(rid), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path in ("/perf", "/perf.html"):
            try:
                with open(os.path.join(HERE, "perf.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "perf.html not found", "text/plain")
        elif path == "/api/perf":
            try:
                self._send(200, json.dumps(cached_perf(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/pick_perf":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            reviewer = (qs.get("reviewer") or [None])[0]
            since = (qs.get("since") or [None])[0]
            try:
                self._send(200, json.dumps(pick_perf_payload(reviewer, since), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/mainline_forward":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            top_s = (qs.get("top") or ["10"])[0]
            if not re.fullmatch(r"\d{1,3}", top_s) or not (1 <= int(top_s) <= 100):
                self._send(400, json.dumps({"error": "top 需为 1~100 的整数"}))
                return
            try:
                self._send(200, json.dumps(
                    mainline_forward_daily.forward_payload(top=int(top_s)), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/earnings-resonance":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            date = (qs.get("date") or [None])[0]
            if date is not None and not re.fullmatch(r"\d{8}", date):
                self._send(400, json.dumps({"error": "date 需为 YYYYMMDD"}))
                return
            try:
                self._send(200, json.dumps(
                    build_earnings_resonance_payload(date), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/moneyflow/board":
            try:
                c = _conn()
                try:
                    self._send(200, json.dumps(board_moneyflow.board_payload(c), ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path in ("/valuation", "/valuation.html"):
            try:
                with open(os.path.join(HERE, "valuation.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "valuation.html not found", "text/plain")
        elif path == "/api/valuation/targets":
            try:
                self._send(200, json.dumps(valuation.api_targets(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/valuation/band":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code = (qs.get("code") or [None])[0]
            if not code:
                self._send(400, json.dumps({"error": "missing code"}))
                return
            try:
                days = int((qs.get("days") or ["250"])[0])
            except ValueError:
                days = 250
            try:
                self._send(200, json.dumps(valuation.api_band(code, days), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/valuation/agent":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code = (qs.get("code") or [None])[0]
            if not code:
                self._send(400, json.dumps({"error": "missing code"}))
                return
            try:
                self._send(200, json.dumps(valuation.api_agent(code), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/valuation/target/drafts":
            try:
                c = _conn()
                try:
                    self._send(200, json.dumps(valuation_admin.list_drafts(c),
                                               ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/valuation/stock-search":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            q = (qs.get("q") or [""])[0]
            try:
                c = _conn()
                try:
                    self._send(200, json.dumps(
                        valuation_admin.search_stocks(c, q, py_match=py_match),
                        ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/since924":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            mode = (qs.get("mode") or ["adj"])[0]   # adj=复权含分红(默认), price=纯价格不含分红
            try:
                c = _conn()
                try:
                    self._send(200, json.dumps(since924.stocks_payload(c, mode), ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/since924/concept":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            mode = (qs.get("mode") or ["adj"])[0]
            try:
                c = _conn()
                try:
                    self._send(200, json.dumps(since924.concept_payload(c, mode), ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/since924/concept_members":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            board = (qs.get("board") or [None])[0]
            mode = (qs.get("mode") or ["adj"])[0]
            if not board:
                self._send(400, json.dumps({"error": "missing board"}))
                return
            try:
                c = _conn()
                try:
                    self._send(200, json.dumps(since924.members_payload(c, board, mode), ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/nailong.jpg":
            try:
                with open(os.path.join(HERE, "..", "workspace-bot11",
                                       "shuiyinailong.jpg"), "rb") as f:
                    self._send(200, f.read(), "image/jpeg")
            except FileNotFoundError:
                self._send(404, "not found", "text/plain")
        elif path == "/laok.jpg":
            try:
                with open(os.path.join(HERE, "..", "workspace-bot7", "avatar.png"), "rb") as f:
                    self._send(200, f.read(), "image/jpeg")
            except FileNotFoundError:
                self._send(404, "not found", "text/plain")
        elif path == "/api/auth/me":
            user = self._req_user()
            if not user:
                self._send(401, json.dumps({"error": "未登录"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"user": user}, ensure_ascii=False))
        elif path == "/api/admin/users":
            user = self._req_user()
            if not user or user["role"] != "admin":
                self._send(403 if user else 401,
                           json.dumps({"error": "需要管理员权限" if user else "未登录"},
                                      ensure_ascii=False))
                return
            try:
                c = _conn()
                try:
                    self._send(200, json.dumps({"users": scout_auth.list_users(c)},
                                               ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/margin":
            try:
                self._send(200, json.dumps(margin_overview(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/index_technical":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            date = (qs.get("date") or [None])[0]
            c = _conn()
            try:
                payload = index_technical_payload(c, date=date)
            finally:
                c.close()
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/index_technical_intraday":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            date = (qs.get("date") or [None])[0]
            c = _conn()
            try:
                payload = index_technical_intraday_payload(c, date=date)
            finally:
                c.close()
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/intraday_market":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            date = (qs.get("date") or [None])[0]
            c = _conn()
            try:
                payload = intraday_market_payload(c, date=date)
            finally:
                c.close()
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/market_series":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            days = (qs.get("days") or [None])[0]
            c = _conn()
            try:
                payload = market_series_payload(c, days=days)
            finally:
                c.close()
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/style_factors":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            days = (qs.get("days") or [None])[0]
            c = _conn()
            try:
                payload = style_factors_payload(c, days=days)
            finally:
                c.close()
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/health":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/auth/login":
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            username = str(body.get("username", "")).strip()
            password = str(body.get("password", ""))
            try:
                c = _conn()
                try:
                    scout_auth.purge_expired(c)
                    status, row = scout_auth.authenticate(c, username, password)
                    if status == "bad":
                        self._send(401, json.dumps({"error": "用户名或密码错误"},
                                                   ensure_ascii=False))
                        return
                    if status == "disabled":
                        self._send(403, json.dumps({"error": "账户已被停用"},
                                                   ensure_ascii=False))
                        return
                    token = scout_auth.create_session(c, row["id"])
                    self._send(200, json.dumps({
                        "token": token,
                        "user": {"id": row["id"], "username": row["username"],
                                 "display_name": row["display_name"], "role": row["role"]},
                    }, ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if path == "/api/auth/logout":
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth.startswith("Bearer ") else ""
            try:
                c = _conn()
                try:
                    if token:
                        scout_auth.delete_session(c, token)
                finally:
                    c.close()
                self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if path == "/api/admin/users":
            user = self._req_user()
            if not user or user["role"] != "admin":
                self._send(403 if user else 401,
                           json.dumps({"error": "需要管理员权限" if user else "未登录"},
                                      ensure_ascii=False))
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            try:
                c = _conn()
                try:
                    code, resp = _admin_create_user(c, body)
                    self._send(code, json.dumps(resp, ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if path == "/api/admin/users/update":
            user = self._req_user()
            if not user or user["role"] != "admin":
                self._send(403 if user else 401,
                           json.dumps({"error": "需要管理员权限" if user else "未登录"},
                                      ensure_ascii=False))
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            try:
                c = _conn()
                try:
                    code, resp = _admin_update_user(c, body)
                    self._send(code, json.dumps(resp, ensure_ascii=False))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if path == "/api/admin/users/password":
            user = self._req_user()
            if not user or user["role"] != "admin":
                self._send(403 if user else 401,
                           json.dumps({"error": "需要管理员权限" if user else "未登录"},
                                      ensure_ascii=False))
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            try:
                uid = int(body.get("id"))
            except (TypeError, ValueError):
                self._send(400, json.dumps({"error": "id 必须是整数"}, ensure_ascii=False))
                return
            pw = str(body.get("password", ""))
            if len(pw) < 4:
                self._send(400, json.dumps({"error": "密码至少 4 位"}, ensure_ascii=False))
                return
            try:
                c = _conn()
                try:
                    scout_auth.set_password(c, uid, pw)
                    self._send(200, json.dumps({"ok": True}))
                finally:
                    c.close()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if path == "/api/holdings":
            _u = self._req_user()
            if not _u:
                self._send(401, json.dumps({"error": "未登录"}, ensure_ascii=False))
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            code = str(body.get("code", "")).strip()
            name = body.get("name")
            cost = body.get("cost_price")
            ok, err = _validate_holding(code, cost)
            if not ok:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
                return
            try:
                c = _conn()
                try:
                    insert_holding(c, code, name, cost,
                                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _u["id"])
                finally:
                    c.close()
                self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/holdings/edit":
            _u = self._req_user()
            if not _u:
                self._send(401, json.dumps({"error": "未登录"}, ensure_ascii=False))
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            hid, err = _validate_holding_id(body.get("id"))
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
                return
            cost = body.get("cost_price")
            try:
                if float(cost) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self._send(400, json.dumps({"error": "成本价必须为正数"}, ensure_ascii=False))
                return
            try:
                c = _conn()
                try:
                    hit = update_holding_cost(c, hid, cost, _u["id"])
                finally:
                    c.close()
                if not hit:
                    self._send(400, json.dumps({"error": "该持仓不存在"}, ensure_ascii=False))
                else:
                    self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/holdings/sell":
            _u = self._req_user()
            if not _u:
                self._send(401, json.dumps({"error": "未登录"}, ensure_ascii=False))
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            hid, err = _validate_holding_id(body.get("id"))
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
                return
            sell = body.get("sell_price")
            ok, serr = _validate_sell(sell)
            if not ok:
                self._send(400, json.dumps({"error": serr}, ensure_ascii=False))
                return
            try:
                c = _conn()
                try:
                    moved = sell_holding(c, hid, sell,
                                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _u["id"])
                finally:
                    c.close()
                if not moved:
                    self._send(400, json.dumps({"error": "该持仓不存在"}, ensure_ascii=False))
                else:
                    self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/mainline/whitelist/add":
            _u = self._req_user()
            if not _u or _u["role"] != "admin":
                self._send(403 if _u else 401,
                           json.dumps({"error": "需要管理员权限" if _u else "未登录"}, ensure_ascii=False))
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            try:
                c = _conn()
                try:
                    code, resp = _whitelist_add(c, body)
                finally:
                    c.close()
                if code == 200:
                    _spawn_whitelist_backfill(resp["code"])
                    resp["backfill"] = "started"
                self._send(code, json.dumps(resp, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/mainline/whitelist/remove":
            _u = self._req_user()
            if not _u or _u["role"] != "admin":
                self._send(403 if _u else 401,
                           json.dumps({"error": "需要管理员权限" if _u else "未登录"}, ensure_ascii=False))
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            try:
                code, resp = _whitelist_remove(body)
                self._send(code, json.dumps(resp, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path in ("/api/valuation/target/add", "/api/valuation/target/enable",
                      "/api/valuation/target/dismiss", "/api/valuation/target/toggle"):
            _u = self._req_user()
            if not _u or _u["role"] != "admin":
                self._send(403 if _u else 401,
                           json.dumps({"error": "需要管理员权限" if _u else "未登录"},
                                      ensure_ascii=False))
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            action = path.rsplit("/", 1)[1]
            try:
                if action == "toggle":                      # 只写配置文件, 不碰库
                    code, resp = valuation_admin.toggle_target(body)
                else:
                    c = _conn()
                    try:
                        fn = {"add": valuation_admin.add_target,
                              "enable": valuation_admin.enable_target,
                              "dismiss": valuation_admin.dismiss_draft}[action]
                        code, resp = fn(c, body)
                    finally:
                        c.close()
                if code == 200 and action == "add":
                    valuation_admin.spawn_onboard(resp["ts_code"])
                    resp["onboard"] = "started"
                if code == 200 and action == "enable":
                    valuation_admin.spawn_first_band(resp["ts_code"])
                    resp["first_band"] = "started"
                self._send(code, json.dumps(resp, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        else:
            self._send(404, "not found", "text/plain")

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/holdings":
            _u = self._req_user()
            if not _u:
                self._send(401, json.dumps({"error": "未登录"}, ensure_ascii=False))
                return
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            hid, err = _validate_holding_id((qs.get("id") or [None])[0])
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
                return
            try:
                c = _conn()
                try:
                    delete_holding(c, hid, _u["id"])
                finally:
                    c.close()
                self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/closed":
            _u = self._req_user()
            if not _u:
                self._send(401, json.dumps({"error": "未登录"}, ensure_ascii=False))
                return
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            raw = (qs.get("id") or [None])[0]
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                self._send(400, json.dumps({"error": "id 必须是整数"}, ensure_ascii=False))
                return
            try:
                c = _conn()
                try:
                    c.execute("DELETE FROM follow_trade_closed WHERE id=? AND owner_id=?",
                              (cid, _u["id"]))
                    c.commit()
                finally:
                    c.close()
                self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        else:
            self._send(404, "not found", "text/plain")


def _ensure_admin_seed(c):
    """无 active admin 时按环境变量建初始 admin, 并把孤儿持仓归到它名下. 幂等."""
    if scout_auth.count_active_admins(c) > 0:
        return
    username = os.environ.get("SCOUT_ADMIN_USER", "admin")
    password = os.environ.get("SCOUT_ADMIN_PASS") or secrets.token_urlsafe(9)
    # 用户名可能已存在(非 admin): 存在则提升为 admin, 否则新建
    existing = c.execute("SELECT id FROM scout_user WHERE username=?", (username,)).fetchone()
    if existing:
        uid = existing["id"]
        c.execute("UPDATE scout_user SET role='admin', status='active' WHERE id=?", (uid,))
        c.commit()
    else:
        uid = scout_auth.create_user(c, username, password, display_name=username, role="admin")
        # env 提供的密码不回显到日志(避免明文落盘), 仅随机生成时打印一次性密码
        if os.environ.get("SCOUT_ADMIN_PASS"):
            print(f"[scout] 已创建初始管理员 user={username} (密码取自 SCOUT_ADMIN_PASS)", flush=True)
        else:
            print(f"[scout] 已创建初始管理员 user={username} pass={password} (请尽快修改)", flush=True)
    scout_auth.reassign_orphan_holdings(c, uid)


def main():
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18901)
    ap.add_argument("--db", default=DB_PATH,
                    help="market.db 路径 (默认生产库; 测试时指向临时库以隔离, 避免污染生产持仓表)")
    a = ap.parse_args()
    DB_PATH = a.db   # 用 --db 覆盖模块级 DB_PATH, 令 _conn() 全部走指定库
    scout_db.init_schema(DB_PATH)   # 幂等建表: 在指定库上确保 follow_holding 等表存在
    _c = _conn()
    try:
        _ensure_admin_seed(_c)
    finally:
        _c.close()
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    # 守护线程: .start() 立即返回不阻塞 serve_forever, 端口秒级可用; 复权重算只在后台发生
    threading.Thread(target=_keep_warm, daemon=True).start()
    print(f"[scout-server] 监听 http://0.0.0.0:{a.port}  (DB={DB_PATH})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
