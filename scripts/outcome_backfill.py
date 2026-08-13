#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1.1：给每个处境主体算「后来怎么样」。

**这一步是底率层唯一的分子来源，也是最容易骗人的一步，所以先把三条边界写死。**

一、**衡量的是 bot 的净值，不是这个动作的因果。** 某天加仓、20 天后净值 +3%，
    这句话只说明"这个 bot 后来涨了 3%"，不说明"是这次加仓带来的 3%"。仓位、
    行情、其它持仓、当天没做的事，全都混在里面。本表所有列都以 `fwd_` 开头就是
    在提醒这一点：它是**事后走势**，不是**归因**。任何前端把它写成"这么干的收益"
    都是在改口径。

二、**窗口不够就必须留白，不许拿最后一天顶数。** run 末尾那些天，前面没有第 H 个
    交易日，`status='truncated'`、收益列写 NULL。写 0 或者用现有的最后一天替代，
    等于把"还没发生"记成"发生了且没动"——和把缺失伪装成 0 是同一个错误形状。

三、**`outcome_available_at` 是 PIT 闸门的唯一依据，等于窗口末日。** 一条 D 日的
    处境，它的 20 日后果要到 D+20 才知道。任何"截至 X 日能看到什么"的查询，
    必须写 `outcome_available_at <= X`。这一列不是注记，是过滤条件；把它当注记
    的后果就是拿 8 月的结果去解释 5 月的决策。

窗口按 **bot 自己的快照序列**数，不按交易所日历——净值只在有快照的日子有定义。
实际落到哪一天写进 `end_date`，可逐条复核。
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

OUTCOME_VERSION = "outcome_v0.1"
HORIZONS = (5, 10, 20)

DDL = """
CREATE TABLE IF NOT EXISTS situation_outcomes (
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  bot_id TEXT, run_id TEXT,
  trade_date TEXT NOT NULL,
  horizon_days INTEGER NOT NULL,
  end_date TEXT,
  nav_start REAL, nav_end REAL,
  fwd_return_pct REAL,
  fwd_min_nav_return_pct REAL,      -- 窗口内最差的一天相对起点（路径痛感）
  bench_return_pct REAL,
  fwd_excess_pct REAL,
  outcome_available_at TEXT,        -- = end_date；PIT 闸门就靠这一列
  status TEXT NOT NULL,             -- ok | truncated | no_nav
  note TEXT,
  outcome_version TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  PRIMARY KEY (subject_id, horizon_days, outcome_version)
);
CREATE INDEX IF NOT EXISTS idx_outcome_avail ON situation_outcomes(outcome_available_at);
CREATE INDEX IF NOT EXISTS idx_outcome_subj ON situation_outcomes(subject_kind, subject_id);
CREATE INDEX IF NOT EXISTS idx_outcome_h ON situation_outcomes(horizon_days, status);
"""


def bench_series(mc) -> dict:
    return {r["trade_date"]: r["close"] for r in mc.execute(
        "SELECT trade_date, close FROM index_daily WHERE ts_code=? ORDER BY trade_date",
        (ev.BENCHMARK,))}


def bench_between(bs: dict, bkeys: list, d0: str, d1: str):
    """区间基准收益。两端都**要求指数在那一天确实有 K 线**，差一天就不算。

    为什么不许"取 <= 该日的最后一个交易日"这种就近回退：账户快照当天就写，
    行情要等收盘落库。2026-08-11 四个 live run 都有净值，而 000300.SH 只到
    20260810。就近回退会把「净值算到 8/11、指数算到 8/10」两段不同长度的区间
    相减，得到一个看起来完全正常、实际上多算了一天 bot 收益的超额。
    这种错不会报警、不会留痕，只会让超额整体偏高——**宁可留空**。

    返回 (区间收益, 起点, 终点, 留空原因)。
    """
    y0, y1 = ev._ymd(d0), ev._ymd(d1)
    if y0 not in bs:
        return None, None, None, f"基准在起点 {d0} 无 K 线"
    if y1 not in bs:
        return None, None, None, (
            f"基准在终点 {d1} 无 K 线（行情最新 {bkeys[-1]}）；"
            f"不做就近回退，避免 bot 与基准区间长度不等")
    c0, c1 = bs[y0], bs[y1]
    if not c0:
        return None, None, None, f"基准起点 {d0} 收盘价为空"
    return (c1 / c0 - 1) * 100.0, y0, y1, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-db", required=True)
    ap.add_argument("--market-db", default=ev.MARKET_DB)
    ap.add_argument("--fund-db", default=ev.FUND_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if os.path.basename(args.library_db) == "experience-library-v7.db":
        print("!! 拒绝写生产库；请指向 P1 副本", file=sys.stderr)
        return 2

    mc, fc = ev.ro(args.market_db), ev.ro(args.fund_db)
    lib = sqlite3.connect(args.library_db)
    lib.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    bs = bench_series(mc)
    bkeys = sorted(bs)
    print(f"== {OUTCOME_VERSION} | 基准 {ev.BENCHMARK} {bkeys[0]}~{bkeys[-1]}（{len(bkeys)} 日）==\n")

    subs = lib.execute(
        "SELECT subject_type, subject_id, trade_date, coords_json FROM situation_vectors"
    ).fetchall()
    # 主体 -> (bot, run)。案例的 bot/run 从 experience_cases 取，日级从坐标注记取。
    cases = {r["case_id"]: (r["bot_id"], r["run_id"]) for r in
             lib.execute("SELECT case_id, bot_id, run_id FROM experience_cases")}

    navs: dict = {}          # (bot,run) -> [(date, nav), ...] 升序

    def nav_seq(bot, run):
        k = (bot, run)
        if k not in navs:
            navs[k] = [(r["trade_date"], r["net_value"]) for r in fc.execute(
                "SELECT trade_date, net_value FROM fund_bot_daily_snapshots "
                "WHERE bot_id=? AND run_id=? ORDER BY trade_date", (bot, run))]
        return navs[k]

    rows = []
    stats: Counter = Counter()
    for s in subs:
        co = json.loads(s["coords_json"])
        if s["subject_type"] == "case":
            bot, run = cases.get(s["subject_id"], (None, None))
        else:
            bot, run = co.get("__bot_id"), co.get("__run_id")
        seq = nav_seq(bot, run) if bot else []
        idx = {d: i for i, (d, _) in enumerate(seq)}
        i = idx.get(s["trade_date"])
        for h in HORIZONS:
            base = dict(subject_kind=s["subject_type"], subject_id=s["subject_id"],
                        bot_id=bot, run_id=run, trade_date=s["trade_date"], horizon_days=h)
            if i is None or not seq:
                rows.append({**base, "status": "no_nav", "end_date": None,
                             "note": f"{bot}/{run} 在 {s['trade_date']} 没有净值行"})
                stats[("no_nav", h)] += 1
                continue
            if i + h >= len(seq):
                rows.append({**base, "status": "truncated", "end_date": None,
                             "note": f"该 run 只到 {seq[-1][0]}，第 {h} 个交易日不存在；"
                                     f"收益列留空，不得用最后一天替代"})
                stats[("truncated", h)] += 1
                continue
            d0, n0 = seq[i]
            d1, n1 = seq[i + h]
            if not n0:
                rows.append({**base, "status": "no_nav", "end_date": d1,
                             "note": "起点净值为空或 0"})
                stats[("no_nav", h)] += 1
                continue
            path = [n for _, n in seq[i + 1: i + h + 1] if n]
            fwd = (n1 / n0 - 1) * 100.0
            worst = (min(path) / n0 - 1) * 100.0 if path else None
            br, b0, b1, why = bench_between(bs, bkeys, d0, d1)
            if br is None:
                stats[("超额留空", h)] += 1
            rows.append({**base, "status": "ok", "end_date": d1,
                         "nav_start": n0, "nav_end": n1,
                         "fwd_return_pct": fwd, "fwd_min_nav_return_pct": worst,
                         "bench_return_pct": br,
                         "fwd_excess_pct": (fwd - br) if br is not None else None,
                         "outcome_available_at": d1,
                         "note": (f"基准区间 {b0}~{b1}" if br is not None else why)})
            stats[("ok", h)] += 1

    print(f"{'窗口':>6s} {'可用':>7s} {'窗口不足':>9s} {'无净值':>7s} {'小计':>7s}")
    for h in HORIZONS:
        a, b, c = stats[("ok", h)], stats[("truncated", h)], stats[("no_nav", h)]
        print(f"{h:5d}日 {a:7d} {b:9d} {c:7d} {a+b+c:7d}")
    # 只对三种 status 求和。曾经写成 sum(stats.values()) —— 把"超额留空"这个
    # 旁路计数也加了进去，合计打成 5,234 而实际行数 5,220。恒等式当场把它抓出来了；
    # 这行注释留着，是因为下次往 stats 里加新键的人会重蹈同一个坑。
    tot = sum(stats[(k, h)] for k in ("ok", "truncated", "no_nav") for h in HORIZONS)
    print(f"{'合计':>6s} {sum(stats[('ok',h)] for h in HORIZONS):7d} "
          f"{sum(stats[('truncated',h)] for h in HORIZONS):9d} "
          f"{sum(stats[('no_nav',h)] for h in HORIZONS):7d} {tot:7d}")
    print(f"  恒等式：主体 {len(subs)} × 窗口 {len(HORIZONS)} = {len(subs)*len(HORIZONS)}"
          f"   （与合计一致：{len(subs)*len(HORIZONS) == tot}；与行数一致：{len(rows) == tot}）\n")

    n_gap = sum(stats[("超额留空", h)] for h in HORIZONS)
    if n_gap:
        print(f"-- 有净值收益、但超额留空 {n_gap} 行 --")
        print("   原因：窗口终点当天基准指数还没落库（账户快照当天写、行情等收盘）。")
        print("   不做就近回退：那会把两段长度不等的区间相减，超额整体偏高且不留痕。")
        seen = set()
        for r in rows:
            if r["status"] == "ok" and r.get("bench_return_pct") is None:
                k = (r["end_date"], r["horizon_days"])
                if k in seen:
                    continue
                seen.add(k)
                print(f"     终点 {r['end_date']}  窗口 {r['horizon_days']}日  {r['note']}")
        print()

    ok = [r for r in rows if r["status"] == "ok"]
    if ok:
        avail = sorted(r["outcome_available_at"] for r in ok)
        print(f"-- PIT 可见性：结果最早在 {avail[0]} 可见，最晚 {avail[-1]} --")
        print("   底率查询必须写 outcome_available_at <= 你问的那一天，否则就是未来信息\n")

    if args.dry_run:
        print("(dry-run，未写库)")
        return 0

    lib.executescript(DDL)
    lib.execute("DELETE FROM situation_outcomes WHERE outcome_version=?", (OUTCOME_VERSION,))
    cols = ("subject_kind subject_id bot_id run_id trade_date horizon_days end_date nav_start "
            "nav_end fwd_return_pct fwd_min_nav_return_pct bench_return_pct fwd_excess_pct "
            "outcome_available_at status note").split()
    lib.executemany(
        f"INSERT OR REPLACE INTO situation_outcomes ({','.join(cols)},outcome_version,computed_at) "
        f"VALUES ({','.join('?'*len(cols))},?,?)",
        [tuple(r.get(c) for c in cols) + (OUTCOME_VERSION, now) for r in rows])
    lib.commit()
    n = lib.execute("SELECT count(*) c FROM situation_outcomes WHERE outcome_version=?",
                    (OUTCOME_VERSION,)).fetchone()["c"]
    print(f"== 落库 {n} 行（与计算行数 {len(rows)} 一致：{n == len(rows)}）==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
