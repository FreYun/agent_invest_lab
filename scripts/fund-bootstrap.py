#!/usr/bin/env python3
"""
基金建仓脚本：从最新一次 fund_selection_runs 的目标组合 → 落地到持仓

执行链：
  1. 读 bot 最新 fund_selection_runs（按 trade_date 取最新）
  2. 在 fund_bot_accounts 开户（initial_capital, cash 初始全部为现金）
  3. 给每只基金（除 CASH/外部货基）插 fund_bot_orders 行（order_type=BUY, status=pending,
     order_amount = target_amount, reference_nav = fund_nav.nav 当日）
  4. T+1 模拟成交：以最新 fund_nav.nav 作为 confirm_nav，
     计算 confirmed_shares = order_amount / confirm_nav，
     更新 orders → confirmed，扣减 accounts.cash，
     在 fund_bot_holdings 写入持仓行

用法：
  python3 scripts/fund-bootstrap.py --bot bot1 --capital 100000
  python3 scripts/fund-bootstrap.py --bot bot1,bot2  # 批量

幂等：同 bot 多次运行 → 重置（DELETE 该 bot 在 4 张表的所有记录后重新建仓）。
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

DB_PATH = "/home/rooot/agent_invest_lab/data/fund.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_latest_selection(conn, bot_id: str):
    """取该 bot 最新一次选品结果"""
    row = conn.execute(
        "SELECT selection_id, run_id, trade_date, selected_funds_json "
        "FROM fund_selection_runs WHERE bot_id=? "
        "ORDER BY trade_date DESC, selection_id DESC LIMIT 1",
        (bot_id,)
    ).fetchone()
    if not row:
        return None
    try:
        funds = json.loads(row["selected_funds_json"])
    except json.JSONDecodeError as e:
        print(f"  [{bot_id}] selected_funds_json 解析失败: {e}", file=sys.stderr)
        return None
    return {
        "selection_id": row["selection_id"],
        "run_id": row["run_id"],
        "trade_date": row["trade_date"],
        "funds": funds,
    }


def get_latest_nav(conn, fund_code: str):
    row = conn.execute(
        "SELECT nav, nav_date FROM fund_nav WHERE fund_code=? ORDER BY nav_date DESC LIMIT 1",
        (fund_code,)
    ).fetchone()
    return (row["nav"], row["nav_date"]) if row else (None, None)


def reset_bot(conn, bot_id: str):
    """清掉该 bot 已有的账户/持仓/订单（重建仓时使用）"""
    with conn:
        for tbl in ("fund_bot_position_snapshots", "fund_bot_daily_snapshots",
                    "fund_bot_actions", "fund_bot_reviews",
                    "fund_bot_orders", "fund_bot_holdings", "fund_bot_accounts"):
            conn.execute(f"DELETE FROM {tbl} WHERE bot_id=?", (bot_id,))


def is_external_or_cash(fund_code: str, fund_name: str) -> bool:
    """判断是否为「外部配置」(非 fund.db 内的基金，比如 CASH/货币基金)"""
    if not fund_code or fund_code in ("-", "CASH", "—"):
        return True
    if fund_name and ("货币" in fund_name or "外部" in fund_name or "现金" in fund_name):
        return True
    # 黄金白名单不在 fund.db 里，但 bot 自己应该写了具体代码（如 002610）
    # 这些可以正常下单（fund_nav 没数据时单独处理）
    return False


def bootstrap_bot(conn, bot_id: str, capital: float, reset: bool = True) -> dict:
    sel = get_latest_selection(conn, bot_id)
    if not sel:
        return {"bot_id": bot_id, "status": "no_selection", "msg": "没有 fund_selection_runs 记录"}

    funds = sel["funds"]
    trade_date = sel["trade_date"]
    print(f"\n=== [{bot_id}] 建仓 ===")
    print(f"  来源选品: selection_id={sel['selection_id']}, trade_date={trade_date}, 共 {len(funds)} 项")

    if reset:
        reset_bot(conn, bot_id)
        print(f"  已清除 {bot_id} 的旧账户/持仓/订单")

    # 1. 开户
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.execute(
            "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (bot_id, capital, capital, now, now)
        )
    print(f"  开户：initial_capital=¥{capital:,.0f}, cash=¥{capital:,.0f}")

    # 2. 下单 + 模拟 T+1 成交（一步到位，因为是初始建仓不分批）
    confirm_date = date.today().isoformat()  # 假设今天就是 T+1
    cash_remaining = capital
    orders_placed = 0
    holdings_created = 0
    skipped_external = []

    for f in funds:
        code = (f.get("fund_code") or "").strip()
        name = f.get("fund_name") or ""
        target_weight = float(f.get("target_weight") or 0)
        target_amount = float(f.get("target_amount") or 0)
        asset_class = f.get("asset_class") or ""
        role = f.get("role") or ""
        reason = f.get("reason") or ""
        theme = f.get("theme")

        if target_amount <= 0:
            continue

        # 外部配置（货币/现金）→ 不下单，作为账户现金保留
        if is_external_or_cash(code, name):
            skipped_external.append((code or "CASH", name, target_amount))
            continue

        # 取参考净值
        ref_nav, nav_date = get_latest_nav(conn, code)
        confirm_nav = ref_nav  # 简化：建仓用最新可见净值作为确认价

        # 3. 写 order（pending → 立刻 confirmed）
        with conn:
            cur = conn.execute(
                "INSERT INTO fund_bot_orders "
                "(bot_id, fund_code, fund_name, order_type, order_date, confirm_date, "
                " order_amount, reference_nav, confirm_nav, confirmed_shares, confirmed_amount, "
                " fee, action_reason, status) "
                "VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (bot_id, code, name, trade_date, confirm_date,
                 target_amount, ref_nav, confirm_nav,
                 target_amount / confirm_nav if confirm_nav else None,
                 target_amount, 0.0,  # 建仓忽略申购费简化
                 "Bootstrap 初始建仓", "confirmed")
            )
            order_id = cur.lastrowid
        orders_placed += 1

        if not confirm_nav:
            print(f"  [WARN] {code} {name}: fund_nav 无数据（可能黄金白名单未入 DB），订单已记但不写持仓")
            continue

        shares = target_amount / confirm_nav

        # 4. 写 holding
        with conn:
            conn.execute(
                "INSERT INTO fund_bot_holdings "
                "(bot_id, fund_code, fund_name, asset_class, role, "
                " entry_date, entry_nav, latest_nav, shares, amount_invested, "
                " market_value, unrealized_pnl, unrealized_pnl_pct, "
                " target_weight, actual_weight, holding_days, high_nav, status, thesis) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (bot_id, code, name, asset_class, role,
                 confirm_date, confirm_nav, confirm_nav, shares, target_amount,
                 target_amount, 0.0, 0.0,
                 target_weight, target_weight, 0, confirm_nav,
                 reason)
            )
        holdings_created += 1
        cash_remaining -= target_amount

    # 5. 更新账户现金
    with conn:
        conn.execute(
            "UPDATE fund_bot_accounts SET cash=?, updated_at=? WHERE bot_id=?",
            (cash_remaining, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), bot_id)
        )

    print(f"  下单 {orders_placed} 笔，建仓 {holdings_created} 只")
    if skipped_external:
        print(f"  外部配置（不下单，留作现金）：")
        for code, name, amt in skipped_external:
            print(f"    {code:8} {name:<22} ¥{amt:,.0f}")
    print(f"  账户现金余额：¥{cash_remaining:,.0f}（含外部配置预留 ¥{capital - sum(target_amount for f in funds for target_amount in [float(f.get('target_amount') or 0)] if not is_external_or_cash((f.get('fund_code') or '').strip(), f.get('fund_name') or '')):,.0f}）")
    return {
        "bot_id": bot_id,
        "status": "ok",
        "orders": orders_placed,
        "holdings": holdings_created,
        "cash": cash_remaining,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True, help="bot_id（多个用逗号分隔）")
    parser.add_argument("--capital", type=float, default=100000, help="初始资金（默认 ¥100,000）")
    parser.add_argument("--no-reset", action="store_true", help="不清除已有账户（默认会清）")
    args = parser.parse_args()

    bot_ids = [b.strip() for b in args.bot.split(",") if b.strip()]
    conn = get_conn()
    try:
        results = []
        for bid in bot_ids:
            r = bootstrap_bot(conn, bid, args.capital, reset=not args.no_reset)
            results.append(r)

        print(f"\n=== 汇总 ===")
        for r in results:
            print(f"  {r['bot_id']}: {r['status']}", end="")
            if r["status"] == "ok":
                print(f" - {r['orders']} 笔订单, {r['holdings']} 只持仓, 现金 ¥{r['cash']:,.0f}")
            else:
                print(f" - {r.get('msg', '')}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
