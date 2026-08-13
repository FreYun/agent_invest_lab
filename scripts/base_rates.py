#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1.2：底率层——"在这种处境下这么干，后来怎么样"。

这一层最容易做成一个**看起来很科学的假象**，所以四条纪律写在最前面，每一条都在
代码里有对应的强制点，不是文档承诺。

**一、PIT 闸门写在 SQL 里，不写在注释里。**
    每条结果都带 `outcome_available_at`（= 窗口末日）。所有查询强制拼上
    `o.outcome_available_at <= :asof`，`asof` 是必填参数，没有默认值。
    `--prove-pit` 用两个不同的 as-of 跑同一条查询，把样本数随 as-of 单调变化打出来
    ——闸门有没有在工作，是能看见的，不是我说了算。

**二、只出分布，不出单点。**
    返回 p10/p25/中位/p75/p90 与胜率，**不返回均值**。均值会被一两个极端值拖着走，
    而在 n=7 的格子里，一个极端值就是全部。前端拿到的是一把分位数，画出来是条带，
    读者一眼能看出它有多宽——单点估计会把这份不确定性藏起来。

**三、分母要说实话：三个数一起给。**
    `n_行` —— 命中的交易日数；
    `n_独立窗口` —— 同一 (bot,run) 内互不重叠的窗口数（贪心取），因为 20 日窗口在
                    相邻两天之间重合 19/20，1,246 个日子远不是 1,246 个独立观测；
    `n_bot` —— 涉及几个 bot。全部来自同一个 bot 的"底率"只是那个 bot 的历史。
    晋升判据只认 `n_独立窗口`（顾云峰 C6 的裁决）；展示三个都给。

**四、案例线与日级线永不合并。**
    案例不是交易日的随机抽样——它们是被挑出来写成案例的日子，本身就有选择偏差。
    两条线分开出数，绝不相加。要比较就并排放，让人看见差多少。

样本不足时不给数字，给一句「你在地图外」。给一个 n=2 的中位数比不给更糟。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

BASE_RATE_VERSION = "base_rate_v0.1"
MIN_N = 5          # 独立窗口数低于此值，一律不出数字
HORIZONS = (5, 10, 20)

# 所有底率查询共用的 FROM/WHERE 骨架。PIT 那一行**不允许**被调用方省略：
# 它不是可选过滤器，它是这层的存在前提。
BASE_SQL = """
FROM situation_outcomes o
JOIN situation_vectors s
  ON s.subject_id = o.subject_id AND s.subject_type = o.subject_kind
WHERE o.outcome_available_at IS NOT NULL
  AND o.outcome_available_at <= :asof      -- ← PIT 闸门，去掉这行就是未来信息
  AND o.status = 'ok'
  AND o.horizon_days = :h
  AND s.subject_type = :kind
"""

DDL_VIEW = """
DROP VIEW IF EXISTS v_situation_outcome;
CREATE VIEW v_situation_outcome AS
SELECT s.subject_type, s.subject_id, s.cell_id, s.trade_date,
       json_extract(s.coords_json,'$.__bot_id')      AS bot_id,
       json_extract(s.coords_json,'$.__run_id')      AS run_id,
       json_extract(s.coords_json,'$.decision_type') AS decision_type,
       json_extract(s.coords_json,'$.stress_bucket') AS stress_bucket,
       json_extract(s.coords_json,'$.dd20_bucket')   AS dd20_bucket,
       o.horizon_days, o.end_date, o.fwd_return_pct, o.fwd_excess_pct,
       o.fwd_min_nav_return_pct, o.bench_return_pct,
       o.outcome_available_at, o.status
FROM situation_vectors s
JOIN situation_outcomes o
  ON o.subject_id = s.subject_id AND o.subject_kind = s.subject_type;

DROP VIEW IF EXISTS v_cohort;
-- 同日多 bot：同一个交易日上，有两个以上 bot 各自在做决定。
-- 这是"横向比较"唯一干净的素材——同一天、同一个市场，不同的人设怎么选。
CREATE VIEW v_cohort AS
SELECT trade_date, count(DISTINCT bot_id) n_bot,
       group_concat(DISTINCT bot_id) bots,
       count(*) n_subject,
       count(DISTINCT cell_id) n_cell,
       group_concat(DISTINCT cell_id) cells
FROM (SELECT s.trade_date, s.cell_id,
             json_extract(s.coords_json,'$.__bot_id') bot_id
      FROM situation_vectors s WHERE s.subject_type='backtest_day')
GROUP BY trade_date HAVING count(DISTINCT bot_id) >= 2;
"""


def quantiles(xs, qs=(0.1, 0.25, 0.5, 0.75, 0.9)):
    """线性插值分位数。不用 statistics.quantiles，因为它在 n<2 时抛异常，
    而这一层恰恰经常遇到 n 很小的格子——那时该返回 None，不该崩。"""
    if not xs:
        return {q: None for q in qs}
    v = sorted(xs)
    out = {}
    for q in qs:
        if len(v) == 1:
            out[q] = v[0]
            continue
        pos = q * (len(v) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(v) - 1)
        out[q] = v[lo] + (v[hi] - v[lo]) * (pos - lo)
    return out


def independent_windows(rows, h: int) -> int:
    """同一 (bot,run) 内贪心取互不重叠的窗口数。

    为什么必须算这个：20 日窗口在相邻交易日之间重合 19/20 天。把 1,246 个
    连续交易日当成 1,246 个独立观测，等于把同一段行情数了 20 遍——置信区间会窄到
    完全失真。贪心（按日期排序，取一条就跳过后面 h 天）给出的是一个**保守下界**，
    这正是分母该有的方向。
    """
    by_run: dict = {}
    for r in rows:
        by_run.setdefault((r["bot_id"], r["run_id"]), []).append(r["trade_date"])
    n = 0
    for _k, ds in by_run.items():
        ds.sort()
        last_i = -(10 ** 9)
        # 用序号近似"h 个交易日"：这里的 ds 是命中日，不是全部交易日，
        # 所以按日期字符串比较更稳——取到一条后，跳过 end_date 之前的所有命中日。
        chosen_end = ""
        for d in ds:
            if d > chosen_end:
                n += 1
                # 窗口末日无法只从 d 推出（要查该 run 的交易日序列），
                # 这里用一个保守的日历近似：h 个交易日 ≈ h*7/5 个自然日。
                chosen_end = _shift_days(d, int(round(h * 7 / 5)))
        _ = last_i
    return n


def _shift_days(d: str, n: int) -> str:
    from datetime import date, timedelta
    y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
    return (date(y, m, dd) + timedelta(days=n)).isoformat()


def cell_rates(lib, asof: str, h: int, kind: str, extra_sql: str = "", params=None):
    q = "SELECT s.cell_id, s.subject_id, o.fwd_return_pct r, o.fwd_excess_pct e, " \
        "json_extract(s.coords_json,'$.__bot_id') bot_id, " \
        "json_extract(s.coords_json,'$.__run_id') run_id, s.trade_date " \
        + BASE_SQL + extra_sql
    p = {"asof": asof, "h": h, "kind": kind}
    p.update(params or {})
    rows = lib.execute(q, p).fetchall()
    by_cell: dict = {}
    for r in rows:
        by_cell.setdefault(r["cell_id"], []).append(r)
    out = []
    for cell, rs in by_cell.items():
        rets = [r["r"] for r in rs if r["r"] is not None]
        exs = [r["e"] for r in rs if r["e"] is not None]
        n_ind = independent_windows(rs, h)
        nbot = len({r["bot_id"] for r in rs})
        qr = quantiles(rets)
        out.append(dict(cell_id=cell, n_rows=len(rs), n_ind=n_ind, n_bot=nbot,
                        win=sum(1 for x in rets if x > 0),
                        n_ret=len(rets), n_ex=len(exs),
                        q=qr, ex_med=quantiles(exs)[0.5] if exs else None,
                        enough=n_ind >= MIN_N))
    out.sort(key=lambda d: -d["n_rows"])
    return out


def fmt(x, w=7, p=2):
    return f"{'—':>{w}}" if x is None else f"{x:{w}.{p}f}"


def show(title, rows, h):
    print(f"-- {title}（窗口 {h} 交易日）--")
    print(f"   {'处境格':16s} {'n行':>5s} {'n独立':>6s} {'nbot':>5s} {'胜率':>7s} "
          f"{'p10':>7s} {'p25':>7s} {'中位':>7s} {'p75':>7s} {'p90':>7s} {'超额中位':>8s}")
    for d in rows:
        if not d["enough"]:
            print(f"   {d['cell_id']:16s} {d['n_rows']:5d} {d['n_ind']:6d} {d['n_bot']:5d} "
                  f"   —— 独立窗口 {d['n_ind']} < {MIN_N}，你在地图外，本格不出数字")
            continue
        wr = d["win"] / d["n_ret"] * 100 if d["n_ret"] else None
        q = d["q"]
        print(f"   {d['cell_id']:16s} {d['n_rows']:5d} {d['n_ind']:6d} {d['n_bot']:5d} "
              f"{fmt(wr)}% {fmt(q[0.1])} {fmt(q[0.25])} {fmt(q[0.5])} {fmt(q[0.75])} "
              f"{fmt(q[0.9])} {fmt(d['ex_med'],8)}")
    if rows:
        tr = sum(d["n_rows"] for d in rows)
        ti = sum(d["n_ind"] for d in rows)
        ng = sum(1 for d in rows if not d["enough"])
        print(f"   {'合计':16s} {tr:5d} {ti:6d}        "
              f"（{len(rows)} 格，其中 {ng} 格样本不足不出数字，{len(rows)-ng} 格出数字）")
    print()


def prove_pit(lib, h: int, kind: str):
    """证明闸门在工作：同一条查询换 as-of，样本数必须单调不减。

    这是 FAIL 1 要的东西。"我在 SQL 里写了 PIT"是一句断言；下面这张表是证据。
    如果哪一段没变，说明那段时间没有新结果可见——也照实打出来，不修饰。
    """
    print(f"== PIT 闸门实证（{kind}，窗口 {h} 日）==")
    print("   同一条查询，只改 as-of。样本数必须单调不减；若去掉闸门则恒等于全量。")
    span = lib.execute(
        "SELECT min(outcome_available_at) a, max(outcome_available_at) b "
        "FROM situation_outcomes WHERE status='ok' AND horizon_days=?", (h,)).fetchone()
    full = lib.execute(
        "SELECT count(*) c FROM situation_outcomes o JOIN situation_vectors s "
        "ON s.subject_id=o.subject_id AND s.subject_type=o.subject_kind "
        "WHERE o.status='ok' AND o.horizon_days=? AND s.subject_type=?", (h, kind)).fetchone()["c"]
    print(f"   结果可见区间 {span['a']} ~ {span['b']}；无闸门时样本 {full}")
    print(f"   {'as-of':12s} {'闸门内样本':>10s} {'占全量':>8s}  {'单调':>4s}")
    prev = -1
    ok = True
    for asof in ("2025-03-31", "2025-06-30", "2025-12-31", "2026-03-31",
                 "2026-06-30", "2026-08-11", "2099-01-01"):
        n = lib.execute(
            "SELECT count(*) c " + BASE_SQL, {"asof": asof, "h": h, "kind": kind}
        ).fetchone()["c"]
        mono = n >= prev
        ok = ok and mono
        print(f"   {asof:12s} {n:10d} {n/max(1,full)*100:7.1f}%  {'✓' if mono else '✗'}")
        prev = n
    print(f"   单调性：{'✓ 通过' if ok else '✗ 失败'}   "
          f"as-of=2099 时应等于全量 {full}：{prev == full}")
    if prev != full:
        print("   !! as-of 放到未来仍取不到全量，说明闸门以外还有别的东西在滤，需查")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-db", required=True)
    ap.add_argument("--asof", default=None, help="必填（或用 --asof-today）；没有默认值是故意的")
    ap.add_argument("--asof-today", action="store_true")
    ap.add_argument("--horizon", type=int, default=20, choices=HORIZONS)
    ap.add_argument("--build-views", action="store_true")
    ap.add_argument("--prove-pit", action="store_true")
    ap.add_argument("--by-action", action="store_true", help="再按当日动作切一刀")
    args = ap.parse_args()

    if os.path.basename(args.library_db) == "experience-library-v7.db":
        print("!! 拒绝写生产库；请指向 P1 副本", file=sys.stderr)
        return 2
    lib = sqlite3.connect(args.library_db)
    lib.row_factory = sqlite3.Row

    if args.build_views:
        lib.executescript(DDL_VIEW)
        lib.commit()
        n = lib.execute("SELECT count(*) c FROM v_cohort").fetchone()["c"]
        nb = lib.execute("SELECT sum(n_bot) s FROM v_cohort").fetchone()["s"]
        print(f"== 视图已建：v_situation_outcome、v_cohort ==")
        print(f"   v_cohort：{n} 个交易日上同时有 ≥2 个 bot 在做决定，"
              f"共 {nb} 个 (日,bot) 组合")
        for r in lib.execute("SELECT * FROM v_cohort ORDER BY n_bot DESC, trade_date LIMIT 8"):
            print(f"     {r['trade_date']}  {r['n_bot']} bot：{r['bots']}")
            print(f"        落在 {r['n_cell']} 个不同处境格：{r['cells']}")
        print()

    asof = datetime.now(timezone.utc).date().isoformat() if args.asof_today else args.asof
    if not asof:
        if args.build_views:
            return 0
        print("!! 必须给 --asof。这一层没有默认 as-of 是故意的：", file=sys.stderr)
        print("   一个能省略的 as-of，迟早会被省略，然后就是未来信息。", file=sys.stderr)
        return 2

    print(f"== {BASE_RATE_VERSION} | as-of {asof} | 窗口 {args.horizon} 交易日 ==")
    print("   口径：`后来怎么样` = 该 bot 自己净值在窗口内的走势，**不是这次动作的归因**。")
    print("   仓位、行情、其它持仓、当天没做的事全混在里面。只出分布，不出均值。\n")

    if args.prove_pit:
        prove_pit(lib, args.horizon, "backtest_day")

    for kind, label in (("backtest_day", "日级（1,619 个交易日）"),
                        ("case", "案例线（121 例）")):
        show(f"{label}：按处境格的后续净值分布", cell_rates(lib, asof, args.horizon, kind),
             args.horizon)
    print("   ⚠ 上面两张表**不可相加**：案例是被挑出来写成案例的日子，不是交易日的随机抽样。")
    print("     并排放是为了让人看见两者差多少，不是为了合并成一个更大的样本。\n")

    if args.by_action:
        for dt in ("加仓", "减仓", "不动"):
            rows = cell_rates(lib, asof, args.horizon, "backtest_day",
                              " AND json_extract(s.coords_json,'$.decision_type') = :dt",
                              {"dt": dt})
            show(f"日级 · 当日动作＝{dt}", rows, args.horizon)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
