#!/usr/bin/env python3
"""Deterministic DAILY mainline plan (skill 日度纪律阈值的确定性引擎).

与 scripts/v5_mainline_plan.py（月度状态机，market_mainline 真源）并行、互不影响：
本引擎把 bots/*/skills/mainline-rotation/SKILL.md 里敲定的「日度纪律」做成确定性
状态机，逐交易日推进，产出 report_type=market_mainline_daily（仅看板观察，bot 不读）。

日度纪律（与 skill 文本一致）：
  - 晋核心：连续 ≥40 交易日在动量 top5 且当日站 MA60
  - 核心/卫星剔除（硬）：连续 ≥3 日收盘破 MA60
  - 核心剔除（软）：连续 ≥20 日出 top15（受最小持有期约束）
  - 卫星纳入：连续 ≥5 日在 top3；卫星剔除：连续 ≥5 日跌出 top5（受最小持有期约束）
  - 最小持有期：纳入后 15 交易日内不因排名波动剔除（破 MA60 硬信号除外）
  - 冷却期：剔除后 15 交易日内不再纳回
  - regime：与 v5 同三态（防御·红利/抱主线·v4/无主线·宽基），但加
    年线 ±3% 迟滞带（跌破 -3% 才转防御、回到 -1% 以上才解除）+ 连续 ≥5 日确认才切换

数据源与 v5 完全相同：board_trend_daily / index_daily(HS300) / coarse_themes.json，
基金映射复用 v5 的 fund_matches（board_fund_match 双测度），按 (板块, 自然月) 缓存。

用法：
  --date YYYY-MM-DD                 单日 JSON（内部从数据起点重放）
  --from YYYY-MM-DD --to YYYY-MM-DD 区间 JSONL（每交易日一行，供回补驱动）
  --include-funds                   抱主线日输出 fund_matches
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import Counter, OrderedDict
from typing import Any

# 复用 v5 的连接/分组/格式化/基金匹配（import 不改动 v5 本身）
from v5_mainline_plan import conn, dashed, fund_matches, load_groups, top_boards, ymd

# ── skill 日度纪律阈值 ───────────────────────────────────────────────────────
K_ENTRY = 5
SAT_N = 3
TOPK_EXIT = 15
MAXHOLD = 5
CORE_PROMOTE_DAYS = 40   # 连续 in_top5 且站 MA60 → 晋核心
SAT_ENTRY_DAYS = 5       # 连续 in_top3 → 纳卫星
SAT_EXIT_DAYS = 5        # 连续出 top5 → 剔卫星
BREAK_MA60_EXIT = 3      # 连续破 MA60 → 剔除（硬，压过最小持有期）
OUT_TOP15_EXIT = 20      # 连续出 top15 → 剔核心
MIN_HOLD = 15            # 最小持有期（交易日）
COOLDOWN = 15            # 剔除后冷却期（交易日）
REGIME_CONFIRM = 5       # regime 切换确认窗口
DEFENSE_ENTER = -0.03    # 年线迟滞带：跌破 -3% 转防御
DEFENSE_RELEASE = -0.01  # 回到 -1% 以上才解除防御
DOMINANT_MIN = 4         # top15 最大一类 ≥4 = 主线集中

# 非抱主线态底线产品（与 v5 一致，场外 C 份额）
DEFENSE_PORTFOLIO = [{"code": "012762", "name": "上证红利ETF联接C", "role": "防御"}]
BROAD_PORTFOLIO = [{"code": "007339", "name": "沪深300ETF联接C", "role": "宽基"}]


def trading_dates(c: sqlite3.Connection) -> list[str]:
    rows = c.execute("SELECT DISTINCT trade_date FROM board_trend_daily ORDER BY trade_date").fetchall()
    return [r["trade_date"] for r in rows]


def load_hs300(c: sqlite3.Connection) -> tuple[list[str], list[float]]:
    rows = c.execute(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date"
    ).fetchall()
    return [r["trade_date"] for r in rows], [float(r["close"]) for r in rows]


def hs300_dist(idx_dates: list[str], idx_closes: list[float], trade_date: str) -> float | None:
    """HS300 收盘 / MA120 - 1（截至 trade_date，与 v5 同口径）。"""
    hi = bisect.bisect_right(idx_dates, trade_date)
    if hi < 120:
        return None
    window = idx_closes[hi - 120:hi]
    return window[-1] / (sum(window) / len(window)) - 1


def snapshot(c: sqlite3.Connection, trade_date: str) -> dict[str, sqlite3.Row]:
    rows = c.execute(
        "SELECT board_code, board_name, rank, above_ma60, ret60, ret20, ret5, trend_score "
        "FROM board_trend_daily WHERE trade_date=?",
        (trade_date,),
    ).fetchall()
    return {r["board_code"]: r for r in rows}


class Streaks:
    """单板块连续天数计数器（缺行语义与 v5 一致：无行 = rank 999 + 破 MA60）。"""

    __slots__ = ("in_top5", "in_top3", "out_top5", "out_top15", "below_ma60")

    def __init__(self) -> None:
        self.in_top5 = self.in_top3 = self.out_top5 = self.out_top15 = self.below_ma60 = 0

    def update(self, rank: int, above: bool) -> None:
        self.in_top5 = self.in_top5 + 1 if rank <= K_ENTRY else 0
        self.in_top3 = self.in_top3 + 1 if rank <= SAT_N else 0
        self.out_top5 = self.out_top5 + 1 if rank > K_ENTRY else 0
        self.out_top15 = self.out_top15 + 1 if rank > TOPK_EXIT else 0
        self.below_ma60 = self.below_ma60 + 1 if not above else 0


def replay(asof_raw: str, emit_from_raw: str | None, include_funds: bool) -> list[dict[str, Any]]:
    """从数据起点逐日重放状态机，返回 [emit_from, asof] 内每个交易日的计划。

    emit_from_raw=None → 只返回最后一天（--date 模式）。
    """
    c = conn()
    groups = load_groups()
    dates = [d for d in trading_dates(c) if d <= asof_raw]
    if not dates:
        raise SystemExit(f"no board_trend_daily data on or before {asof_raw}")
    idx_dates, idx_closes = load_hs300(c)

    streaks: dict[str, Streaks] = {}
    holdings: OrderedDict[str, dict[str, Any]] = OrderedDict()  # code → {role, since_idx, name, promoted}
    cooldown: dict[str, int] = {}                               # code → 剔除日 idx
    regime_name: str | None = None
    regime_days = 0
    pending: dict[str, Any] | None = None                        # {name, days}
    fund_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}  # (board, YYYYMM) → matches rows

    out: list[dict[str, Any]] = []
    emit_all = emit_from_raw is not None

    for idx, d in enumerate(dates):
        snap = snapshot(c, d)
        # 计数器：今日快照出现的 + 已在跟踪的（缺行按 rank999/破MA60 归零推进）
        for code in set(snap) | set(streaks) | set(holdings):
            row = snap.get(code)
            st = streaks.setdefault(code, Streaks())
            st.update(int(row["rank"]) if row and row["rank"] else 999,
                      bool(row and row["above_ma60"] == 1))

        actions: list[dict[str, str]] = []

        # ① 剔除（先出后进，与 v5 状态机同序）
        for code in list(holdings):
            h = holdings[code]
            st = streaks[code]
            held_days = idx - h["since_idx"] + 1
            reason = ""
            if st.below_ma60 >= BREAK_MA60_EXIT:
                reason = f"连续{st.below_ma60}日破MA60（硬信号）"
            elif held_days > MIN_HOLD:
                if h["role"] == "核心" and st.out_top15 >= OUT_TOP15_EXIT:
                    reason = f"连续{st.out_top15}日出top{TOPK_EXIT}"
                elif h["role"] == "卫星" and st.out_top5 >= SAT_EXIT_DAYS:
                    reason = f"连续{st.out_top5}日跌出top{K_ENTRY}"
            if reason:
                actions.append({"type": "剔除", "code": code, "name": h["name"], "role": h["role"], "reason": reason})
                del holdings[code]
                cooldown[code] = idx

        # ② 晋核心：连续 ≥40 日 top5 且当日站 MA60（卫星升级 / 新进核心）
        for code, st in streaks.items():
            if st.in_top5 < CORE_PROMOTE_DAYS:
                continue
            row = snap.get(code)
            if not row or row["above_ma60"] != 1:
                continue
            if code in holdings:
                if holdings[code]["role"] == "卫星":
                    holdings[code]["role"] = "核心"
                    actions.append({"type": "升级", "code": code, "name": holdings[code]["name"],
                                    "role": "核心", "reason": f"连续{st.in_top5}日top{K_ENTRY}且站MA60，卫星→核心"})
                continue
            if code in cooldown and idx - cooldown[code] <= COOLDOWN:
                continue
            if len(holdings) >= MAXHOLD:
                continue
            holdings[code] = {"role": "核心", "since_idx": idx, "name": row["board_name"]}
            actions.append({"type": "新进", "code": code, "name": row["board_name"],
                            "role": "核心", "reason": f"连续{st.in_top5}日top{K_ENTRY}且站MA60"})

        # ③ 纳卫星：连续 ≥5 日 top3（按当日 rank 升序）
        top3 = sorted((r for r in snap.values() if r["rank"] and r["rank"] <= SAT_N), key=lambda r: r["rank"])
        for row in top3:
            code = row["board_code"]
            if code in holdings or streaks[code].in_top3 < SAT_ENTRY_DAYS:
                continue
            if code in cooldown and idx - cooldown[code] <= COOLDOWN:
                continue
            if len(holdings) >= MAXHOLD:
                continue
            holdings[code] = {"role": "卫星", "since_idx": idx, "name": row["board_name"]}
            actions.append({"type": "新进", "code": code, "name": row["board_name"],
                            "role": "卫星", "reason": f"连续{streaks[code].in_top3}日top{SAT_N}"})

        # ④ regime：迟滞带 + 5 日确认
        dist = hs300_dist(idx_dates, idx_closes, d)
        counts = Counter(groups.get(r["board_code"], "其他") for r in snap.values() if r["rank"] and r["rank"] <= TOPK_EXIT)
        dom, dom_n = counts.most_common(1)[0] if counts else ("无", 0)
        if dist is None:
            raw = "数据不足"
        else:
            in_defense = regime_name == "防御·红利"
            defense = (dist <= DEFENSE_RELEASE) if in_defense else (dist < DEFENSE_ENTER)
            raw = "防御·红利" if defense else ("抱主线·v4" if dom_n >= DOMINANT_MIN else "无主线·宽基")
        if regime_name is None:
            regime_name, regime_days, pending = raw, 1, None
        elif raw == regime_name:
            regime_days += 1
            pending = None
        else:
            pending = {"name": raw, "days": pending["days"] + 1} if pending and pending["name"] == raw else {"name": raw, "days": 1}
            if pending["days"] >= REGIME_CONFIRM:
                regime_name, regime_days, pending = raw, 1, None
                actions.insert(0, {"type": "regime切换", "code": "", "name": "", "role": "",
                                   "reason": f"新状态「{raw}」连续{REGIME_CONFIRM}日确认"})
            else:
                regime_days += 1

        if not emit_all and idx < len(dates) - 1:
            continue
        if emit_all and d < emit_from_raw:
            continue

        # ── 输出该日计划 ────────────────────────────────────────────────────
        hold_rows = []
        for code, h in holdings.items():
            row = snap.get(code)
            st = streaks[code]
            hold_rows.append({
                "code": code,
                "name": h["name"],
                "role": h["role"],
                "rank": int(row["rank"]) if row and row["rank"] else None,
                "above_ma60": bool(row and row["above_ma60"] == 1),
                "since": dashed(dates[h["since_idx"]]),
                "held_days": idx - h["since_idx"] + 1,
                "min_hold_left": max(0, MIN_HOLD - (idx - h["since_idx"] + 1)),
                "counters": {"in_top5": st.in_top5, "in_top3": st.in_top3, "out_top5": st.out_top5,
                             "out_top15": st.out_top15, "below_ma60": st.below_ma60},
            })
        # 候选板块（rotation 视角）：当日 top5 中未持仓的，带计数器与冷却，供「距纳入/晋升还差几日」展示
        cand_rows = []
        for row in sorted((r for r in snap.values() if r["rank"] and r["rank"] <= K_ENTRY), key=lambda r: r["rank"]):
            code = row["board_code"]
            if code in holdings:
                continue
            st = streaks[code]
            cand_rows.append({
                "code": code,
                "name": row["board_name"],
                "rank": int(row["rank"]),
                "above_ma60": bool(row["above_ma60"] == 1),
                "cooldown_left": max(0, COOLDOWN - (idx - cooldown[code])) if code in cooldown else 0,
                "counters": {"in_top5": st.in_top5, "in_top3": st.in_top3, "out_top5": st.out_top5,
                             "out_top15": st.out_top15, "below_ma60": st.below_ma60},
            })
        is_mainline = regime_name is not None and regime_name.startswith("抱主线")
        portfolio: list[dict[str, Any]] = (
            hold_rows if is_mainline
            else list(DEFENSE_PORTFOLIO) if regime_name and regime_name.startswith("防御")
            else list(BROAD_PORTFOLIO) if regime_name and regime_name.startswith("无主线")
            else []
        )
        today_action = "、".join(
            f"{a['type']}{('[' + a['role'] + '] ' + a['code'] + ' ' + a['name']) if a['code'] else ''}（{a['reason']}）"
            for a in actions
        ) or "维持不动"

        plan: dict[str, Any] = {
            "source": {"name": "mainline_daily_skill_v1", "engine": "scripts/mainline_daily_plan.py",
                       "note": "skill 日度纪律确定性状态机；与月度 market_mainline（v5）并行观察"},
            "as_of_date": dashed(d),
            "decision_trade_date": dashed(d),
            "regime": {
                "name": regime_name,
                "raw_name": raw,
                "days": regime_days,
                "pending": pending,
                "hs300_vs_ma120": dist,
                "concentration": {"dominant_group": dom, "dominant_count": dom_n, "counts": dict(counts)},
            },
            "mainline_theme": dom if is_mainline else None,
            "top15": top_boards(c, d, 15),
            "holdings": hold_rows,
            "candidates": cand_rows,
            "portfolio": portfolio,
            "actions": actions,
            "today_action": today_action,
            "cooldowns": [
                {"code": code, "days_left": COOLDOWN - (idx - t)}
                for code, t in cooldown.items() if idx - t <= COOLDOWN
            ],
        }
        if include_funds:
            if is_mainline:
                month = d[:6]
                matches: list[dict[str, Any]] = []
                for h in hold_rows:
                    key = (h["code"], month)
                    if key not in fund_cache:
                        fund_cache[key] = fund_matches([h], d)
                    for m in fund_cache[key]:
                        matches.append({**m, "role": h["role"]})
                plan["fund_matches"] = matches
            else:
                plan["fund_matches"] = []
        out.append(plan)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="单日模式 as-of 日期")
    ap.add_argument("--from", dest="date_from", help="区间模式起始（JSONL）")
    ap.add_argument("--to", dest="date_to", help="区间模式结束（JSONL）")
    ap.add_argument("--include-funds", action="store_true")
    args = ap.parse_args()

    if args.date and (args.date_from or args.date_to):
        ap.error("--date 与 --from/--to 互斥")
    if args.date:
        plans = replay(ymd(args.date), None, args.include_funds)
        print(json.dumps(plans[-1], ensure_ascii=False, indent=2))
    elif args.date_from and args.date_to:
        plans = replay(ymd(args.date_to), ymd(args.date_from), args.include_funds)
        for p in plans:
            print(json.dumps(p, ensure_ascii=False))
    else:
        ap.error("需要 --date 或 --from/--to")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
