#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1.3：其余关系边——SCOPED_TO / CORROBORATES / REFUTES / COMPETES_WITH。

INSTANTIATES（谁是这张卡的实例）已由 card_predicates.py 建好。本脚本建剩下四类，
每一类都要先回答"这条边凭什么"，凭据写进 `basis` 列，前端必须显示它。

**SCOPED_TO —— 这张卡的先例实际落在哪几个处境格里。**
  不是作者声明的适用范围，是**实测**范围。两者常常不一样，而差异本身有信息：
  一张自称"适用于组合层面"的卡，如果先例全部落在「平静|无」一个格里，那它其实
  只在平静无回撤时被观察到过。边上带覆盖比例，让人看得见这件事。

**CORROBORATES / REFUTES —— 先例的后续走势是否与卡的主张同向。**
  ⚠ 这是全库最弱的一类边，必须当成**弱证据**用，理由有三条，缺一不可地说清楚：
   1. 后续走势是 bot 净值，不是这次动作的归因（见 outcome_backfill 的口径）；
   2. 只有 `hit_means='违反'` 的卡才有方向可言——卡说"别这么干"，那么"这么干了
      之后净值跌了"才算与主张同向。`处境`/`缺陷`/`流程` 三类卡没有方向，
      **一律不建这类边**，而不是硬凑一个方向出来；
   3. 样本极小。下面每条边都带 n，n 小于阈值的整组标 `weak_evidence`。
  同向记 CORROBORATES，反向记 REFUTES。**两种都建**——只建支持的那一半，
  就是在给卡片挑好看的证据。

**COMPETES_WITH —— 两张卡可能在同一处境下给出相反指令。**
  库里已有 16 条，是**按关键词重叠**检出的（signature 形如
  `portfolio|unspecified|liquidity`），它的问题是"出现相同关键词"不等于"条件真的
  重叠"——这一点连它自己的 discriminating_evidence 都写着。
  本脚本按**实测处境格重叠**重建，并把旧的 16 条**保留**为 `superseded`，
  不删。删掉就看不见"我们曾经用关键词判过、判出了什么"，也就无法比较两种判法。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

RELATION_VERSION = "relation_v0.1"
MIN_N_EVIDENCE = 5      # 低于此值，方向性证据整组标 weak
HORIZON = 20


def eid(*p):
    return "e_" + hashlib.sha1("|".join(map(str, p)).encode()).hexdigest()[:20]


def card_meta(lib):
    out = {}
    for r in lib.execute("SELECT card_id, version, title, status, card_type, "
                         "applicability_json FROM experience_card_versions"):
        if r["card_id"] in out and out[r["card_id"]]["version"] >= r["version"]:
            continue
        out[r["card_id"]] = dict(version=r["version"], title=r["title"], status=r["status"],
                                 card_type=r["card_type"],
                                 appl=json.loads(r["applicability_json"] or "{}"))
    return out


def build_scoped_to(lib, cards, now):
    """卡 → 处境格。分母是该卡的全部实例，分子是落在该格里的实例。"""
    rows = lib.execute(
        "SELECT card_id, cell_id, subject_kind, count(*) n FROM experience_card_verdicts "
        "WHERE verdict=1 AND cell_id IS NOT NULL GROUP BY 1,2,3").fetchall()
    tot = defaultdict(int)
    for r in rows:
        tot[r["card_id"]] += r["n"]
    made = 0
    for r in rows:
        share = r["n"] / tot[r["card_id"]]
        lib.execute(
            "INSERT OR REPLACE INTO experience_edges (edge_id, edge_type, src_kind, src_id,"
            " dst_kind, dst_id, role, basis, basis_json, cell_id, computed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (eid("SCOPED_TO", r["card_id"], r["cell_id"], r["subject_kind"]),
             "SCOPED_TO", "card", r["card_id"], "cell", r["cell_id"],
             r["subject_kind"], "observed_instances",
             json.dumps({"实例数": r["n"], "该卡实例总数": tot[r["card_id"]],
                         "占比": round(share, 4),
                         "说明": "实测范围，不是作者声明的适用范围"}, ensure_ascii=False),
             r["cell_id"], now))
        made += 1
    print(f"-- SCOPED_TO {made} 条（卡 {len(tot)} 张有实例）--")
    for cid, n in sorted(tot.items(), key=lambda x: -x[1])[:12]:
        cs = [r for r in rows if r["card_id"] == cid]
        top = sorted(cs, key=lambda r: -r["n"])[:3]
        conc = max(r["n"] for r in cs) / n
        flag = "  ⚠ 先例高度集中于单格，实测范围远窄于自称范围" if conc > 0.6 else ""
        print(f"   {cid[7:15] if cid.startswith('lesson') else cid[4:12]}  实例 {n:5d}  "
              f"覆盖 {len({r['cell_id'] for r in cs}):2d} 格  "
              f"最大格 {top[0]['cell_id']} {conc*100:.0f}%{flag}")
    print()
    return made


def build_evidence(lib, cards, reg_roles, now):
    """CORROBORATES / REFUTES。只对 hit_means='违反' 的卡建。"""
    directional = [c for c, r in reg_roles.items() if r == "违反"]
    print(f"-- 方向性证据：{len(directional)}/{len(reg_roles)} 张卡有方向"
          f"（只有「命中=违反」的卡才谈得上同向/反向）--")
    made = Counter()
    for cid in sorted(directional):
        rows = lib.execute(
            "SELECT v.subject_id, v.subject_kind, v.cell_id, o.fwd_return_pct r,"
            " o.fwd_excess_pct e, o.outcome_available_at av "
            "FROM experience_card_verdicts v JOIN situation_outcomes o "
            "  ON o.subject_id = v.subject_id AND o.subject_kind = v.subject_kind "
            "WHERE v.card_id=? AND v.verdict=1 AND o.horizon_days=? AND o.status='ok'",
            (cid, HORIZON)).fetchall()
        n = len(rows)
        weak = n < MIN_N_EVIDENCE
        pos = sum(1 for r in rows if (r["r"] or 0) > 0)
        for r in rows:
            ret = r["r"]
            if ret is None:
                continue
            # 卡说"别这么干"。干了之后跌 → 与主张同向（CORROBORATES）；涨 → 反向。
            et = "CORROBORATES" if ret < 0 else "REFUTES"
            lib.execute(
                "INSERT OR REPLACE INTO experience_edges (edge_id, edge_type, src_kind, src_id,"
                " dst_kind, dst_id, role, basis, basis_json, trade_date, cell_id, computed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid(et, r["subject_id"], cid, HORIZON), et, r["subject_kind"],
                 r["subject_id"], "card", cid,
                 "弱证据" if weak else "证据", "forward_nav_not_attribution",
                 json.dumps({f"{HORIZON}日净值%": round(ret, 4),
                             "超额%": None if r["e"] is None else round(r["e"], 4),
                             "结果可见于": r["av"],
                             "同组样本n": n,
                             "口径": "bot 自身净值走势，不是本次动作的归因；"
                                     "样本小于 5 时整组标弱证据"}, ensure_ascii=False),
                 None, r["cell_id"], now))
            made[et] += 1
        print(f"   {cid[7:15]}  实例有结果 {n:3d} 例  其中上涨 {pos}  下跌 {n-pos}  "
              f"{'⚠ 弱证据（n<5）' if weak else ''}")
    print(f"   建边 CORROBORATES {made['CORROBORATES']}  REFUTES {made['REFUTES']}"
          f"  合计 {sum(made.values())}\n")
    return sum(made.values())


def build_competes(lib, cards, now):
    """按实测处境格重叠重建 COMPETES_WITH；旧的关键词边保留并标 superseded。"""
    old = lib.execute("SELECT * FROM experience_conflicts").fetchall()
    for r in old:
        lib.execute(
            "INSERT OR REPLACE INTO experience_edges (edge_id, edge_type, src_kind, src_id,"
            " dst_kind, dst_id, role, basis, basis_json, computed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid("COMPETES_WITH_kw", r["conflict_id"]), "COMPETES_WITH", "card",
             r["left_card_id"], "card", r["right_card_id"], "关键词判定(已被取代)",
             "keyword_signature_superseded",
             json.dumps({"conflict_id": r["conflict_id"],
                         "overlap": json.loads(r["overlap_json"]),
                         "说明": "按 topics 关键词重叠检出。保留是为了能和处境格重叠的判法"
                                 "作对比——「出现相同关键词」不等于「条件真的重叠」，"
                                 "这一点它自己的 discriminating_evidence 就写着。"},
                        ensure_ascii=False), now))

    # 新判法：两张卡的实例落在同一处境格，且各自在该格里都有 ≥1 实例
    cells = defaultdict(lambda: defaultdict(int))
    for r in lib.execute("SELECT card_id, cell_id, count(*) n FROM experience_card_verdicts "
                         "WHERE verdict=1 AND cell_id IS NOT NULL GROUP BY 1,2"):
        cells[r["cell_id"]][r["card_id"]] = r["n"]
    made = 0
    pairs = []
    for cell, m in cells.items():
        ids = sorted(m)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.append((ids[i], ids[j], cell, m[ids[i]], m[ids[j]]))
    agg = defaultdict(list)
    for a, b, cell, na, nb in pairs:
        agg[(a, b)].append((cell, na, nb))
    for (a, b), cs in agg.items():
        lib.execute(
            "INSERT OR REPLACE INTO experience_edges (edge_id, edge_type, src_kind, src_id,"
            " dst_kind, dst_id, role, basis, basis_json, computed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid("CO_OCCURS", a, b), "CO_OCCURS_IN_CELL", "card", a, "card", b,
             "同格共现", "observed_cell_overlap",
             json.dumps({"共现格数": len(cs),
                         "格": [{"cell": c, "左实例": na, "右实例": nb} for c, na, nb in cs],
                         "说明": "两张卡的先例落在同一处境格。**这只是共现，不是冲突**——"
                                 "要判冲突还需要「两张卡在该格给出相反指令」，"
                                 "而指令方向目前只有 action_policy_json 的文字，未机械化。"},
                        ensure_ascii=False), now))
        made += 1
    print(f"-- COMPETES_WITH：旧关键词边 {len(old)} 条已保留并标 superseded --")
    print(f"   新建 CO_OCCURS_IN_CELL {made} 条（卡对）")
    print("   注意：**没有**把共现直接升级成冲突。共现是可测的，冲突需要指令方向，")
    print("   而指令方向现在只有文字，没有机械口径。硬升级等于用一个测得到的东西")
    print("   冒充一个测不到的东西。\n")
    # ⚠ 两种判法**不能直接比**，比之前先看它们作用在哪些卡上。
    kw_cards = {r["left_card_id"] for r in old} | {r["right_card_id"] for r in old}
    cell_cards = {c for pair in agg for c in pair}
    shared = kw_cards & cell_cards
    ov = len({(r["left_card_id"], r["right_card_id"]) for r in old} & set(agg))
    print(f"   关键词边覆盖卡 {len(kw_cards)} 张；处境格共现覆盖卡 {len(cell_cards)} 张；"
          f"两者交集 {len(shared)} 张")
    if not shared:
        print(f"   ⚠ **两种判法作用在互不相交的两批卡上，因此「{len(old)} 对里 {ov} 对重合」"
              f"这句话没有信息量**——")
        print("     它不是「关键词判错了」的证据，而是恒成立的：关键词边全部落在 204 张")
        print("     case_only 卡之间，那些卡没有谓词、不进判定表，所以永远不可能出现在")
        print("     处境格共现里。要真比，得先给 case_only 卡也写谓词。")
        print("     在那之前，旧的 16 条既不能说它对，也不能说它错——只能说没测过。")
    else:
        print(f"   在两者都覆盖的 {len(shared)} 张卡上：关键词判出的对里有 {ov} 对确实同格共现")
    print()
    return made + len(old)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-db", required=True)
    args = ap.parse_args()
    if os.path.basename(args.library_db) == "experience-library-v7.db":
        print("!! 拒绝写生产库；请指向 P1 副本", file=sys.stderr)
        return 2
    lib = sqlite3.connect(args.library_db)
    lib.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    cards = card_meta(lib)
    print(f"== {RELATION_VERSION} | 卡 {len(cards)} 张 ==\n")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import card_predicates as cp
    reg_roles = {c: m["hit_means"] for c, m in cp.REGISTRY.items()
                 if m["kind"] != "unexpressible"}

    for t in ("SCOPED_TO", "CORROBORATES", "REFUTES", "COMPETES_WITH", "CO_OCCURS_IN_CELL"):
        lib.execute("DELETE FROM experience_edges WHERE edge_type=?", (t,))
    n1 = build_scoped_to(lib, cards, now)
    n2 = build_evidence(lib, cards, reg_roles, now)
    n3 = build_competes(lib, cards, now)
    lib.commit()

    print("== 边表全貌 ==")
    tot = 0
    for r in lib.execute("SELECT edge_type, basis, count(*) n FROM experience_edges "
                         "GROUP BY 1,2 ORDER BY n DESC"):
        print(f"   {r['edge_type']:20s} {r['basis']:34s} {r['n']:6d}")
        tot += r["n"]
    real = lib.execute("SELECT count(*) c FROM experience_edges").fetchone()["c"]
    print(f"   {'合计':20s} {'':34s} {tot:6d}   （表内实际 {real}，一致：{tot == real}）")
    print(f"   本次新建 {n1} + {n2} + {n3} = {n1+n2+n3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
