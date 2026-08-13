#!/usr/bin/env python3
"""处境坐标系 v0 求值器（P0.3）

纯机械求值，无 LLM。对经验库中的历史案例批量计算处境坐标并落 situation_vectors 表。

数据源（全部只读）：
  - data/market.db      行情正本：index_daily(000300.SH)、board_trend_daily、mkt_gvix_daily
  - data/fund.db        账户正本：fund_bot_daily_snapshots
  - scripts/v5_mainline_plan.py  信号维正本（直接 import 复用，禁止重实现以免口径漂移）

口径纪律：
  - 日期：库内 YYYY-MM-DD，market.db 为 YYYYMMDD，转换集中在 _ymd()
  - index_daily.pct_chg 存在 NULL，收益一律由 close 自算
  - 缺数维度显式写 null 并在 coords_json.__missing 里记原因，不用 0 或默认值冒充
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/agent_invest_lab")
MARKET_DB = os.path.join(ROOT, "data", "market.db")
FUND_DB = os.path.join(ROOT, "data", "fund.db")
SCRIPTS = os.path.join(ROOT, "scripts")

EVALUATOR_VERSION = "situation_eval_v0.1"
BENCHMARK = "000300.SH"

sys.path.insert(0, SCRIPTS)
import v5_mainline_plan as v5  # noqa: E402  信号维正本

# 只有 multi_equity 族跑 v5 主线状态机；其余 persona 的信号维标 null
V5_FAMILIES = {"multi_equity_high", "multi_equity_medium", "multi_equity_low"}


def _ymd(d: str) -> str:
    return d.replace("-", "")


def _dashed(d: str) -> str:
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


def ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- 市场维

def market_axes(mc: sqlite3.Connection, date_ymd: str) -> dict:
    """基准指数的趋势 / 波动 / 下跌速度 / 宽度。全部 trade_date<=D，PIT 安全。"""
    rows = mc.execute(
        "SELECT trade_date, close FROM index_daily WHERE ts_code=? AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 500",
        (BENCHMARK, date_ymd),
    ).fetchall()
    out: dict = {}
    missing: dict = {}

    if not rows or rows[0]["trade_date"] != date_ymd:
        missing["market"] = f"index_daily 无 {BENCHMARK} 于 {date_ymd} 的行"
        return {"axes": {}, "missing": missing}

    closes = [float(r["close"]) for r in rows]  # closes[0] = 当日
    close = closes[0]

    # --- 趋势状态
    ma60 = sum(closes[:60]) / 60 if len(closes) >= 60 else None
    ma250 = sum(closes[:250]) / 250 if len(closes) >= 250 else None
    out["dist_ma60_pct"] = round((close / ma60 - 1) * 100, 2) if ma60 else None
    out["dist_ma250_pct"] = round((close / ma250 - 1) * 100, 2) if ma250 else None
    if ma60 is None or ma250 is None:
        out["trend_state"] = None
        missing["trend_state"] = "均线窗口不足"
    elif close >= ma60 and close >= ma250:
        out["trend_state"] = "完好"
    elif close < ma60 and close >= ma250:
        out["trend_state"] = "中期回调"
    elif close < ma250 and out["dist_ma250_pct"] > -5:
        out["trend_state"] = "破位"
    else:
        out["trend_state"] = "深度破位"

    # --- 波动分位：20 日已实现波动率在过去 250 日同measure中的分位（普适，全史可得）
    def rvol(offset: int):
        seg = closes[offset : offset + 21]
        if len(seg) < 21:
            return None
        rets = [math.log(seg[i] / seg[i + 1]) for i in range(20)]
        m = sum(rets) / 20
        var = sum((x - m) ** 2 for x in rets) / 19
        return math.sqrt(var * 252)

    today_rv = rvol(0)
    hist = [v for v in (rvol(i) for i in range(0, 250)) if v is not None]
    if today_rv is not None and len(hist) >= 60:
        pctl = sum(1 for v in hist if v <= today_rv) / len(hist)
        out["rvol20_annual_pct"] = round(today_rv * 100, 2)
        out["vol_pctl"] = round(pctl, 3)
        out["vol_bucket"] = (
            "极端" if pctl >= 0.95 else "高" if pctl >= 0.8 else "中" if pctl >= 0.5 else "低"
        )
    else:
        out["vol_pctl"] = out["vol_bucket"] = None
        missing["vol_pctl"] = "已实现波动率历史窗口不足"

    # --- GVIX 分位（次要轴，2025-08-28 起才有；缺则 null，不回退不冒充）
    g = mc.execute(
        "SELECT percentile_1y FROM mkt_gvix_daily WHERE trade_date=?", (date_ymd,)
    ).fetchone()
    out["gvix_pctl_1y"] = round(float(g["percentile_1y"]), 3) if g and g["percentile_1y"] is not None else None
    if out["gvix_pctl_1y"] is None:
        missing["gvix_pctl_1y"] = "mkt_gvix_daily 覆盖不到该日（2025-08-28 起）"

    # --- 近期走势与急跌尖峰（拆成两轴）
    #     v0.1 教训：原 drop_speed = max(-单日最深, -5日累计/2) 退化成二值——任何 5 天里
    #     总有一天下跌，-worst_day 恒为正。改为：cum5 定"方向与幅度"（含上涨），
    #     worst_day 单独作"尖峰"轴（保留 2026-07-17 单日 -3.6% 被反抽抹平的教训）。
    if len(closes) >= 6:
        daily = [(closes[i] / closes[i + 1] - 1) * 100 for i in range(5)]
        worst_day = min(daily)
        cum5 = (closes[0] / closes[5] - 1) * 100
        out["worst_day_5d_pct"] = round(worst_day, 2)
        out["cum5_pct"] = round(cum5, 2)
        out["move5_bucket"] = (
            "上涨" if cum5 > 1.5 else "横盘" if cum5 >= -1.5 else "回调" if cum5 >= -4
            else "急跌" if cum5 >= -8 else "崩盘"
        )
        out["spike_bucket"] = (
            "无尖峰" if worst_day > -1.5 else "小尖峰" if worst_day > -3
            else "大尖峰" if worst_day > -5 else "极端尖峰"
        )
    else:
        out["move5_bucket"] = out["spike_bucket"] = None
        missing["move5"] = "近 5 日行情不足"

    # --- 宽度：板块 above_ma60 占比（board_trend_daily 自 2024-01-30）
    b = mc.execute(
        "SELECT count(*) n, sum(CASE WHEN above_ma60=1 THEN 1 ELSE 0 END) up "
        "FROM board_trend_daily WHERE trade_date=?",
        (date_ymd,),
    ).fetchone()
    if b and b["n"]:
        frac = b["up"] / b["n"]
        out["breadth_above_ma60"] = round(frac, 3)
        out["breadth_bucket"] = (
            "崩塌" if frac < 0.2 else "弱" if frac < 0.4 else "中" if frac < 0.6 else "强"
        )
    else:
        out["breadth_above_ma60"] = out["breadth_bucket"] = None
        missing["breadth"] = "board_trend_daily 无该日数据"

    # --- 市场压力档（复合轴）
    #     直接交叉 trend×move5×breadth×vol 会格子爆炸（47 格 /121 例，18 个孤格）。
    #     改为先合成一个 0-10 的压力分再分档：格子数受控，且每档语义可读。
    pts = 0
    known = True
    for axis, table in (
        ("trend_state", {"完好": 0, "中期回调": 1, "破位": 2, "深度破位": 3}),
        ("move5_bucket", {"上涨": 0, "横盘": 0, "回调": 1, "急跌": 2, "崩盘": 3}),
        ("breadth_bucket", {"强": 0, "中": 0, "弱": 1, "崩塌": 2}),
        ("vol_bucket", {"低": 0, "中": 0, "高": 1, "极端": 2}),
    ):
        v = out.get(axis)
        if v is None:
            known = False
            break
        pts += table[v]
    if known:
        out["stress_score"] = pts
        out["stress_bucket"] = (
            "平静" if pts <= 1 else "偏紧" if pts <= 3 else "紧张" if pts <= 5 else "极端"
        )
    else:
        out["stress_score"] = out["stress_bucket"] = None
        missing["stress"] = "组成轴缺数，压力档不可求"

    return {"axes": out, "missing": missing}


# ---------------------------------------------------------------- 账户维

def account_axes(fc: sqlite3.Connection, bot_id: str, run_id: str, date: str) -> dict:
    """当前回撤（run 内 running peak）与仓位。

    注意：本舰队【没有硬回撤闸门】——bot101 的 AGENTS.md 明确「回撤红线没有写死的
    数字，由 USER.md 的风险性格自己拿捏」。故不设 dist_to_dd_gate_pp，改用普适的
    current_dd_pp，其严重性解释交给 persona 属性（risk_profile）与 SCOPED_TO 边。
    """
    out: dict = {}
    missing: dict = {}
    rows = fc.execute(
        "SELECT trade_date, net_value, equity_weight, cash_weight, daily_return_pct, cumulative_return_pct "
        "FROM fund_bot_daily_snapshots WHERE bot_id=? AND run_id=? AND trade_date<=? "
        "ORDER BY trade_date",
        (bot_id, run_id, date),
    ).fetchall()
    if not rows or rows[-1]["trade_date"] != date:
        missing["account"] = f"fund_bot_daily_snapshots 无 {bot_id}/{run_id}/{date}"
        return {"axes": out, "missing": missing}

    navs = [float(r["net_value"]) for r in rows if r["net_value"] is not None]
    cur = navs[-1]
    peak = max(navs)
    dd = (cur / peak - 1) * 100
    out["current_dd_pp"] = round(dd, 2)
    a = abs(dd)
    out["dd_bucket"] = "无" if a < 1 else "浅" if a < 3 else "中" if a < 6 else "深"
    out["run_days_elapsed"] = len(rows)

    last = rows[-1]
    ew = last["equity_weight"]
    if ew is None:
        out["equity_weight"] = out["equity_bucket"] = None
        missing["equity_weight"] = "快照缺 equity_weight"
    else:
        ew = float(ew) * 100
        out["equity_weight"] = round(ew, 2)
        out["equity_bucket"] = (
            "空仓" if ew < 5 else "轻仓" if ew < 40 else "中仓" if ew < 70 else "重仓"
        )
    out["daily_return_pct"] = last["daily_return_pct"]
    out["cumulative_return_pct"] = last["cumulative_return_pct"]
    return {"axes": out, "missing": missing}


# ---------------------------------------------------------------- 信号维

_regime_cache: dict = {}


def _regime_name(mc: sqlite3.Connection, date_ymd: str):
    if date_ymd in _regime_cache:
        return _regime_cache[date_ymd]
    try:
        top15 = v5.top_boards(mc, date_ymd, limit=15)
        r = v5.regime(mc, date_ymd, top15)
        val = (r["name"], r["hs300_vs_ma120"], r["concentration"]["dominant_count"])
    except Exception as e:  # 数据不足等
        val = (None, None, None)
    _regime_cache[date_ymd] = val
    return val


def signal_axes(mc: sqlite3.Connection, strategy_family: str, date_ymd: str) -> dict:
    """v5 主线状态机。仅 multi_equity 族适用；其余 persona 用别的信号体系，标 null。

    信号年龄 = 同 regime 名连续维持的交易日数（向前走，上限 250 日）。
    """
    out: dict = {}
    missing: dict = {}
    if strategy_family not in V5_FAMILIES:
        missing["signal"] = f"persona 家族 {strategy_family} 不使用 v5 主线信号，v0 不求值"
        return {"axes": out, "missing": missing}

    name, dist120, dom = _regime_name(mc, date_ymd)
    if name is None:
        missing["signal"] = "v5 regime 求值失败（板块或指数数据不足）"
        return {"axes": out, "missing": missing}

    out["regime_name"] = name
    out["signal_direction"] = {
        "抱主线·v4": "进攻",
        "无主线·宽基": "中性",
        "防御·红利": "防御",
        "数据不足": None,
    }.get(name)
    out["hs300_vs_ma120_pct"] = round(dist120 * 100, 2) if dist120 is not None else None
    out["concentration_dominant_count"] = dom

    days = mc.execute(
        "SELECT DISTINCT trade_date FROM index_daily WHERE ts_code=? AND trade_date<? "
        "ORDER BY trade_date DESC LIMIT 250",
        (BENCHMARK, date_ymd),
    ).fetchall()
    age = 1
    for d in days:
        prev, _, _ = _regime_name(mc, d["trade_date"])
        if prev != name:
            break
        age += 1
    out["signal_age_days"] = age
    out["signal_age_bucket"] = "D0-1" if age <= 1 else "D2-5" if age <= 5 else "D6-20" if age <= 20 else "D20+"
    return {"axes": out, "missing": missing}


# ---------------------------------------------------------------- 决策类型维

def decision_axes(actions_json: str) -> dict:
    try:
        actions = json.loads(actions_json or "[]")
    except json.JSONDecodeError:
        return {"axes": {"decision_type": None}, "missing": {"decision_type": "actual_actions_json 解析失败"}}
    types = {a.get("action_type") for a in actions if isinstance(a, dict)}
    if not types:
        dt = "不动"
    elif types == {"ADD"}:
        dt = "加仓"
    elif types == {"REDUCE"}:
        dt = "减仓"
    else:
        dt = "换仓"
    return {"axes": {"decision_type": dt, "action_count": len(actions)}, "missing": {}}


# ---------------------------------------------------------------- 装配

# cell_id 只用粗轴：粗到跨 bot 能相遇，细化留给后续 regime 警报边驱动。
# 候选方案并列出直方图供拍板点 1 定夺；正式 cell_id 用 CELL_AXES。
CELL_CANDIDATES = {
    "A_趋势×回撤": ["trend_state", "dd_bucket"],
    "B_市场3轴": ["trend_state", "move5_bucket", "breadth_bucket"],
    "C_市场2+账户1": ["trend_state", "move5_bucket", "dd_bucket"],
    "D_四轴交叉": ["trend_state", "vol_bucket", "move5_bucket", "dd_bucket"],
    "E_压力×回撤": ["stress_bucket", "dd_bucket"],
    "F_压力×回撤×仓位": ["stress_bucket", "dd_bucket", "equity_bucket"],
}
CELL_AXES = CELL_CANDIDATES["E_压力×回撤"]


def cell_of(coords: dict, axes) -> str:
    return "|".join(str(coords.get(a) or "NA") for a in axes)


def cell_id_of(coords: dict) -> str:
    return cell_of(coords, CELL_AXES)


REPORT_AXES = [
    "trend_state", "vol_bucket", "move5_bucket", "spike_bucket", "breadth_bucket",
    "stress_bucket", "dd_bucket", "equity_bucket", "signal_direction", "signal_age_bucket",
    "decision_type",
]


def evaluate_case(mc, fc, case: sqlite3.Row, family: str) -> dict:
    date = case["trade_date"]
    ymd = _ymd(date)
    parts = [
        market_axes(mc, ymd),
        account_axes(fc, case["bot_id"], case["run_id"], date),
        signal_axes(mc, family, ymd),
        decision_axes(case["actual_actions_json"]),
    ]
    coords: dict = {}
    missing: dict = {}
    for p in parts:
        coords.update(p["axes"])
        missing.update(p["missing"])
    coords["__missing"] = missing
    coords["__persona_family"] = family
    return coords


DDL = """
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
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-db", required=True)
    ap.add_argument("--market-db", default=MARKET_DB)
    ap.add_argument("--fund-db", default=FUND_DB)
    ap.add_argument("--dry-run", action="store_true", help="只算不写，打印直方图")
    args = ap.parse_args()

    mc = ro(args.market_db)
    fc = ro(args.fund_db)
    lib = sqlite3.connect(args.library_db)
    lib.row_factory = sqlite3.Row

    # 按 (bot_id, run_id) 取 family——bot102 有两份 persona 快照（dash run 的 family 为空，
    # oos run 为 multi_equity_medium），只按 bot_id 键会让后写的那份覆盖前一份。
    families = {}
    fallback = {}
    for r in lib.execute("SELECT bot_id, run_id, strategy_family FROM agent_persona_snapshots"):
        fam = r["strategy_family"] or "unknown"
        families[(r["bot_id"], r["run_id"])] = fam
        if fam != "unknown" or r["bot_id"] not in fallback:
            fallback[r["bot_id"]] = fam

    cases = lib.execute(
        "SELECT case_id, bot_id, run_id, trade_date, actual_actions_json FROM experience_cases ORDER BY trade_date"
    ).fetchall()

    vintage = mc.execute(
        "SELECT max(trade_date) d FROM index_daily WHERE ts_code=?", (BENCHMARK,)
    ).fetchone()["d"]
    now = datetime.now(timezone.utc).isoformat()

    results = []
    for c in cases:
        family = families.get((c["bot_id"], c["run_id"])) or fallback.get(c["bot_id"], "unknown")
        coords = evaluate_case(mc, fc, c, family)
        coords["__bot_id"] = c["bot_id"]
        results.append((c["case_id"], c["trade_date"], coords, cell_id_of(coords)))

    if not args.dry_run:
        lib.executescript(DDL)
        lib.executemany(
            "INSERT OR REPLACE INTO situation_vectors "
            "(situation_id, subject_type, subject_id, trade_date, coords_json, cell_id, "
            " evaluator_version, data_vintage, computed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (f"sv_{EVALUATOR_VERSION}_{cid}", "case", cid, d, json.dumps(co, ensure_ascii=False), cell,
                 EVALUATOR_VERSION, vintage, now)
                for cid, d, co, cell in results
            ],
        )
        lib.commit()

    # ---- 健全性检查报告
    print(f"== 求值器 {EVALUATOR_VERSION} | 行情 vintage {vintage} | 案例 {len(results)} ==\n")

    axis_cov: dict = {}
    for _, _, co, _ in results:
        for a in REPORT_AXES:
            axis_cov.setdefault(a, [0, 0])
            axis_cov[a][1] += 1
            if co.get(a) is not None:
                axis_cov[a][0] += 1
    print("-- 各轴覆盖率 --")
    for a, (ok, tot) in axis_cov.items():
        print(f"  {a:24s} {ok:3d}/{tot}  {ok/tot*100:5.1f}%")

    from collections import Counter
    print("\n-- 候选分格方案对比（供拍板点 1）--")
    print(f"  {'方案':16s} {'格数':>4s} {'最大格':>5s} {'中位格':>5s} {'孤格数':>5s} {'≥5例格覆盖率':>10s}")
    for tag, axes in CELL_CANDIDATES.items():
        c = Counter(cell_of(co, axes) for _, _, co, _ in results)
        sizes = sorted(c.values())
        med = sizes[len(sizes) // 2]
        singles = sum(1 for v in sizes if v == 1)
        cov = sum(v for v in sizes if v >= 5) / len(results)
        print(f"  {tag:16s} {len(c):4d} {max(sizes):5d} {med:5d} {singles:5d} {cov*100:9.1f}%")

    print("\n-- 分格直方图（正式 cell = %s）--" % "×".join(CELL_AXES))
    cnt = Counter(cell for _, _, _, cell in results)
    for cell, n in cnt.most_common():
        print(f"  {n:3d}  {cell}")
    print(f"  合计 {len(cnt)} 格 / {len(results)} 案例；单案例格 {sum(1 for v in cnt.values() if v==1)} 个")

    # 坐标系起作用的直接证据：同日多 bot 应落在市场维相同、账户维不同的格子
    print("\n-- 同日多 bot 检查（市场维应同、账户维应异）--")
    by_date: dict = {}
    for cid, d, co, _ in results:
        by_date.setdefault(d, []).append((co.get("__bot_id"), co))
    multi = {d: v for d, v in by_date.items() if len(v) > 1}
    same_mkt = diff_acct = 0
    for d, items in sorted(multi.items()):
        mkts = {cell_of(co, ["trend_state", "move5_bucket", "breadth_bucket"]) for _, co in items}
        accts = {cell_of(co, ["dd_bucket", "equity_bucket"]) for _, co in items}
        if len(mkts) == 1:
            same_mkt += 1
        if len(accts) > 1:
            diff_acct += 1
    print(f"  多 bot 交易日 {len(multi)} 天；市场维完全一致 {same_mkt} 天；账户维出现分化 {diff_acct} 天")
    for d, items in sorted(multi.items())[:5]:
        print(f"    {d}: " + "; ".join(
            f"{b}[{cell_of(co,['dd_bucket','equity_bucket','decision_type'])}]" for b, co in items))

    print("\n-- 各轴取值分布 --")
    for a in REPORT_AXES:
        c = Counter(co.get(a) for _, _, co, _ in results)
        print(f"  {a:24s} " + "  ".join(f"{k}={v}" for k, v in c.most_common()))

    print("\n-- 缺数原因汇总 --")
    mc2 = Counter()
    for _, _, co, _ in results:
        for k, v in co.get("__missing", {}).items():
            mc2[f"{k}: {v[:60]}"] += 1
    for k, v in mc2.most_common():
        print(f"  {v:3d}  {k}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
