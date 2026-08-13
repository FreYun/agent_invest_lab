# -*- coding: utf-8 -*-
"""B1 × B2 × B3 口径矩阵：把三个待拍板的口径当参数跑满全部组合，逐例比对差异。

为什么这么做而不是替人选一个：这三件事都不是技术最优问题，是口径归属问题。
  B1 阈值比较用原始值还是四舍五入值——决定全库"违反自订风控"那条结论是否成立；
  B2 每个 bot 最深那条线按 20 日峰值还是历史峰值——文档没写清，是文本歧义；
  B3 处境取 D 收盘还是 D-1 收盘——决定我们判 bot 违规时用的数它当时看没看得到。
把八种读法全算出来落到一张表，前端给开关，让人看着结论翻转再拍板。

自检：默认组合 (rounded, dd20, close) 必须与已落库的 121 行在【生产库已有的键上】
逐字节相同；新增键单独列出。不通过退出码 2。
"""
import argparse, hashlib, json, os, sqlite3, sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/rooot/agent_invest_lab/scripts")
import situation_eval_v02 as ev

DEFAULT = ("rounded", "dd20", "close")
COMBOS = [(t, d, a) for t in ("rounded", "raw")
                    for d in ("dd20", "alltime")
                    for a in ("close", "predecision")]

# persist() 在写库时才补的键，evaluate_case 不产出
PERSIST_ONLY = ("__coords_sha", "__annot_sha")

DDL = """
CREATE TABLE IF NOT EXISTS situation_caliber_matrix (
  subject_id       TEXT NOT NULL,
  subject_type     TEXT NOT NULL,
  bot_id           TEXT NOT NULL,
  run_id           TEXT NOT NULL,
  trade_date       TEXT NOT NULL,
  threshold_mode   TEXT NOT NULL CHECK (threshold_mode IN ('rounded','raw')),
  deepest_caliber  TEXT NOT NULL CHECK (deepest_caliber IN ('dd20','alltime')),
  asof_caliber     TEXT NOT NULL CHECK (asof_caliber IN ('close','predecision')),
  cell_id          TEXT NOT NULL,
  coords_json      TEXT NOT NULL CHECK (json_valid(coords_json)),
  computed_at      TEXT NOT NULL,
  evaluator_sha256 TEXT NOT NULL,
  PRIMARY KEY (subject_id, threshold_mode, deepest_caliber, asof_caliber)
);
CREATE INDEX IF NOT EXISTS idx_calmx_combo
  ON situation_caliber_matrix(threshold_mode, deepest_caliber, asof_caliber);
CREATE INDEX IF NOT EXISTS idx_calmx_subject ON situation_caliber_matrix(subject_id);
"""

WATCH = ["cell_id", "dd20_bucket", "stress_bucket",
         "persona_derisk_stage", "persona_stage_bucket", "persona_crossed_label",
         "persona_req_band", "persona_req_no_add", "persona_gate_gap_pp",
         "persona_band_conform", "persona_no_add_flag", "persona_rule_state",
         "defensive_state", "defensive_on_since", "days_since_first_cross",
         "equity_bucket", "equity_weight"]


def run_combo(mc, fc, cases, fams, fb, tm, dc, ac):
    ev.THRESHOLD_MODE, ev.DEEPEST_CALIBER, ev.ASOF_CALIBER = tm, dc, ac
    out = {}
    for c in cases:
        fam = fams.get((c["bot_id"], c["run_id"])) or fb.get(c["bot_id"], "unknown")
        co = ev.evaluate_case(mc, fc, c, fam)
        co["__bot_id"], co["__run_id"] = c["bot_id"], c["run_id"]
        out[c["case_id"]] = (co, ev.cell_id_of(co))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-db", required=True)
    ap.add_argument("--baseline-db", required=True)
    ap.add_argument("--market-db", default=ev.MARKET_DB)
    ap.add_argument("--fund-db", default=ev.FUND_DB)
    a = ap.parse_args()

    # 只挡写入目标。--baseline-db 是 `?mode=ro` 只读打开的，它本来就该指向生产库做对拍。
    if os.path.basename(a.library_db) == "experience-library-v7.db":
        print("!! 拒绝写生产库 experience-library-v7.db；请指向 P1 副本", file=sys.stderr)
        return 2

    ev_sha = hashlib.sha256(open(ev.__file__, "rb").read()).hexdigest()
    mc, fc = ev.ro(a.market_db), ev.ro(a.fund_db)
    lib = sqlite3.connect(a.library_db); lib.row_factory = sqlite3.Row

    fams, fb = {}, {}
    for r in lib.execute("SELECT bot_id, run_id, strategy_family FROM agent_persona_snapshots"):
        f = r["strategy_family"] or "unknown"
        fams[(r["bot_id"], r["run_id"])] = f
        if f != "unknown" or r["bot_id"] not in fb:
            fb[r["bot_id"]] = f
    cases = lib.execute("SELECT case_id,bot_id,run_id,trade_date,actual_actions_json "
                        "FROM experience_cases ORDER BY trade_date").fetchall()
    date_of = {c["case_id"]: c["trade_date"] for c in cases}
    print(f"案例 {len(cases)} 例｜求值器 sha256 {ev_sha[:16]}…｜组合 {len(COMBOS)} 种\n")

    res = {k: run_combo(mc, fc, cases, fams, fb, *k) for k in COMBOS}

    # ---------------- 自检 ----------------
    base = sqlite3.connect(f"file:{a.baseline_db}?mode=ro", uri=True); base.row_factory = sqlite3.Row
    prod = {r["subject_id"]: (json.loads(r["coords_json"]), r["cell_id"])
            for r in base.execute("SELECT subject_id,coords_json,cell_id FROM situation_vectors")}
    mism, newkeys, added = 0, {}, set()
    for cid, (co, cell) in res[DEFAULT].items():
        pj, pc = prod.get(cid, (None, None))
        if pj is None:
            mism += 1; continue
        shared = {k for k in pj if k not in PERSIST_ONLY}
        for k in shared:
            pv, cv = pj.get(k), co.get(k)
            if k == "__missing" and isinstance(pv, dict) and isinstance(cv, dict):
                # __missing 是诊断注记而非坐标：允许新增条目（本次多了 equity_weight_post），
                # 但生产库已有的每一条的措辞必须逐字不变——改措辞属于静默重述，要挡住。
                bad = [x for x in pv if pv[x] != cv.get(x)]
                if bad:
                    mism += 1
                    if mism <= 5:
                        print(f"  !! {cid} __missing[{bad[0]}] 生产={pv[bad[0]]!r} 本次={cv.get(bad[0])!r}")
                    break
                added.update(set(cv) - set(pv))
                continue
            if pv != cv:
                mism += 1
                if mism <= 5:
                    print(f"  !! {cid} 键 {k}: 生产={pv!r} 本次={cv!r}")
                break
        if pc != cell:
            mism += 1; print(f"  !! {cid} cell 生产={pc} 本次={cell}")
        for k in co:
            if k not in pj and k not in PERSIST_ONLY:
                newkeys.setdefault(k, set()).add(json.dumps(co[k], ensure_ascii=False))
    print(f"[自检] 默认组合 {DEFAULT} 在生产库已有键上的不一致行数 = {mism} / {len(cases)}")
    if added:
        print(f"[自检] __missing 内新增的诊断条目：{sorted(added)}（仅新增，原有条目逐字未变）")
    if newkeys:
        print("[自检] 本次新增的键（生产库没有，属参数化引入）：")
        for k, vs in sorted(newkeys.items()):
            s = sorted(vs)
            print(f"    {k:24s} 取值 {len(s)} 种" + (f"  例：{s[0][:60]}" if len(s) <= 3 else ""))
    if mism:
        print("自检未通过：参数化动了默认行为，退出。")
        return 2

    # ---------------- 差异报告 ----------------
    print("\n== 各组合相对默认的差异 ==")
    print(f"  {'B1阈值':8s} {'B2最深线':9s} {'B3时点':12s} {'cell变':>6s} {'受影响案例':>10s}  主要变化键")
    for k in COMBOS:
        b = res[DEFAULT]
        ncell = sum(1 for cid in res[k] if res[k][cid][1] != b[cid][1])
        changed, keycnt = set(), {}
        for cid, (co, cell) in res[k].items():
            bco, bcell = b[cid]
            for w in WATCH:
                x = bcell if w == "cell_id" else bco.get(w)
                y = cell if w == "cell_id" else co.get(w)
                if x != y:
                    keycnt[w] = keycnt.get(w, 0) + 1; changed.add(cid)
        top = "、".join(f"{w}×{n}" for w, n in sorted(keycnt.items(), key=lambda t: -t[1])[:3]) or "—"
        print(f"  {k[0]:8s} {k[1]:9s} {k[2]:12s} {ncell:6d} {len(changed):10d}  {top}")

    # ---------------- 违规计数随口径的变化 ----------------
    print("\n== 「越线未到位」（band 超上限）案例日数随口径的变化 ==")
    for k in COMBOS:
        hits = [cid for cid, (co, _) in res[k].items() if co.get("persona_band_conform") == "超上限"]
        segs = {}
        for cid in hits:
            co = res[k][cid][0]
            segs.setdefault((co.get("__bot_id"), co.get("__run_id")), []).append(date_of[cid])
        seg_desc = "；".join(f"{b} {min(v)}~{max(v)} 共{len(v)}天" for (b, _), v in sorted(segs.items()))
        print(f"  {k[0]:8s}/{k[1]:8s}/{k[2]:12s}  {len(hits):2d} 例日  → {seg_desc or '无'}")

    # ---------------- 落表 ----------------
    lib.executescript(DDL)
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for k, d in res.items():
        for cid, (co, cell) in d.items():
            lib.execute("INSERT OR REPLACE INTO situation_caliber_matrix "
                        "(subject_id,subject_type,bot_id,run_id,trade_date,threshold_mode,"
                        " deepest_caliber,asof_caliber,cell_id,coords_json,computed_at,evaluator_sha256)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, "case", co.get("__bot_id"), co.get("__run_id"), date_of[cid],
                         k[0], k[1], k[2], cell,
                         json.dumps(co, ensure_ascii=False, sort_keys=True), now, ev_sha))
            n += 1
    lib.commit()
    got = lib.execute("SELECT COUNT(*) FROM situation_caliber_matrix").fetchone()[0]
    exp = len(cases) * len(COMBOS)
    print(f"\n[落表] 写入 {n} 行；表内 {got} 行；应为 {len(cases)}×{len(COMBOS)}={exp} "
          + ("✓" if got == exp else "✗"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
