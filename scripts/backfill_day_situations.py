#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0.5：把处境坐标从「121 个案例」扩到「每个交易日」。

**为什么必须做这一步。** 底率（"在这种处境下这么干，后来怎么样"）的分母是处境格
里的先例数。121 个案例摊进 14 个格子，中位格只有 9 例、最小格 1 例——在这个样本
上算出来的任何比例都是噪音，前端把它显示出来只会制造一个"看起来很科学"的假象。
本脚本对 8 个带 persona 快照的 (bot, run) 的**每一个交易日**求同一套坐标，
1,619 行 ≈ 案例数的 13.4 倍。

**与 situation_eval_v02 的关系：不复制它的任何一行判定逻辑。**
market_axes / account_axes / signal_axes / decision_axes / compliance_axis /
cell_id_of 全部 import 自求值器；B1/B2/B3 三个口径参数也通过设置求值器的全局量
生效，保证案例线与日级线永远同口径。若两边的坐标定义出现分叉，底率的分子分母
就来自两个不同的世界——这正是必须共用同一份代码的原因。

**唯一必须新写的东西是"当日动作"的来源。** 案例有现成的 `actual_actions_json`，
回测日没有，得从 `fund_bot_actions`（dash/回测 run）与 `oos_bot_actions`
（oos live run）重建。重建是本脚本唯一的新增判定，因此它带回归测试：

    --verify  用重建出来的动作重算那 121 个案例的决策轴（action_count /
              decision_type），与图书馆里已存的坐标逐例比对，任何一例不一致即退出 2。

**写哪个库。** 默认写 P1 库（生产库的副本），不碰 experience-library-v7.db。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import situation_eval_v02 as ev  # noqa: E402


BACKFILL_VERSION = "backfill_day_v0.1"

# 动作来源两张表：oos live run 走 oos_bot_actions（键 live_run_id），
# 其余走 fund_bot_actions（键 run_id）。没有第三种来源；若某 (bot,run) 在两张表
# 里都查不到任何一天的动作，会在报告里单独列出来，不静默当成"全程不动"——
# "查不到动作"和"确实没动作"是两件事，混在一起会让 decision_type 全变成"不动"。
ACTION_SOURCES = (
    ("oos_bot_actions", "live_run_id"),
    ("fund_bot_actions", "run_id"),
)


def action_rows(fc: sqlite3.Connection, bot_id: str, run_id: str):
    """取该 (bot, run) 全部动作，返回 {trade_date: [action_type, ...]} 与命中的表名。"""
    for table, key in ACTION_SOURCES:
        try:
            rows = fc.execute(
                f"SELECT action_date, action_type FROM {table} "
                f"WHERE bot_id=? AND {key}=? ORDER BY action_date",
                (bot_id, run_id),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        if rows:
            by_date: dict = {}
            for r in rows:
                by_date.setdefault(r["action_date"], []).append(r["action_type"])
            return by_date, table
    return {}, None


def actions_json(by_date: dict, date: str) -> str:
    """拼成 decision_axes 认得的形状。它只读 action_type，其余字段不构造，
    免得凭空造出一份看起来像原始记录、实际是本脚本编的 JSON。"""
    return json.dumps([{"action_type": t} for t in by_date.get(date, [])], ensure_ascii=False)


DDL_DAY = """
CREATE TABLE IF NOT EXISTS situation_vectors (
  situation_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('case','backtest_day')),
  subject_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  coords_json TEXT NOT NULL CHECK (json_valid(coords_json)),
  cell_id TEXT NOT NULL,
  evaluator_version TEXT NOT NULL,
  data_vintage TEXT NOT NULL,
  computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_situation_cell ON situation_vectors(cell_id);
CREATE INDEX IF NOT EXISTS idx_situation_subject ON situation_vectors(subject_type, subject_id);
CREATE TABLE IF NOT EXISTS situation_vectors_history (
  history_id INTEGER PRIMARY KEY AUTOINCREMENT,
  situation_id TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  coords_json TEXT NOT NULL,
  cell_id TEXT NOT NULL,
  evaluator_version TEXT NOT NULL,
  data_vintage TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  superseded_at TEXT NOT NULL,
  superseded_by_sha TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sv_hist_subject ON situation_vectors_history(subject_type, subject_id);
"""


def persist_days(lib: sqlite3.Connection, results, vintage: str, now: str) -> Counter:
    """与 ev.persist 同一套留痕纪律（重述前先归档），但 subject_type='backtest_day'。

    没有直接复用 ev.persist 的原因：它把 subject_type 写死成 'case'，
    而改它就要重新过一轮独立验证。此处宁可重写这 30 行搬运代码，
    也不改那份已被验证过的文件——**判定逻辑一行都没重复，重复的只有搬运。**
    """
    lib.executescript(DDL_DAY)
    stats: Counter = Counter()
    for sid_key, date, co, cell in results:
        sid = f"sv_{ev.EVALUATOR_VERSION}_{sid_key}"
        sha = ev.coords_sha(co)
        ash = ev.annot_sha(co)
        old = lib.execute(
            "SELECT * FROM situation_vectors WHERE situation_id=?", (sid,)
        ).fetchone()
        if old is not None:
            try:
                old_co = json.loads(old["coords_json"])
                old_sha, old_ash = ev.coords_sha(old_co), ev.annot_sha(old_co)
            except json.JSONDecodeError:
                old_sha = old_ash = "unparseable"
            if old_sha == sha and old_ash == ash:
                stats["未变"] += 1
                continue
            stats["重述(旧值已存档)" if old_sha != sha else "仅注记变更(旧值已存档)"] += 1
            lib.execute(
                "INSERT INTO situation_vectors_history "
                "(situation_id, subject_type, subject_id, trade_date, coords_json, cell_id,"
                " evaluator_version, data_vintage, computed_at, superseded_at, superseded_by_sha)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (old["situation_id"], old["subject_type"], old["subject_id"], old["trade_date"],
                 old["coords_json"], old["cell_id"], old["evaluator_version"],
                 old["data_vintage"], old["computed_at"], now, sha),
            )
        else:
            stats["新增"] += 1
        co = dict(co, __coords_sha=sha, __annot_sha=ash)
        lib.execute(
            "INSERT OR REPLACE INTO situation_vectors "
            "(situation_id, subject_type, subject_id, trade_date, coords_json, cell_id,"
            " evaluator_version, data_vintage, computed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, "backtest_day", sid_key, date, json.dumps(co, ensure_ascii=False), cell,
             ev.EVALUATOR_VERSION, vintage, now),
        )
    lib.commit()
    return stats


def verify_against_cases(lib, fc) -> int:
    """回归：用重建出来的动作重算 121 个案例的决策轴，与库里已存坐标逐例比对。

    这是本脚本唯一新增判定（动作重建）的唯一证据。不通过就不该相信另外 1,498 天。
    """
    cases = lib.execute(
        "SELECT case_id, bot_id, run_id, trade_date, actual_actions_json FROM experience_cases"
    ).fetchall()
    stored = {
        r["subject_id"]: json.loads(r["coords_json"])
        for r in lib.execute(
            "SELECT subject_id, coords_json FROM situation_vectors WHERE subject_type='case'"
        )
    }
    cache: dict = {}
    bad, checked, no_stored = [], 0, 0
    for c in cases:
        key = (c["bot_id"], c["run_id"])
        if key not in cache:
            cache[key] = action_rows(fc, *key)
        by_date, _ = cache[key]
        rebuilt = ev.decision_axes(actions_json(by_date, c["trade_date"]))["axes"]
        truth = ev.decision_axes(c["actual_actions_json"])["axes"]
        co = stored.get(c["case_id"])
        if co is None:
            no_stored += 1
            continue
        checked += 1
        for f in ("action_count", "decision_type"):
            if rebuilt.get(f) != truth.get(f) or truth.get(f) != co.get(f):
                bad.append((c["case_id"], c["bot_id"], c["trade_date"], f,
                            rebuilt.get(f), truth.get(f), co.get(f)))
    print(f"-- 动作重建回归：比对 {checked} 例（库中无坐标 {no_stored} 例）--")
    if not bad:
        print("   ✓ 重建动作 == 案例自带动作 == 已落库坐标，逐例一致\n")
        return 0
    print(f"   ✗ {len(bad)} 处不一致（重建 / 案例自带 / 已落库）：")
    for cid, bot, d, f, a, b, c2 in bad[:25]:
        print(f"     {cid[:22]} {bot} {d} {f}: {a!r} / {b!r} / {c2!r}")
    if len(bad) > 25:
        print(f"     …… 另有 {len(bad)-25} 处")
    print()
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-db", required=True, help="写入目标（应为 P1 副本，不要指生产库）")
    ap.add_argument("--market-db", default=ev.MARKET_DB)
    ap.add_argument("--fund-db", default=ev.FUND_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="只跑动作重建回归，不算不写")
    ap.add_argument("--max-date", default=None,
                    help="只算到该交易日（含）。默认取行情 vintage：账户快照比行情先落库，"
                         "当天的账户行已经在、行情还没到，硬算出来的只能是「坐标不全」，"
                         "而且没有任何东西会在行情补齐后触发重算——不如不算，下次自然纳入")
    ap.add_argument("--threshold-mode", choices=("rounded", "raw"), default=ev.THRESHOLD_MODE)
    ap.add_argument("--deepest-caliber", choices=("dd20", "alltime"), default=ev.DEEPEST_CALIBER)
    ap.add_argument("--asof-caliber", choices=("close", "predecision"), default=ev.ASOF_CALIBER)
    args = ap.parse_args()

    if os.path.basename(args.library_db) == "experience-library-v7.db":
        print("!! 拒绝写生产库 experience-library-v7.db；请指向 P1 副本", file=sys.stderr)
        return 2

    ev.THRESHOLD_MODE = args.threshold_mode
    ev.DEEPEST_CALIBER = args.deepest_caliber
    ev.ASOF_CALIBER = args.asof_caliber
    caliber = (args.threshold_mode, args.deepest_caliber, args.asof_caliber)

    mc = ev.ro(args.market_db)
    fc = ev.ro(args.fund_db)
    lib = sqlite3.connect(args.library_db)
    lib.row_factory = sqlite3.Row

    if args.verify:
        return verify_against_cases(lib, fc)

    # 口径与案例线必须同一份：先跑一次回归再算，不通过直接停。
    if verify_against_cases(lib, fc) != 0:
        print("!! 动作重建回归未通过，拒绝回填（否则 1,498 天的决策轴不可信）", file=sys.stderr)
        return 2

    personas = lib.execute(
        "SELECT DISTINCT bot_id, run_id, strategy_family FROM agent_persona_snapshots"
    ).fetchall()
    fams = {(r["bot_id"], r["run_id"]): (r["strategy_family"] or "unknown") for r in personas}

    vintage = mc.execute(
        "SELECT max(trade_date) d FROM index_daily WHERE ts_code=?", (ev.BENCHMARK,)
    ).fetchone()["d"]
    now = datetime.now(timezone.utc).isoformat()

    max_date = args.max_date or ev._dashed(vintage)

    mkt_cache: dict = {}
    sig_cache: dict = {}
    results = []
    per_pair = []
    no_action_src = []
    skipped_future = []

    for (bot_id, run_id), family in sorted(fams.items()):
        all_days = [
            r["trade_date"] for r in fc.execute(
                "SELECT trade_date FROM fund_bot_daily_snapshots "
                "WHERE bot_id=? AND run_id=? ORDER BY trade_date", (bot_id, run_id)
            )
        ]
        days = [d for d in all_days if d <= max_date]
        skipped_future += [(bot_id, run_id, d) for d in all_days if d > max_date]
        if not days:
            per_pair.append((bot_id, run_id, family, 0, 0, "无快照" if not all_days else "全部晚于行情"))
            continue
        by_date, src = action_rows(fc, bot_id, run_id)
        if src is None:
            no_action_src.append((bot_id, run_id))
        n_ok = 0
        for date in days:
            ymd = ev._ymd(date)
            if ymd not in mkt_cache:
                mkt_cache[ymd] = ev.market_axes(mc, ymd)
            if (family, ymd) not in sig_cache:
                sig_cache[(family, ymd)] = ev.signal_axes(mc, family, ymd)
            parts = [
                mkt_cache[ymd],
                ev.account_axes(fc, bot_id, run_id, date),
                sig_cache[(family, ymd)],
                ev.decision_axes(actions_json(by_date, date)),
            ]
            coords: dict = {}
            missing: dict = {}
            for p in parts:
                coords.update(p["axes"])
                missing.update(p["missing"])
            coords.update(ev.compliance_axis(coords))
            if src is None:
                missing["decision_type"] = (
                    f"{bot_id}/{run_id} 在 fund_bot_actions 与 oos_bot_actions 里都没有任何动作行，"
                    "无法区分「全程未操作」与「动作未入库」；决策轴按未知处理")
                coords["decision_type"] = None
                coords["action_count"] = None
            coords["__missing"] = missing
            coords["__persona_family"] = family
            coords["__bot_id"] = bot_id
            coords["__run_id"] = run_id
            coords["__subject_kind"] = "backtest_day"
            coords["__action_source"] = src
            coords["__caliber"] = {"threshold_mode": caliber[0],
                                   "deepest_caliber": caliber[1],
                                   "asof_caliber": caliber[2]}
            sid_key = f"{bot_id}|{run_id}|{date}"
            results.append((sid_key, date, coords, ev.cell_id_of(coords)))
            n_ok += 1
        per_pair.append((bot_id, run_id, family, len(days), n_ok,
                         src or "无动作来源"))

    print(f"== {BACKFILL_VERSION} | 求值器 {ev.EVALUATOR_VERSION} | 行情 vintage {vintage} "
          f"| 口径 {'/'.join(caliber)} ==\n")
    print(f"{'bot':9s} {'run':44s} {'family':22s} {'快照日':>5s} {'已算':>5s}  动作来源")
    for bot, run, fam, nd, nk, src in per_pair:
        print(f"{bot:9s} {run:44s} {fam:22s} {nd:5d} {nk:5d}  {src}")
    tot_days = sum(p[3] for p in per_pair)
    tot_ok = sum(p[4] for p in per_pair)
    print(f"{'合计':9s} {'':44s} {'':22s} {tot_days:5d} {tot_ok:5d}")
    print(f"  加数校验：{'+'.join(str(p[4]) for p in per_pair if p[4])} = {tot_ok}"
          f"   （与 results 行数 {len(results)} 一致：{tot_ok == len(results)}）\n")
    if no_action_src:
        print("!! 以下 (bot,run) 两张动作表里都查不到行，决策轴已标未知而非「不动」：")
        for b, r in no_action_src:
            print(f"     {b} / {r}")
        print()
    if skipped_future:
        print(f"-- 已跳过 {len(skipped_future)} 个晚于行情 vintage({max_date}) 的账户快照日 --")
        print("   （账户快照当天就写，行情要等收盘落库；这些日子的市场轴现在必然算不出来，")
        print("     强算只会得到「坐标不全」并永久留在库里。跳过，等行情补齐后自动纳入。）")
        for b, r, d in skipped_future:
            print(f"     {b:9s} {r:44s} {d}")
        print()

    n_cases = lib.execute("SELECT count(*) c FROM experience_cases").fetchone()["c"]
    print(f"-- 扩量倍数：{len(results)} 日 / {n_cases} 例 = {len(results)/n_cases:.1f}x --\n")

    cells = Counter(cell for _, _, _, cell in results)
    live = {k: v for k, v in cells.items() if k != ev.INCOMPLETE_CELL}
    print(f"-- 处境格分布（日级，共 {len(live)} 个有效格，坐标不全 "
          f"{cells.get(ev.INCOMPLETE_CELL, 0)} 行）--")
    for cell, n in sorted(live.items(), key=lambda x: -x[1]):
        print(f"   {cell:16s} {n:5d}  {n/max(1,sum(live.values()))*100:5.1f}%  {'█'*max(1,n//8)}")
    if live:
        ge5 = sum(v for v in live.values() if v >= 5)
        print(f"\n   ≥5 例格覆盖 {ge5}/{sum(live.values())} = "
              f"{ge5/sum(live.values())*100:.1f}%   最大格 {max(live.values())} "
              f"({max(live.values())/sum(live.values())*100:.1f}%)   "
              f"孤格 {sum(1 for v in live.values() if v == 1)} 个")
        print(f"   恒等式 有效 + 不全 = 总行：{sum(live.values())} + "
              f"{cells.get(ev.INCOMPLETE_CELL, 0)} = "
              f"{sum(live.values()) + cells.get(ev.INCOMPLETE_CELL, 0)} "
              f"-> {sum(live.values()) + cells.get(ev.INCOMPLETE_CELL,0) == len(results)}")
    print()

    if args.dry_run:
        print("(dry-run，未写库)")
        return 0

    stats = persist_days(lib, results, vintage, now)
    print("== 落库 ==  " + "  ".join(f"{k}={v}" for k, v in stats.most_common()))
    n_day = lib.execute(
        "SELECT count(*) c FROM situation_vectors WHERE subject_type='backtest_day'"
    ).fetchone()["c"]
    n_case = lib.execute(
        "SELECT count(*) c FROM situation_vectors WHERE subject_type='case'"
    ).fetchone()["c"]
    print(f"   库内 situation_vectors：case={n_case}  backtest_day={n_day}  "
          f"合计={n_case + n_day}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
