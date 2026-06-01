"""fund_md_to_db — 将 bot 写的 5 份基金 portfolio MD 解析后调 admin MCP 写库。

入口: process_bot(bot_id, run_id, trade_date, paradigm_active, mcp_call_fn, mcp_url)

设计要点（与 tougu_md_to_db.py 同源，差异点用 ⚠️ 标出）：
- bot 只写 MD（含 YAML frontmatter）；本模块代替 bot 调:
 actions 覆盖检查：用 DB 真值，不用 bot 的 holdings_fm
    if bot_id:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            rows = conn.execute(
                "SELECT fund_code FROM fund_bot_holdings "
                "WHERE bot_id=? AND status='active'",
                (bot_id,)
            ).fetchall()
            db_active = {r[0] for r in rows}
        finally:
            conn.close()
        missing = db_active - action_codes
        if missing:
            for code in sorted(missing):
                actions.append({
                    "fund_code": code,
                    "action_type": "HOLD",
                    "timing_state": "",
                    "momentum_state": "",
                    "matrix_suggestion": "",
                    "final_decision": "HOLD",
                    "reason": "system_auto_fill_hold",
                })
            action_codes.update(missing)
            issues.append(f"warn: 自动补齐 HOLD action: {sorted(missing)}")
save_allocation_run         ← 市场环境判断.md
    save_selection_run          ← 个性化基金选择.md
    apply_fund_review_and_rebalance  ← 基金巡检记录.md + 当前基金持仓.md
  ⚠️ 投资框架.md 仅做校验（paradigm + 三层结构字段），不写新表；
     paradigm 已由 Phase B-1 写到 fund_paradigm_runs。

- frontmatter 是结构化数据；正文是叙事，分别入 *_md / market_summary_md / selection_md / review_md 字段。

- 任一步失败立即返回 (False, issues)，写顺序：
    1) 校验 5 份 MD（投资框架 → 市场 → 选基 → 持仓 → 巡检）
    2) save_allocation_run     (写 fund_allocation_runs)
    3) save_selection_run      (写 fund_selection_runs)
    4) apply_fund_review_and_rebalance (写 fund_bot_reviews + fund_bot_actions + fund_bot_holdings + cash + 在途订单)

- 写库表（fund_allocation_runs / fund_bot_reviews / fund_bot_actions）的 paradigm 字段
  在写入参数里直接填 paradigm_active；MCP 工具会原样落表。

参考链路文档 § 十二点五 "统一 MD 格式与 MD→DB 落库" 的完整 schema。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import yaml

WORKSPACE_ROOT = "/home/rooot/agent_invest_lab/bots/{bot_id}"
PORTFOLIO_REL = "memory/portfolio/fund"  # ⚠️ 基金链路的子目录

MD_FILES = {
    "framework": "投资框架.md",
    "market":    "市场环境判断.md",
    "selection": "个性化基金选择.md",
    "holdings":  "当前基金持仓.md",
    "review":    "基金巡检记录.md",
}

EXPECTED_STEPS = {
    "framework": "investment_framework",
    "market":    "market_context",
    "selection": "fund_selection",
    "holdings":  "fund_holdings_snapshot",
    "review":    "fund_review",
}

# Enum 白名单（与 fund-review/SKILL.md 同步）
VALID_PARADIGMS = {"A", "B1", "B2", "C"}
VALID_REGIMES = {"bull", "range", "bear", "crisis"}
VALID_TIMING_STANCES = {
    "aggressively_add", "add_on_pullback", "hold", "defensive", "risk_off",
}
VALID_DECISIONS = {"KEEP", "REBALANCE", "SWITCH"}
VALID_ACTION_TYPES = {"HOLD", "ADD", "REDUCE", "TAKE_PROFIT", "STOP_LOSS"}
VALID_ASSET_CLASSES = {"股票类", "债券类", "黄金类", "现金"}
# 加"流动性储备"覆盖 A 范式下 bot 把现金/货基当独立角色的写法
VALID_ROLES = {"核心底仓", "卫星增强", "对冲配置", "防御缓冲", "流动性储备"}
VALID_LAYER2_PIVOTS = {"大类资产", "行业", "单一行业", "单基金"}
REQUIRED_PERFORMANCE_PERIODS = ("1m", "3m", "6m", "1y", "3y")

ASSET_KEYS = ("equity_pct", "bond_pct", "gold_pct", "cash_pct")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _to_float(value: Any) -> float | None:
    return float(value) if _is_number(value) else None


def _normalize_review_actions(actions_input: Any, active_codes: set[str]) -> tuple[list[dict], list[str]]:
    normalized: list[dict] = []
    warnings: list[str] = []
    for action in actions_input or []:
        if isinstance(action, dict):
            normalized.append(dict(action))

    action_codes = {
        str(action.get("fund_code") or "")
        for action in normalized
        if str(action.get("fund_code") or "")
    }
    missing = sorted(code for code in active_codes if code not in action_codes)
    for code in missing:
        normalized.append({
            "fund_code": code,
            "action_type": "HOLD",
            "timing_state": "",
            "momentum_state": "",
            "matrix_suggestion": "",
            "final_decision": "HOLD",
            "reason": "system_auto_fill_hold",
        })
    if missing:
        warnings.append(f"warn: 自动补齐 HOLD action: {missing}")
    return normalized, warnings


def _extract_target_allocation_context(market_fm: dict | None) -> tuple[dict | None, dict | None]:
    if not isinstance(market_fm, dict):
        return None, None
    today_target = market_fm.get("today_target")
    central_baseline = market_fm.get("central_baseline")
    return (today_target if isinstance(today_target, dict) else None,
            central_baseline if isinstance(central_baseline, dict) else None)


def _has_complete_asset_target(target: dict | None) -> bool:
    if not isinstance(target, dict):
        return False
    return all(isinstance(target.get(k), (int, float)) for k in ASSET_KEYS)


# ============================================================
# Frontmatter parsing
# ============================================================

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    """切出 frontmatter 字典和正文。frontmatter 缺失返回 (None, original_text)。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None, text
    if not isinstance(fm, dict):
        return None, text
    return fm, m.group(2)


def _dump_frontmatter_text(fm: dict, body: str) -> str:
    dumped = yaml.safe_dump(
        fm,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    body_text = body.lstrip("\n")
    return f"---\n{dumped}\n---\n\n{body_text}"


def _fetch_fund_performance_snapshot(db_path: str, fund_code: str) -> dict[str, dict]:
    """从 fund_performance 取一个基金所需周期的最新业绩快照。"""
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in REQUIRED_PERFORMANCE_PERIODS)
        rows = conn.execute(
            f"""
            SELECT fund_code, as_of_date, period, return_pct, rank_pct, rank_text,
                   max_drawdown_pct, volatility_pct, sharpe_ratio, calmar_ratio
            FROM fund_performance
            WHERE fund_code = ? AND period IN ({placeholders})
            ORDER BY period, as_of_date DESC
            """,
            (fund_code, *REQUIRED_PERFORMANCE_PERIODS),
        ).fetchall()
    finally:
        conn.close()

    perf_by_period: dict[str, dict] = {}
    for row in rows:
        period = row["period"]
        if period in perf_by_period:
            continue
        payload = {
            "return_pct": row["return_pct"],
            "rank_pct": row["rank_pct"],
            "rank_text": row["rank_text"],
            "max_drawdown_pct": row["max_drawdown_pct"],
            "volatility_pct": row["volatility_pct"],
            "sharpe_ratio": row["sharpe_ratio"],
            "calmar_ratio": row["calmar_ratio"],
            "as_of_date": row["as_of_date"],
        }
        perf_by_period[period] = {k: v for k, v in payload.items() if v is not None}
    return perf_by_period


def _autofill_selection_performance(
    selection_fm: dict,
    selection_body: str,
    selection_path: Path,
    db_path: str,
) -> list[str]:
    """尽量从 fund_performance 自动补齐 funds[*].performance，优先保证 bot 当天成功。"""
    warnings: list[str] = []
    funds = selection_fm.get("funds")
    if not isinstance(funds, list):
        return warnings

    changed = False
    cache: dict[str, dict[str, dict]] = {}
    for idx, fund in enumerate(funds):
        if not isinstance(fund, dict):
            continue
        code = str(fund.get("fund_code") or "").strip()
        if not code:
            continue
        perf = fund.get("performance")
        if not isinstance(perf, dict):
            perf = {}
            fund["performance"] = perf
            changed = True
        snapshot = cache.get(code)
        if snapshot is None:
            snapshot = _fetch_fund_performance_snapshot(db_path, code)
            cache[code] = snapshot
        filled: list[str] = []
        still_missing: list[str] = []
        for period in REQUIRED_PERFORMANCE_PERIODS:
            existing_payload = perf.get(period)
            needs_fill = (
                period not in perf
                or not isinstance(existing_payload, dict)
                or not isinstance(existing_payload.get("return_pct"), (int, float))
            )
            if not needs_fill:
                continue
            period_payload = snapshot.get(period)
            if isinstance(period_payload, dict) and period_payload:
                perf[period] = dict(period_payload)
                filled.append(period)
                changed = True
            else:
                still_missing.append(period)
        if filled:
            warnings.append(
                f"warn: selection 自动补齐 {code} 的 performance 周期 {filled}"
            )
        if still_missing:
            warnings.append(
                f"warn: selection 无法从 fund_performance 补齐 {code} 的周期 {still_missing}"
            )

    if changed:
        selection_path.write_text(
            _dump_frontmatter_text(selection_fm, selection_body),
            encoding="utf-8",
        )
    return warnings


def extract_review_section(body: str, header: str) -> str:
    """从 review body 抽出 H2 段落（包含 header 行，到下一个 H2 之前）。"""
    if not header:
        return ""
    pattern = re.compile(
        r"(^|\n)" + re.escape(header) + r"\s*\n(.*?)(?=\n## [^#]|\Z)",
        re.DOTALL,
    )
    m = pattern.search(body)
    if not m:
        return ""
    return (header + "\n" + m.group(2)).strip()


# ============================================================
# Validators
# ============================================================

def _check_common(fm: dict, expected_step: str, run_id: str, trade_date: str,
                  paradigm_active: str, issues: list[str], label: str) -> None:
    if fm.get("step") != expected_step:
        issues.append(f"{label}: frontmatter.step != '{expected_step}' (got {fm.get('step')!r})")
    fm_run = str(fm.get("run_id") or "")
    if fm_run and fm_run != run_id:
        issues.append(f"{label}: run_id 不匹配 (md={fm_run}, expected={run_id})")
    fm_td = str(fm.get("trade_date") or "")
    if fm_td and fm_td != trade_date:
        issues.append(f"{label}: trade_date 不匹配 (md={fm_td}, expected={trade_date})")
    fm_p = str(fm.get("paradigm_active") or "")
    if fm_p and fm_p != paradigm_active:
        issues.append(
            f"{label}: paradigm_active 不匹配 (md={fm_p}, expected={paradigm_active})"
        )


def _validate_framework(fm: dict, body: str, run_id: str, trade_date: str,
                        paradigm_active: str, issues: list[str]) -> None:
    _check_common(fm, "investment_framework", run_id, trade_date, paradigm_active,
                  issues, "framework")
    if paradigm_active not in VALID_PARADIGMS:
        issues.append(f"framework: paradigm_active={paradigm_active!r} 不在白名单 (B-1 可能写错)")
    for k in ("layer1_capability_brief", "layer2_pivot",
              "layer2_structure_brief", "layer3_signal_source",
              "layer3_discipline_brief"):
        if not (fm.get(k) or "").strip():
            issues.append(f"framework: {k} 缺失或为空")
    pivot = fm.get("layer2_pivot")
    if pivot and pivot not in VALID_LAYER2_PIVOTS:
        issues.append(f"framework: layer2_pivot={pivot!r} 不在白名单 {VALID_LAYER2_PIVOTS}")
    if not body.strip():
        issues.append("framework: 正文为空")


def _validate_market(fm: dict, body: str, run_id: str, trade_date: str,
                     paradigm_active: str, issues: list[str]) -> None:
    _check_common(fm, "market_context", run_id, trade_date, paradigm_active,
                  issues, "market")
    regime = fm.get("regime")
    if regime not in VALID_REGIMES:
        issues.append(f"market: regime={regime!r} 不在白名单 {VALID_REGIMES}")
    stance = fm.get("timing_stance")
    if stance not in VALID_TIMING_STANCES:
        issues.append(f"market: timing_stance={stance!r} 不在白名单")

    today = fm.get("today_target")
    if not isinstance(today, dict):
        issues.append("market: today_target 缺失")
    else:
        miss = [k for k in ASSET_KEYS if not isinstance(today.get(k), (int, float))]
        if miss:
            issues.append(f"market: today_target 缺字段 {miss}")
        else:
            tot = sum(today[k] for k in ASSET_KEYS)
            if abs(tot - 100) > 1:
                issues.append(f"market: today_target 4 大类合计 = {tot}%（应=100）")
    central = fm.get("central_baseline")
    if not isinstance(central, dict):
        issues.append("market: central_baseline 缺失")
    else:
        miss = [k for k in ASSET_KEYS if not isinstance(central.get(k), (int, float))]
        if miss:
            issues.append(f"market: central_baseline 缺字段 {miss}")
    # B1/B2 必填 focus_industries
    if paradigm_active in ("B1", "B2"):
        fi = fm.get("focus_industries")
        if not isinstance(fi, list) or not fi:
            issues.append(
                f"market: paradigm={paradigm_active} 必填 focus_industries (非空 list)"
            )
    if not body.strip():
        issues.append("market: 正文为空")


def _validate_selection(fm: dict, body: str, run_id: str, trade_date: str,
                        paradigm_active: str, issues: list[str],
                        db_path: str = "/home/rooot/agent_invest_lab/data/fund.db",
                        market_fm: dict | None = None) -> None:
    """校验 selection MD。

    硬约束：funds[*].fund_code 必须在 fund_info 表里（即核心池抓到的范围）。
    用户只做指数基金投资，bot 不能从记忆里凭空选核心池外的代码。
    """
    _check_common(fm, "fund_selection", run_id, trade_date, paradigm_active,
                  issues, "selection")
    funnel = fm.get("funnel")
    if not isinstance(funnel, dict):
        issues.append("selection: funnel 缺失（必须是 dict）")
    funds = fm.get("funds")
    if not isinstance(funds, list) or not funds:
        issues.append("selection: funds 必须是非空 list")
        return

    # 一次性查 fund_info 里所有"指数型"基金（fund_type 以 '指数型' 开头）
    # 用户口径：只做指数基金投资，主动型/混合型/股票型都不允许
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        valid_codes = {r[0] for r in conn.execute(
            "SELECT fund_code FROM fund_info WHERE fund_type LIKE '指数型%'"
        ).fetchall()}
    finally:
        conn.close()

    weight_sum = 0.0
    for i, f in enumerate(funds):
        if not isinstance(f, dict):
            issues.append(f"selection: funds[{i}] 非字典")
            continue
        code = f.get("fund_code")
        if not code:
            issues.append(f"selection: funds[{i}].fund_code 缺失")
            continue
        if code not in valid_codes:
            issues.append(
                f"selection: funds[{i}].fund_code={code!r} 不是指数型基金 "
                f"(fund_info.fund_type 必须以 '指数型' 开头)。本系统只做指数基金投资。"
            )
        ac = f.get("asset_class")
        if ac and ac not in VALID_ASSET_CLASSES:
            issues.append(
                f"selection: funds[{i}].asset_class={ac!r} 不在白名单 {VALID_ASSET_CLASSES}"
            )
        role = f.get("role")
        if role and role not in VALID_ROLES:
            issues.append(
                f"selection: funds[{i}].role={role!r} 不在白名单 {VALID_ROLES}"
            )
        perf = f.get("performance")
        if not isinstance(perf, dict):
            issues.append(
                f"selection: funds[{i}].performance 缺失（必须包含 {REQUIRED_PERFORMANCE_PERIODS} 五个周期）"
            )
        else:
            missing_periods = [p for p in REQUIRED_PERFORMANCE_PERIODS if p not in perf]
            if missing_periods:
                issues.append(
                    f"selection: funds[{i}].performance 缺周期 {missing_periods}"
                )
            for period in REQUIRED_PERFORMANCE_PERIODS:
                if period not in perf:
                    continue
                period_payload = perf.get(period)
                if not isinstance(period_payload, dict):
                    issues.append(
                        f"selection: funds[{i}].performance.{period} 必须是 dict"
                    )
                    continue
                if not isinstance(period_payload.get("return_pct"), (int, float)):
                    issues.append(
                        f"selection: funds[{i}].performance.{period}.return_pct 必须是数字"
                    )
        w = f.get("target_weight")
        if not isinstance(w, (int, float)):
            issues.append(f"selection: funds[{i}].target_weight 必须是数字")
        else:
            weight_sum += float(w)

    # 循环外：合计校验（允许 < 1.0 留 cash buffer，但不能超 1.0）
    if weight_sum > 1.01:
        issues.append(f"selection: target_weight 合计={weight_sum:.4f} 超过 1.0")
    elif weight_sum < 0.5:
        issues.append(
            f"selection: target_weight 合计={weight_sum:.4f} 偏低 (<50%)，看起来缺权重字段"
        )
    if market_fm and isinstance(market_fm.get("today_target"), dict):
        target = market_fm.get("today_target") or {}
        class_weights = defaultdict(float)
        for fund in funds:
            if not isinstance(fund, dict):
                continue
            ac = fund.get("asset_class")
            w = fund.get("target_weight")
            if ac in VALID_ASSET_CLASSES and isinstance(w, (int, float)):
                class_weights[ac] += float(w) * 100
        expected = {
            "股票类": float(target.get("equity_pct") or 0),
            "债券类": float(target.get("bond_pct") or 0),
            "黄金类": float(target.get("gold_pct") or 0),
        }
        cash_expected = float(target.get("cash_pct") or 0)
        implied_cash = max(0.0, 100.0 - sum(class_weights.values()))
        for asset_class, expected_pct in expected.items():
            actual_pct = class_weights.get(asset_class, 0.0)
            if abs(actual_pct - expected_pct) > 10.0:
                issues.append(
                    f"selection: {asset_class} 目标占比 {actual_pct:.1f}% 与 today_target {expected_pct:.1f}% 偏离超过 10%"
                )
        if abs(implied_cash - cash_expected) > 10.0:
            issues.append(
                f"selection: 隐含现金占比 {implied_cash:.1f}% 与 today_target {cash_expected:.1f}% 偏离超过 10%"
            )
    if not body.strip():
        issues.append("selection: 正文为空")


def _validate_holdings(fm: dict, body: str, run_id: str, trade_date: str,
                       paradigm_active: str, issues: list[str]) -> None:
    _check_common(fm, "fund_holdings_snapshot", run_id, trade_date, paradigm_active,
                  issues, "holdings")
    account = fm.get("account")
    if not isinstance(account, dict):
        issues.append("holdings: account 缺失")
    else:
        for k in ("cash", "total_value"):
            if not isinstance(account.get(k), (int, float)):
                issues.append(f"holdings: account.{k} 必须是数字")
    holdings = fm.get("holdings")
    if not isinstance(holdings, list):
        issues.append("holdings: holdings 必须是 list")
    else:
        codes = set()
        cash = _to_float(account.get("cash")) if isinstance(account, dict) else None
        total_value = _to_float(account.get("total_value")) if isinstance(account, dict) else None
        market_value_sum = 0.0
        has_all_market_values = True
        for i, h in enumerate(holdings):
            if not isinstance(h, dict):
                issues.append(f"holdings: holdings[{i}] 非字典")
                continue
            code = h.get("fund_code")
            if not code:
                issues.append(f"holdings: holdings[{i}].fund_code 缺失")
            elif code in codes:
                issues.append(f"holdings: holdings[{i}].fund_code={code!r} 重复")
            else:
                codes.add(code)
            market_value = _to_float(h.get("market_value"))
            if market_value is None:
                has_all_market_values = False
            else:
                market_value_sum += market_value

            weight_value = h.get("actual_weight")
            if weight_value is None:
                weight_value = h.get("weight")
            weight = _to_float(weight_value)
            if weight is not None and total_value and total_value > 0 and market_value is not None:
                expected_weight = market_value / total_value
                if abs(weight - expected_weight) > 0.02:
                    issues.append(
                        f"holdings: holdings[{i}].fund_code={code!r} 权重与市值/总资产不一致 "
                        f"(md={weight:.4f}, expected={expected_weight:.4f})"
                    )

        if cash is not None and total_value is not None and has_all_market_values:
            expected_total = cash + market_value_sum
            if abs(total_value - expected_total) > 1.0:
                issues.append(
                    f"holdings: account.total_value 与 cash + holdings.market_value 合计不一致 "
                    f"(md={total_value:.2f}, expected={expected_total:.2f})"
                )


def _validate_review(fm: dict, body: str, run_id: str, trade_date: str,
                     paradigm_active: str, holdings_fm: dict | None,
                     issues: list[str],
                     db_path: str = "/home/rooot/agent_invest_lab/data/fund.db",
                     bot_id: str | None = None) -> None:
    """校验 review MD。

    bot 必填字段（决策类）：decision / 每条 action 的 fund_code + action_type
    bot 选填字段（数值类）：nav_used / before_weight / after_weight / shares / fee
        — 系统在 build_review_args 里会从 DB + actions 推演覆盖，bot 写错也无影响

    actions 必须覆盖 DB 中所有 active 持仓（HOLD 也要写一条）。这里用 DB 真值校验，
    不依赖 holdings_fm（bot 的快照 MD 可能漏写或写错）。
    """
    _check_common(fm, "fund_review", run_id, trade_date, paradigm_active,
                  issues, "review")
    decision = (fm.get("decision") or "").upper()
    if decision not in VALID_DECISIONS:
        issues.append(
            f"review: decision={fm.get('decision')!r} 不在白名单 {VALID_DECISIONS}"
        )
    actions = fm.get("actions") or []
    if not isinstance(actions, list):
        issues.append("review: actions 必须是 list")
        return

    turnover_amount = _to_float(fm.get("turnover_amount"))
    turnover_ratio = _to_float(fm.get("turnover_ratio"))

    # 一次性查 fund_info 里所有指数型基金 fund_code（用于 ADD 动作的核心池校验）
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        valid_codes = {r[0] for r in conn.execute(
            "SELECT fund_code FROM fund_info WHERE fund_type LIKE '指数型%'"
        ).fetchall()}
    finally:
        conn.close()

    holdings_codes = set()
    if isinstance(holdings_fm, dict) and isinstance(holdings_fm.get("holdings"), list):
        holdings_codes = {
            str(h.get("fund_code") or "")
            for h in holdings_fm.get("holdings") or []
            if isinstance(h, dict) and str(h.get("fund_code") or "")
        }

    action_codes = set()
    material_action_found = False
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            issues.append(f"review: actions[{i}] 非字典")
            continue
        code = a.get("fund_code")
        if not code:
            issues.append(f"review: actions[{i}].fund_code 缺失")
        else:
            action_codes.add(code)
        atype = (a.get("action_type") or "").upper()
        if atype not in VALID_ACTION_TYPES:
            issues.append(
                f"review: actions[{i}].action_type={a.get('action_type')!r} 不在白名单"
            )
        amount = float(a.get("amount") or 0)
        if atype != "HOLD" and abs(amount) > 0.01:
            material_action_found = True
        # 新建仓 ADD 动作的 fund_code 必须是指数型 — 不能从记忆里凭空买入主动型基金
        if atype == "ADD" and code and code not in valid_codes:
            issues.append(
                f"review: actions[{i}] ADD {code!r} 不是指数型基金 "
                f"(fund_info.fund_type 必须以 '指数型' 开头)。本系统只做指数基金投资。"
            )
        if atype != "ADD" and code and holdings_codes and code not in holdings_codes:
            issues.append(
                f"review: actions[{i}].fund_code={code!r} 不在 holdings 快照中，且不是 ADD"
            )
        # 数值字段（before_weight/after_weight/nav_used/shares/fee）系统侧会重算覆盖，
        # 这里不再要求 bot 写得对；空字符串/None/数字都接受。

    if decision == "KEEP":
        if material_action_found:
            issues.append("review: decision=KEEP 时不允许出现非 HOLD 的非零调仓动作")
        if turnover_amount is not None and abs(turnover_amount) > 0.01:
            issues.append("review: decision=KEEP 时 turnover_amount 必须为 0")
        if turnover_ratio is not None and abs(turnover_ratio) > 0.01:
            issues.append("review: decision=KEEP 时 turnover_ratio 必须为 0")

    review_after = fm.get("account_after")
    holdings_account = holdings_fm.get("account") if isinstance(holdings_fm, dict) else None
    if isinstance(review_after, dict) and isinstance(holdings_account, dict):
        review_cash = _to_float(review_after.get("cash"))
        review_total = _to_float(review_after.get("portfolio_value"))
        holdings_cash = _to_float(holdings_account.get("cash"))
        holdings_total = _to_float(holdings_account.get("total_value"))
        if review_cash is not None and holdings_cash is not None and abs(review_cash - holdings_cash) > 0.2:
            issues.append(
                f"review: account_after.cash 与 holdings.account.cash 不一致 "
                f"(review={review_cash:.2f}, holdings={holdings_cash:.2f})"
            )
        if review_total is not None and holdings_total is not None and abs(review_total - holdings_total) > 0.2:
            issues.append(
                f"review: account_after.portfolio_value 与 holdings.account.total_value 不一致 "
                f"(review={review_total:.2f}, holdings={holdings_total:.2f})"
            )

    # actions 覆盖检查：用 DB 真值，不用 bot 的 holdings_fm
    if bot_id:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            rows = conn.execute(
                "SELECT fund_code FROM fund_bot_holdings "
                "WHERE bot_id=? AND status='active'",
                (bot_id,)
            ).fetchall()
            db_active = {r[0] for r in rows}
        finally:
            conn.close()
        _normalized_actions, warnings = _normalize_review_actions(actions, db_active)
        issues.extend(warnings)
    if not body.strip():
        issues.append("review: 正文为空")


# ============================================================
# Payload builders
# ============================================================

def build_allocation_args(fm: dict, body: str, run_id: str, trade_date: str,
                          bot_id: str, paradigm_active: str) -> dict:
    """组装 save_allocation_run 调用参数。market_summary_md = 整段正文。"""
    asset_target = {
        "regime": fm.get("regime"),
        "regime_code": fm.get("regime_code"),
        "confidence": fm.get("confidence"),
        "timing_stance": fm.get("timing_stance"),
        "summary": fm.get("summary"),
        "central_baseline": fm.get("central_baseline"),
        "today_target": fm.get("today_target"),
        "focus_industries": fm.get("focus_industries") or [],
        "paradigm_active": paradigm_active,
    }
    return {
        "run_id": run_id,
        "bot_id": bot_id,
        "asset_target_json": json.dumps(asset_target, ensure_ascii=False, default=str),
        "trade_date": trade_date,
        "regime": fm.get("regime") or "",
        "market_summary_md": body.strip(),
        "paradigm": paradigm_active,
    }


def build_selection_args(fm: dict, body: str, run_id: str, trade_date: str,
                         bot_id: str) -> dict:
    """组装 save_selection_run 调用参数。selection_md = 整段正文。"""
    funnel = fm.get("funnel") or {}
    return {
        "run_id": run_id,
        "bot_id": bot_id,
        "trade_date": trade_date,
        "layer1_count": int(funnel.get("layer1_count") or 0),
        "layer2_count": int(funnel.get("layer2_count") or 0),
        "layer3_count": int(funnel.get("layer3_count") or 0),
        "layer4_count": int(funnel.get("layer4_count") or 0),
        "selected_funds_json": json.dumps(fm.get("funds") or [], ensure_ascii=False),
        "eliminated_json": json.dumps(fm.get("eliminated") or [], ensure_ascii=False),
        "selection_md": body.strip(),
    }


def build_selection_thesis_map(selection_fm: dict | None) -> dict[str, str]:
    """从系统已解析的选基 frontmatter 中提取每只基金的选中逻辑。

    thesis 是持仓展示字段，但来源必须是 selection 阶段的系统解析结果，
    不信任 bot 在 holdings 快照里单独填写的同名字段。
    """
    funds = (selection_fm or {}).get("funds") or []
    thesis_by_code: dict[str, str] = {}
    if not isinstance(funds, list):
        return thesis_by_code
    for fund in funds:
        if not isinstance(fund, dict):
            continue
        code = str(fund.get("fund_code") or "").strip()
        thesis = str(fund.get("thesis") or "").strip()
        if code and thesis:
            thesis_by_code[code] = thesis[:500]
    return thesis_by_code


def build_selection_fund_map(selection_fm: dict | None) -> dict[str, dict]:
    """从 selection frontmatter 取基金级系统字段，供持仓落库使用。"""
    funds = (selection_fm or {}).get("funds") or []
    funds_by_code: dict[str, dict] = {}
    if not isinstance(funds, list):
        return funds_by_code
    for fund in funds:
        if not isinstance(fund, dict):
            continue
        code = str(fund.get("fund_code") or "").strip()
        if code:
            funds_by_code[code] = fund
    return funds_by_code


def _read_db_state(db_path: str, bot_id: str) -> dict | None:
    """读 DB 当前状态：account + active holdings + market_value."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        acc = conn.execute(
            "SELECT * FROM fund_bot_accounts WHERE bot_id=?", (bot_id,)
        ).fetchone()
        if not acc:
            return None
        holdings = conn.execute(
            "SELECT * FROM fund_bot_holdings WHERE bot_id=? AND status='active'",
            (bot_id,)
        ).fetchall()
        invested = sum((h["market_value"] or 0) for h in holdings)
        return {
            "cash": float(acc["cash"] or 0),
            "initial_capital": float(acc["initial_capital"] or 0),
            "invested_value": invested,
            "portfolio_value": float(acc["cash"] or 0) + invested,
            "holdings": [dict(h) for h in holdings],
            "holdings_by_code": {h["fund_code"]: dict(h) for h in holdings},
        }
    finally:
        conn.close()


def _get_nav_on_date(db_path: str, fund_code: str, trade_date: str) -> float | None:
    """取 fund_code 在 <= trade_date 的最新 NAV。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT nav FROM fund_nav WHERE fund_code=? AND nav_date<=? "
            "ORDER BY nav_date DESC LIMIT 1",
            (fund_code, trade_date)
        ).fetchone()
        return float(row[0]) if row and row[0] else None
    finally:
        conn.close()


def build_review_args(review_fm: dict, review_body: str, holdings_fm: dict,
                      run_id: str, trade_date: str, bot_id: str,
                      paradigm_active: str,
                      selection_fm: dict | None = None,
                      market_fm: dict | None = None,
                      db_path: str = "/home/rooot/agent_invest_lab/data/fund.db") -> dict:
    """组装 apply_fund_review_and_rebalance 调用参数。

    系统侧规则：
    - 现金/市值/权重/份额/费用全部由系统从 DB + actions 推演，不取 bot MD 里的数值字段
    - bot 只贡献 decision/action_type/timing_state/momentum_state/matrix_suggestion/
      final_decision/reason/trigger/amount(若 REBALANCE/SWITCH)
    - bot 在 MD 里写的 account_before/account_after/before_weight/nav_used/shares/fee 等
      仅作为自检参考，DB 写入以系统计算为准
    """
    decision = (review_fm.get("decision") or "").upper()
    actions_input = review_fm.get("actions") or []
    today_target, central_baseline = _extract_target_allocation_context(market_fm)
    if not _has_complete_asset_target(today_target):
        raise ValueError("调仓前缺少合法 today_target，大类资产目标未建立")
    if not _has_complete_asset_target(central_baseline):
        raise ValueError("调仓前缺少合法 central_baseline，大类资产配置中枢未建立")

    # ① 系统读 DB 当前状态作为 before
    state = _read_db_state(db_path, bot_id)
    if state is None:
        raise ValueError(f"bot {bot_id} 在 fund_bot_accounts 不存在")

    cash_before = state["cash"]
    portfolio_value_before = state["portfolio_value"]
    current_holdings_map = state["holdings_by_code"]
    selection_thesis_by_code = build_selection_thesis_map(selection_fm)
    selection_funds_by_code = build_selection_fund_map(selection_fm)
    actions_input, _warnings = _normalize_review_actions(actions_input, set(current_holdings_map.keys()))

    # ② 在 actions 上做推演，得到 cash_after / new_holdings
    new_cash = cash_before
    new_holdings_map: dict[str, dict] = {
        code: dict(h) for code, h in current_holdings_map.items()
    }
    enriched_actions: list[dict] = []
    fee_default_rate = 0.005   # 0.5% 默认短持惩罚费（REDUCE/STOP_LOSS 时用）

    for a in actions_input:
        if not isinstance(a, dict):
            continue
        code = a.get("fund_code")
        atype = (a.get("action_type") or "HOLD").upper()
        amount = float(a.get("amount") or 0)
        nav_used = _get_nav_on_date(db_path, code, trade_date) or 1.0
        cur_h = current_holdings_map.get(code, {})
        cur_mv = float(cur_h.get("market_value") or 0)
        before_weight = (cur_mv / portfolio_value_before) if portfolio_value_before else 0.0

        cash_delta = 0.0
        share_delta = 0.0
        fee = 0.0

        if atype == "HOLD":
            pass
        elif atype == "ADD":
            cash_delta = -amount
            share_delta = amount / nav_used
            new_cash += cash_delta
            if code in new_holdings_map:
                new_holdings_map[code]["shares"] = (new_holdings_map[code].get("shares") or 0) + share_delta
                new_holdings_map[code]["amount_invested"] = (new_holdings_map[code].get("amount_invested") or 0) + amount
                new_holdings_map[code]["latest_nav"] = nav_used
                new_holdings_map[code]["market_value"] = new_holdings_map[code]["shares"] * nav_used
            else:
                # 新建仓
                new_holdings_map[code] = {
                    "fund_code": code,
                    "shares": share_delta,
                    "amount_invested": amount,
                    "entry_nav": nav_used,
                    "latest_nav": nav_used,
                    "market_value": amount,
                    "entry_date": trade_date,
                    "asset_class": a.get("asset_class") or cur_h.get("asset_class"),
                    "role": a.get("role") or cur_h.get("role"),
                    "status": "active",
                    "thesis": selection_thesis_by_code.get(code) or cur_h.get("thesis"),
                }
        elif atype in ("REDUCE", "TAKE_PROFIT", "STOP_LOSS"):
            if not cur_h:
                # 想卖但实际不持有 → 跳过
                continue
            share_delta = -(amount / nav_used)
            fee = amount * fee_default_rate
            cash_delta = amount - fee
            new_cash += cash_delta
            cur_shares = float(new_holdings_map[code].get("shares") or 0)
            cur_amount = float(new_holdings_map[code].get("amount_invested") or 0)
            cur_market = float(new_holdings_map[code].get("market_value") or 0)
            sell_ratio = min(1.0, amount / cur_market) if cur_market > 0 else 1.0
            cost_reduction = cur_amount * sell_ratio
            new_amount = max(cur_amount - cost_reduction, 0.0)
            new_shares = cur_shares + share_delta
            if new_shares < -0.01:
                raise ValueError(f"{code} 卖出份额超过当前持仓，无法执行调仓")
            new_holdings_map[code]["shares"] = max(new_shares, 0.0)
            new_holdings_map[code]["amount_invested"] = new_amount
            new_holdings_map[code]["latest_nav"] = nav_used
            if new_shares <= 0.001:
                # 已清仓
                del new_holdings_map[code]
            else:
                new_holdings_map[code]["market_value"] = new_shares * nav_used

        # 系统计算的 after_weight（中间值，等遍历完才能定）
        enriched_actions.append({
            "fund_code": code,
            "action_type": atype,
            "timing_state": a.get("timing_state"),
            "momentum_state": a.get("momentum_state"),
            "matrix_suggestion": a.get("matrix_suggestion"),
            "final_decision": a.get("final_decision") or atype,
            "before_weight": round(before_weight, 6),
            "after_weight": None,                          # 后面填
            "nav_used": round(nav_used, 6),
            "amount": round(amount, 2),
            "shares": round(abs(share_delta), 4),
            "fee": round(fee, 2),
            "reason": (a.get("reason") or "")[:500],
            "trigger": a.get("trigger") or "timing_matrix",
        })

    new_invested = sum(h.get("market_value") or 0 for h in new_holdings_map.values())
    portfolio_value_after = new_cash + new_invested
    if new_cash < -0.01:
        raise ValueError(f"调仓后现金为负 ({new_cash:.2f})，买入金额超过可用资金，拒绝执行")

    # 回填 enriched_actions 的 after_weight
    for ea in enriched_actions:
        h = new_holdings_map.get(ea["fund_code"])
        if h and portfolio_value_after > 0:
            ea["after_weight"] = round((h.get("market_value") or 0) / portfolio_value_after, 6)
        else:
            ea["after_weight"] = 0.0

    # turnover：所有非 HOLD 的 amount 绝对值之和
    turnover_amount = sum(
        abs(float(a.get("amount") or 0)) for a in actions_input
        if (a.get("action_type") or "").upper() != "HOLD"
    )
    turnover_ratio = (turnover_amount / portfolio_value_before * 100) if portfolio_value_before else 0.0

    # holdings_after 列表
    holdings_after_list = []
    for h in new_holdings_map.values():
        code = h.get("fund_code")
        selected_fund = selection_funds_by_code.get(code) or {}
        market_value = float(h.get("market_value") or 0)
        actual_weight = (market_value / portfolio_value_after) if portfolio_value_after else 0.0
        cost_basis = float(h.get("amount_invested") or 0)
        unrealized_pnl_pct = ((market_value / cost_basis) - 1) * 100 if cost_basis > 0 else 0.0
        holdings_after_list.append({
            "fund_code": code,
            "fund_name": h.get("fund_name") or selected_fund.get("fund_name"),
            "asset_class": h.get("asset_class") or selected_fund.get("asset_class"),
            "role": h.get("role") or selected_fund.get("role"),
            "shares": round(float(h.get("shares") or 0), 4),
            "entry_nav": round(float(h.get("entry_nav") or 0), 4),
            "latest_nav": round(float(h.get("latest_nav") or 0), 4),
            "market_value": round(market_value, 2),
            "amount_invested": round(cost_basis, 2),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 6),
            "target_weight": selected_fund.get("target_weight") if selected_fund.get("target_weight") is not None else h.get("target_weight"),
            "actual_weight": round(actual_weight, 6),
            "entry_date": h.get("entry_date"),
            "status": "active",
            "thesis": selection_thesis_by_code.get(code) or h.get("thesis"),
        })

    section_header = review_fm.get("review_md_section") or f"## {trade_date}"
    review_md = extract_review_section(review_body, section_header) or review_body.strip()
    cooldown_days = int(review_fm.get("cooldown_days", 7))
    regime = (review_fm.get("regime") or "")

    return {
        "bot_id": bot_id,
        "decision": decision,
        "reason": (review_fm.get("reason") or "")[:500],
        "actions_json": json.dumps(enriched_actions, ensure_ascii=False, default=str),
        "holdings_json": json.dumps(holdings_after_list, ensure_ascii=False, default=str),
        "orders_json": json.dumps([], ensure_ascii=False),
        "cash_after": round(new_cash, 2),
        "regime": regime,
        "review_md": review_md,
        "cooldown_days": cooldown_days,
        "trade_date": trade_date,
        "cash_before": round(cash_before, 2),
        "portfolio_value_before": round(portfolio_value_before, 2),
        "portfolio_value_after": round(portfolio_value_after, 2),
        "turnover_amount": round(turnover_amount, 2),
        "turnover_ratio": round(turnover_ratio, 4),
        "paradigm": paradigm_active,
    }


# ============================================================
# Helpers
# ============================================================

def _read_md(bot_id: str, key: str) -> tuple[str, Path]:
    path = Path(WORKSPACE_ROOT.format(bot_id=bot_id)) / PORTFOLIO_REL / MD_FILES[key]
    if not path.exists():
        return "", path
    return path.read_text(encoding="utf-8"), path


def _parse_or_fail(bot_id: str, key: str,
                   issues: list[str]) -> tuple[dict | None, str, Path]:
    """读 + parse；任意失败往 issues append 并返回 (None, "", path)。"""
    text, path = _read_md(bot_id, key)
    if not text:
        issues.append(f"{key}: 文件不存在或为空 ({path})")
        return None, "", path
    fm, body = split_frontmatter(text)
    if fm is None:
        issues.append(f"{key}: frontmatter 缺失或非合法 YAML ({path})")
        return None, body, path
    return fm, body, path


def _mcp_succeeded(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("success") is True:
            return True
        if result.get("success") is False:
            return False
        if "allocation_id" in result or "selection_id" in result or "review_id" in result:
            return True
    return False


# ============================================================
# Post-write paradigm UPDATE (3 张表的 paradigm 字段)
# ============================================================

def _backfill_paradigm(db_path: str, bot_id: str, trade_date: str,
                      paradigm_active: str) -> None:
    """对刚写入的 3 张表的 paradigm 字段做 UPDATE。

    MCP 工具签名里没有 paradigm 形参，统一在写完后用 sqlite3 直接 UPDATE。
    幂等：UPDATE 多次不影响。
    """
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            conn.execute(
                "UPDATE fund_allocation_runs SET paradigm = ? "
                "WHERE bot_id = ? AND trade_date = ?",
                (paradigm_active, bot_id, trade_date)
            )
            conn.execute(
                "UPDATE fund_bot_reviews SET paradigm = ? "
                "WHERE bot_id = ? AND review_date = ?",
                (paradigm_active, bot_id, trade_date)
            )
            conn.execute(
                "UPDATE fund_bot_actions SET paradigm = ? "
                "WHERE bot_id = ? AND action_date = ?",
                (paradigm_active, bot_id, trade_date)
            )
    finally:
        conn.close()


def _compensate_partial_writes(
    db_path: str,
    bot_id: str,
    trade_date: str,
    run_id: str,
    allocation_written: bool,
    selection_written: bool,
) -> list[str]:
    """删除当前 run 已写入但未完成整轮提交的 allocation/selection 记录。"""
    if not allocation_written and not selection_written:
        return []

    import sqlite3

    messages: list[str] = []
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            if selection_written:
                conn.execute(
                    "DELETE FROM fund_selection_runs WHERE bot_id=? AND trade_date=? AND run_id=?",
                    (bot_id, trade_date, run_id),
                )
                messages.append(
                    f"warn: 已补偿删除 fund_selection_runs 当前 run 记录 ({bot_id}, {trade_date}, {run_id})"
                )
            if allocation_written:
                conn.execute(
                    "DELETE FROM fund_allocation_runs WHERE bot_id=? AND trade_date=? AND run_id=?",
                    (bot_id, trade_date, run_id),
                )
                messages.append(
                    f"warn: 已补偿删除 fund_allocation_runs 当前 run 记录 ({bot_id}, {trade_date}, {run_id})"
                )
    finally:
        conn.close()
    return messages


# ============================================================
# Validation entry
# ============================================================

def validate_bot_md(
    bot_id: str,
    run_id: str,
    trade_date: str,
    paradigm_active: str,
    db_path: str = "/home/rooot/agent_invest_lab/data/fund.db",
) -> tuple[bool, list[str]]:
    issues: list[str] = []

    framework_fm, framework_body, _ = _parse_or_fail(bot_id, "framework", issues)
    market_fm, market_body, _ = _parse_or_fail(bot_id, "market", issues)
    selection_fm, selection_body, _ = _parse_or_fail(bot_id, "selection", issues)
    holdings_fm, holdings_body, _ = _parse_or_fail(bot_id, "holdings", issues)
    review_fm, review_body, _ = _parse_or_fail(bot_id, "review", issues)

    if issues:
        return False, issues

    # 尽量自动补齐 selection 中缺失的多周期业绩，优先保证 bot 当天成功。
    selection_path = Path(WORKSPACE_ROOT.format(bot_id=bot_id)) / PORTFOLIO_REL / MD_FILES["selection"]
    autofill_warnings = _autofill_selection_performance(
        selection_fm=selection_fm,
        selection_body=selection_body,
        selection_path=selection_path,
        db_path=db_path,
    )
    issues.extend(autofill_warnings)

    _validate_framework(framework_fm, framework_body, run_id, trade_date, paradigm_active, issues)
    _validate_market(market_fm, market_body, run_id, trade_date, paradigm_active, issues)
    _validate_selection(selection_fm, selection_body, run_id, trade_date, paradigm_active, issues, db_path=db_path, market_fm=market_fm)
    _validate_holdings(holdings_fm, holdings_body, run_id, trade_date, paradigm_active, issues)
    today_target, central_baseline = _extract_target_allocation_context(market_fm)
    if not _has_complete_asset_target(today_target):
        issues.append("review: 调仓前缺少合法 today_target，大类资产目标中枢未建立")
    if not _has_complete_asset_target(central_baseline):
        issues.append("review: 调仓前缺少合法 central_baseline，大类资产配置中枢未建立")
    _validate_review(review_fm, review_body, run_id, trade_date, paradigm_active, holdings_fm, issues, db_path=db_path, bot_id=bot_id)

    # 调仓可行性演练：到这里若字段层面已没有阻断问题，就回放一遍 actions
    # （build_review_args 就是落库前的强校验本体），把"调仓后现金为负 / 卖出超过当前持仓"
    # 这类硬错误提前暴露成 blocking issue —— 让 bot 在自检阶段（validate_fund_bot_md）就能
    # 发现并整改，而不是字段都对、落库时才被拒、直接判失败。
    blocking_so_far = [msg for msg in issues if not str(msg).startswith("warn:")]
    if not blocking_so_far and bot_id:
        try:
            build_review_args(review_fm, review_body, holdings_fm,
                              run_id, trade_date, bot_id, paradigm_active,
                              selection_fm=selection_fm, market_fm=market_fm,
                              db_path=db_path)
        except ValueError as e:
            issues.append(
                f"review: 调仓演练未通过 — {e}。请只调整 actions 的 amount："
                "加仓(ADD)金额合计不得超过可用现金、赎回(REDUCE/TAKE_PROFIT/STOP_LOSS)金额不得超过该基金当前持仓市值；"
                "不要改 thesis/理由/范式。"
            )
        except Exception as e:  # noqa: BLE001 — 演练失败不应让 validate 崩，转成 blocking issue
            issues.append(f"review: 调仓演练异常 — {type(e).__name__}: {e}")

    blocking_issues = [msg for msg in issues if not str(msg).startswith("warn:")]
    return len(blocking_issues) == 0, issues


# ============================================================
# 系统机械收口（cron 端 3 轮 repair 仍不过时的兜底）
# ============================================================

_LAYER2_PIVOT_BY_PARADIGM = {"A": "大类资产", "B1": "行业", "B2": "单一行业", "C": "单基金"}
_LAYER_BRIEF_DEFAULTS = {
    "layer1_capability_brief": "系统机械收口占位：能力圈以 memory/portfolio/fund/能力圈宣告.md 为准。",
    "layer2_structure_brief": "系统机械收口占位：结构层按本轮 paradigm 的 pivot 组织。",
    "layer3_signal_source": "系统机械收口占位：信号源 = 择时矩阵 + 动量。",
    "layer3_discipline_brief": "系统机械收口占位：纪律 = 冷却期 + 上限约束。",
}


def _index_fund_codes(db_path: str) -> set[str]:
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        return {r[0] for r in conn.execute(
            "SELECT fund_code FROM fund_info WHERE fund_type LIKE '指数型%'").fetchall()}
    finally:
        conn.close()


def _fund_asset_class(db_path: str, code: str, fallback: str = "股票类") -> str:
    """按 fund_info.fund_type / fund_name 粗分大类。"""
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        row = conn.execute(
            "SELECT fund_type, fund_name FROM fund_info WHERE fund_code=?", (code,)).fetchone()
    finally:
        conn.close()
    text = " ".join(str(x or "") for x in (row or ()))
    if "债" in text:
        return "债券类"
    if "黄金" in text or "金ETF" in text:
        return "黄金类"
    return fallback


def _round_weights_to_sum(funds: list[dict], target_sum: float) -> None:
    """把 funds[*].target_weight 等比缩放到 sum == target_sum，四舍五入到 4 位，
    误差塞进权重最大的那只。原地修改。target_sum<=0 时全部置 0。"""
    if not funds:
        return
    if target_sum <= 0:
        for f in funds:
            f["target_weight"] = 0.0
        return
    cur = sum(max(0.0, float(f.get("target_weight") or 0)) for f in funds)
    if cur <= 0:
        each = target_sum / len(funds)
        for f in funds:
            f["target_weight"] = round(each, 4)
    else:
        for f in funds:
            f["target_weight"] = round(max(0.0, float(f.get("target_weight") or 0)) / cur * target_sum, 4)
    diff = round(target_sum - sum(f["target_weight"] for f in funds), 4)
    if abs(diff) >= 0.0001:
        big = max(funds, key=lambda f: f["target_weight"])
        big["target_weight"] = round(big["target_weight"] + diff, 4)


def _normalize_pct_dict(d: dict) -> dict:
    """把 ASSET_KEYS 4 个 *_pct 规整成非负、合计 == 100（余数塞 cash_pct）。"""
    out = {k: max(0.0, float(d.get(k) or 0)) if _is_number(d.get(k)) else 0.0 for k in ASSET_KEYS}
    tot = sum(out.values())
    if tot <= 0:
        out = {"equity_pct": 70.0, "bond_pct": 10.0, "gold_pct": 0.0, "cash_pct": 20.0}
        tot = 100.0
    out = {k: round(v / tot * 100, 1) for k, v in out.items()}
    out["cash_pct"] = round(out["cash_pct"] + (100.0 - sum(out.values())), 1)
    return out


def reconcile_bot_md(bot_id: str, run_id: str, trade_date: str, paradigm_active: str,
                     db_path: str = "/home/rooot/agent_invest_lab/data/fund.db") -> tuple[bool, list[str]]:
    """系统机械收口：cron 端 3 轮 repair 后 bot 写的 5 份 MD 仍过不了校验时调用。
    不改 bot 的投资观点/thesis/正文，只把**纯机械的数值字段**按权威源强制收口：
      - 投资框架.md：step/run_id/trade_date/paradigm 修对；layer2_pivot 按 paradigm 锚定；缺的 brief 补占位
      - 市场环境判断.md：regime/timing_stance 落到白名单；today_target/central_baseline 规整成合计=100；B1/B2 补 focus_industries
      - 个性化基金选择.md：剔除非指数型 fund_code（含 'CASH' 之类）；asset_class/role 落到白名单；performance 补齐；
        按 today_target 把各 asset_class 的 target_weight 等比缩放对齐（"调持仓去匹配 target"）
      - 当前基金持仓.md：account/holdings 直接用 DB 当前真实持仓重建（total_value==cash+Σmv，weight==mv/total）
      - 基金巡检记录.md：decision/action_type 落到白名单；ADD 总额封顶到可用现金、卖出封顶到该基金当前市值；
        覆盖所有 active 持仓（缺的补 HOLD）；KEEP 时清零非 HOLD 动作；account_after 与持仓快照对齐
    收口后重新跑 validate_bot_md，返回 (是否通过, issues)。"""
    paths: dict[str, Path] = {}
    fms: dict[str, dict] = {}
    bodies: dict[str, str] = {}
    for key in ("framework", "market", "selection", "holdings", "review"):
        text, path = _read_md(bot_id, key)
        paths[key] = path
        fm, body = split_frontmatter(text)
        if fm is None:
            return False, [f"reconcile: {MD_FILES[key]} 无法解析 frontmatter，无法机械收口"]
        fms[key] = fm
        bodies[key] = body if body.strip() else f"## {trade_date}\n\n(系统机械收口占位正文)\n"

    # 公共字段
    for key in fms:
        fm = fms[key]
        fm["step"] = EXPECTED_STEPS[key]
        fm["run_id"] = run_id
        fm["trade_date"] = trade_date
        fm["paradigm_active"] = paradigm_active

    # ① framework
    fw = fms["framework"]
    fw["layer2_pivot"] = _LAYER2_PIVOT_BY_PARADIGM.get(paradigm_active, "大类资产")
    for k, default in _LAYER_BRIEF_DEFAULTS.items():
        if not str(fw.get(k) or "").strip():
            fw[k] = default

    # ② market
    mk = fms["market"]
    rg = str(mk.get("regime") or "").lower()
    if rg not in VALID_REGIMES:
        if rg.startswith("bull"):
            mk["regime"] = "bull"
        elif rg.startswith("bear"):
            mk["regime"] = "bear"
        elif rg.startswith("crisis"):
            mk["regime"] = "crisis"
        else:
            mk["regime"] = "range"
    if str(mk.get("timing_stance") or "") not in VALID_TIMING_STANCES:
        mk["timing_stance"] = "hold"
    today_target = _normalize_pct_dict(mk.get("today_target") if isinstance(mk.get("today_target"), dict) else {})
    central_baseline = _normalize_pct_dict(mk.get("central_baseline") if isinstance(mk.get("central_baseline"), dict) else (today_target.copy()))
    if paradigm_active in ("B1", "B2"):
        fi = mk.get("focus_industries")
        if not isinstance(fi, list) or not fi:
            mk["focus_industries"] = ["综合"]

    # ③ selection — 先把 funds 清洗 + 重分类，再按 today_target 缩放
    idx_codes = _index_fund_codes(db_path)
    sel = fms["selection"]
    raw_funds = sel.get("funds") if isinstance(sel.get("funds"), list) else []
    clean_funds: list[dict] = []
    seen_codes: set[str] = set()
    for f in raw_funds:
        if not isinstance(f, dict):
            continue
        code = str(f.get("fund_code") or "").strip()
        if not code or code in seen_codes or code not in idx_codes:
            continue  # 非指数型 / 缺 code / 重复 → 剔除（'CASH' 这类也在此被剔）
        seen_codes.add(code)
        if f.get("asset_class") not in ("股票类", "债券类", "黄金类"):
            f["asset_class"] = _fund_asset_class(db_path, code)
        if f.get("role") not in VALID_ROLES:
            f["role"] = "卫星增强"
        perf = f.get("performance") if isinstance(f.get("performance"), dict) else {}
        for p in REQUIRED_PERFORMANCE_PERIODS:
            pp = perf.get(p) if isinstance(perf.get(p), dict) else {}
            if not isinstance(pp.get("return_pct"), (int, float)):
                pp["return_pct"] = 0.0
            perf[p] = pp
        f["performance"] = perf
        if not isinstance(f.get("target_weight"), (int, float)):
            f["target_weight"] = 0.0
        f.setdefault("fund_name", code)
        f.setdefault("thesis", "系统机械收口占位（选基逻辑见正文）")
        clean_funds.append(f)
    if not clean_funds:
        return False, ["reconcile: selection 里没有任何合法指数型基金，无法机械收口（需要 bot 重选）"]

    by_class = {c: [f for f in clean_funds if f["asset_class"] == c] for c in ("股票类", "债券类", "黄金类")}
    pct_key = {"股票类": "equity_pct", "债券类": "bond_pct", "黄金类": "gold_pct"}
    # 某 asset_class 无基金但 today_target 给了权重 → 把那份权重挪到 cash
    for c, funds_c in by_class.items():
        if not funds_c and today_target[pct_key[c]] > 0:
            today_target["cash_pct"] = round(today_target["cash_pct"] + today_target[pct_key[c]], 1)
            today_target[pct_key[c]] = 0.0
    # 投资比例 < 50% 会被 "target_weight 合计偏低" 拒 → 从 cash 抽到 50%
    invested = today_target["equity_pct"] + today_target["bond_pct"] + today_target["gold_pct"]
    if invested < 50.0 and today_target["cash_pct"] > 0:
        need = min(today_target["cash_pct"], 50.0 - invested)
        # 优先补到有基金的大类（按现有占比，没占比就均分）
        targets = {c: today_target[pct_key[c]] for c, fc in by_class.items() if fc}
        if targets:
            base = sum(targets.values()) or 1.0
            for c in targets:
                add = need * (targets[c] / base) if base else need / len(targets)
                today_target[pct_key[c]] = round(today_target[pct_key[c]] + add, 1)
            today_target["cash_pct"] = round(today_target["cash_pct"] - need, 1)
    today_target = _normalize_pct_dict(today_target)
    # 按各大类的 today_target 把 target_weight 缩放对齐（小数 0~1）
    for c, funds_c in by_class.items():
        _round_weights_to_sum(funds_c, today_target[pct_key[c]] / 100.0)
    # 现金类/其它（清洗时已重分类掉，理论不会有）— 兜底再清一遍
    sel["funds"] = [f for f in clean_funds if f["asset_class"] in ("股票类", "债券类", "黄金类")]
    if not isinstance(sel.get("funnel"), dict):
        sel["funnel"] = {}
    for fk, dv in (("layer1_count", 50), ("layer2_count", 20), ("layer3_count", 10), ("layer4_count", len(sel["funds"]))):
        if not isinstance(sel["funnel"].get(fk), int):
            sel["funnel"][fk] = dv
    mk["today_target"] = today_target
    mk["central_baseline"] = central_baseline

    # ④ holdings — 用 DB 真实持仓重建
    state = _read_db_state(db_path, bot_id)
    if state is None:
        return False, [f"reconcile: bot {bot_id} 在 fund_bot_accounts 不存在，无法机械收口"]
    db_cash = round(state["cash"], 2)
    db_holdings = state["holdings_by_code"]
    mv_sum = round(sum(float(h.get("market_value") or 0) for h in db_holdings.values()), 2)
    total_value = round(db_cash + mv_sum, 2)
    hd = fms["holdings"]
    hd["account"] = {"cash": db_cash, "total_value": total_value}
    hd["holdings"] = [
        {
            "fund_code": code,
            "fund_name": h.get("fund_name") or code,
            "market_value": round(float(h.get("market_value") or 0), 2),
            "weight": round(float(h.get("market_value") or 0) / total_value, 6) if total_value > 0 else 0.0,
            "actual_weight": round(float(h.get("market_value") or 0) / total_value, 6) if total_value > 0 else 0.0,
        }
        for code, h in sorted(db_holdings.items())
    ]

    # ⑤ review — decision/action_type 落白名单；ADD 封顶可用现金、卖出封顶当前市值；覆盖所有 active 持仓
    rv = fms["review"]
    decision = str(rv.get("decision") or "").upper()
    raw_actions = rv.get("actions") if isinstance(rv.get("actions"), list) else []
    actions: list[dict] = []
    for a in raw_actions:
        if not isinstance(a, dict):
            continue
        code = str(a.get("fund_code") or "").strip()
        if not code:
            continue
        atype = str(a.get("action_type") or "HOLD").upper()
        if atype not in VALID_ACTION_TYPES:
            atype = "HOLD"
        if atype == "ADD" and code not in idx_codes:
            continue  # 不能加仓非指数型
        if atype != "ADD" and code not in db_holdings:
            continue  # 非持有的不能减/止盈/止损/HOLD
        amount = float(a.get("amount") or 0) if _is_number(a.get("amount")) else 0.0
        if atype == "HOLD":
            amount = 0.0
        elif atype in ("REDUCE", "TAKE_PROFIT", "STOP_LOSS"):
            cur_mv = float(db_holdings.get(code, {}).get("market_value") or 0)
            amount = min(abs(amount), cur_mv)  # 卖出不超过当前市值
        else:  # ADD
            amount = abs(amount)
        na = {k: a.get(k) for k in ("timing_state", "momentum_state", "matrix_suggestion", "final_decision", "reason", "trigger") if a.get(k) is not None}
        na.update({"fund_code": code, "action_type": atype, "amount": round(amount, 2)})
        na.setdefault("reason", "")
        na.setdefault("final_decision", atype)
        actions.append(na)
    # 覆盖所有 active 持仓：缺的补 HOLD
    covered = {a["fund_code"] for a in actions}
    for code in db_holdings:
        if code not in covered:
            actions.append({"fund_code": code, "action_type": "HOLD", "amount": 0.0,
                            "final_decision": "HOLD", "reason": "system_reconcile_hold"})
    # ADD 总额封顶到可用现金（留 1% buffer）
    add_actions = [a for a in actions if a["action_type"] == "ADD"]
    add_total = sum(a["amount"] for a in add_actions)
    if add_total > db_cash * 0.999 and add_total > 0:
        scale = (db_cash * 0.99) / add_total
        for a in add_actions:
            a["amount"] = round(a["amount"] * scale, 2)
    # KEEP 时不允许非 HOLD 非零动作
    material = any(a["action_type"] != "HOLD" and abs(a["amount"]) > 0.01 for a in actions)
    if decision not in VALID_DECISIONS:
        decision = "REBALANCE" if material else "KEEP"
    if decision == "KEEP" and material:
        for a in actions:
            a["action_type"] = "HOLD"
            a["amount"] = 0.0
            a["final_decision"] = "HOLD"
        material = False
    rv["decision"] = decision
    rv["actions"] = actions
    turnover_amount = round(sum(abs(a["amount"]) for a in actions if a["action_type"] != "HOLD"), 2)
    rv["turnover_amount"] = 0.0 if decision == "KEEP" else turnover_amount
    rv["turnover_ratio"] = 0.0 if (decision == "KEEP" or total_value <= 0) else round(turnover_amount / total_value * 100, 4)
    rv["account_after"] = {"cash": db_cash, "portfolio_value": total_value}

    # 写回 5 份 MD
    for key in fms:
        paths[key].write_text(_dump_frontmatter_text(fms[key], bodies[key]), encoding="utf-8")

    # 重新校验
    return validate_bot_md(bot_id=bot_id, run_id=run_id, trade_date=trade_date,
                           paradigm_active=paradigm_active, db_path=db_path)


# ============================================================
# Main entry
# ============================================================

def process_bot(
    bot_id: str,
    run_id: str,
    trade_date: str,
    paradigm_active: str,
    mcp_call_fn: Callable[..., dict],
    mcp_url: str,
    db_path: str = "/home/rooot/agent_invest_lab/data/fund.db",
) -> tuple[bool, list[str]]:
    """读 5 份 MD，按顺序调 MCP 写库。任一步失败立即返回 (False, issues)。

    mcp_call_fn 签名: (url, tool_name, arguments, **kwargs) -> dict
    paradigm_active: A | B1 | B2 | C（必须，与 fund_paradigm_runs 一致）
    """
    issues: list[str] = []

    if paradigm_active == "SKIP":
        return False, ["paradigm_active=SKIP 的 bot 不应进入 process_bot"]

    # 1. 读 + parse 5 份 MD —— 全部必填（投资框架.md 与能力圈宣告.md 是基金投资链路的硬约束，
    #   缺任何一份都拒绝落库。能力圈宣告.md 由 Phase B-1 在 cron 上游已强制存在；
    #   投资框架.md 是 bot 在每轮决策前必须重申的三层结构自审）。
    framework_fm, framework_body, _ = _parse_or_fail(bot_id, "framework", issues)
    market_fm, market_body, _ = _parse_or_fail(bot_id, "market", issues)
    selection_fm, selection_body, _ = _parse_or_fail(bot_id, "selection", issues)
    holdings_fm, holdings_body, _ = _parse_or_fail(bot_id, "holdings", issues)
    review_fm, review_body, _ = _parse_or_fail(bot_id, "review", issues)

    if issues:
        return False, issues

    ok, issues = validate_bot_md(
        bot_id=bot_id,
        run_id=run_id,
        trade_date=trade_date,
        paradigm_active=paradigm_active,
        db_path=db_path,
    )
    if not ok:
        return False, issues

    def _rollback(extra_msg: str) -> tuple[bool, list[str]]:
        """中途失败 → 调 MCP rollback_fund_bot_run 清掉本轮半成品。"""
        rb = mcp_call_fn(mcp_url, "rollback_fund_bot_run",
                         {"bot_id": bot_id, "trade_date": trade_date, "run_id": run_id})
        rb_msg = f"rollback: {rb.get('deleted')}" if _mcp_succeeded(rb) else f"rollback 失败: {rb}"
        return False, [extra_msg, rb_msg]

    # 3. save_allocation_run（市场环境，含 paradigm）
    alloc_args = build_allocation_args(market_fm, market_body, run_id, trade_date,
                                       bot_id, paradigm_active)
    alloc_res = mcp_call_fn(mcp_url, "save_allocation_run", alloc_args)
    if not _mcp_succeeded(alloc_res):
        return _rollback(f"save_allocation_run failed: {alloc_res}")

    # 4. save_selection_run（选基）
    sel_args = build_selection_args(selection_fm, selection_body, run_id, trade_date,
                                    bot_id)
    sel_res = mcp_call_fn(mcp_url, "save_selection_run", sel_args)
    if not _mcp_succeeded(sel_res):
        return _rollback(f"save_selection_run failed: {sel_res}")

    # 5. apply_fund_review_and_rebalance（写巡检 + 把动作转 T 日 pending 在途单 + 更新持仓元数据，含 paradigm）
    #    现金/持仓/收益的实际变动由 settle_pending_fund_orders 在 T+1 收口；bot 的数值字段仅供自检。
    review_args = build_review_args(review_fm, review_body, holdings_fm,
                                    run_id, trade_date, bot_id, paradigm_active,
                                    selection_fm=selection_fm,
                                    market_fm=market_fm,
                                    db_path=db_path)
    review_res = mcp_call_fn(mcp_url, "apply_fund_review_and_rebalance", review_args)
    if not _mcp_succeeded(review_res):
        return _rollback(f"apply_fund_review_and_rebalance failed: {review_res}")

    return True, issues
