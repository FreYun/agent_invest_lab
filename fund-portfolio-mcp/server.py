"""
fund-portfolio-mcp — 基金直投组合管理 MCP 服务

基金数据侧（刷新脚本写入，bot 只读）:
  fund_info, fund_nav, fund_performance, fund_style, fund_industry, fund_top_stocks

Bot 执行侧（MCP 读写）:
  fund_bot_accounts, fund_bot_holdings, fund_bot_orders, fund_bot_reviews,
  fund_bot_actions, fund_bot_daily_snapshots, fund_bot_position_snapshots

系统表:
  fund_system_runs, fund_allocation_runs, fund_selection_runs

功能模块:
  A. 基金数据查询 — get_fund_pool, get_fund_detail, get_fund_perf
  B. Bot 持仓管理 — init_fund_account, get_fund_holdings, save_fund_holdings
  C. 订单管理     — create_fund_orders, confirm_fund_orders, get_pending_orders
  D. 巡检调仓     — save_fund_review, save_fund_actions, get_fund_review_history,
                    apply_fund_review_and_rebalance
  E. 每日快照     — record_fund_snapshot, get_fund_curve, get_fund_position_snapshots
  F. 执行状态     — save_system_run, save_allocation_run, get_latest_allocation_run,
                    save_selection_run, get_latest_selection_run
  G. 数据更新     — upsert_fund_info, upsert_fund_nav, upsert_fund_performance,
                    upsert_fund_style, upsert_fund_industry, upsert_fund_top_stocks

启动:
  python3 server.py --transport streamable-http --port 18071
"""

import argparse
import json
import os
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from db import get_conn, init_db

# 只读模式：bot 调写工具会得到 "tool not found"。由环境变量控制，必须在 import 时就生效，
# 因为 @mcp.tool() 装饰器在 module load 时执行。
# 启动只读端: FUND_MCP_READONLY=1 python3 server.py --port 18071
# 启动 admin 端: python3 server.py --port 18073
READONLY = os.getenv("FUND_MCP_READONLY", "0") == "1"

mcp = FastMCP(
    "fund-portfolio" + (" (readonly)" if READONLY else ""),
    instructions=(
        "基金直投组合管理服务。提供基金数据查询、bot 持仓管理、订单管理（T+1）、"
        "巡检调仓记录、每日快照追踪、选品漏斗追踪等工具。"
        "所有 bot 共用同一个数据库，通过 bot_id 区分。"
        + ("【当前为 READONLY 模式：写工具未注册，bot 写库请走系统层 fund_md_to_db。】" if READONLY else "")
    ),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def writer_tool(func):
    """用在写工具上：READONLY 模式下不注册到 MCP，bot 调用会得到 tool not found。

    与 @mcp.tool() 等效但带模式开关。
    """
    if READONLY:
        return func
    return mcp.tool()(func)


# ============================================================
# 辅助函数
# ============================================================

def _get_nav(conn, fund_code: str, nav_date: str = "") -> tuple[float, bool]:
    if not nav_date:
        row = conn.execute(
            "SELECT nav FROM fund_nav WHERE fund_code = ? ORDER BY nav_date DESC LIMIT 1",
            (fund_code,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT nav FROM fund_nav WHERE fund_code = ? AND nav_date <= ? "
            "ORDER BY nav_date DESC LIMIT 1",
            (fund_code, nav_date)
        ).fetchone()
    if row and row["nav"]:
        return row["nav"], True
    return 1.0, False


def _get_account(conn, bot_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM fund_bot_accounts WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    return dict(row) if row else None


def _effective_action_shares(
    conn,
    fund_code: str,
    amount: float,
    shares: float,
    nav_used: float,
    fee: float,
    action_date: str,
    trade_date: str,
    action_type: str,
) -> tuple[float, float]:
    """回放 fund_bot_actions 时，统一按当日复权净值重建份额。

    buy:
      shares = (amount - fee) / real_nav
    sell:
      shares = amount / real_nav   # amount 在 action 表里存的是赎回 gross

    若拿不到 real_nav，再回退到 action 表原始 shares/nav_used。
    """
    amount = float(amount or 0.0)
    shares = float(shares or 0.0)
    nav_used = float(nav_used or 0.0)
    fee = float(fee or 0.0)
    real_nav, ok = _get_nav(conn, fund_code, action_date or trade_date)
    real_nav = float(real_nav or 0.0)

    if amount <= 0 or not ok or real_nav <= 0:
        return shares, nav_used

    atype = (action_type or "").strip().upper()
    if atype in _BUY_ACTION_TYPES:
        net_amount = max(amount - fee, 0.0)
        rebuilt = net_amount / real_nav if net_amount > 0 else 0.0
        return (rebuilt if rebuilt > 0 else shares), real_nav
    if atype in _SELL_ACTION_TYPES:
        rebuilt = amount / real_nav
        return (rebuilt if rebuilt > 0 else shares), real_nav
    return shares, real_nav


def _r(val, n=2):
    return round(val, n) if val is not None else None


def _normalize_trade_date(trade_date: str = "") -> str:
    return trade_date or datetime.now().strftime("%Y-%m-%d")


def _calc_max_drawdown(nav_list: list[float]) -> float:
    if not nav_list:
        return 0.0
    peak = nav_list[0]
    max_dd = 0.0
    for nav in nav_list:
        if nav > peak:
            peak = nav
        dd = (nav - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
    return max_dd


_BUY_ACTION_TYPES = {"ADD", "BUY", "INCREASE", "INIT"}
_SELL_ACTION_TYPES = {"REDUCE", "SELL", "DECREASE", "TAKE_PROFIT", "STOP_LOSS", "EXIT"}


def _material_action_count(actions: list[dict]) -> int:
    count = 0
    for action in actions:
        action_type = (action.get("action_type") or "").strip().upper()
        amount = float(action.get("amount") or 0.0)
        if action_type in _BUY_ACTION_TYPES or action_type in _SELL_ACTION_TYPES:
            if amount > 0.01:
                count += 1
    return count


def _holding_rows_by_code(conn, bot_id: str) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT * FROM fund_bot_holdings WHERE bot_id=? "
        "ORDER BY COALESCE(entry_date, ''), COALESCE(exit_date, ''), holding_id",
        (bot_id,),
    ).fetchall()
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row["fund_code"], []).append(dict(row))
    return result


def _select_holding_metadata(rows_by_code: dict[str, list[dict]], fund_code: str, as_of_date: str) -> dict:
    rows = rows_by_code.get(fund_code) or []
    if not rows:
        return {}

    active_on_date = []
    started_rows = []
    for row in rows:
        entry_date = (row.get("entry_date") or "")[:10]
        exit_date = (row.get("exit_date") or "")[:10]
        if entry_date and entry_date <= as_of_date:
            started_rows.append(row)
            if not exit_date or exit_date >= as_of_date:
                active_on_date.append(row)

    if active_on_date:
        return active_on_date[-1]
    if started_rows:
        return started_rows[-1]
    return rows[-1]


def _fund_info_defaults(conn, fund_code: str) -> dict:
    row = conn.execute(
        "SELECT fund_name, share_class, fund_type FROM fund_info WHERE fund_code=?",
        (fund_code,),
    ).fetchone()
    if not row:
        return {}
    fund_type = (row["fund_type"] or "").strip()
    asset_class = ""
    if fund_type.startswith("债"):
        asset_class = "债券类"
    elif "黄金" in fund_type:
        asset_class = "黄金类"
    elif fund_type:
        asset_class = "股票类"
    return {
        "fund_name": row["fund_name"] or fund_code,
        "share_class": row["share_class"] or "",
        "asset_class": asset_class,
    }


def _replay_fund_account_state(conn, bot_id: str, trade_date: str) -> dict:
    """按截至 trade_date 的已确认动作重建账户状态。

    返回:
      {
        "cash": float,
        "positions": {fund_code: {...active position...}},
        "last_exit_dates": {fund_code: "YYYY-MM-DD"},
      }
    """
    account = _get_account(conn, bot_id)
    if not account:
        return {"cash": 0.0, "positions": {}, "last_exit_dates": {}}

    rows_by_code = _holding_rows_by_code(conn, bot_id)
    initial_capital = float(account["initial_capital"] or 0.0)
    cash = initial_capital
    positions: dict[str, dict] = {}
    last_exit_dates: dict[str, str] = {}

    actions = conn.execute(
        "SELECT fund_code, action_type, amount, shares, nav_used, fee, action_date "
        "FROM fund_bot_actions "
        "WHERE bot_id=? AND fund_code IS NOT NULL AND fund_code != '' "
        "  AND action_date IS NOT NULL AND action_date <= ? "
        "ORDER BY action_date, action_id",
        (bot_id, trade_date),
    ).fetchall()

    for action in actions:
        fund_code = action["fund_code"]
        action_date = (action["action_date"] or trade_date)[:10]
        action_type = (action["action_type"] or "").strip().upper()
        amount = float(action["amount"] or 0.0)
        fee = float(action["fee"] or 0.0)
        shares, nav_used = _effective_action_shares(
            conn,
            fund_code,
            amount,
            action["shares"],
            action["nav_used"],
            fee,
            action_date,
            trade_date,
            action_type,
        )
        shares = float(shares or 0.0)
        nav_used = float(nav_used or 0.0)
        meta = _select_holding_metadata(rows_by_code, fund_code, action_date)
        if not meta:
            meta = _fund_info_defaults(conn, fund_code)

        if action_type in _BUY_ACTION_TYPES:
            if amount > 0:
                cash -= amount
            if shares <= 0:
                continue
            current = positions.get(fund_code)
            if not current or current["shares"] <= 1e-6:
                current = {
                    "fund_code": fund_code,
                    "fund_name": meta.get("fund_name") or fund_code,
                    "share_class": meta.get("share_class") or "",
                    "asset_class": meta.get("asset_class") or "",
                    "role": meta.get("role") or "",
                    "thesis": meta.get("thesis") or "",
                    "entry_date": action_date,
                    "entry_nav": nav_used if nav_used > 0 else _get_nav(conn, fund_code, action_date)[0],
                    "shares": 0.0,
                    "cost": 0.0,
                }
                positions[fund_code] = current
            else:
                for key in ("fund_name", "share_class", "asset_class", "role", "thesis"):
                    if not current.get(key) and meta.get(key):
                        current[key] = meta.get(key)
            current["shares"] += shares
            current["cost"] += amount if amount > 0 else shares * nav_used
        elif action_type in _SELL_ACTION_TYPES:
            if amount > 0:
                cash += max(amount - fee, 0.0)
            current = positions.get(fund_code)
            if not current or current["shares"] <= 1e-6:
                continue
            sell_shares = min(shares, current["shares"]) if shares > 0 else 0.0
            if sell_shares <= 1e-6:
                continue
            shares_before = current["shares"]
            proportion = min(sell_shares / shares_before, 1.0)
            current["cost"] -= current["cost"] * proportion
            current["shares"] -= sell_shares
            if current["shares"] <= 1e-6:
                current["shares"] = 0.0
                current["cost"] = 0.0
                last_exit_dates[fund_code] = action_date

    active_positions = {}
    for fund_code, current in positions.items():
        if current["shares"] > 1e-6:
            active_positions[fund_code] = current

    return {"cash": cash, "positions": active_positions, "last_exit_dates": last_exit_dates}


def _replay_and_repair_fund_holdings(conn, bot_id: str, trade_date: str) -> int:
    """按截至 trade_date 的 action 历史重建账户，并回写当前 active holdings / cash。

    口径：
    - 买入（ADD/BUY/INCREASE/INIT）: shares += (amount-fee)/real_nav, cash -= amount
    - 卖出（REDUCE/SELL/DECREASE/TAKE_PROFIT/STOP_LOSS/EXIT）:
      shares -= amount/real_nav, cash += amount-fee
    - 成本按卖出份额占 shares_before 的比例扣减

    Returns: 修复后的 active 持仓数量
    """
    state = _replay_fund_account_state(conn, bot_id, trade_date)
    positions = state["positions"]
    last_exit_dates = state["last_exit_dates"]
    cash_v = float(state["cash"] or 0.0)

    conn.execute(
        "UPDATE fund_bot_accounts SET cash=?, updated_at=datetime('now') WHERE bot_id=?",
        (_r(cash_v), bot_id),
    )

    active_rows = conn.execute(
        "SELECT * FROM fund_bot_holdings WHERE bot_id=? AND status='active'",
        (bot_id,),
    ).fetchall()
    active_by_code = {row["fund_code"]: dict(row) for row in active_rows}

    total_market_value = 0.0
    position_payloads: dict[str, dict] = {}
    for fund_code, current in positions.items():
        nav_v, _ = _get_nav(conn, fund_code, trade_date)
        nav_v = float(nav_v or current.get("entry_nav") or 1.0)
        market_value = current["shares"] * nav_v
        pnl = market_value - current["cost"]
        pnl_pct = pnl / current["cost"] * 100 if current["cost"] else 0.0
        holding_days = _calc_holding_days(current.get("entry_date") or trade_date, trade_date)
        position_payloads[fund_code] = {
            "entry_date": current.get("entry_date") or trade_date,
            "entry_nav": float(current.get("entry_nav") or nav_v or 1.0),
            "latest_nav": nav_v,
            "shares": current["shares"],
            "amount_invested": current["cost"],
            "market_value": market_value,
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": pnl_pct,
            "holding_days": holding_days,
            "fund_name": current.get("fund_name") or fund_code,
            "share_class": current.get("share_class") or "",
            "asset_class": current.get("asset_class") or "",
            "role": current.get("role") or "",
            "thesis": current.get("thesis") or "",
            "high_nav": max(float((active_by_code.get(fund_code) or {}).get("high_nav") or 0.0), nav_v),
        }
        total_market_value += market_value

    total_value = cash_v + total_market_value

    repaired = 0
    for fund_code, payload in position_payloads.items():
        actual_weight = payload["market_value"] / total_value if total_value else 0.0
        row = active_by_code.get(fund_code)
        if row:
            conn.execute(
                "UPDATE fund_bot_holdings SET entry_date=?, exit_date=NULL, entry_nav=?, latest_nav=?, "
                "shares=?, amount_invested=?, market_value=?, unrealized_pnl=?, unrealized_pnl_pct=?, "
                "actual_weight=?, holding_days=?, high_nav=?, fund_name=COALESCE(?, fund_name), "
                "share_class=COALESCE(?, share_class), asset_class=COALESCE(?, asset_class), "
                "role=COALESCE(?, role), thesis=COALESCE(?, thesis) "
                "WHERE holding_id=?",
                (
                    payload["entry_date"], _r(payload["entry_nav"], 6), _r(payload["latest_nav"], 6),
                    _r(payload["shares"], 6), _r(payload["amount_invested"]), _r(payload["market_value"]),
                    _r(payload["unrealized_pnl"]), _r(payload["unrealized_pnl_pct"], 4),
                    _r(actual_weight, 6), payload["holding_days"], _r(payload["high_nav"], 6),
                    payload["fund_name"], payload["share_class"], payload["asset_class"],
                    payload["role"], payload["thesis"], row["holding_id"],
                ),
            )
        else:
            conn.execute(
                "INSERT INTO fund_bot_holdings "
                "(bot_id, fund_code, fund_name, share_class, asset_class, role, "
                "entry_date, entry_nav, latest_nav, shares, amount_invested, "
                "market_value, unrealized_pnl, unrealized_pnl_pct, "
                "target_weight, actual_weight, holding_days, high_nav, status, thesis) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'active', ?)",
                (
                    bot_id, fund_code, payload["fund_name"], payload["share_class"], payload["asset_class"],
                    payload["role"], payload["entry_date"], _r(payload["entry_nav"], 6),
                    _r(payload["latest_nav"], 6), _r(payload["shares"], 6), _r(payload["amount_invested"]),
                    _r(payload["market_value"]), _r(payload["unrealized_pnl"]),
                    _r(payload["unrealized_pnl_pct"], 4), _r(actual_weight, 6),
                    payload["holding_days"], _r(payload["high_nav"], 6), payload["thesis"],
                ),
            )
        repaired += 1

    for fund_code, row in active_by_code.items():
        if fund_code in position_payloads:
            continue
        exit_date = last_exit_dates.get(fund_code) or trade_date
        conn.execute(
            "UPDATE fund_bot_holdings SET status='closed', exit_date=?, shares=0, amount_invested=0, "
            "market_value=0, unrealized_pnl=0, unrealized_pnl_pct=0, actual_weight=0, holding_days=? "
            "WHERE holding_id=?",
            (exit_date, _calc_holding_days(row.get("entry_date") or exit_date, exit_date), row["holding_id"]),
        )

    return repaired


def _calc_holding_days(entry_date: str, as_of: str) -> int:
    try:
        d1 = datetime.strptime(entry_date[:10], "%Y-%m-%d")
        d2 = datetime.strptime(as_of[:10], "%Y-%m-%d")
        return (d2 - d1).days
    except (ValueError, TypeError):
        return 0


# ============================================================
#  交易日历 + 费率（T+1 收口结算用）
# ============================================================

def _next_trading_day(conn, date: str) -> str | None:
    """交易日历用 fund_nav 里出现过的 nav_date 集合近似。返回严格大于 date 的最近一个交易日；没有则 None。"""
    row = conn.execute(
        "SELECT MIN(nav_date) AS d FROM fund_nav WHERE nav_date > ?",
        (date,),
    ).fetchone()
    return row["d"] if row and row["d"] else None


def _fund_fee_rates(conn, fund_code: str) -> tuple[float, list[dict]]:
    """返回 (申购费率小数, 赎回费阶梯list)。阶梯元素 {"max_days": int|None, "rate": float}。
    申购费率：fund_info.purchase_fee 是百分比数（0.08 = 0.08%），转成小数 0.0008。
    赎回费阶梯：fund_info.redeem_fee_json；缺失则用监管标准默认。"""
    row = conn.execute(
        "SELECT purchase_fee, redeem_fee_json FROM fund_info WHERE fund_code=?",
        (fund_code,),
    ).fetchone()
    pf_pct = 0.0
    tiers: list[dict] = [
        {"max_days": 7, "rate": 0.015},
        {"max_days": 30, "rate": 0.005},
        {"max_days": None, "rate": 0.0},
    ]
    if row:
        try:
            pf_pct = float(row["purchase_fee"] or 0.0)
        except (TypeError, ValueError):
            pf_pct = 0.0
        if row["redeem_fee_json"]:
            try:
                parsed = json.loads(row["redeem_fee_json"])
                if isinstance(parsed, list) and parsed:
                    tiers = parsed
            except Exception:
                pass
    return pf_pct / 100.0, tiers


def _redeem_fee_rate(tiers: list[dict], holding_days: int) -> float:
    """按持有天数从阶梯表取赎回费率（小数）。tiers 按 max_days 升序，None 表示无上限。"""
    for t in tiers:
        md = t.get("max_days")
        if md is None or holding_days < md:
            try:
                return float(t.get("rate") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


# ============================================================
# A. 基金数据查询（读 fund_info / fund_nav / fund_performance）
# ============================================================

@mcp.tool()
async def get_fund_pool(fund_type: str = "", top_n: int = 500) -> str:
    """获取基金池。可按 fund_type 过滤（股票型/混合型/债券型/指数型）。
    返回基金基本信息、申购状态、规模、费率。"""
    with get_conn() as conn:
        if fund_type:
            rows = conn.execute(
                "SELECT fund_code, fund_name, fund_company, fund_manager, fund_type, "
                "share_class, scale, purchase_status, mgmt_fee, purchase_fee "
                "FROM fund_info WHERE fund_type = ? ORDER BY scale DESC LIMIT ?",
                (fund_type, top_n)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT fund_code, fund_name, fund_company, fund_manager, fund_type, "
                "share_class, scale, purchase_status, mgmt_fee, purchase_fee "
                "FROM fund_info ORDER BY scale DESC LIMIT ?",
                (top_n,)
            ).fetchall()

        funds = [dict(r) for r in rows]
        return json.dumps({
            "success": True, "count": len(funds), "funds": funds,
        }, ensure_ascii=False)


@mcp.tool()
async def get_fund_detail(fund_code: str) -> str:
    """获取单只基金详情：基本信息 + 最新净值 + 最新业绩 + 风格 + 行业持仓 + 重仓股。"""
    with get_conn() as conn:
        info = conn.execute("SELECT * FROM fund_info WHERE fund_code = ?", (fund_code,)).fetchone()
        if not info:
            return json.dumps({"success": False, "message": f"基金 {fund_code} 不存在"}, ensure_ascii=False)

        result = dict(info)

        nav_row = conn.execute(
            "SELECT nav_date, nav, acc_nav, daily_return_pct FROM fund_nav "
            "WHERE fund_code = ? ORDER BY nav_date DESC LIMIT 1",
            (fund_code,)
        ).fetchone()
        if nav_row:
            result["latest_nav"] = dict(nav_row)

        perf_rows = conn.execute(
            "SELECT period, return_pct, rank_pct, rank_text, max_drawdown_pct, "
            "volatility_pct, sharpe_ratio, calmar_ratio, as_of_date "
            "FROM fund_performance WHERE fund_code = ? "
            "ORDER BY as_of_date DESC, period",
            (fund_code,)
        ).fetchall()
        if perf_rows:
            latest_date = perf_rows[0]["as_of_date"]
            result["performance"] = [dict(r) for r in perf_rows if r["as_of_date"] == latest_date]

        style_row = conn.execute(
            "SELECT * FROM fund_style WHERE fund_code = ? ORDER BY as_of_date DESC LIMIT 1",
            (fund_code,)
        ).fetchone()
        if style_row:
            result["style"] = dict(style_row)

        industry_rows = conn.execute(
            "SELECT industry, weight_pct FROM fund_industry "
            "WHERE fund_code = ? ORDER BY as_of_date DESC, weight_pct DESC",
            (fund_code,)
        ).fetchall()
        if industry_rows:
            latest_date_ind = conn.execute(
                "SELECT MAX(as_of_date) as d FROM fund_industry WHERE fund_code = ?",
                (fund_code,)
            ).fetchone()["d"]
            result["industry"] = [dict(r) for r in conn.execute(
                "SELECT industry, weight_pct FROM fund_industry "
                "WHERE fund_code = ? AND as_of_date = ? ORDER BY weight_pct DESC",
                (fund_code, latest_date_ind)
            ).fetchall()]

        top_stocks = conn.execute(
            "SELECT stock_rank, stock_code, stock_name, weight_pct, as_of_date "
            "FROM fund_top_stocks WHERE fund_code = ? "
            "ORDER BY as_of_date DESC, stock_rank LIMIT 10",
            (fund_code,)
        ).fetchall()
        if top_stocks:
            result["top_stocks"] = [dict(r) for r in top_stocks]

        return json.dumps({"success": True, "data": result}, ensure_ascii=False)


@mcp.tool()
async def get_fund_perf(fund_code: str) -> str:
    """获取基金所有区间的绩效数据。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT as_of_date, period, return_pct, rank_pct, rank_text, "
            "max_drawdown_pct, volatility_pct, sharpe_ratio, calmar_ratio "
            "FROM fund_performance WHERE fund_code = ? ORDER BY as_of_date DESC",
            (fund_code,)
        ).fetchall()

        if not rows:
            return json.dumps({"success": False, "message": f"基金 {fund_code} 无绩效数据"}, ensure_ascii=False)

        latest_date = rows[0]["as_of_date"]
        intervals = [dict(r) for r in rows if r["as_of_date"] == latest_date]

        return json.dumps({
            "success": True, "fund_code": fund_code, "intervals": intervals,
        }, ensure_ascii=False)


# ============================================================
# B. Bot 持仓管理
# ============================================================

@writer_tool
async def init_fund_account(bot_id: str, initial_capital: float, allocations_json: str = "[]", entry_date: str = "") -> str:
    """初始化基金账户。可选同时传入初始配置。
    allocations_json: [{"fund_code":"008528","fund_name":"华泰柏瑞质量成长A",
      "share_class":"A","asset_class":"股票类","role":"核心底仓",
      "target_weight":12,"thesis":"理由"}]
    weight 总和 ≤ 100，差额为现金。"""
    with get_conn() as conn:
        existing = _get_account(conn, bot_id)
        if existing:
            return json.dumps({
                "success": False,
                "message": f"bot {bot_id} 已有账户（initial_capital={existing['initial_capital']}），请用 save_fund_holdings 更新",
            }, ensure_ascii=False)

    try:
        allocations = json.loads(allocations_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "allocations_json 格式错误"}, ensure_ascii=False)

    total_weight = sum(a.get("target_weight", 0) for a in allocations)
    if total_weight > 100:
        return json.dumps({"success": False, "message": f"总权重 {total_weight}% 超过 100%"}, ensure_ascii=False)

    entry_date = entry_date or datetime.now().strftime("%Y-%m-%d")
    holdings_created = []

    with get_conn() as conn:
        total_invested = 0.0

        for a in allocations:
            fc = a["fund_code"]
            weight = a.get("target_weight", 0)
            amount = initial_capital * weight / 100

            entry_nav, _ = _get_nav(conn, fc, entry_date)
            shares = amount / entry_nav if entry_nav else 0

            conn.execute(
                "INSERT INTO fund_bot_holdings "
                "(bot_id, fund_code, fund_name, share_class, asset_class, role, "
                "entry_date, entry_nav, latest_nav, shares, amount_invested, "
                "market_value, unrealized_pnl, unrealized_pnl_pct, "
                "target_weight, actual_weight, holding_days, high_nav, status, thesis) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 0, ?, 'active', ?)",
                (
                    bot_id, fc, a.get("fund_name", ""), a.get("share_class", ""),
                    a.get("asset_class", ""), a.get("role", ""),
                    entry_date, entry_nav, entry_nav, shares, amount,
                    amount, weight, weight, entry_nav, a.get("thesis", ""),
                )
            )
            total_invested += amount
            holdings_created.append({
                "fund_code": fc, "amount": _r(amount),
                "shares": _r(shares, 4), "entry_nav": _r(entry_nav, 4),
            })

        cash = initial_capital - total_invested
        conn.execute(
            "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash) VALUES (?, ?, ?)",
            (bot_id, initial_capital, cash)
        )

        cooldown_end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO fund_bot_reviews "
            "(bot_id, review_date, regime, decision, action_count, reason, cooldown_end, "
            "cash_before, cash_after, portfolio_value_before, portfolio_value_after, "
            "turnover_amount, turnover_ratio) "
            "VALUES (?, ?, '', 'INIT', ?, '初始化建仓', ?, ?, ?, 0, ?, ?, ?)",
            (bot_id, entry_date, len(holdings_created), cooldown_end,
             initial_capital, cash, initial_capital,
             total_invested, total_invested / initial_capital * 100 if initial_capital else 0)
        )
        review_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # 写 INIT 动作，让 _replay_and_repair_fund_holdings 有据可回放
        for hc in holdings_created:
            conn.execute(
                "INSERT INTO fund_bot_actions "
                "(review_id, bot_id, fund_code, action_type, nav_used, amount, shares, fee, reason, action_date) "
                "VALUES (?, ?, ?, 'INIT', ?, ?, ?, 0, '初始化建仓', ?)",
                (review_id, bot_id, hc["fund_code"], hc["entry_nav"], hc["amount"], hc["shares"], entry_date)
            )

    return json.dumps({
        "success": True, "bot_id": bot_id,
        "initial_capital": initial_capital, "cash": _r(cash),
        "total_invested": _r(total_invested),
        "holdings_created": len(holdings_created),
        "holdings": holdings_created,
    }, ensure_ascii=False)


@mcp.tool()
async def get_fund_holdings(bot_id: str) -> str:
    """获取 bot 当前基金持仓。返回持仓明细 + 账户概况 + 大类配置。"""
    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)

        rows = conn.execute(
            "SELECT h.*, i.fund_company, i.fund_manager, i.purchase_status "
            "FROM fund_bot_holdings h "
            "LEFT JOIN fund_info i ON h.fund_code = i.fund_code "
            "WHERE h.bot_id = ? AND h.status = 'active' ORDER BY h.market_value DESC",
            (bot_id,)
        ).fetchall()

        holdings = []
        invested_value = 0.0
        asset_weights = {"股票类": 0.0, "债券类": 0.0, "黄金类": 0.0}

        for r in rows:
            mv = r["market_value"] or 0
            invested_value += mv
            h = dict(r)
            holdings.append(h)

        cash = account["cash"]
        total_value = cash + invested_value
        net_value = total_value / account["initial_capital"] if account["initial_capital"] else 1.0

        for h in holdings:
            mv = h["market_value"] or 0
            h["actual_weight"] = _r(mv / total_value * 100 if total_value else 0)
            ac = h.get("asset_class", "")
            if ac in asset_weights:
                asset_weights[ac] += mv

        asset_allocation = {}
        for ac, mv in asset_weights.items():
            asset_allocation[ac] = _r(mv / total_value * 100 if total_value else 0)
        asset_allocation["现金"] = _r(cash / total_value * 100 if total_value else 0)

        last_review = conn.execute(
            "SELECT review_date, decision, reason, cooldown_end FROM fund_bot_reviews "
            "WHERE bot_id = ? ORDER BY review_date DESC LIMIT 1",
            (bot_id,)
        ).fetchone()

        pending_orders = conn.execute(
            "SELECT COUNT(*) as cnt FROM fund_bot_orders "
            "WHERE bot_id = ? AND status = 'pending'",
            (bot_id,)
        ).fetchone()["cnt"]

        return json.dumps({
            "success": True,
            "bot_id": bot_id,
            "initial_capital": account["initial_capital"],
            "cash": _r(cash),
            "invested_value": _r(invested_value),
            "total_value": _r(total_value),
            "net_value": _r(net_value, 6),
            "asset_allocation": asset_allocation,
            "holdings_count": len(holdings),
            "holdings": holdings,
            "pending_orders": pending_orders,
            "last_review": dict(last_review) if last_review else None,
        }, ensure_ascii=False)


@writer_tool
async def save_fund_holdings(bot_id: str, holdings_json: str, cash: float = -1) -> str:
    """增量更新 bot 基金持仓。
    holdings_json: [{"fund_code":"008528","fund_name":"...",
      "shares":1000,"latest_nav":2.5,"market_value":2500,
      "asset_class":"股票类","role":"核心底仓","target_weight":12}]
    不在列表中的活跃持仓会被 close。"""
    try:
        incoming_list = json.loads(holdings_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "holdings_json 格式错误"}, ensure_ascii=False)

    today = datetime.now().strftime("%Y-%m-%d")
    incoming = {h["fund_code"]: h for h in incoming_list}

    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)

        current_rows = conn.execute(
            "SELECT * FROM fund_bot_holdings WHERE bot_id = ? AND status = 'active'",
            (bot_id,)
        ).fetchall()
        current = {r["fund_code"]: dict(r) for r in current_rows}

        closed = updated = inserted = 0

        for fc in current:
            if fc not in incoming:
                conn.execute(
                    "UPDATE fund_bot_holdings SET status = 'closed', exit_date = ? WHERE holding_id = ?",
                    (today, current[fc]["holding_id"])
                )
                closed += 1

        # 金额相关字段不接受 bot 传入；由 _replay_and_repair_fund_holdings 从 fund_bot_actions 回放。
        # 对没有任何 action 历史的纯新建仓（如初始化场景），仍允许 bot 传 shares/amount 占位，
        # 因为此时 actions 表里没东西可回放——但只要后续有 ADD/REDUCE 动作，回放就会接管。
        for fc, h in incoming.items():
            if fc in current:
                conn.execute(
                    "UPDATE fund_bot_holdings SET "
                    "fund_name = COALESCE(?, fund_name), "
                    "target_weight = COALESCE(?, target_weight), "
                    "asset_class = COALESCE(?, asset_class), "
                    "role = COALESCE(?, role), "
                    "holding_days = ?, "
                    "thesis = COALESCE(?, thesis) "
                    "WHERE holding_id = ?",
                    (
                        h.get("fund_name"),
                        h.get("target_weight"),
                        h.get("asset_class"), h.get("role"),
                        _calc_holding_days(current[fc].get("entry_date", today), today),
                        h.get("thesis"),
                        current[fc]["holding_id"],
                    )
                )
                updated += 1
            else:
                entry_nav = h.get("entry_nav") or _get_nav(conn, fc)[0] or 1.0
                amount = float(h.get("amount_invested") or h.get("market_value") or 0)
                shares = h.get("shares") or (amount / entry_nav if entry_nav else 0)
                latest_nav = _get_nav(conn, fc)[0] or entry_nav
                market_value = shares * latest_nav if shares and latest_nav else amount
                conn.execute(
                    "INSERT INTO fund_bot_holdings "
                    "(bot_id, fund_code, fund_name, share_class, asset_class, role, "
                    "entry_date, entry_nav, latest_nav, shares, amount_invested, "
                    "market_value, unrealized_pnl, unrealized_pnl_pct, "
                    "target_weight, actual_weight, holding_days, high_nav, status, thesis) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'active', ?)",
                    (
                        bot_id, fc, h.get("fund_name", ""), h.get("share_class", ""),
                        h.get("asset_class", ""), h.get("role", ""),
                        today, entry_nav, latest_nav,
                        shares, amount, market_value,
                        market_value - amount, ((market_value - amount) / amount * 100) if amount else 0,
                        h.get("target_weight"), h.get("actual_weight"),
                        latest_nav, h.get("thesis", ""),
                    )
                )
                inserted += 1

        if cash >= 0:
            conn.execute(
                "UPDATE fund_bot_accounts SET cash = ?, updated_at = datetime('now') WHERE bot_id = ?",
                (cash, bot_id)
            )

        # 有 action 历史的持仓，统一用回放纠正金额；纯占位的新建仓不受影响（actions 表无记录）
        repaired = _replay_and_repair_fund_holdings(conn, bot_id, today)

    return json.dumps({
        "success": True, "bot_id": bot_id,
        "closed": closed, "updated": updated, "inserted": inserted,
        "holdings_repaired": repaired,
        "cash_updated": cash >= 0, "date": today,
    }, ensure_ascii=False)


# ============================================================
# C0. Bot 自助下单（T+1 pending + 冻结）
#     这 3 个工具用 @mcp.tool() 直接注册（即使在 READONLY 端点也暴露），
#     因为 bot 在 lab 中需要自己按当日净值下单/查看自己的历史。
# ============================================================


def _get_active_holding(conn, bot_id: str, fund_code: str):
    return conn.execute(
        "SELECT * FROM fund_bot_holdings WHERE bot_id=? AND fund_code=? AND status='active'",
        (bot_id, fund_code),
    ).fetchone()


def _strict_nav(conn, fund_code: str, trade_date: str) -> float | None:
    """严格按 (fund_code, trade_date) 取净值；缺失返回 None（不 fallback）。"""
    row = conn.execute(
        "SELECT nav FROM fund_nav WHERE fund_code=? AND nav_date=?",
        (fund_code, trade_date),
    ).fetchone()
    if row and row["nav"] is not None:
        return float(row["nav"])
    return None


@mcp.tool()
async def place_buy_order(
    bot_id: str,
    fund_code: str,
    amount: float,
    trade_date: str,
    reason: str = "",
) -> str:
    """Bot 在 T 日自助下买入单（按当日 NAV，T+1 settle）。

    行为：
      - 用 trade_date 当日的 fund_nav.nav 作 reference_nav 写入订单（之后 settle 用同一净值）
      - 立即从 cash 扣除 amount → 转入 cash_in_transit（冻结，避免重复下单超额）
      - status='pending'，confirm_date=NULL，confirm_nav/confirmed_shares/fee 留空
      - 实际份额到账 + 冻结释放在 settle_pending_fund_orders（外部 loop 在 T+1 触发）

    校验：
      - 账户必须存在
      - fund_code 必须在 fund_info
      - trade_date 必须在 fund_nav 有 nav 行（缺失直接报错，外部 loop 自己保数据齐）
      - amount > 0 且 ≤ accounts.cash（available，不含 in_transit）

    返回 JSON 带 order_id / reference_nav / estimated_fee / estimated_shares / cash_after。
    """
    if amount <= 0:
        return json.dumps({"success": False, "message": f"amount 必须 > 0，传入 {amount}"}, ensure_ascii=False)
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户，先调 init_fund_account"}, ensure_ascii=False)
        info = conn.execute("SELECT fund_name FROM fund_info WHERE fund_code=?", (fund_code,)).fetchone()
        if not info:
            return json.dumps({"success": False, "message": f"基金 {fund_code} 不在 fund_info"}, ensure_ascii=False)
        nav = _strict_nav(conn, fund_code, trade_date)
        if nav is None:
            return json.dumps({"success": False, "message": f"fund_nav 缺失 ({fund_code}, {trade_date})；外部 loop 须保证净值齐全"}, ensure_ascii=False)
        cash = float(account["cash"] or 0.0)
        if amount > cash + 1e-6:
            return json.dumps({"success": False, "message": f"现金不足：amount={amount} > cash={cash:.2f}"}, ensure_ascii=False)

        pf_rate, _ = _fund_fee_rates(conn, fund_code)
        est_fee = amount * pf_rate / (1 + pf_rate)
        est_shares = (amount - est_fee) / nav

        cur = conn.execute(
            "INSERT INTO fund_bot_orders "
            "(review_id, bot_id, fund_code, fund_name, order_type, order_date, confirm_date, "
            " order_amount, reference_nav, action_reason, status) "
            "VALUES (NULL, ?, ?, ?, 'buy', ?, NULL, ?, ?, ?, 'pending')",
            (bot_id, fund_code, info["fund_name"], trade_date, _r(amount), _r(nav, 6), reason or "")
        )
        order_id = cur.lastrowid
        new_cash = cash - amount
        new_in_transit = float(account["cash_in_transit"] or 0.0) + amount
        conn.execute(
            "UPDATE fund_bot_accounts SET cash=?, cash_in_transit=?, updated_at=datetime('now') WHERE bot_id=?",
            (_r(new_cash), _r(new_in_transit), bot_id)
        )

    return json.dumps({
        "success": True,
        "order_id": order_id,
        "bot_id": bot_id,
        "fund_code": fund_code,
        "trade_date": trade_date,
        "reference_nav": _r(nav, 6),
        "amount": _r(amount),
        "estimated_fee": _r(est_fee),
        "estimated_shares": _r(est_shares, 6),
        "status": "pending",
        "cash_after": _r(new_cash),
        "cash_in_transit_after": _r(new_in_transit),
        "note": "T+1 settle 后份额到账、冻结释放；fee/shares 实际值以 settle 时为准（reference_nav 不变）",
    }, ensure_ascii=False)


@mcp.tool()
async def place_sell_order(
    bot_id: str,
    fund_code: str,
    shares: float,
    trade_date: str,
    reason: str = "",
) -> str:
    """Bot 在 T 日自助下卖出单（按当日 NAV，T+1 settle）。

    行为：
      - 用 trade_date 当日 NAV 作 reference_nav；order_amount 字段存"想卖出的份额"
      - 立即把 holding.pending_sell_shares += shares（冻结），份额暂不出账户
      - status='pending'；T+1 settle 时实际扣 shares、加 cash、释放 pending 份额

    校验：
      - 账户存在；持仓存在且 active
      - shares > 0 且 ≤ holding.shares - holding.pending_sell_shares（已冻结的不能再卖）
      - trade_date 在 fund_nav 有 nav

    返回 JSON 带 order_id / reference_nav / estimated_proceeds / estimated_fee。
    """
    if shares <= 0:
        return json.dumps({"success": False, "message": f"shares 必须 > 0，传入 {shares}"}, ensure_ascii=False)
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)
        holding = _get_active_holding(conn, bot_id, fund_code)
        if not holding:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无 {fund_code} 的活跃持仓"}, ensure_ascii=False)
        nav = _strict_nav(conn, fund_code, trade_date)
        if nav is None:
            return json.dumps({"success": False, "message": f"fund_nav 缺失 ({fund_code}, {trade_date})"}, ensure_ascii=False)
        total_shares = float(holding["shares"] or 0.0)
        already_pending = float(holding["pending_sell_shares"] or 0.0)
        sellable = total_shares - already_pending
        if shares > sellable + 1e-6:
            return json.dumps({"success": False, "message": f"可卖份额不足：want={shares} sellable={sellable:.4f} (total={total_shares:.4f}, pending_sell={already_pending:.4f})"}, ensure_ascii=False)

        # 估算赎回费 — 按当前 holding_days + 当前阶梯算（settle 时按真实持有天数重算）
        _, redeem_tiers = _fund_fee_rates(conn, fund_code)
        holding_days = _calc_holding_days(holding["entry_date"] or trade_date, trade_date)
        rf_rate = _redeem_fee_rate(redeem_tiers, holding_days)
        gross = shares * nav
        est_fee = gross * rf_rate
        est_proceeds = gross - est_fee

        cur = conn.execute(
            "INSERT INTO fund_bot_orders "
            "(review_id, bot_id, fund_code, fund_name, order_type, order_date, confirm_date, "
            " order_amount, reference_nav, action_reason, status) "
            "VALUES (NULL, ?, ?, ?, 'sell', ?, NULL, ?, ?, ?, 'pending')",
            (bot_id, fund_code, holding["fund_name"], trade_date, _r(shares, 6), _r(nav, 6), reason or "")
        )
        order_id = cur.lastrowid
        new_pending = already_pending + shares
        conn.execute(
            "UPDATE fund_bot_holdings SET pending_sell_shares=? WHERE holding_id=?",
            (_r(new_pending, 6), holding["holding_id"])
        )

    return json.dumps({
        "success": True,
        "order_id": order_id,
        "bot_id": bot_id,
        "fund_code": fund_code,
        "trade_date": trade_date,
        "reference_nav": _r(nav, 6),
        "shares": _r(shares, 6),
        "estimated_gross": _r(gross),
        "estimated_fee": _r(est_fee),
        "estimated_proceeds": _r(est_proceeds),
        "estimated_fee_rate_pct": round(rf_rate * 100, 4),
        "holding_days_at_order": holding_days,
        "status": "pending",
        "pending_sell_shares_after": _r(new_pending, 6),
        "note": "T+1 settle 后实际扣份额、加 cash；赎回费以真实持有天数重算",
    }, ensure_ascii=False)


@mcp.tool()
async def get_my_history(
    bot_id: str,
    limit: int = 30,
    fund_code: str = "",
) -> str:
    """Bot 查看自己的账户/持仓/订单历史，一次返回 4 块：

      account     当前账户：cash / cash_in_transit / total = cash + in_transit + 持仓市值
      holdings    所有持仓行（active + closed），含 pending_sell_shares
      orders      最近 N 单订单（pending + confirmed + 其他），按 order_date desc
      summary     当前 pending 单计数与冻结金额合计

    传 fund_code 可只看那只基金；不传 = 全部。"""
    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)

        h_args: list = [bot_id]
        h_sql = "SELECT * FROM fund_bot_holdings WHERE bot_id=?"
        if fund_code:
            h_sql += " AND fund_code=?"
            h_args.append(fund_code)
        h_sql += " ORDER BY status, fund_code"
        holdings = [dict(r) for r in conn.execute(h_sql, h_args).fetchall()]

        o_args: list = [bot_id]
        o_sql = "SELECT * FROM fund_bot_orders WHERE bot_id=?"
        if fund_code:
            o_sql += " AND fund_code=?"
            o_args.append(fund_code)
        o_sql += " ORDER BY order_date DESC, order_id DESC LIMIT ?"
        o_args.append(int(max(1, limit)))
        orders = [dict(r) for r in conn.execute(o_sql, o_args).fetchall()]

        # 估值：用每只持仓 latest_nav × shares（含 pending sell 那部分，因为份额没出账）
        market_value = sum(float(h.get("market_value") or 0.0) for h in holdings if h.get("status") == "active")
        cash = float(account["cash"] or 0.0)
        in_transit = float(account.get("cash_in_transit") or 0.0)
        total = cash + in_transit + market_value

        pending = [o for o in orders if o.get("status") == "pending"]
        pending_buy_amount = sum(float(o["order_amount"] or 0.0) for o in pending if o["order_type"] == "buy")
        pending_sell_shares = sum(float(o["order_amount"] or 0.0) for o in pending if o["order_type"] == "sell")

        return json.dumps({
            "success": True,
            "bot_id": bot_id,
            "account": {
                "initial_capital": _r(float(account["initial_capital"] or 0.0)),
                "cash_available": _r(cash),
                "cash_in_transit": _r(in_transit),
                "market_value": _r(market_value),
                "total_value": _r(total),
            },
            "holdings": holdings,
            "orders": orders,
            "summary": {
                "active_holdings": sum(1 for h in holdings if h.get("status") == "active"),
                "pending_orders": len(pending),
                "pending_buy_amount": _r(pending_buy_amount),
                "pending_sell_shares": _r(pending_sell_shares, 6),
            },
        }, ensure_ascii=False)


@mcp.tool()
async def close_my_day(bot_id: str, trade_date: str) -> str:
    """Bot 当日全部 place_buy_order / place_sell_order 调用完成后，调一次做"收盘核算"。

    本工具会：
      1. 调用系统快照计算（_compute_fund_snapshot）—— 落 fund_bot_daily_snapshots /
         fund_bot_position_snapshots，并把 fund_bot_holdings 的 latest_nav / market_value /
         actual_weight 更新成 trade_date 收盘态
      2. 读 account / holdings / pending orders 拼回 4 块结构化返回：
           assets   现金（available + in_transit）+ 持仓市值 + 总资产 + 净值
           holdings 每只持仓的份额（含 pending_sell）/ 成本 / 市值 / 浮盈 / 权重 / 持有天数
           pnl      日收益率 / 累计收益率 / 最大回撤
           pending  当前所有 pending 单 + 冻结金额 / 冻结份额汇总

    适合在 bot 一天结束时打印 / 写入"收盘报告 MD"。每个 (bot, trade_date) 最多写一行
    daily_snapshot；同日重复调用会用 INSERT OR REPLACE 覆盖。"""
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        snap = _compute_fund_snapshot(conn, bot_id, trade_date)
        if not snap.get("success"):
            return json.dumps(snap, ensure_ascii=False)
        account = _get_account(conn, bot_id)

        holdings_rows = conn.execute(
            "SELECT * FROM fund_bot_holdings WHERE bot_id=? ORDER BY status, fund_code",
            (bot_id,),
        ).fetchall()
        pendings = conn.execute(
            "SELECT order_id, fund_code, fund_name, order_type, order_date, order_amount, "
            " reference_nav, action_reason, created_at "
            "FROM fund_bot_orders WHERE bot_id=? AND status='pending' "
            "ORDER BY order_date, order_id",
            (bot_id,),
        ).fetchall()

        cash = float(account["cash"] or 0.0)
        in_transit = float(account["cash_in_transit"] or 0.0)
        active_mv = sum(float(h["market_value"] or 0.0) for h in holdings_rows if h["status"] == "active")
        total = cash + in_transit + active_mv
        initial = float(account["initial_capital"] or 0.0)
        net_value = total / initial if initial else 1.0

        frozen_cash = 0.0
        frozen_shares_by_fund: dict[str, float] = {}
        pending_list = []
        for p in pendings:
            amt = float(p["order_amount"] or 0.0)
            ref = p["reference_nav"]
            est_proceeds = None
            if p["order_type"] == "buy":
                frozen_cash += amt
            else:  # sell
                frozen_shares_by_fund[p["fund_code"]] = frozen_shares_by_fund.get(p["fund_code"], 0.0) + amt
                if ref is not None:
                    est_proceeds = _r(amt * float(ref))
            pending_list.append({
                "order_id": p["order_id"],
                "fund_code": p["fund_code"],
                "fund_name": p["fund_name"],
                "type": p["order_type"],
                "order_date": p["order_date"],
                "amount_or_shares": _r(amt, 6),
                "reference_nav": _r(float(ref), 6) if ref is not None else None,
                "estimated_gross_proceeds": est_proceeds,
                "reason": p["action_reason"],
            })

        holdings_out = [{
            "fund_code": h["fund_code"], "fund_name": h["fund_name"],
            "status": h["status"], "asset_class": h["asset_class"], "role": h["role"],
            "shares": h["shares"], "pending_sell_shares": h["pending_sell_shares"],
            "amount_invested": h["amount_invested"],
            "latest_nav": h["latest_nav"], "market_value": h["market_value"],
            "unrealized_pnl": h["unrealized_pnl"], "unrealized_pnl_pct": h["unrealized_pnl_pct"],
            "weight": h["actual_weight"],
            "entry_date": h["entry_date"], "exit_date": h["exit_date"],
            "holding_days": h["holding_days"], "high_nav": h["high_nav"],
        } for h in holdings_rows]

    return json.dumps({
        "success": True,
        "bot_id": bot_id,
        "trade_date": trade_date,
        "assets": {
            "initial_capital": _r(initial),
            "cash_available": _r(cash),
            "cash_in_transit": _r(in_transit),
            "market_value": _r(active_mv),
            "total_value": _r(total),
            "net_value": _r(net_value, 6),
        },
        "holdings": holdings_out,
        "pnl": {
            "daily_return_pct": snap.get("daily_return_pct"),
            "cumulative_return_pct": snap.get("cumulative_return_pct"),
            "max_drawdown_pct": snap.get("max_drawdown_pct"),
        },
        "pending": {
            "count": len(pending_list),
            "frozen_cash_total": _r(frozen_cash),
            "frozen_shares_by_fund": {k: _r(v, 6) for k, v in frozen_shares_by_fund.items()},
            "orders": pending_list,
        },
        "snapshot_written": True,
        "asset_allocation_pct": snap.get("asset_allocation"),
    }, ensure_ascii=False)


# ============================================================
# C. 订单管理（基金直投特有，T+1 结算）
# ============================================================

@writer_tool
async def create_fund_orders(bot_id: str, orders_json: str, review_id: int = 0) -> str:
    """创建在途订单（Step 4 统一出单）。
    orders_json: [{"fund_code":"008528","fund_name":"...",
      "order_type":"buy","order_amount":10000,
      "reference_nav":2.5,"action_reason":"BUILD"}]
    order_type: buy/sell。order_amount: 买入金额 或 卖出份额。"""
    try:
        orders = json.loads(orders_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "orders_json 格式错误"}, ensure_ascii=False)

    today = datetime.now().strftime("%Y-%m-%d")

    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)

        created = []
        for o in orders:
            conn.execute(
                "INSERT INTO fund_bot_orders "
                "(review_id, bot_id, fund_code, fund_name, order_type, order_date, "
                "order_amount, reference_nav, action_reason, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    review_id or None, bot_id, o["fund_code"], o.get("fund_name", ""),
                    o["order_type"], today,
                    o.get("order_amount", 0), o.get("reference_nav"),
                    o.get("action_reason", ""),
                )
            )
            order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            created.append({"order_id": order_id, "fund_code": o["fund_code"], "order_type": o["order_type"]})

    return json.dumps({
        "success": True, "bot_id": bot_id,
        "orders_created": len(created), "orders": created,
        "order_date": today,
    }, ensure_ascii=False)


@mcp.tool()
async def get_pending_orders(bot_id: str) -> str:
    """获取 bot 所有待确认的在途订单。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM fund_bot_orders WHERE bot_id = ? AND status = 'pending' "
            "ORDER BY order_date, order_id",
            (bot_id,)
        ).fetchall()
        return json.dumps({
            "success": True, "bot_id": bot_id,
            "count": len(rows), "orders": [dict(r) for r in rows],
        }, ensure_ascii=False)


@writer_tool
async def confirm_fund_orders(bot_id: str, confirmations_json: str) -> str:
    """确认在途订单（Step 0 每日确认）。T+1 净值已出，补填成交信息。
    confirmations_json: [{"order_id":1,"confirm_nav":2.55,
      "confirmed_shares":3921.57,"fee":15.0}]
    买入订单: 传 confirm_nav + confirmed_shares + fee。
    卖出订单: 传 confirm_nav + confirmed_amount + fee。
    同时更新 fund_bot_holdings 和 fund_bot_accounts。"""
    try:
        confirmations = json.loads(confirmations_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "confirmations_json 格式错误"}, ensure_ascii=False)

    today = datetime.now().strftime("%Y-%m-%d")
    results = []

    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)

        cash = account["cash"]

        for c in confirmations:
            oid = c["order_id"]
            order = conn.execute(
                "SELECT * FROM fund_bot_orders WHERE order_id = ? AND bot_id = ?",
                (oid, bot_id)
            ).fetchone()
            if not order:
                results.append({"order_id": oid, "status": "not_found"})
                continue
            if order["status"] != "pending":
                results.append({"order_id": oid, "status": f"already_{order['status']}"})
                continue

            confirm_nav = float(c.get("confirm_nav") or 0.0)
            fee = c.get("fee")
            fee = float(fee) if fee not in (None, "") else None
            pf_rate, redeem_tiers = _fund_fee_rates(conn, order["fund_code"])

            if order["order_type"] == "buy":
                order_amount = float(order["order_amount"] or 0.0)
                if fee is None:
                    fee = order_amount * pf_rate
                confirmed_shares = float(c.get("confirmed_shares") or 0.0)
                if not confirmed_shares and confirm_nav:
                    confirmed_shares = (order_amount - fee) / confirm_nav

                conn.execute(
                    "UPDATE fund_bot_orders SET confirm_date = ?, confirm_nav = ?, "
                    "confirmed_shares = ?, fee = ?, status = 'confirmed' WHERE order_id = ?",
                    (today, confirm_nav, confirmed_shares, fee, oid)
                )
                conn.execute(
                    "INSERT INTO fund_bot_actions "
                    "(review_id, bot_id, fund_code, action_type, nav_used, amount, shares, fee, reason, action_date) "
                    "VALUES (?, ?, ?, 'ADD', ?, ?, ?, ?, ?, ?)",
                    (
                        order["review_id"], bot_id, order["fund_code"], _r(confirm_nav, 6),
                        _r(order_amount), _r(confirmed_shares, 6), _r(fee),
                        f"人工确认收口:{order['action_reason'] or 'buy'}", today,
                    )
                )

                holding = conn.execute(
                    "SELECT * FROM fund_bot_holdings WHERE bot_id = ? AND fund_code = ? AND status = 'active'",
                    (bot_id, order["fund_code"])
                ).fetchone()

                if holding:
                    new_shares = (holding["shares"] or 0) + confirmed_shares
                    new_invested = (holding["amount_invested"] or 0) + order_amount
                    new_mv = new_shares * confirm_nav
                    conn.execute(
                        "UPDATE fund_bot_holdings SET shares = ?, amount_invested = ?, "
                        "latest_nav = ?, market_value = ?, "
                        "unrealized_pnl = ?, unrealized_pnl_pct = ?, "
                        "high_nav = MAX(COALESCE(high_nav, 0), ?) "
                        "WHERE holding_id = ?",
                        (new_shares, new_invested, confirm_nav, new_mv,
                         new_mv - new_invested,
                         (new_mv - new_invested) / new_invested * 100 if new_invested else 0,
                         confirm_nav, holding["holding_id"])
                    )
                else:
                    conn.execute(
                        "INSERT INTO fund_bot_holdings "
                        "(bot_id, fund_code, fund_name, share_class, asset_class, role, "
                        "entry_date, entry_nav, latest_nav, shares, amount_invested, "
                        "market_value, unrealized_pnl, unrealized_pnl_pct, "
                        "target_weight, holding_days, high_nav, status, thesis) "
                        "VALUES (?, ?, ?, '', '', '', ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, 'active', ?)",
                        (bot_id, order["fund_code"], order["fund_name"],
                         today, confirm_nav, confirm_nav,
                         confirmed_shares, order_amount, order_amount,
                         confirm_nav, order["action_reason"] or "")
                    )

                cash -= order_amount
                results.append({"order_id": oid, "status": "confirmed", "type": "buy",
                                "shares_added": _r(confirmed_shares, 4), "fee": _r(fee)})

            elif order["order_type"] == "sell":
                sell_shares = float(order["order_amount"] or 0.0)
                holding = conn.execute(
                    "SELECT * FROM fund_bot_holdings WHERE bot_id = ? AND fund_code = ? AND status = 'active'",
                    (bot_id, order["fund_code"])
                ).fetchone()
                holding_days = _calc_holding_days(holding["entry_date"] or today, today) if holding else 0
                if fee is None:
                    rf_rate = _redeem_fee_rate(redeem_tiers, holding_days)
                    fee = sell_shares * confirm_nav * rf_rate
                confirmed_amount = float(c.get("confirmed_amount") or 0.0)
                if not confirmed_amount and confirm_nav:
                    confirmed_amount = sell_shares * confirm_nav - fee

                conn.execute(
                    "UPDATE fund_bot_orders SET confirm_date = ?, confirm_nav = ?, "
                    "confirmed_amount = ?, fee = ?, status = 'confirmed' WHERE order_id = ?",
                    (today, confirm_nav, confirmed_amount, fee, oid)
                )
                conn.execute(
                    "INSERT INTO fund_bot_actions "
                    "(review_id, bot_id, fund_code, action_type, nav_used, amount, shares, fee, reason, action_date) "
                    "VALUES (?, ?, ?, 'REDUCE', ?, ?, ?, ?, ?, ?)",
                    (
                        order["review_id"], bot_id, order["fund_code"], _r(confirm_nav, 6),
                        _r(sell_shares * confirm_nav), _r(sell_shares, 6), _r(fee),
                        f"人工确认收口:{order['action_reason'] or 'sell'}", today,
                    )
                )

                if holding:
                    new_shares = (holding["shares"] or 0) - sell_shares
                    if new_shares <= 0.001:
                        conn.execute(
                            "UPDATE fund_bot_holdings SET status = 'closed', exit_date = ?, "
                            "shares = 0, market_value = 0 WHERE holding_id = ?",
                            (today, holding["holding_id"])
                        )
                    else:
                        ratio = new_shares / holding["shares"] if holding["shares"] else 0
                        new_invested = (holding["amount_invested"] or 0) * ratio
                        new_mv = new_shares * confirm_nav
                        conn.execute(
                            "UPDATE fund_bot_holdings SET shares = ?, amount_invested = ?, "
                            "latest_nav = ?, market_value = ?, "
                            "unrealized_pnl = ?, unrealized_pnl_pct = ? "
                            "WHERE holding_id = ?",
                            (new_shares, new_invested, confirm_nav, new_mv,
                             new_mv - new_invested,
                             (new_mv - new_invested) / new_invested * 100 if new_invested else 0,
                             holding["holding_id"])
                        )

                cash += confirmed_amount
                results.append({"order_id": oid, "status": "confirmed", "type": "sell",
                                "amount_received": _r(confirmed_amount), "fee": _r(fee)})

        conn.execute(
            "UPDATE fund_bot_accounts SET cash = ?, updated_at = datetime('now') WHERE bot_id = ?",
            (cash, bot_id)
        )
        repaired = _replay_and_repair_fund_holdings(conn, bot_id, today)

    return json.dumps({
        "success": True, "bot_id": bot_id,
        "confirmed": len([r for r in results if r["status"] == "confirmed"]),
        "cash_after": _r(cash), "holdings_repaired": repaired, "results": results,
    }, ensure_ascii=False)


@writer_tool
async def cancel_fund_orders(bot_id: str, order_ids_json: str) -> str:
    """取消在途订单。order_ids_json: [1, 2, 3]"""
    try:
        order_ids = json.loads(order_ids_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "order_ids_json 格式错误"}, ensure_ascii=False)

    with get_conn() as conn:
        cancelled = 0
        for oid in order_ids:
            result = conn.execute(
                "UPDATE fund_bot_orders SET status = 'cancelled' "
                "WHERE order_id = ? AND bot_id = ? AND status = 'pending'",
                (oid, bot_id)
            )
            cancelled += result.rowcount

    return json.dumps({
        "success": True, "bot_id": bot_id, "cancelled": cancelled,
    }, ensure_ascii=False)


@writer_tool
async def settle_pending_fund_orders(bot_id: str, as_of_date: str = "") -> str:
    """T+1 收口（新版：用订单上锁定的 reference_nav 结算 + 释放 pending 期间的冻结）。

    settle 触发条件：order.order_date < as_of_date（即 T+1 已到）。

    申购（buy）：
      - 净值用 order.reference_nav（下单时锁定的 T 日 NAV，不再去查 T+1 NAV）
      - 申购费 = order_amount × pf_rate / (1 + pf_rate)
      - 到账份额 = (order_amount − fee) / reference_nav
      - cash_in_transit -= order_amount  ← 释放 pending 时冻结的现金
        （cash 在下单时已扣，settle 时不再动 cash）
      - holdings：已存在则加仓；不存在则新建（entry_date=order_date）

    赎回（sell）：
      - 净值用 order.reference_nav
      - sell_shares = min(order.order_amount, holding.shares)（防御性兜底）
      - 赎回费率按 settle 当天的真实持有天数从阶梯表取
      - cash += gross − fee
      - holding.shares -= sell_shares; holding.pending_sell_shares -= sell_shares
      - shares→0 → status='closed', exit_date=as_of_date

    跳过条件 → 写入 skipped 列表：
      - order.order_date >= as_of_date：T+1 还没到
      - order.reference_nav 为 null：legacy 订单，新 settle 不处理
      - sell 找不到对应 active holding"""
    as_of_date = _normalize_trade_date(as_of_date)
    settled = []
    skipped = []
    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)
        cash = float(account["cash"] or 0.0)
        cash_in_transit = float(account["cash_in_transit"] or 0.0)

        pending = conn.execute(
            "SELECT * FROM fund_bot_orders WHERE bot_id=? AND status='pending' ORDER BY order_date, order_id",
            (bot_id,)
        ).fetchall()

        for o in pending:
            oid = o["order_id"]
            fc = o["fund_code"]
            order_date = o["order_date"][:10] if o["order_date"] else as_of_date
            if order_date >= as_of_date:
                skipped.append({"order_id": oid, "reason": f"T+1 未到 (order_date={order_date} >= as_of={as_of_date})"})
                continue
            ref_nav = o["reference_nav"]
            if ref_nav is None:
                skipped.append({"order_id": oid, "reason": "legacy order without reference_nav"})
                continue
            nav = float(ref_nav)
            pf_rate, redeem_tiers = _fund_fee_rates(conn, fc)
            paradigm = None
            if o["review_id"]:
                prow = conn.execute("SELECT paradigm FROM fund_bot_reviews WHERE review_id=?", (o["review_id"],)).fetchone()
                paradigm = (prow["paradigm"] if prow else None) or None
            holding = conn.execute(
                "SELECT * FROM fund_bot_holdings WHERE bot_id=? AND fund_code=? AND status='active'",
                (bot_id, fc)
            ).fetchone()

            if o["order_type"] == "buy":
                order_amount = float(o["order_amount"] or 0.0)
                fee = order_amount * pf_rate / (1 + pf_rate) if pf_rate else 0.0
                net = order_amount - fee
                add_shares = net / nav if nav > 0 else 0.0
                if holding:
                    new_shares = float(holding["shares"] or 0.0) + add_shares
                    new_cost = float(holding["amount_invested"] or 0.0) + order_amount
                    new_mv = new_shares * nav
                    conn.execute(
                        "UPDATE fund_bot_holdings SET shares=?, amount_invested=?, latest_nav=?, "
                        "market_value=?, unrealized_pnl=?, unrealized_pnl_pct=?, "
                        "high_nav=MAX(COALESCE(high_nav,0),?) WHERE holding_id=?",
                        (_r(new_shares, 6), _r(new_cost), _r(nav, 6), _r(new_mv),
                         _r(new_mv - new_cost), _r((new_mv - new_cost) / new_cost * 100 if new_cost else 0, 4),
                         _r(nav, 6), holding["holding_id"])
                    )
                else:
                    new_mv = add_shares * nav
                    conn.execute(
                        "INSERT INTO fund_bot_holdings "
                        "(bot_id, fund_code, fund_name, share_class, asset_class, role, "
                        "entry_date, entry_nav, latest_nav, shares, pending_sell_shares, amount_invested, "
                        "market_value, unrealized_pnl, unrealized_pnl_pct, "
                        "target_weight, actual_weight, holding_days, high_nav, status, thesis) "
                        "VALUES (?, ?, ?, '', '', '', ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL, 0, 0, ?, 'active', ?)",
                        (bot_id, fc, o["fund_name"] or "", order_date, nav, nav,
                         _r(add_shares, 6), _r(order_amount), _r(new_mv),
                         _r(new_mv - order_amount), _r((new_mv - order_amount) / order_amount * 100 if order_amount else 0, 4),
                         _r(nav, 6), o["action_reason"] or "")
                    )
                # 释放 pending 时冻结的 cash_in_transit；不动 cash（cash 在下单时已扣）
                cash_in_transit -= order_amount
                conn.execute(
                    "INSERT INTO fund_bot_actions "
                    "(review_id, bot_id, fund_code, action_type, before_weight, after_weight, "
                    "nav_used, amount, shares, fee, reason, action_date, paradigm) "
                    "VALUES (?, ?, ?, 'ADD', NULL, NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (o["review_id"], bot_id, fc, _r(nav, 6), _r(order_amount), _r(add_shares, 6),
                     _r(fee), f"T+1 settle 申购 (nav={nav:.4f} 申购费 {pf_rate*100:.2f}%)", as_of_date, paradigm)
                )
                conn.execute(
                    "UPDATE fund_bot_orders SET status='confirmed', confirm_date=?, confirm_nav=?, "
                    "confirmed_shares=?, fee=? WHERE order_id=?",
                    (as_of_date, _r(nav, 6), _r(add_shares, 6), _r(fee), oid)
                )
                settled.append({"order_id": oid, "fund_code": fc, "type": "buy",
                                "shares_added": _r(add_shares, 4), "fee": _r(fee), "nav": _r(nav, 6)})

            elif o["order_type"] == "sell":
                if not holding:
                    skipped.append({"order_id": oid, "reason": "无持仓可卖"})
                    continue
                cur_shares = float(holding["shares"] or 0.0)
                cur_pending = float(holding["pending_sell_shares"] or 0.0)
                want = float(o["order_amount"] or 0.0)
                sell_shares = min(want, cur_shares)
                if sell_shares <= 1e-6:
                    skipped.append({"order_id": oid, "reason": "卖出份额为 0"})
                    continue
                holding_days = _calc_holding_days(holding["entry_date"] or order_date, as_of_date)
                rf_rate = _redeem_fee_rate(redeem_tiers, holding_days)
                gross = sell_shares * nav
                fee = gross * rf_rate
                cash += gross - fee
                new_pending = max(0.0, cur_pending - sell_shares)
                ratio_left = (cur_shares - sell_shares) / cur_shares if cur_shares else 0.0
                new_shares = cur_shares - sell_shares
                if new_shares <= 1e-6:
                    conn.execute(
                        "UPDATE fund_bot_holdings SET status='closed', exit_date=?, shares=0, "
                        "pending_sell_shares=0, market_value=0 WHERE holding_id=?",
                        (as_of_date, holding["holding_id"])
                    )
                else:
                    new_cost = float(holding["amount_invested"] or 0.0) * ratio_left
                    new_mv = new_shares * nav
                    conn.execute(
                        "UPDATE fund_bot_holdings SET shares=?, pending_sell_shares=?, "
                        "amount_invested=?, latest_nav=?, market_value=?, "
                        "unrealized_pnl=?, unrealized_pnl_pct=? WHERE holding_id=?",
                        (_r(new_shares, 6), _r(new_pending, 6), _r(new_cost), _r(nav, 6), _r(new_mv),
                         _r(new_mv - new_cost), _r((new_mv - new_cost) / new_cost * 100 if new_cost else 0, 4),
                         holding["holding_id"])
                    )
                conn.execute(
                    "INSERT INTO fund_bot_actions "
                    "(review_id, bot_id, fund_code, action_type, before_weight, after_weight, "
                    "nav_used, amount, shares, fee, reason, action_date, paradigm) "
                    "VALUES (?, ?, ?, 'REDUCE', NULL, NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (o["review_id"], bot_id, fc, _r(nav, 6), _r(gross), _r(sell_shares, 6),
                     _r(fee), f"T+1 settle 赎回 (nav={nav:.4f} 持有 {holding_days}天 赎回费 {rf_rate*100:.2f}%)", as_of_date, paradigm)
                )
                conn.execute(
                    "UPDATE fund_bot_orders SET status='confirmed', confirm_date=?, confirm_nav=?, "
                    "confirmed_shares=?, confirmed_amount=?, fee=? WHERE order_id=?",
                    (as_of_date, _r(nav, 6), _r(sell_shares, 6), _r(gross - fee), _r(fee), oid)
                )
                settled.append({"order_id": oid, "fund_code": fc, "type": "sell",
                                "shares_sold": _r(sell_shares, 4), "gross": _r(gross),
                                "proceeds": _r(gross - fee), "fee": _r(fee), "nav": _r(nav, 6)})

        conn.execute(
            "UPDATE fund_bot_accounts SET cash=?, cash_in_transit=?, updated_at=datetime('now') WHERE bot_id=?",
            (_r(cash), _r(max(0.0, cash_in_transit)), bot_id)
        )

    return json.dumps({
        "success": True, "bot_id": bot_id, "as_of_date": as_of_date,
        "settled": settled, "skipped": skipped,
        "cash_after": _r(cash), "cash_in_transit_after": _r(max(0.0, cash_in_transit)),
    }, ensure_ascii=False)


# ============================================================
# D. 巡检与调仓
# ============================================================

@writer_tool
async def save_fund_review(
    bot_id: str,
    decision: str,
    reason: str,
    regime: str = "",
    action_count: int = 0,
    review_md: str = "",
    cooldown_days: int = 7,
    cash_before: float = 0.0,
    cash_after: float = 0.0,
    portfolio_value_before: float = 0.0,
    portfolio_value_after: float = 0.0,
    turnover_amount: float = 0.0,
    turnover_ratio: float = 0.0,
) -> str:
    """保存一次巡检结论。decision: KEEP/REBALANCE/SWITCH。返回 review_id。"""
    today = datetime.now().strftime("%Y-%m-%d")
    cooldown_end = (datetime.now() + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
    if decision == "KEEP" and not (reason or "").strip():
        reason = "维持现有仓位，无实际调仓动作"
    if decision == "KEEP":
        action_count = 0
        turnover_amount = 0.0
        turnover_ratio = 0.0

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_bot_reviews "
            "(bot_id, review_date, regime, decision, action_count, reason, review_md, "
            "cooldown_end, cash_before, cash_after, "
            "portfolio_value_before, portfolio_value_after, "
            "turnover_amount, turnover_ratio) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bot_id, today, regime, decision, action_count, reason, review_md,
             cooldown_end, cash_before, cash_after,
             portfolio_value_before, portfolio_value_after,
             turnover_amount, turnover_ratio)
        )
        review_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    return json.dumps({
        "success": True, "review_id": review_id, "bot_id": bot_id,
        "decision": decision, "review_date": today, "cooldown_end": cooldown_end,
    }, ensure_ascii=False)


@writer_tool
async def save_fund_actions(bot_id: str, review_id: int, actions_json: str) -> str:
    """保存调仓动作（关联 review_id）。
    actions_json: [{"fund_code":"008528","action_type":"REDUCE",
      "trigger":"timing_matrix","timing_state":"大幅盈利",
      "momentum_state":"减速但未转","matrix_suggestion":"减仓1/3",
      "final_decision":"减仓1/4","before_weight":12,"after_weight":9,
      "nav_used":2.5,"amount":3000,"shares":1200,"fee":15,"reason":"..."}]"""
    try:
        actions = json.loads(actions_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "actions_json 格式错误"}, ensure_ascii=False)

    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        for a in actions:
            conn.execute(
                "INSERT INTO fund_bot_actions "
                "(review_id, bot_id, fund_code, action_type, trigger, "
                "timing_state, momentum_state, matrix_suggestion, final_decision, "
                "before_weight, after_weight, nav_used, amount, shares, fee, "
                "reason, action_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id, bot_id, a.get("fund_code"), a.get("action_type"),
                    a.get("trigger"), a.get("timing_state"), a.get("momentum_state"),
                    a.get("matrix_suggestion"), a.get("final_decision"),
                    a.get("before_weight"), a.get("after_weight"),
                    a.get("nav_used"), a.get("amount"), a.get("shares"),
                    a.get("fee"), a.get("reason"), today,
                )
            )

    return json.dumps({
        "success": True, "review_id": review_id, "actions_saved": len(actions),
    }, ensure_ascii=False)


# 路径在调用时再读环境变量（OPENCLAW_ROOT / FUND_DB_PATH / FUND_WORKSPACE_TEMPLATE），便于测试 monkeypatch
# 在 agent_invest_lab 部署中：
#   OPENCLAW_ROOT   = /home/rooot/agent_invest_lab     (默认值)
#   FUND_DB_PATH    = <OPENCLAW_ROOT>/data/fund.db     (默认)
#   workspace dir   = <OPENCLAW_ROOT>/world/bots/<bot> (lab 把 workspace-<bot> 改成 world/bots/<bot>)
def _openclaw_root() -> str:
    return os.environ.get("OPENCLAW_ROOT", "/home/rooot/agent_invest_lab")

def _openclaw_scripts_dir() -> str:
    return os.path.join(_openclaw_root(), "scripts")

def _fund_db_path() -> str:
    return os.environ.get("FUND_DB_PATH", os.path.join(_openclaw_root(), "data", "fund.db"))

def _fund_workspace_dir(bot_id: str) -> str:
    # agent_invest_lab: bots/<bot> 替代生产环境的 workspace-<bot>
    template = os.environ.get(
        "FUND_WORKSPACE_TEMPLATE",
        os.path.join(_openclaw_root(), "bots", "{bot_id}"),
    )
    return template.format(bot_id=bot_id)


@mcp.tool()
async def get_fund_md_schema() -> str:
    """返回 bot 要写的 5 份基金 MD 的**完整 frontmatter schema**（从 fund_md_to_db 的校验常量实时读出，
    永远跟 validate_fund_bot_md 的校验规则同步）：每份文件的 step、必填字段、枚举白名单、数字口径
    （大类比例 0-100 / 基金权重 0-1）、跨文件一致性约束。bot 写 MD 前**先调这个**，照着写就不会被校验拒。

    返回 {success, schema:{...结构化...}, markdown:"...人读版..."}。"""
    try:
        import sys as _sys
        if _openclaw_scripts_dir() not in _sys.path:
            _sys.path.insert(0, _openclaw_scripts_dir())
        import fund_md_to_db as _fmd
    except Exception as e:
        return json.dumps({"success": False, "message": f"fund_md_to_db 不可用: {e}"}, ensure_ascii=False)
    L2 = sorted(_fmd.VALID_LAYER2_PIVOTS); RG = sorted(_fmd.VALID_REGIMES)
    TS = sorted(_fmd.VALID_TIMING_STANCES); DEC = sorted(_fmd.VALID_DECISIONS)
    ACT = sorted(_fmd.VALID_ACTION_TYPES); AC = sorted(_fmd.VALID_ASSET_CLASSES)
    RO = sorted(_fmd.VALID_ROLES); PER = list(_fmd.REQUIRED_PERFORMANCE_PERIODS)
    AK = list(_fmd.ASSET_KEYS); PARA = sorted(_fmd.VALID_PARADIGMS)
    schema = {
        "common_frontmatter": {
            "step": "每份文件固定值（见各文件）",
            "run_id": "必须 = 本轮运行参数 run_id",
            "trade_date": "必须 = 本轮运行参数 trade_date (YYYY-MM-DD)",
            "paradigm_active": f"必须 = 本轮运行参数 paradigm（{PARA} 之一）",
            "body": "frontmatter 之后的正文 markdown 必须非空",
        },
        "files": {
            "投资框架.md": {
                "step": "investment_framework",
                "required_fields": {
                    "layer2_pivot": {"type": "enum", "values": L2,
                                     "hint": "按 paradigm：A→大类资产, B1→行业, B2→单一行业, C→单基金。这是枚举不是句子！"},
                    "layer1_capability_brief": {"type": "non_empty_str", "hint": "自由文字简述，一两句"},
                    "layer2_structure_brief": {"type": "non_empty_str", "hint": "自由文字简述"},
                    "layer3_signal_source": {"type": "non_empty_str", "hint": "自由文字简述"},
                    "layer3_discipline_brief": {"type": "non_empty_str", "hint": "自由文字简述"},
                },
            },
            "市场环境判断.md": {
                "step": "market_context",
                "required_fields": {
                    "regime": {"type": "enum", "values": RG, "hint": "只能四选一，不许 bull_trend/range_down 这种变体"},
                    "timing_stance": {"type": "enum", "values": TS},
                    "today_target": {"type": "dict", "keys": AK,
                                     "hint": "四个键全是数字，0-100 百分数，加起来=100（如 equity_pct:75, bond_pct:5, gold_pct:0, cash_pct:20）"},
                    "central_baseline": {"type": "dict", "keys": AK, "hint": "同 today_target，四个 0-100 数字键"},
                    "focus_industries": {"type": "non_empty_list", "required_when": "paradigm in (B1, B2)",
                                         "hint": "B1/B2 必填非空 list；A/C 可不写"},
                },
                "optional_fields": ["regime_code", "confidence", "summary"],
            },
            "个性化基金选择.md": {
                "step": "fund_selection",
                "required_fields": {
                    "funnel": {"type": "dict", "hint": "含 layer1_count/layer2_count/layer3_count/layer4_count 整数计数"},
                    "funds": {"type": "non_empty_list", "item_schema": {
                        "fund_code": {"type": "str", "constraint": "必须是核心池里 fund_info.fund_type 以「指数型」开头的基金；主动型(如008528易方达科技动力)会被拒"},
                        "fund_name": {"type": "str"},
                        "asset_class": {"type": "enum", "values": AC},
                        "role": {"type": "enum", "values": RO, "hint": "5 选一的枚举，不是「核心持仓-半导体赛道」这种自由文字"},
                        "thesis": {"type": "str", "hint": "选中逻辑，会被当作该基金持仓的 thesis"},
                        "performance": {"type": "dict", "keys": PER,
                                        "hint": f"每个周期是 dict 含 return_pct(数字)，如 {{'1m':{{'return_pct':4.2}}, ...}}；系统会从 fund_info 补但最好自己填"},
                        "target_weight": {"type": "number_0_1", "hint": "0~1 的小数！占 25% 就写 0.25 不要写 25"},
                    }},
                },
                "weight_constraints": [
                    "所有 funds 的 target_weight 之和必须 ≤ 1.0（且 ≥ 0.5）",
                    "股票类基金 target_weight 之和 × 100 ≈ today_target.equity_pct（偏差 >10pp 被拒）；债券类↔bond_pct、黄金类↔gold_pct 同理",
                    "隐含现金 = 100 - 所有 target_weight 之和×100 ≈ today_target.cash_pct（偏差 >10pp 被拒）",
                ],
                "optional_fields": ["eliminated"],
            },
            "当前基金持仓.md": {
                "step": "fund_holdings_snapshot",
                "required_fields": {
                    "account": {"type": "dict", "keys": ["cash", "total_value"], "hint": "都是数字"},
                    "holdings": {"type": "list", "item_schema": {
                        "fund_code": {"type": "str", "constraint": "不能重复"},
                        "market_value": {"type": "number"},
                        "weight": {"type": "number_0_1", "hint": "0~1 小数，≈ market_value/total_value（偏差>0.02 被拒）；也可叫 actual_weight"},
                    }},
                },
                "consistency": ["account.total_value == account.cash + sum(holdings[*].market_value)（±1.0）"],
            },
            "基金巡检记录.md": {
                "step": "fund_review",
                "required_fields": {
                    "decision": {"type": "enum", "values": DEC},
                    "actions": {"type": "list", "item_schema": {
                        "fund_code": {"type": "str"},
                        "action_type": {"type": "enum", "values": ACT},
                        "amount": {"type": "number", "hint": "ADD=要花的现金；REDUCE/TAKE_PROFIT/STOP_LOSS=要赎回的¥金额；HOLD=0"},
                        "reason": {"type": "str"},
                    }, "rules": [
                        "actions 必须覆盖 DB 里所有 active 持仓（不动的也要写一条 HOLD）",
                        "ADD 的 fund_code 必须是指数型基金；非 ADD 的 fund_code 必须是当前持有的基金",
                        "decision=KEEP 时：不允许非 HOLD 的非零 amount 动作；turnover_amount/turnover_ratio 必须为 0",
                        "可执行性（落库时会回放 actions 强校验）：所有 ADD 的 amount 之和 ≤ 当前可用现金（= 当前基金持仓.md 的 account.cash）；REDUCE/TAKE_PROFIT/STOP_LOSS 的 amount ≤ 该基金当前持仓市值。透支/超卖整轮被拒——宁可少加仓也不要透支",
                    ]},
                },
                "optional_fields": ["account_after(含 cash/portfolio_value, 应与持仓快照一致 ±0.2)",
                                    "cooldown_days(默认7)", "review_md_section", "turnover_amount", "turnover_ratio",
                                    "每条 action 的 before_weight/after_weight/nav_used/shares/fee/timing_state/momentum_state/matrix_suggestion/final_decision/trigger（系统会重算覆盖，可留空）"],
            },
        },
        "key_reminders": [
            "大类比例 today_target/central_baseline 用 0-100；基金/持仓的 target_weight、weight 用 0-1 小数",
            "layer2_pivot、regime、timing_stance、asset_class、role、decision、action_type 都是固定枚举，不是自由文字",
            "fund_code 只能是核心池里 fund_type 以「指数型」开头的基金",
            "三个共同字段 step / run_id / trade_date / paradigm_active 每份 MD 都要写、值要对",
            "巡检记录 actions 要可执行：ADD 总额 ≤ account.cash、卖出 ≤ 该基金当前市值，否则落库被拒",
            "写完 5 份 MD 一定要调 validate_fund_bot_md 自检，反复改到 success=true 再结束（它现在也会回放 actions 检查现金够不够、卖出超不超）",
        ],
    }
    # 人读版 markdown
    md_lines = ["# 基金 5 份 MD frontmatter 完整 schema（系统校验同源，照此写不会被拒）", ""]
    md_lines.append("## 每份 MD 的公共 frontmatter 字段")
    for k, v in schema["common_frontmatter"].items():
        md_lines.append(f"- `{k}`: {v}")
    for fname, fspec in schema["files"].items():
        md_lines.append(f"\n## {fname} — `step: {fspec['step']}`")
        for fk, fv in fspec.get("required_fields", {}).items():
            md_lines.append(f"- `{fk}` ({fv.get('type')}): " + (f"枚举值={fv['values']}; " if 'values' in fv else "")
                            + (f"keys={fv['keys']}; " if 'keys' in fv else "")
                            + (fv.get('hint', '') or fv.get('constraint', '')))
            if 'item_schema' in fv:
                for ik, iv in fv['item_schema'].items():
                    md_lines.append(f"    · `{ik}` ({iv.get('type')}): " + (f"枚举值={iv['values']}; " if 'values' in iv else "")
                                    + (f"keys={iv['keys']}; " if 'keys' in iv else "")
                                    + (iv.get('hint', '') or iv.get('constraint', '')))
                for r in fv.get('rules', []):
                    md_lines.append(f"    ⚠ {r}")
        for c in fspec.get("weight_constraints", []) + fspec.get("consistency", []):
            md_lines.append(f"  ⚠ {c}")
        if fspec.get("optional_fields"):
            md_lines.append(f"  （可选/系统可重算：{', '.join(fspec['optional_fields'])}）")
    md_lines.append("\n## 一定记牢")
    for r in schema["key_reminders"]:
        md_lines.append(f"- {r}")
    return json.dumps({"success": True, "schema": schema, "markdown": "\n".join(md_lines)}, ensure_ascii=False)


@mcp.tool()
async def validate_fund_bot_md(bot_id: str, run_id: str, trade_date: str, paradigm_active: str) -> str:
    """校验 bot 写的 5 份基金 MD（投资框架/市场环境判断/个性化基金选择/当前基金持仓/基金巡检记录）：
    三层结构 / paradigm 一致性 / 必填字段 / 与 DB 持仓的一致性。返回 {success, blocking_issues, warnings}。
    bot 决策完写完 MD 后应先调这个自检，再调 save_allocation_run / save_selection_run /
    apply_fund_review_and_rebalance。校验逻辑与 cron 落库前的强校验同源。
    （写 MD 之前先调 get_fund_md_schema() 拿完整 schema，照着写。）"""
    try:
        import sys as _sys
        if _openclaw_scripts_dir() not in _sys.path:
            _sys.path.insert(0, _openclaw_scripts_dir())
        import fund_md_to_db as _fmd
        ok, issues = _fmd.validate_bot_md(
            bot_id=bot_id, run_id=run_id, trade_date=trade_date,
            paradigm_active=paradigm_active, db_path=_fund_db_path(),
        )
    except Exception as e:
        return json.dumps({"success": False, "blocking_issues": [f"validate 调用异常: {e}"], "warnings": []}, ensure_ascii=False)
    blocking = [str(m) for m in issues if not str(m).startswith("warn:")]
    warnings = [str(m) for m in issues if str(m).startswith("warn:")]
    return json.dumps({"success": ok, "bot_id": bot_id, "trade_date": trade_date,
                       "blocking_issues": blocking, "warnings": warnings}, ensure_ascii=False)


@writer_tool
async def select_fund_paradigm(bot_id: str, trade_date: str, run_id: str = "") -> str:
    """Phase B-1：读 workspace-{bot}/memory/portfolio/fund/能力圈宣告.md → 选定本日 paradigm
    （A/B1/B2/C/SKIP）→ UPSERT fund_paradigm_runs。返回决策 dict。
    缺宣告 / 解析失败 → SKIP（该 bot 当日不进入决策链路）。这是系统层的范式闸门计算入口。"""
    trade_date = _normalize_trade_date(trade_date)
    md_path = os.path.join(_fund_workspace_dir(bot_id), "memory", "portfolio", "fund", "能力圈宣告.md")
    try:
        import sys as _sys
        if _openclaw_scripts_dir() not in _sys.path:
            _sys.path.insert(0, _openclaw_scripts_dir())
        from fund_capability_lib import parse_capability_md, select_paradigm, GATE_SKIP
    except Exception as e:
        return json.dumps({"success": False, "message": f"fund_capability_lib 不可用: {e}"}, ensure_ascii=False)

    def _write(conn, paradigm_active, cap_field, cap_value, switched_from, reason):
        conn.execute(
            "INSERT INTO fund_paradigm_runs "
            "(bot_id, trade_date, run_id, paradigm_active, capability_field, capability_value, switched_from, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bot_id, trade_date) DO UPDATE SET "
            " run_id=excluded.run_id, paradigm_active=excluded.paradigm_active, "
            " capability_field=excluded.capability_field, capability_value=excluded.capability_value, "
            " switched_from=excluded.switched_from, reason=excluded.reason",
            (bot_id, trade_date, run_id or None, paradigm_active, cap_field, cap_value, switched_from, reason)
        )

    with get_conn() as conn:
        if not os.path.exists(md_path):
            _write(conn, GATE_SKIP, None, None, None, "missing_declaration_skip")
            return json.dumps({"success": True, "bot_id": bot_id, "trade_date": trade_date,
                               "paradigm_active": GATE_SKIP, "reason": "missing_declaration_skip"}, ensure_ascii=False)
        try:
            with open(md_path, encoding="utf-8") as f:
                cc = parse_capability_md(f.read())
        except Exception as e:
            reason = f"parse_error_skip:{e}"
            _write(conn, GATE_SKIP, None, None, None, reason)
            return json.dumps({"success": True, "bot_id": bot_id, "trade_date": trade_date,
                               "paradigm_active": GATE_SKIP, "reason": reason}, ensure_ascii=False)
        # 上一日 paradigm + 上次切换日
        row = conn.execute(
            "SELECT paradigm_active FROM fund_paradigm_runs WHERE bot_id=? AND trade_date<? "
            "ORDER BY trade_date DESC LIMIT 1", (bot_id, trade_date)).fetchone()
        current = row["paradigm_active"] if row else None
        row2 = conn.execute(
            "SELECT trade_date FROM fund_paradigm_runs WHERE bot_id=? AND switched_from IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT 1", (bot_id,)).fetchone()
        if row2:
            last_switch = row2["trade_date"]
        else:
            row3 = conn.execute("SELECT MIN(trade_date) AS d FROM fund_paradigm_runs WHERE bot_id=?", (bot_id,)).fetchone()
            last_switch = row3["d"] if row3 and row3["d"] else None
        pd = select_paradigm(cc, current_paradigm=current, last_switch_date=last_switch, today=trade_date)
        _write(conn, pd.paradigm_active, pd.capability_field, pd.capability_value, pd.switched_from, pd.reason)
        return json.dumps({"success": True, "bot_id": bot_id, "trade_date": trade_date,
                           "paradigm_active": pd.paradigm_active, "capability_field": pd.capability_field,
                           "capability_value": pd.capability_value, "switched_from": pd.switched_from,
                           "reason": pd.reason}, ensure_ascii=False)


@writer_tool
async def select_all_fund_paradigms(trade_date: str, run_id: str = "", bot_ids_json: str = "") -> str:
    """对一批 bot 批量跑 Phase B-1 范式闸门（cron 用）。bot_ids_json 缺省 = 所有有账户的 bot。"""
    trade_date = _normalize_trade_date(trade_date)
    try:
        bot_ids = json.loads(bot_ids_json) if bot_ids_json else None
    except json.JSONDecodeError:
        bot_ids = None
    results = []
    if bot_ids is None:
        with get_conn() as conn:
            bot_ids = [r["bot_id"] for r in conn.execute("SELECT bot_id FROM fund_bot_accounts ORDER BY bot_id")]
    for bid in bot_ids:
        try:
            r = await select_fund_paradigm(bot_id=bid, trade_date=trade_date, run_id=run_id)
            results.append(json.loads(r))
        except Exception as e:
            results.append({"success": False, "bot_id": bid, "message": str(e)})
    return json.dumps({"success": True, "trade_date": trade_date, "results": results}, ensure_ascii=False)


@mcp.tool()
async def get_fund_review_history(bot_id: str, limit: int = 10) -> str:
    """查询 bot 最近的巡检历史（含调仓动作和关联订单）。"""
    with get_conn() as conn:
        reviews = conn.execute(
            "SELECT * FROM fund_bot_reviews WHERE bot_id = ? ORDER BY review_date DESC LIMIT ?",
            (bot_id, limit)
        ).fetchall()

        result = []
        for r in reviews:
            actions = conn.execute(
                "SELECT * FROM fund_bot_actions WHERE review_id = ?",
                (r["review_id"],)
            ).fetchall()

            orders = conn.execute(
                "SELECT * FROM fund_bot_orders WHERE review_id = ?",
                (r["review_id"],)
            ).fetchall()

            result.append({
                **dict(r),
                "actions": [dict(a) for a in actions],
                "orders": [dict(o) for o in orders],
            })

        return json.dumps({"success": True, "bot_id": bot_id, "reviews": result}, ensure_ascii=False)


@writer_tool
async def apply_fund_review_and_rebalance(
    bot_id: str,
    decision: str,
    reason: str,
    actions_json: str,
    holdings_json: str,
    orders_json: str = "[]",
    cash_after: float = -1.0,
    regime: str = "",
    review_md: str = "",
    cooldown_days: int = 7,
    trade_date: str = "",
    cash_before: float = -1.0,
    portfolio_value_before: float = -1.0,
    portfolio_value_after: float = -1.0,
    turnover_amount: float = -1.0,
    turnover_ratio: float = -1.0,
    paradigm: str = "",
) -> str:
    """写巡检结论 + 把实质动作转成 T 日 pending 在途单 + 更新持仓元数据。
    现金/持仓/收益的实际变动一律由 settle_pending_fund_orders 在 T+1 收口。
    paradigm: A | B1 | B2 | C，直接落 fund_bot_reviews/fund_bot_actions，不再事后回填。"""
    trade_date = _normalize_trade_date(trade_date)
    try:
        actions = json.loads(actions_json) if actions_json else []
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "actions_json 格式错误"}, ensure_ascii=False)
    try:
        holdings = json.loads(holdings_json) if holdings_json else []
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "holdings_json 格式错误"}, ensure_ascii=False)
    try:
        orders = json.loads(orders_json) if orders_json else []
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "orders_json 格式错误"}, ensure_ascii=False)

    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)
        material_action_count = _material_action_count(actions)
        if decision == "KEEP" and material_action_count > 0:
            return json.dumps({
                "success": False,
                "message": "decision=KEEP 时不允许带实质加减仓动作",
            }, ensure_ascii=False)
        if decision == "KEEP" and not (reason or "").strip():
            reason = "维持现有仓位，无实际调仓动作"

        if cash_before < 0:
            cash_before = float(account["cash"] or 0)

        current_rows = conn.execute(
            "SELECT fund_code, market_value FROM fund_bot_holdings "
            "WHERE bot_id = ? AND status = 'active'",
            (bot_id,)
        ).fetchall()
        current_invested = sum((r["market_value"] or 0) for r in current_rows)

        if portfolio_value_before < 0:
            portfolio_value_before = cash_before + current_invested
        if turnover_amount < 0:
            turnover_amount = sum(abs(float(a.get("amount") or 0)) for a in actions)
        if turnover_ratio < 0:
            base = portfolio_value_before or (cash_before + current_invested)
            turnover_ratio = turnover_amount / base * 100 if base else 0
        if decision == "KEEP":
            material_action_count = 0
            turnover_amount = 0.0
            turnover_ratio = 0.0

        # 0. 去重：同一 (bot_id, trade_date) 已有 review → 删掉它（及其 pending 在途单和 actions），
        #    避免 cron 当天多次重跑/手动收口落库各堆一条。已结算的（有 confirmed 在途单 或 有 actions
        #    —— 都意味着 settle 已跑过）保留不动，新写一条作为兜底（这种情况理论上不会发生在同一天）。
        for r in conn.execute(
            "SELECT review_id FROM fund_bot_reviews WHERE bot_id=? AND review_date=?",
            (bot_id, trade_date)
        ).fetchall():
            rid = r["review_id"]
            settled = conn.execute(
                "SELECT 1 FROM fund_bot_orders WHERE review_id=? AND status='confirmed' LIMIT 1", (rid,)
            ).fetchone() or conn.execute(
                "SELECT 1 FROM fund_bot_actions WHERE review_id=? LIMIT 1", (rid,)
            ).fetchone()
            if settled:
                continue
            conn.execute("DELETE FROM fund_bot_orders WHERE review_id=?", (rid,))
            conn.execute("DELETE FROM fund_bot_actions WHERE review_id=?", (rid,))
            conn.execute("DELETE FROM fund_bot_reviews WHERE review_id=?", (rid,))

        # 1. 写巡检记录
        cooldown_end = (datetime.now() + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO fund_bot_reviews "
            "(bot_id, review_date, regime, decision, action_count, reason, review_md, "
            "cooldown_end, cash_before, cash_after, "
            "portfolio_value_before, portfolio_value_after, "
            "turnover_amount, turnover_ratio, paradigm) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bot_id, trade_date, regime, decision, material_action_count, reason, review_md,
             cooldown_end, cash_before, cash_after if cash_after >= 0 else cash_before,
             portfolio_value_before, portfolio_value_after if portfolio_value_after >= 0 else portfolio_value_before,
             turnover_amount, turnover_ratio, paradigm or None)
        )
        review_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 2. 更新已有持仓的元数据（fund_name / asset_class / role / target_weight / thesis）。
        #    金额/份额/市值/收益类字段一律不动——它们只能由 settle_pending_fund_orders 在 T+1
        #    结算时按 fund_nav + 费率写入。bot 的 holdings_json 只贡献元数据。
        incoming = {h["fund_code"]: h for h in holdings}
        active_rows = conn.execute(
            "SELECT fund_code, holding_id FROM fund_bot_holdings WHERE bot_id = ? AND status = 'active'",
            (bot_id,)
        ).fetchall()
        active = {r["fund_code"]: dict(r) for r in active_rows}
        meta_updated = 0
        for fc, h in incoming.items():
            if fc in active:
                conn.execute(
                    "UPDATE fund_bot_holdings SET "
                    "fund_name = COALESCE(?, fund_name), "
                    "target_weight = COALESCE(?, target_weight), "
                    "asset_class = COALESCE(?, asset_class), "
                    "role = COALESCE(?, role), "
                    "thesis = COALESCE(?, thesis) "
                    "WHERE holding_id = ?",
                    (h.get("fund_name"), h.get("target_weight"), h.get("asset_class"),
                     h.get("role"), h.get("thesis"), active[fc]["holding_id"])
                )
                meta_updated += 1
            else:
                # holdings_json 里出现但还没建仓的基金：先建一个 shares=0 的占位行带上元数据，
                # 等 settle 收口时 UPDATE 填入份额/成本。这样新建仓当天的快照里 asset_class 就是对的。
                entry_nav = _get_nav(conn, fc, trade_date)[0] or 1.0
                conn.execute(
                    "INSERT INTO fund_bot_holdings "
                    "(bot_id, fund_code, fund_name, share_class, asset_class, role, "
                    "entry_date, entry_nav, latest_nav, shares, amount_invested, "
                    "market_value, unrealized_pnl, unrealized_pnl_pct, "
                    "target_weight, actual_weight, holding_days, high_nav, status, thesis) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?, 0, 0, ?, 'active', ?)",
                    (bot_id, fc, h.get("fund_name", ""), h.get("share_class", ""),
                     h.get("asset_class", ""), h.get("role", ""),
                     trade_date, entry_nav, entry_nav, h.get("target_weight"), entry_nav, h.get("thesis", ""))
                )
                active[fc] = {"fund_code": fc}
                meta_updated += 1

        # 3. 把每条实质动作转成 T 日 pending 在途单——T+1 由 settle_pending_fund_orders 收口。
        #    买入（ADD/BUY/INCREASE/INIT）：order_type='buy', order_amount = 申购金额(现金)。
        #    卖出（REDUCE/SELL/DECREASE/TAKE_PROFIT/STOP_LOSS/EXIT）：order_type='sell',
        #      order_amount = 要赎回的份额；按 bot 给的 ¥amount / 当日净值折算份额，
        #      若 ¥amount ≥ 当前市值的 99% 则视为全仓赎回（份额 = 当前全部份额）。
        #    HOLD：不下单。
        cur_holdings_full = {r["fund_code"]: dict(r) for r in conn.execute(
            "SELECT fund_code, shares, market_value, latest_nav FROM fund_bot_holdings "
            "WHERE bot_id=? AND status='active'", (bot_id,)
        ).fetchall()}
        orders_created = 0
        for a in actions:
            fc = a.get("fund_code")
            if not fc:
                continue
            atype = (a.get("action_type") or "HOLD").strip().upper()
            amount = float(a.get("amount") or 0)
            reason = a.get("reason") or atype
            if atype in _BUY_ACTION_TYPES:
                if amount <= 0:
                    continue
                conn.execute(
                    "INSERT INTO fund_bot_orders "
                    "(review_id, bot_id, fund_code, fund_name, order_type, order_date, "
                    "order_amount, reference_nav, action_reason, status) "
                    "VALUES (?, ?, ?, ?, 'buy', ?, ?, NULL, ?, 'pending')",
                    (review_id, bot_id, fc, a.get("fund_name") or (incoming.get(fc, {}).get("fund_name")) or "",
                     trade_date, _r(amount), reason)
                )
                orders_created += 1
            elif atype in _SELL_ACTION_TYPES:
                cur = cur_holdings_full.get(fc)
                if not cur:
                    continue
                cur_shares = float(cur["shares"] or 0.0)
                cur_mv = float(cur["market_value"] or 0.0)
                cur_nav = float(cur["latest_nav"] or 0.0)
                explicit_shares = a.get("shares")
                if explicit_shares not in (None, ""):
                    sell_shares = min(float(explicit_shares), cur_shares)
                elif amount <= 0 or (cur_mv > 0 and amount >= cur_mv * 0.99) or atype == "EXIT":
                    sell_shares = cur_shares  # 全仓赎回
                elif cur_nav > 0:
                    sell_shares = min(amount / cur_nav, cur_shares)
                else:
                    sell_shares = cur_shares
                if sell_shares <= 1e-6:
                    continue
                conn.execute(
                    "INSERT INTO fund_bot_orders "
                    "(review_id, bot_id, fund_code, fund_name, order_type, order_date, "
                    "order_amount, reference_nav, action_reason, status) "
                    "VALUES (?, ?, ?, ?, 'sell', ?, ?, NULL, ?, 'pending')",
                    (review_id, bot_id, fc, cur.get("fund_name") or "", trade_date,
                     _r(sell_shares, 6), reason)
                )
                orders_created += 1

        # 4. 也接受调用方显式传入的 orders_json（实践中 fund_md_to_db 传 []，留作扩展）
        for o in orders:
            conn.execute(
                "INSERT INTO fund_bot_orders "
                "(review_id, bot_id, fund_code, fund_name, order_type, order_date, "
                "order_amount, reference_nav, action_reason, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (review_id, bot_id, o["fund_code"], o.get("fund_name", ""), o["order_type"],
                 trade_date, o.get("order_amount", 0), o.get("reference_nav"), o.get("action_reason", ""))
            )
            orders_created += 1

        # 注意：不在此处改 cash / holdings / 写 actions——全部留给 T+1 的 settle_pending_fund_orders。

    return json.dumps({
        "success": True, "bot_id": bot_id, "review_id": review_id,
        "decision": decision, "trade_date": trade_date, "cooldown_end": cooldown_end,
        "metadata_updated": meta_updated,
        "orders_created": orders_created,
        "note": "在途单已挂 pending；T+1 由 settle_pending_fund_orders 按 fund_nav + 费率收口",
    }, ensure_ascii=False)


# ============================================================
# E. 每日快照
# ============================================================

def _material_fund_action_codes(conn, bot_id: str, trade_date: str) -> set[str]:
    """trade_date 当天有真实买卖（非 HOLD、有金额）的基金代码集合。"""
    rows = conn.execute(
        "SELECT DISTINCT fund_code FROM fund_bot_actions "
        "WHERE bot_id=? AND action_date=? AND fund_code IS NOT NULL AND fund_code != '' "
        "  AND UPPER(action_type) IN ('ADD','BUY','INCREASE','INIT','REDUCE','SELL','DECREASE','TAKE_PROFIT','STOP_LOSS','EXIT') "
        "  AND COALESCE(amount,0) > 0.01",
        (bot_id, trade_date)
    ).fetchall()
    return {r["fund_code"] for r in rows}


def _historical_holding_meta(conn, bot_id: str, fund_code: str, trade_date: str) -> dict:
    row = conn.execute(
        "SELECT * FROM fund_bot_holdings "
        "WHERE bot_id=? AND fund_code=? AND COALESCE(entry_date,'') <= ? "
        "ORDER BY CASE WHEN status='active' THEN 1 ELSE 0 END DESC, COALESCE(exit_date,''), holding_id DESC LIMIT 1",
        (bot_id, fund_code, trade_date),
    ).fetchone()
    if row:
        return dict(row)
    return _fund_info_defaults(conn, fund_code)


def _compute_fund_snapshot(conn, bot_id: str, trade_date: str) -> dict:
    """单 bot 某交易日的快照计算 + 写库（fund_bot_daily_snapshots / fund_bot_position_snapshots /
    更新 fund_bot_holdings 现态）。这是系统层唯一的基金账户业绩计算入口。

    市值口径：默认 shares × 当日 fund_nav；当天没有真实调仓且份额未变、且 fund_nav 有日收益率时，
    用「上一日持仓快照市值 × (1+日收益率)」递推，避免 fund_nav 在累计/单位净值口径间切换造成跳变。
    收益率：daily_return = total/prev_total - 1；cumulative = total/initial - 1；
    max_drawdown：扫整条 net_value 序列（起点 1.0）找最大 peak-to-trough。
    管理费/托管费/销售服务费已内含在公布净值里，不二次计提。
    """
    account = _get_account(conn, bot_id)
    if not account:
        return {"success": False, "message": f"bot {bot_id} 无账户"}
    initial_capital = float(account["initial_capital"] or 0.0)
    state = _replay_fund_account_state(conn, bot_id, trade_date)
    cash = float(state["cash"] or 0.0)
    positions = state["positions"]

    prev_snapshot = conn.execute(
        "SELECT total_value FROM fund_bot_daily_snapshots WHERE bot_id = ? AND trade_date < ? "
        "ORDER BY trade_date DESC LIMIT 1",
        (bot_id, trade_date)
    ).fetchone()
    prev_total = float(prev_snapshot["total_value"]) if prev_snapshot and prev_snapshot["total_value"] else initial_capital

    # 上一日的持仓级快照（用于日收益率递推 + 产品 daily_pnl）
    prev_positions: dict[str, dict] = {}
    prev_date_row = conn.execute(
        "SELECT MAX(trade_date) as d FROM fund_bot_position_snapshots WHERE bot_id = ? AND trade_date < ?",
        (bot_id, trade_date)
    ).fetchone()
    if prev_date_row and prev_date_row["d"]:
        for p in conn.execute(
            "SELECT fund_code, market_value, shares FROM fund_bot_position_snapshots WHERE bot_id = ? AND trade_date = ?",
            (bot_id, prev_date_row["d"])
        ).fetchall():
            prev_positions[p["fund_code"]] = {"mv": p["market_value"], "shares": p["shares"]}

    material_codes = _material_fund_action_codes(conn, bot_id, trade_date)
    invested_value = 0.0
    asset_weights = {"股票类": 0.0, "债券类": 0.0, "黄金类": 0.0}
    position_snapshots = []

    for fc, current in sorted(positions.items()):
        shares = float(current["shares"] or 0.0)
        amount_invested = float(current["cost"] or 0.0)
        meta = _historical_holding_meta(conn, bot_id, fc, trade_date)
        # 当日 fund_nav 行（含日收益率）
        nav_row = conn.execute(
            "SELECT nav, daily_return_pct FROM fund_nav WHERE fund_code=? AND nav_date<=? ORDER BY nav_date DESC LIMIT 1",
            (fc, trade_date)
        ).fetchone()
        nav = float(nav_row["nav"]) if nav_row and nav_row["nav"] else float(current.get("entry_nav") or 1.0)
        daily_nav_return_pct = nav_row["daily_return_pct"] if nav_row and nav_row["daily_return_pct"] is not None else None

        prev = prev_positions.get(fc)
        prev_mv = float(prev["mv"]) if prev and prev["mv"] is not None else amount_invested
        prev_shares = float(prev["shares"]) if prev and prev["shares"] is not None else None
        shares_changed = prev_shares is None or abs(shares - prev_shares) > 1e-6

        nav_mv = shares * nav
        if fc in material_codes and shares_changed:
            mv = nav_mv  # 真实调仓且份额确实变了 → 用调仓后的 shares×nav
        elif prev and daily_nav_return_pct is not None and not shares_changed:
            mv = prev_mv * (1 + daily_nav_return_pct / 100.0)  # 份额没变 → 沿用日收益率递推
        else:
            mv = nav_mv

        unrealized_pnl = mv - amount_invested
        unrealized_pnl_pct = (unrealized_pnl / amount_invested * 100) if amount_invested else 0.0
        holding_days = _calc_holding_days(current.get("entry_date") or trade_date, trade_date)
        high_nav = max(float((meta or {}).get("high_nav") or 0.0), nav)
        daily_pnl = mv - prev_mv
        invested_value += mv
        ac = current.get("asset_class") or meta.get("asset_class") or ""
        if ac in asset_weights:
            asset_weights[ac] += mv

        position_snapshots.append({
            "fund_code": fc, "asset_class": ac, "role": current.get("role") or meta.get("role"),
            "shares": shares, "nav": nav, "market_value": mv, "daily_pnl": daily_pnl,
            "cumulative_return_pct": unrealized_pnl_pct, "holding_days": holding_days,
        })
        active_row = conn.execute(
            "SELECT holding_id FROM fund_bot_holdings WHERE bot_id=? AND fund_code=? AND status='active' ORDER BY holding_id DESC LIMIT 1",
            (bot_id, fc),
        ).fetchone()
        if active_row:
            conn.execute(
                "UPDATE fund_bot_holdings SET latest_nav=?, market_value=?, unrealized_pnl=?, "
                "unrealized_pnl_pct=?, holding_days=?, high_nav=? WHERE holding_id=?",
                (_r(nav, 6), _r(mv), _r(unrealized_pnl), _r(unrealized_pnl_pct, 4),
                 holding_days, _r(high_nav, 6), active_row["holding_id"])
            )

    total_value = cash + invested_value
    net_value = total_value / initial_capital if initial_capital else 1.0
    for ps in position_snapshots:
        ps["weight"] = _r(ps["market_value"] / total_value if total_value else 0, 6)
        conn.execute(
            "UPDATE fund_bot_holdings SET actual_weight=? WHERE bot_id=? AND fund_code=? AND status='active'",
            (ps["weight"], bot_id, ps["fund_code"])
        )

    daily_return_pct = (total_value - prev_total) / prev_total * 100 if prev_total else 0.0
    cumulative_return_pct = (total_value - initial_capital) / initial_capital * 100 if initial_capital else 0.0
    hist_navs = [r[0] for r in conn.execute(
        "SELECT net_value FROM fund_bot_daily_snapshots WHERE bot_id=? AND trade_date < ? ORDER BY trade_date",
        (bot_id, trade_date)
    ).fetchall() if r[0] is not None]
    max_drawdown_pct = _calc_max_drawdown([1.0] + hist_navs + [net_value])

    eq_w = asset_weights.get("股票类", 0) / total_value if total_value else 0
    bd_w = asset_weights.get("债券类", 0) / total_value if total_value else 0
    gd_w = asset_weights.get("黄金类", 0) / total_value if total_value else 0
    ch_w = cash / total_value if total_value else 0
    holdings_summary = [{"fund_code": ps["fund_code"], "weight": ps["weight"],
                        "asset_class": ps["asset_class"], "market_value": _r(ps["market_value"])}
                       for ps in position_snapshots]

    conn.execute(
        "INSERT OR REPLACE INTO fund_bot_daily_snapshots "
        "(bot_id, trade_date, initial_capital, cash, invested_value, total_value, "
        "net_value, daily_return_pct, cumulative_return_pct, max_drawdown_pct, "
        "equity_weight, bond_weight, gold_weight, cash_weight, holdings_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (bot_id, trade_date, _r(initial_capital), _r(cash), _r(invested_value),
         _r(total_value), _r(net_value, 6), _r(daily_return_pct, 4),
         _r(cumulative_return_pct, 4), _r(max_drawdown_pct, 4),
         _r(eq_w, 6), _r(bd_w, 6), _r(gd_w, 6), _r(ch_w, 6),
         json.dumps(holdings_summary, ensure_ascii=False))
    )
    for ps in position_snapshots:
        conn.execute(
            "INSERT OR REPLACE INTO fund_bot_position_snapshots "
            "(bot_id, fund_code, trade_date, asset_class, role, shares, nav, market_value, "
            "weight, daily_pnl, cumulative_return_pct, holding_days) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bot_id, ps["fund_code"], trade_date, ps["asset_class"], ps["role"],
             _r(ps["shares"], 6), _r(ps["nav"], 6), _r(ps["market_value"]),
             ps["weight"], _r(ps["daily_pnl"]), _r(ps["cumulative_return_pct"], 4), ps["holding_days"])
        )
    return {
        "success": True, "bot_id": bot_id, "trade_date": trade_date,
        "total_value": _r(total_value), "net_value": _r(net_value, 6),
        "daily_return_pct": _r(daily_return_pct, 4),
        "cumulative_return_pct": _r(cumulative_return_pct, 4),
        "max_drawdown_pct": _r(max_drawdown_pct, 4),
        "asset_allocation": {"equity": _r(eq_w*100), "bond": _r(bd_w*100), "gold": _r(gd_w*100), "cash": _r(ch_w*100)},
        "positions": len(position_snapshots),
    }


@writer_tool
async def record_fund_snapshot(bot_id: str, trade_date: str = "") -> str:
    """记录单 bot 某交易日的收益快照（系统层唯一的账户业绩计算入口）。
    写 fund_bot_daily_snapshots + fund_bot_position_snapshots，并更新 fund_bot_holdings 现态。
    计算口径见 _compute_fund_snapshot 的 docstring。"""
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        result = _compute_fund_snapshot(conn, bot_id, trade_date)
    return json.dumps(result, ensure_ascii=False)


@writer_tool
async def record_all_fund_snapshots(trade_date: str = "") -> str:
    """对所有有账户的 bot 批量记录某交易日快照（cron Phase D 用，一次调用代替逐 bot 调）。"""
    trade_date = _normalize_trade_date(trade_date)
    results = []
    with get_conn() as conn:
        bot_ids = [r["bot_id"] for r in conn.execute("SELECT bot_id FROM fund_bot_accounts ORDER BY bot_id")]
        for bid in bot_ids:
            try:
                results.append(_compute_fund_snapshot(conn, bid, trade_date))
            except Exception as e:
                results.append({"success": False, "bot_id": bid, "message": str(e)})
    ok = sum(1 for r in results if r.get("success"))
    return json.dumps({"success": True, "trade_date": trade_date, "ok": ok, "total": len(results), "results": results}, ensure_ascii=False)


@mcp.tool()
async def get_fund_curve(bot_id: str, start_date: str = "", end_date: str = "") -> str:
    """获取 bot 的净值曲线（每日快照序列）。"""
    with get_conn() as conn:
        query = "SELECT * FROM fund_bot_daily_snapshots WHERE bot_id = ?"
        params = [bot_id]
        if start_date:
            query += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND trade_date <= ?"
            params.append(end_date)
        query += " ORDER BY trade_date"

        rows = conn.execute(query, params).fetchall()
        return json.dumps({
            "success": True, "bot_id": bot_id,
            "count": len(rows), "snapshots": [dict(r) for r in rows],
        }, ensure_ascii=False)


@mcp.tool()
async def get_fund_position_snapshots(bot_id: str, trade_date: str = "") -> str:
    """获取某日的持仓级快照。默认取最近一天。"""
    with get_conn() as conn:
        if not trade_date:
            latest = conn.execute(
                "SELECT MAX(trade_date) as d FROM fund_bot_position_snapshots WHERE bot_id = ?",
                (bot_id,)
            ).fetchone()
            trade_date = latest["d"] if latest and latest["d"] else ""

        if not trade_date:
            return json.dumps({"success": False, "message": "无快照数据"}, ensure_ascii=False)

        rows = conn.execute(
            "SELECT * FROM fund_bot_position_snapshots "
            "WHERE bot_id = ? AND trade_date = ? ORDER BY weight DESC",
            (bot_id, trade_date)
        ).fetchall()

        return json.dumps({
            "success": True, "bot_id": bot_id, "trade_date": trade_date,
            "positions": [dict(r) for r in rows],
        }, ensure_ascii=False)


# ============================================================
# F. 执行状态管理
# ============================================================

@writer_tool
async def save_system_run(
    run_id: str,
    trade_date: str = "",
    data_version: str = "",
    phase_a_status: str = "success",
    gate_status: str = "passed",
    skip_reason: str = "",
) -> str:
    """保存或更新一轮系统执行记录。"""
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fund_system_runs "
            "(run_id, trade_date, data_version, phase_a_status, gate_status, skip_reason, "
            "created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, "
            "COALESCE((SELECT created_at FROM fund_system_runs WHERE run_id = ?), datetime('now')))",
            (run_id, trade_date, data_version, phase_a_status, gate_status, skip_reason, run_id)
        )
    return json.dumps({
        "success": True, "run_id": run_id, "trade_date": trade_date,
    }, ensure_ascii=False)


@writer_tool
async def save_allocation_run(
    run_id: str,
    bot_id: str,
    asset_target_json: str,
    trade_date: str = "",
    regime: str = "",
    market_summary_md: str = "",
    paradigm: str = "",
) -> str:
    """保存大类资产配置结果（市场环境判断）。
    asset_target_json: {"regime":..,"timing_stance":..,"central_baseline":..,"today_target":..,...}
    paradigm: A | B1 | B2 | C（与 fund_paradigm_runs 一致），直接落表，不再事后回填。"""
    trade_date = _normalize_trade_date(trade_date)
    try:
        parsed = json.loads(asset_target_json) if asset_target_json else {}
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "asset_target_json 格式错误"}, ensure_ascii=False)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_allocation_runs "
            "(run_id, bot_id, trade_date, regime, market_summary_md, asset_target_json, paradigm) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, bot_id, trade_date, regime, market_summary_md,
             json.dumps(parsed, ensure_ascii=False), paradigm or None)
        )
        allocation_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    return json.dumps({
        "success": True, "allocation_id": allocation_id,
        "run_id": run_id, "bot_id": bot_id, "trade_date": trade_date,
        "regime": regime, "paradigm": paradigm or None, "asset_target": parsed,
    }, ensure_ascii=False)


@mcp.tool()
async def get_latest_allocation_run(bot_id: str, trade_date: str = "") -> str:
    """获取 bot 最近一次大类资产配置结果。"""
    with get_conn() as conn:
        if trade_date:
            row = conn.execute(
                "SELECT * FROM fund_allocation_runs WHERE bot_id = ? AND trade_date = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (bot_id, trade_date)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM fund_allocation_runs WHERE bot_id = ? "
                "ORDER BY trade_date DESC, created_at DESC LIMIT 1",
                (bot_id,)
            ).fetchone()

    if not row:
        return json.dumps({"success": False, "message": f"bot {bot_id} 无 allocation_run"}, ensure_ascii=False)

    result = dict(row)
    try:
        result["asset_target"] = json.loads(result.get("asset_target_json") or "{}")
    except json.JSONDecodeError:
        result["asset_target"] = {}
    return json.dumps({"success": True, "data": result}, ensure_ascii=False)


@writer_tool
async def save_selection_run(
    bot_id: str,
    trade_date: str = "",
    run_id: str = "",
    layer1_count: int = 0,
    layer2_count: int = 0,
    layer3_count: int = 0,
    layer4_count: int = 0,
    selected_funds_json: str = "[]",
    eliminated_json: str = "[]",
    selection_md: str = "",
) -> str:
    """保存选品漏斗追踪（Phase B 结果）。"""
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_selection_runs "
            "(run_id, bot_id, trade_date, layer1_count, layer2_count, "
            "layer3_count, layer4_count, selected_funds_json, eliminated_json, selection_md) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id or None, bot_id, trade_date, layer1_count, layer2_count,
             layer3_count, layer4_count, selected_funds_json, eliminated_json, selection_md)
        )
        selection_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    return json.dumps({
        "success": True, "selection_id": selection_id,
        "bot_id": bot_id, "trade_date": trade_date,
        "funnel": {"L1": layer1_count, "L2": layer2_count, "L3": layer3_count, "L4": layer4_count},
    }, ensure_ascii=False)


@writer_tool
async def rollback_fund_bot_run(bot_id: str, trade_date: str, run_id: str = "") -> str:
    """回滚某 bot 某交易日某 run 的半成品写入（process_bot 中途失败时用，取代脚本里的裸 SQL 补偿）。
    删除：fund_allocation_runs / fund_selection_runs（按 run_id）+ 当日 fund_bot_reviews +
    当日 status='pending' 的 fund_bot_orders。注意 fund_bot_actions 是 T+1 settle 时才写的，
    同日回滚不涉及；持仓/现金也只在 settle 改，同日回滚不动。"""
    trade_date = _normalize_trade_date(trade_date)
    deleted = {}
    with get_conn() as conn:
        if run_id:
            r = conn.execute("DELETE FROM fund_selection_runs WHERE bot_id=? AND trade_date=? AND run_id=?",
                             (bot_id, trade_date, run_id))
            deleted["selection_runs"] = r.rowcount
            r = conn.execute("DELETE FROM fund_allocation_runs WHERE bot_id=? AND trade_date=? AND run_id=?",
                             (bot_id, trade_date, run_id))
            deleted["allocation_runs"] = r.rowcount
        # reviews/orders 没有 run_id 字段，按 bot+date 删（cron 一天一轮，安全）
        review_ids = [row["review_id"] for row in conn.execute(
            "SELECT review_id FROM fund_bot_reviews WHERE bot_id=? AND review_date=?", (bot_id, trade_date)
        ).fetchall()]
        if review_ids:
            qmarks = ",".join("?" * len(review_ids))
            conn.execute(f"DELETE FROM fund_bot_orders WHERE review_id IN ({qmarks}) AND status='pending'", review_ids)
            r = conn.execute("DELETE FROM fund_bot_reviews WHERE bot_id=? AND review_date=?", (bot_id, trade_date))
            deleted["reviews"] = r.rowcount
        r2 = conn.execute("DELETE FROM fund_bot_orders WHERE bot_id=? AND order_date=? AND status='pending'",
                          (bot_id, trade_date))
        deleted["pending_orders"] = r2.rowcount
    return json.dumps({"success": True, "bot_id": bot_id, "trade_date": trade_date, "run_id": run_id or None, "deleted": deleted}, ensure_ascii=False)


@mcp.tool()
async def get_latest_selection_run(bot_id: str) -> str:
    """获取 bot 最近一次选品漏斗结果。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM fund_selection_runs WHERE bot_id = ? "
            "ORDER BY trade_date DESC, created_at DESC LIMIT 1",
            (bot_id,)
        ).fetchone()

    if not row:
        return json.dumps({"success": False, "message": f"bot {bot_id} 无 selection_run"}, ensure_ascii=False)

    result = dict(row)
    try:
        result["selected_funds"] = json.loads(result.get("selected_funds_json") or "[]")
    except json.JSONDecodeError:
        result["selected_funds"] = []
    try:
        result["eliminated"] = json.loads(result.get("eliminated_json") or "[]")
    except json.JSONDecodeError:
        result["eliminated"] = []
    return json.dumps({"success": True, "data": result}, ensure_ascii=False)


# ============================================================
# G. 数据更新（刷新脚本用，批量 upsert）
# ============================================================

@writer_tool
async def upsert_fund_info(funds_json: str) -> str:
    """批量更新基金主表。funds_json: [{"fund_code":"008528","fund_name":"...","fund_type":"混合型",...}]"""
    try:
        funds = json.loads(funds_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "funds_json 格式错误"}, ensure_ascii=False)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        for f in funds:
            conn.execute(
                "INSERT OR REPLACE INTO fund_info "
                "(fund_code, fund_name, fund_company, fund_manager, fund_type, "
                "share_class, established_date, scale, purchase_status, redeem_status, "
                "mgmt_fee, custody_fee, purchase_fee, sales_service_fee, redeem_fee_json, "
                "theme, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f["fund_code"], f.get("fund_name", ""), f.get("fund_company"),
                 f.get("fund_manager"), f.get("fund_type"), f.get("share_class"),
                 f.get("established_date"), f.get("scale"), f.get("purchase_status"),
                 f.get("redeem_status"), f.get("mgmt_fee"), f.get("custody_fee"),
                 f.get("purchase_fee"), f.get("sales_service_fee"),
                 f.get("redeem_fee_json"), f.get("theme"), now)
            )

    return json.dumps({"success": True, "upserted": len(funds)}, ensure_ascii=False)


@writer_tool
async def upsert_fund_fees(fees_json: str) -> str:
    """批量更新基金费率列（只动 fund_info 的费率字段，不碰其它列）。
    fees_json: [{"fund_code":"003957","mgmt_fee":0.8,"custody_fee":0.1,"sales_service_fee":0.0,
      "purchase_fee":0.08,"redeem_fee_json":"[{\"max_days\":7,\"rate\":0.015},...]"}]
    purchase_fee/mgmt_fee/... 是百分比数（0.08 = 0.08%）；redeem_fee_json 是阶梯表 JSON 字符串。"""
    try:
        rows = json.loads(fees_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "fees_json 格式错误"}, ensure_ascii=False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated = 0
    with get_conn() as conn:
        for f in rows:
            fc = f.get("fund_code")
            if not fc:
                continue
            r = conn.execute(
                "UPDATE fund_info SET mgmt_fee=?, custody_fee=?, sales_service_fee=?, "
                "purchase_fee=?, redeem_fee_json=?, updated_at=? WHERE fund_code=?",
                (f.get("mgmt_fee"), f.get("custody_fee"), f.get("sales_service_fee"),
                 f.get("purchase_fee"), f.get("redeem_fee_json"), now, fc)
            )
            updated += r.rowcount
    return json.dumps({"success": True, "updated": updated, "received": len(rows)}, ensure_ascii=False)


@writer_tool
async def upsert_fund_nav(navs_json: str) -> str:
    """批量更新基金净值。navs_json: [{"fund_code":"008528","nav_date":"2026-04-23","nav":2.55,"acc_nav":2.55,"daily_return_pct":0.5}]"""
    try:
        navs = json.loads(navs_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "navs_json 格式错误"}, ensure_ascii=False)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        for n in navs:
            conn.execute(
                "INSERT OR REPLACE INTO fund_nav "
                "(fund_code, nav_date, nav, acc_nav, daily_return_pct, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (n["fund_code"], n["nav_date"], n.get("nav"), n.get("acc_nav"),
                 n.get("daily_return_pct"), now)
            )

    return json.dumps({"success": True, "upserted": len(navs)}, ensure_ascii=False)


@writer_tool
async def upsert_fund_performance(perfs_json: str) -> str:
    """批量更新基金业绩。"""
    try:
        perfs = json.loads(perfs_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "perfs_json 格式错误"}, ensure_ascii=False)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        for p in perfs:
            conn.execute(
                "INSERT OR REPLACE INTO fund_performance "
                "(fund_code, as_of_date, period, return_pct, rank_pct, rank_text, "
                "max_drawdown_pct, volatility_pct, sharpe_ratio, calmar_ratio, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (p["fund_code"], p["as_of_date"], p["period"],
                 p.get("return_pct"), p.get("rank_pct"), p.get("rank_text"),
                 p.get("max_drawdown_pct"), p.get("volatility_pct"),
                 p.get("sharpe_ratio"), p.get("calmar_ratio"), now)
            )

    return json.dumps({"success": True, "upserted": len(perfs)}, ensure_ascii=False)


@writer_tool
async def upsert_fund_style(styles_json: str) -> str:
    """批量更新基金风格。"""
    try:
        styles = json.loads(styles_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "styles_json 格式错误"}, ensure_ascii=False)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        for s in styles:
            conn.execute(
                "INSERT OR REPLACE INTO fund_style "
                "(fund_code, as_of_date, size_style, invest_style, "
                "equity_pct, bond_pct, cash_pct, other_pct, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s["fund_code"], s["as_of_date"], s.get("size_style"),
                 s.get("invest_style"), s.get("equity_pct"), s.get("bond_pct"),
                 s.get("cash_pct"), s.get("other_pct"), now)
            )

    return json.dumps({"success": True, "upserted": len(styles)}, ensure_ascii=False)


@writer_tool
async def upsert_fund_industry(industries_json: str) -> str:
    """批量更新基金行业持仓。"""
    try:
        industries = json.loads(industries_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "industries_json 格式错误"}, ensure_ascii=False)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        for i in industries:
            conn.execute(
                "INSERT OR REPLACE INTO fund_industry "
                "(fund_code, as_of_date, industry, weight_pct, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (i["fund_code"], i["as_of_date"], i["industry"], i.get("weight_pct"), now)
            )

    return json.dumps({"success": True, "upserted": len(industries)}, ensure_ascii=False)


@writer_tool
async def upsert_fund_top_stocks(stocks_json: str) -> str:
    """批量更新基金重仓股。"""
    try:
        stocks = json.loads(stocks_json)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "message": "stocks_json 格式错误"}, ensure_ascii=False)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        for s in stocks:
            conn.execute(
                "INSERT OR REPLACE INTO fund_top_stocks "
                "(fund_code, as_of_date, stock_rank, stock_code, stock_name, weight_pct, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (s["fund_code"], s["as_of_date"], s.get("stock_rank"),
                 s["stock_code"], s.get("stock_name"), s.get("weight_pct"), now)
            )

    return json.dumps({"success": True, "upserted": len(stocks)}, ensure_ascii=False)


# ============================================================
# H. 能力圈宣告与 Phase B-1 范式(2026-04-29 三层框架)
# ============================================================

@writer_tool
async def save_capability_circle(
    bot_id: str,
    as_of_date: str,
    macro: bool = False,
    industry_rotation: bool = False,
    industry_focus: str = "",
    fund_alpha: bool = False,
    default_paradigm: str = "",
    secondary_paradigm: str = "",
    switch_rules_json: str = "",
    evidence_md: str = "",
    next_assessment_due: str = "",
) -> str:
    """保存(或覆盖)某 bot 在某 as_of_date 的能力圈宣告。

    互斥规则: industry_rotation=true 时 industry_focus 必须为空;反之亦然。
    至少有一项能力为真,否则在写入时仍会接受(用于"无能力圈"边界场景),
    但 paradigm_active 在 Phase B-1 会输出 SKIP。
    """
    has_b1 = bool(industry_rotation)
    has_b2 = bool(industry_focus.strip())
    if has_b1 and has_b2:
        return json.dumps({
            "success": False,
            "message": "B1 (industry_rotation=true) 与 B2 (industry_focus 非空) 互斥,不能同时声明",
        }, ensure_ascii=False)

    focus = industry_focus.strip() or None
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_capability_circle "
            "(bot_id, as_of_date, macro, industry_rotation, industry_focus, "
            " fund_alpha, default_paradigm, secondary_paradigm, switch_rules_json, "
            " evidence_md, next_assessment_due) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bot_id, as_of_date) DO UPDATE SET "
            " macro=excluded.macro, industry_rotation=excluded.industry_rotation, "
            " industry_focus=excluded.industry_focus, fund_alpha=excluded.fund_alpha, "
            " default_paradigm=excluded.default_paradigm, "
            " secondary_paradigm=excluded.secondary_paradigm, "
            " switch_rules_json=excluded.switch_rules_json, "
            " evidence_md=excluded.evidence_md, "
            " next_assessment_due=excluded.next_assessment_due",
            (bot_id, as_of_date, int(macro), int(industry_rotation), focus,
             int(fund_alpha), default_paradigm or None, secondary_paradigm or None,
             switch_rules_json or None, evidence_md or None,
             next_assessment_due or None)
        )
    return json.dumps({"success": True, "bot_id": bot_id, "as_of_date": as_of_date},
                      ensure_ascii=False)


@mcp.tool()
async def get_capability_circle(bot_id: str, as_of_date: str = "") -> str:
    """读取 bot 最新(或指定日期)的能力圈宣告。"""
    with get_conn() as conn:
        if as_of_date:
            row = conn.execute(
                "SELECT * FROM fund_capability_circle "
                "WHERE bot_id = ? AND as_of_date = ?",
                (bot_id, as_of_date)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM fund_capability_circle WHERE bot_id = ? "
                "ORDER BY as_of_date DESC LIMIT 1",
                (bot_id,)
            ).fetchone()
    if not row:
        return json.dumps({"found": False, "bot_id": bot_id}, ensure_ascii=False)
    d = dict(row)
    d["macro"] = bool(d["macro"])
    d["industry_rotation"] = bool(d["industry_rotation"])
    d["fund_alpha"] = bool(d["fund_alpha"])
    return json.dumps({"found": True, **d}, ensure_ascii=False)


@writer_tool
async def save_paradigm_run(
    bot_id: str,
    trade_date: str,
    paradigm_active: str,
    run_id: str = "",
    capability_field: str = "",
    capability_value: str = "",
    switched_from: str = "",
    reason: str = "",
) -> str:
    """保存某 bot 某日的 Phase B-1 输出。

    paradigm_active: A | B1 | B2 | C | SKIP
    capability_field: macro | industry_rotation | industry_focus | fund_alpha
    """
    valid = {"A", "B1", "B2", "C", "SKIP"}
    if paradigm_active not in valid:
        return json.dumps({
            "success": False,
            "message": f"paradigm_active 必须是 {sorted(valid)} 之一,当前: {paradigm_active}",
        }, ensure_ascii=False)
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_paradigm_runs "
            "(bot_id, trade_date, run_id, paradigm_active, capability_field, "
            " capability_value, switched_from, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bot_id, trade_date) DO UPDATE SET "
            " run_id=excluded.run_id, paradigm_active=excluded.paradigm_active, "
            " capability_field=excluded.capability_field, "
            " capability_value=excluded.capability_value, "
            " switched_from=excluded.switched_from, reason=excluded.reason",
            (bot_id, trade_date, run_id or None, paradigm_active,
             capability_field or None, capability_value or None,
             switched_from or None, reason or None)
        )
    return json.dumps({
        "success": True, "bot_id": bot_id, "trade_date": trade_date,
        "paradigm_active": paradigm_active,
    }, ensure_ascii=False)


@mcp.tool()
async def get_latest_paradigm(bot_id: str, trade_date: str = "") -> str:
    """读取 bot 最近一日(或指定 trade_date)的 paradigm。"""
    with get_conn() as conn:
        if trade_date:
            row = conn.execute(
                "SELECT * FROM fund_paradigm_runs "
                "WHERE bot_id = ? AND trade_date = ?",
                (bot_id, trade_date)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM fund_paradigm_runs WHERE bot_id = ? "
                "ORDER BY trade_date DESC LIMIT 1",
                (bot_id,)
            ).fetchone()
    if not row:
        return json.dumps({"found": False, "bot_id": bot_id}, ensure_ascii=False)
    return json.dumps({"found": True, **dict(row)}, ensure_ascii=False)


# ============================================================
# 启动
# ============================================================

def main():
    init_db()

    parser = argparse.ArgumentParser(description="fund-portfolio-mcp")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="streamable-http")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18071)
    args = parser.parse_args()

    if args.transport != "stdio":
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
